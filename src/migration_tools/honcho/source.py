from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

EXPECTED_TABLES = {
    "workspaces",
    "peers",
    "sessions",
    "messages",
    "collections",
    "documents",
    "message_embeddings",
    "queue",
}


@dataclass(frozen=True)
class MigrationConfig:
    workspace_tenants: dict[str, str]
    peer_types: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> MigrationConfig:
        payload = json.loads(path.read_text())
        workspace_tenants = payload.get("workspace_tenants", {})
        peer_types = payload.get("peer_types", {})
        invalid_types = sorted(set(peer_types.values()) - {"user", "agent", "company"})
        if invalid_types:
            raise ValueError(f"unsupported peer types: {invalid_types}")
        if not workspace_tenants:
            raise ValueError("workspace_tenants must not be empty")
        return cls(workspace_tenants=workspace_tenants, peer_types=peer_types)


@dataclass(frozen=True)
class MigrationBatch:
    workspace: str
    tenant_id: str
    source_namespace: str
    sessions: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    all_document_ids: frozenset[str]
    peer_types: dict[str, str]


def create_source_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


@contextmanager
def readonly_connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        with connection.begin():
            read_only = connection.execute(text("SHOW transaction_read_only")).scalar_one()
            if read_only != "on":
                raise RuntimeError(
                    "Honcho connection is not read-only; add "
                    "?options=-c%20default_transaction_read_only%3Don to HONCHO_DATABASE_URL"
                )
            yield connection


def _rows(connection: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(text(sql), params).mappings()]


def _scalar(connection: Connection, sql: str, **params: Any) -> Any:
    return connection.execute(text(sql), params).scalar_one()


def source_fingerprint(connection: Connection) -> dict[str, Any]:
    fingerprints: dict[str, Any] = {}
    key_columns = {
        "workspaces": "name",
        "peers": "workspace_name || ':' || name",
        "sessions": "workspace_name || ':' || name",
        "messages": "workspace_name || ':' || id::text || ':' || md5(content)",
        "collections": "workspace_name || ':' || id",
        "documents": "workspace_name || ':' || id || ':' || md5(content)",
        "message_embeddings": "workspace_name || ':' || id::text",
        "queue": "id::text",
    }
    for table_name, order_expression in key_columns.items():
        row = (
            connection.execute(
                text(
                    f"SELECT count(*) AS row_count, "  # noqa: S608 - fixed table/expression map
                    f"md5(COALESCE(string_agg(md5({table_name}::text), '|' "
                    f"ORDER BY {order_expression}), '')) "
                    f"AS digest FROM {table_name}"
                )
            )
            .mappings()
            .one()
        )
        fingerprints[table_name] = {
            "row_count": row["row_count"],
            "digest": row["digest"],
        }
    return fingerprints


def build_profile(connection: Connection) -> dict[str, Any]:
    tables = _rows(
        connection,
        """
        SELECT table_name, count(*) OVER () AS table_count
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
    )
    table_names = {row["table_name"] for row in tables}
    missing_tables = sorted(EXPECTED_TABLES - table_names)
    if missing_tables:
        raise RuntimeError(f"Honcho schema is missing expected tables: {missing_tables}")

    counts = {
        table_name: _scalar(connection, f"SELECT count(*) FROM {table_name}")  # noqa: S608
        for table_name in sorted(EXPECTED_TABLES)
    }
    observer_pairs = _rows(
        connection,
        """
        SELECT observer, observed, count(*) AS collection_count
        FROM collections
        GROUP BY observer, observed
        ORDER BY collection_count DESC, observer, observed
        """,
    )
    document_observer_pairs = _rows(
        connection,
        """
        SELECT observer, observed, count(*) AS document_count
        FROM documents
        GROUP BY observer, observed
        ORDER BY document_count DESC, observer, observed
        """,
    )
    levels = _rows(
        connection,
        "SELECT level, count(*) AS count FROM documents GROUP BY level ORDER BY level",
    )
    document_source_stats = (
        connection.execute(
            text(
                """
            WITH refs AS (
                SELECT d.id AS downstream_document_id,
                       jsonb_array_elements_text(d.source_ids) AS source_id
                FROM documents d
                WHERE jsonb_typeof(d.source_ids) = 'array'
            ), classified AS (
                SELECT refs.*,
                       source_document.id IS NOT NULL AS matches_document,
                       message_by_public.id IS NOT NULL OR message_by_id.id IS NOT NULL
                           AS matches_message
                FROM refs
                LEFT JOIN documents source_document ON source_document.id = refs.source_id
                LEFT JOIN messages message_by_public
                    ON message_by_public.public_id = refs.source_id
                LEFT JOIN messages message_by_id
                    ON message_by_id.id::text = refs.source_id
            )
            SELECT count(*) AS reference_occurrences,
                   count(*) FILTER (WHERE matches_document) AS matching_documents,
                   count(*) FILTER (WHERE matches_message) AS matching_messages,
                   count(*) FILTER (WHERE NOT matches_document AND NOT matches_message)
                       AS unmatched_occurrences,
                   count(DISTINCT source_id) FILTER (
                       WHERE NOT matches_document AND NOT matches_message
                   ) AS distinct_unmatched_ids,
                   count(DISTINCT downstream_document_id) FILTER (
                       WHERE NOT matches_document AND NOT matches_message
                   ) AS affected_documents
            FROM classified
            """
            )
        )
        .mappings()
        .one()
    )
    message_source_stats = (
        connection.execute(
            text(
                """
            WITH refs AS (
                SELECT d.id AS downstream_document_id,
                       jsonb_array_elements_text(d.internal_metadata->'message_ids') AS message_id
                FROM documents d
                WHERE jsonb_typeof(d.internal_metadata->'message_ids') = 'array'
            )
            SELECT count(*) AS reference_occurrences,
                   count(*) FILTER (WHERE messages.id IS NOT NULL) AS matching_messages,
                   count(*) FILTER (WHERE messages.id IS NULL) AS unmatched_occurrences,
                   count(DISTINCT refs.downstream_document_id) FILTER (
                       WHERE messages.id IS NULL
                   ) AS affected_documents
            FROM refs
            LEFT JOIN messages ON messages.id::text = refs.message_id
            """
            )
        )
        .mappings()
        .one()
    )
    unmatched_examples = _rows(
        connection,
        """
        WITH refs AS (
            SELECT d.id AS downstream_document_id, d.level,
                   jsonb_array_elements_text(d.source_ids) AS source_id
            FROM documents d
            WHERE jsonb_typeof(d.source_ids) = 'array'
        )
        SELECT refs.downstream_document_id, refs.level, refs.source_id
        FROM refs
        LEFT JOIN documents source_document ON source_document.id = refs.source_id
        LEFT JOIN messages message_by_public ON message_by_public.public_id = refs.source_id
        LEFT JOIN messages message_by_id ON message_by_id.id::text = refs.source_id
        WHERE source_document.id IS NULL
          AND message_by_public.id IS NULL
          AND message_by_id.id IS NULL
        ORDER BY refs.downstream_document_id, refs.source_id
        LIMIT 10
        """,
    )
    source_count_distribution = _rows(
        connection,
        """
        SELECT source_count, count(*) AS document_count
        FROM (
            SELECT CASE
                WHEN jsonb_typeof(source_ids) = 'array' THEN jsonb_array_length(source_ids)
                ELSE 0
            END AS source_count
            FROM documents
        ) counts
        GROUP BY source_count
        ORDER BY source_count
        """,
    )
    time_ranges = {
        row["entity"]: {"first_at": row["first_at"], "last_at": row["last_at"]}
        for row in _rows(
            connection,
            """
            SELECT 'messages' AS entity, min(created_at) AS first_at, max(created_at) AS last_at
            FROM messages
            UNION ALL
            SELECT 'documents', min(created_at), max(created_at) FROM documents
            """,
        )
    }
    vector_columns = _rows(
        connection,
        """
        SELECT table_name, column_name, udt_name,
               format_type(a.atttypid, a.atttypmod) AS formatted_type
        FROM information_schema.columns c
        JOIN pg_class cls ON cls.relname = c.table_name
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace AND ns.nspname = c.table_schema
        JOIN pg_attribute a ON a.attrelid = cls.oid AND a.attname = c.column_name
        WHERE c.table_schema = 'public'
          AND c.table_name IN ('documents', 'message_embeddings')
          AND c.column_name = 'embedding'
        ORDER BY table_name
        """,
    )
    sync_states = {
        "documents": _rows(
            connection,
            "SELECT sync_state, count(*) AS count FROM documents GROUP BY sync_state "
            "ORDER BY sync_state",
        ),
        "message_embeddings": _rows(
            connection,
            "SELECT sync_state, count(*) AS count FROM message_embeddings GROUP BY sync_state "
            "ORDER BY sync_state",
        ),
    }
    return {
        "generated_at": datetime.now(UTC),
        "source_read_only": True,
        "schema": {
            "expected_tables_present": True,
            "public_tables": sorted(table_names),
            "vector_columns": vector_columns,
        },
        "counts": counts,
        "observer_observed": {
            "collections": observer_pairs,
            "documents": document_observer_pairs,
        },
        "document_levels": levels,
        "document_sources": {
            "column_source_ids_semantics": "document references (derives)",
            "column_source_ids": dict(document_source_stats),
            "internal_message_ids_semantics": "numeric message references (supports)",
            "internal_message_ids": dict(message_source_stats),
            "source_count_distribution": source_count_distribution,
            "documents_without_message_or_document_sources": _scalar(
                connection,
                """
                SELECT count(*) FROM documents
                WHERE COALESCE(jsonb_array_length(
                    CASE
                        WHEN jsonb_typeof(source_ids) = 'array' THEN source_ids
                        ELSE '[]'::jsonb
                    END
                ), 0) = 0
                  AND COALESCE(jsonb_array_length(
                    CASE
                        WHEN jsonb_typeof(internal_metadata->'message_ids') = 'array'
                        THEN internal_metadata->'message_ids'
                        ELSE '[]'::jsonb
                    END
                ), 0) = 0
                """,
            ),
            "unmatched_examples": unmatched_examples,
        },
        "soft_deleted_documents": _scalar(
            connection, "SELECT count(*) FROM documents WHERE deleted_at IS NOT NULL"
        ),
        "time_ranges": time_ranges,
        "sync_states": sync_states,
        "source_fingerprint": source_fingerprint(connection),
    }


def load_workspace_snapshot(connection: Connection, workspace: str) -> dict[str, Any]:
    sessions = _rows(
        connection,
        """
        SELECT s.name, s.created_at, count(m.id) AS message_count
        FROM sessions s
        LEFT JOIN messages m
          ON m.workspace_name = s.workspace_name AND m.session_name = s.name
        WHERE s.workspace_name = :workspace
        GROUP BY s.name, s.created_at
        ORDER BY message_count DESC, s.name
        """,
        workspace=workspace,
    )
    messages = _rows(
        connection,
        """
        SELECT id, public_id, content, metadata, internal_metadata, created_at,
               peer_name, workspace_name, session_name, seq_in_session
        FROM messages
        WHERE workspace_name = :workspace
        ORDER BY created_at, id
        """,
        workspace=workspace,
    )
    documents = _rows(
        connection,
        """
        SELECT id, content, internal_metadata, created_at, workspace_name, session_name,
               observer, observed, level, source_ids, deleted_at
        FROM documents
        WHERE workspace_name = :workspace
        ORDER BY created_at, id
        """,
        workspace=workspace,
    )
    return {"sessions": sessions, "messages": messages, "documents": documents}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _document_dependencies(document: Mapping[str, Any], existing_ids: set[str]) -> set[str]:
    return {str(value) for value in _as_list(document.get("source_ids")) if value in existing_ids}


def _has_missing_source(document: Mapping[str, Any], existing_ids: set[str]) -> bool:
    values = {str(value) for value in _as_list(document.get("source_ids"))}
    return bool(values - existing_ids)


def _has_no_sources(document: Mapping[str, Any]) -> bool:
    metadata = document.get("internal_metadata") or {}
    return not _as_list(document.get("source_ids")) and not _as_list(metadata.get("message_ids"))


def select_batch(
    snapshot: Mapping[str, Any],
    config: MigrationConfig,
    *,
    workspace: str,
    limit_sessions: int = 10,
    limit_messages: int = 200,
    limit_documents: int = 50,
) -> MigrationBatch:
    if workspace not in config.workspace_tenants:
        raise ValueError(f"workspace {workspace!r} has no target tenant mapping")
    if limit_sessions <= 0 or limit_messages <= 0 or limit_documents <= 0:
        raise ValueError("batch limits must be positive")

    sessions = list(snapshot["sessions"])
    selected_sessions = sessions[:limit_sessions]
    session_names = {row["name"] for row in selected_sessions}
    messages = [row for row in snapshot["messages"] if row["session_name"] in session_names][
        :limit_messages
    ]
    selected_message_ids = {row["id"] for row in messages}

    all_documents = list(snapshot["documents"])
    by_id = {str(row["id"]): row for row in all_documents}
    all_document_ids = set(by_id)
    candidates = [row for row in all_documents if row["session_name"] in session_names]

    missing_first = [row for row in candidates if _has_missing_source(row, all_document_ids)]
    no_sources_next = [
        row for row in candidates if _has_no_sources(row) and row not in missing_first
    ]
    by_level: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        by_level.setdefault(str(row["level"]), []).append(row)
    round_robin: list[dict[str, Any]] = []
    max_level_size = max((len(rows) for rows in by_level.values()), default=0)
    for index in range(max_level_size):
        for level in sorted(by_level):
            rows = by_level[level]
            if index < len(rows):
                round_robin.append(rows[index])

    ordered_candidates = []
    seen_candidates: set[str] = set()
    for row in [*missing_first, *no_sources_next, *round_robin]:
        document_id = str(row["id"])
        if document_id not in seen_candidates:
            seen_candidates.add(document_id)
            ordered_candidates.append(row)

    selected_ids: set[str] = set()

    def dependency_closure(document_id: str) -> set[str]:
        pending = [document_id]
        closure: set[str] = set()
        while pending:
            current = pending.pop()
            if current in closure:
                continue
            closure.add(current)
            pending.extend(_document_dependencies(by_id[current], all_document_ids) - closure)
        return closure

    for candidate in ordered_candidates:
        closure = dependency_closure(str(candidate["id"]))
        if len(selected_ids | closure) <= limit_documents:
            selected_ids.update(closure)
        if len(selected_ids) == limit_documents:
            break

    selected_documents = [row for row in all_documents if str(row["id"]) in selected_ids]
    referenced_message_ids = {
        int(value)
        for row in selected_documents
        for value in _as_list((row.get("internal_metadata") or {}).get("message_ids"))
        if str(value).isdigit()
    }
    missing_selected_messages = referenced_message_ids - selected_message_ids
    if missing_selected_messages:
        extra_messages = [
            row for row in snapshot["messages"] if row["id"] in missing_selected_messages
        ]
        if len(messages) + len(extra_messages) > limit_messages:
            raise RuntimeError("message limit is too small for selected document dependencies")
        messages.extend(extra_messages)
        messages.sort(key=lambda row: (row["created_at"], row["id"]))

    unresolved_peers = sorted(
        {
            *(str(row["peer_name"]) for row in messages),
            *(str(row["observed"]) for row in selected_documents),
        }
        - set(config.peer_types)
    )
    if unresolved_peers:
        raise RuntimeError(f"peer types are unresolved: {unresolved_peers}")

    return MigrationBatch(
        workspace=workspace,
        tenant_id=config.workspace_tenants[workspace],
        source_namespace=f"workspace/{workspace}",
        sessions=selected_sessions,
        messages=messages,
        documents=selected_documents,
        all_document_ids=frozenset(all_document_ids),
        peer_types=config.peer_types,
    )


def batch_plan_report(batch: MigrationBatch) -> dict[str, Any]:
    selected_ids = {str(row["id"]) for row in batch.documents}
    message_ids = {str(row["id"]) for row in batch.messages}
    supports = 0
    derives = 0
    missing_occurrences = 0
    affected_documents = 0
    synthetic_sources = 0
    levels = Counter(str(row["level"]) for row in batch.documents)
    document_sessions = {row["session_name"] for row in batch.documents}
    for row in batch.documents:
        metadata = row.get("internal_metadata") or {}
        matched_messages = {
            str(value) for value in _as_list(metadata.get("message_ids"))
        } & message_ids
        source_ids = {str(value) for value in _as_list(row.get("source_ids"))}
        matched_documents = source_ids & selected_ids
        missing = source_ids - batch.all_document_ids
        supports += len(matched_messages)
        derives += len(matched_documents)
        missing_occurrences += len(missing)
        if missing or (not matched_messages and not matched_documents):
            affected_documents += 1
            synthetic_sources += 1
            supports += 1
    return {
        "workspace": batch.workspace,
        "tenant_id": batch.tenant_id,
        "source_namespace": batch.source_namespace,
        "selected_sessions": len(batch.sessions),
        "sessions_with_documents": len(document_sessions),
        "selected_messages": len(batch.messages),
        "selected_documents": len(batch.documents),
        "document_levels": dict(sorted(levels.items())),
        "planned_supports_edges": supports,
        "planned_derives_edges": derives,
        "planned_evidence_edges": supports + derives,
        "legacy_import_sources": synthetic_sources,
        "unmatched_source_id_occurrences": missing_occurrences,
        "selection_note": (
            "Only two source sessions contain documents; the selected message batch still spans "
            f"{len(batch.sessions)} sessions."
        ),
    }


def stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()
    return f"{prefix}_{digest[:48]}"


def json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default) + "\n")


def load_sanitized_batch(path: Path) -> MigrationBatch:
    payload = json.loads(path.read_text())
    for collection in (payload["sessions"], payload["messages"], payload["documents"]):
        for row in collection:
            for field in ("created_at", "deleted_at"):
                if row.get(field):
                    row[field] = datetime.fromisoformat(row[field])
    documents = payload["documents"]
    return MigrationBatch(
        workspace=payload["workspace"],
        tenant_id=payload["tenant_id"],
        source_namespace=f"workspace/{payload['workspace']}",
        sessions=payload["sessions"],
        messages=payload["messages"],
        documents=documents,
        all_document_ids=frozenset(str(row["id"]) for row in documents),
        peer_types=payload["peer_types"],
    )
