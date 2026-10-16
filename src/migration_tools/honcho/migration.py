from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from memory_cmic.audit import record_audit
from memory_cmic.models import (
    ExternalEntityMapping,
    MemoryAuditLog,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    SourceRecord,
)
from migration_tools.honcho.source import MigrationBatch, batch_plan_report, stable_id


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _values(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _message_source_id(batch: MigrationBatch, public_id: str) -> str:
    return stable_id("hsrc", batch.tenant_id, batch.source_namespace, "message", public_id)


def _memory_id(batch: MigrationBatch, document_id: str) -> str:
    return stable_id("hmem", batch.tenant_id, batch.source_namespace, "document", document_id)


def _legacy_source_id(batch: MigrationBatch, document_id: str) -> str:
    return stable_id(
        "hsrc", batch.tenant_id, batch.source_namespace, "legacy_document", document_id
    )


def _evidence_group_id(batch: MigrationBatch, document_id: str) -> str:
    return stable_id("hgrp", batch.tenant_id, batch.source_namespace, document_id)


def _evidence_id(
    batch: MigrationBatch, relationship_type: str, upstream_id: str, downstream_id: str
) -> str:
    return stable_id("hevd", batch.tenant_id, relationship_type, upstream_id, downstream_id)


def _mapping(
    session: Session,
    batch: MigrationBatch,
    entity_type: str,
    external_id: str,
) -> ExternalEntityMapping | None:
    return session.get(
        ExternalEntityMapping,
        (
            batch.tenant_id,
            "honcho",
            batch.source_namespace,
            entity_type,
            external_id,
        ),
    )


def _add_audit(
    session: Session,
    batch: MigrationBatch,
    *,
    target_type: str,
    target_id: str,
    correlation_id: str,
    state_after: Mapping[str, Any],
) -> None:
    record_audit(
        session,
        tenant_id=batch.tenant_id,
        action="MIGRATE",
        target_type=target_type,
        target_id=target_id,
        operator_type="system",
        operator_id="honcho_migration",
        reason_code="HONCHO_MIGRATION",
        reason="Honcho small-batch migration validation",
        correlation_id=correlation_id,
        state_after=dict(state_after),
    )


def _verify_mapping_target(
    session: Session,
    batch: MigrationBatch,
    mapping: ExternalEntityMapping,
    expected_target_type: str,
) -> None:
    if mapping.target_type != expected_target_type:
        raise RuntimeError(
            f"mapping target type mismatch for {mapping.entity_type}/{mapping.external_id}"
        )
    model = SourceRecord if expected_target_type == "source" else MemoryItem
    target = session.get(model, mapping.target_id)
    if target is None or target.tenant_id != batch.tenant_id:
        raise RuntimeError(
            f"mapping target is missing for {mapping.entity_type}/{mapping.external_id}"
        )


def _insert_message(
    session: Session,
    batch: MigrationBatch,
    row: Mapping[str, Any],
    *,
    migration_batch_id: str,
    correlation_id: str,
) -> bool:
    external_id = str(row["public_id"])
    mapping = _mapping(session, batch, "message", external_id)
    if mapping is not None:
        _verify_mapping_target(session, batch, mapping, "source")
        return False

    target_id = _message_source_id(batch, external_id)
    if session.get(SourceRecord, target_id) is not None:
        raise RuntimeError(f"source {target_id} exists without external mapping")
    content = str(row["content"])
    source = SourceRecord(
        id=target_id,
        tenant_id=batch.tenant_id,
        source_system="honcho",
        source_type="chat",
        session_id=str(row["session_name"]),
        external_ref_id=external_id,
        author_type=batch.peer_types[str(row["peer_name"])],
        author_id=str(row["peer_name"]),
        raw_content=content,
        content_hash=_sha256(content),
        metadata_json={
            "honcho_workspace": batch.workspace,
            "honcho_message_id": row["id"],
            "honcho_seq_in_session": row["seq_in_session"],
            "honcho_metadata": row.get("metadata") or {},
        },
        status="active",
        version=1,
        occurred_at=row["created_at"],
    )
    session.add(source)
    session.add(
        ExternalEntityMapping(
            tenant_id=batch.tenant_id,
            source_system="honcho",
            source_namespace=batch.source_namespace,
            entity_type="message",
            external_id=external_id,
            target_type="source",
            target_id=target_id,
            migration_batch_id=migration_batch_id,
        )
    )
    _add_audit(
        session,
        batch,
        target_type="source",
        target_id=target_id,
        correlation_id=correlation_id,
        state_after={"honcho_entity": "message", "external_id": external_id},
    )
    return True


def _insert_document(
    session: Session,
    batch: MigrationBatch,
    row: Mapping[str, Any],
    *,
    migration_batch_id: str,
    correlation_id: str,
) -> bool:
    external_id = str(row["id"])
    mapping = _mapping(session, batch, "document", external_id)
    if mapping is not None:
        _verify_mapping_target(session, batch, mapping, "memory")
        return False

    target_id = _memory_id(batch, external_id)
    if session.get(MemoryItem, target_id) is not None:
        raise RuntimeError(f"memory {target_id} exists without external mapping")
    level = str(row["level"])
    cognitive_types = {"explicit": "fact", "deductive": "inference", "inductive": "inference"}
    if level not in cognitive_types:
        raise RuntimeError(f"unsupported Honcho document level: {level}")
    source_ids = {str(value) for value in _values(row.get("source_ids"))}
    missing_source_ids = sorted(source_ids - batch.all_document_ids)
    metadata = row.get("internal_metadata") or {}
    message_ids = [str(value) for value in _values(metadata.get("message_ids"))]
    content = str(row["content"])
    deleted_at = row.get("deleted_at")
    memory = MemoryItem(
        id=target_id,
        tenant_id=batch.tenant_id,
        subject_type=batch.peer_types[str(row["observed"])],
        subject_id=str(row["observed"]),
        cognitive_type=cognitive_types[level],
        summary=content[:512],
        content=content,
        metadata_json={
            "honcho_workspace": batch.workspace,
            "honcho_document_id": external_id,
            "honcho_session_name": row.get("session_name"),
            "honcho_observer": row["observer"],
            "honcho_observed": row["observed"],
            "honcho_level": level,
            "honcho_message_ids": message_ids,
            "honcho_source_ids": sorted(source_ids),
            "missing_honcho_source_ids": missing_source_ids,
            "evidence_quality": ("legacy_incomplete" if missing_source_ids else "legacy_batch"),
        },
        confidence=1,
        status="invalidated" if deleted_at is not None else "active",
        version=1,
        effective_at=row["created_at"],
        invalidated_at=deleted_at,
        created_by="honcho_migration",
    )
    session.add(memory)
    session.add(
        ExternalEntityMapping(
            tenant_id=batch.tenant_id,
            source_system="honcho",
            source_namespace=batch.source_namespace,
            entity_type="document",
            external_id=external_id,
            target_type="memory",
            target_id=target_id,
            migration_batch_id=migration_batch_id,
        )
    )
    _add_audit(
        session,
        batch,
        target_type="memory",
        target_id=target_id,
        correlation_id=correlation_id,
        state_after={"honcho_entity": "document", "external_id": external_id},
    )
    return True


def _legacy_content(row: Mapping[str, Any]) -> str:
    metadata = row.get("internal_metadata") or {}
    values = _values(metadata.get("premises")) or _values(metadata.get("sources"))
    if values:
        return "\n".join(str(value) for value in values)
    return str(row["content"])


def _ensure_legacy_source(
    session: Session,
    batch: MigrationBatch,
    row: Mapping[str, Any],
    *,
    missing_document_ids: list[str],
    missing_message_ids: list[str],
    correlation_id: str,
) -> tuple[str, bool]:
    document_id = str(row["id"])
    target_id = _legacy_source_id(batch, document_id)
    existing = session.get(SourceRecord, target_id)
    if existing is not None:
        if existing.tenant_id != batch.tenant_id or existing.source_type != "legacy_import":
            raise RuntimeError(f"legacy source conflict: {target_id}")
        return target_id, False
    content = _legacy_content(row)
    session.add(
        SourceRecord(
            id=target_id,
            tenant_id=batch.tenant_id,
            source_system="honcho",
            source_type="legacy_import",
            session_id=str(row["session_name"]) if row.get("session_name") else None,
            external_ref_id=f"legacy-document:{document_id}",
            author_type="system",
            author_id="honcho_migration",
            raw_content=content,
            content_hash=_sha256(content),
            metadata_json={
                "honcho_workspace": batch.workspace,
                "honcho_document_id": document_id,
                "missing_honcho_document_ids": missing_document_ids,
                "missing_honcho_message_ids": missing_message_ids,
                "synthetic": True,
            },
            status="active",
            version=1,
            occurred_at=row["created_at"],
        )
    )
    _add_audit(
        session,
        batch,
        target_type="source",
        target_id=target_id,
        correlation_id=correlation_id,
        state_after={"honcho_entity": "legacy_import", "document_id": document_id},
    )
    return target_id, True


def _ensure_evidence(
    session: Session,
    batch: MigrationBatch,
    *,
    relationship_type: str,
    upstream_id: str,
    downstream_id: str,
    group_id: str,
    snippet: str,
    locator: Mapping[str, Any],
    correlation_id: str,
) -> bool:
    evidence_id = _evidence_id(batch, relationship_type, upstream_id, downstream_id)
    existing = session.get(MemoryEvidence, evidence_id)
    if existing is not None:
        expected_upstream = (
            existing.upstream_source_id
            if relationship_type == "supports"
            else existing.upstream_memory_id
        )
        if (
            existing.tenant_id != batch.tenant_id
            or existing.relationship_type != relationship_type
            or expected_upstream != upstream_id
            or existing.downstream_memory_id != downstream_id
            or existing.evidence_group_id != group_id
        ):
            raise RuntimeError(f"evidence conflict: {evidence_id}")
        return False
    values: dict[str, Any] = {
        "id": evidence_id,
        "tenant_id": batch.tenant_id,
        "relationship_type": relationship_type,
        "evidence_group_id": group_id,
        "downstream_memory_id": downstream_id,
        "upstream_version": 1,
        "evidence_snippet": snippet,
        "evidence_locator": dict(locator),
        "status": "active",
    }
    if relationship_type == "supports":
        values["upstream_source_id"] = upstream_id
    else:
        values["upstream_memory_id"] = upstream_id
    session.add(MemoryEvidence(**values))
    _add_audit(
        session,
        batch,
        target_type="evidence",
        target_id=evidence_id,
        correlation_id=correlation_id,
        state_after={"relationship_type": relationship_type, "group_id": group_id},
    )
    return True


def _ensure_vector_task(
    session: Session,
    batch: MigrationBatch,
    *,
    memory_id: str,
    correlation_id: str,
    model_id: str,
) -> bool:
    idempotency_key = f"vector_upsert:memory:{memory_id}:v1"
    existing = session.scalar(
        select(MemoryTask).where(
            MemoryTask.tenant_id == batch.tenant_id,
            MemoryTask.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return False
    session.add(
        MemoryTask(
            id=stable_id("htsk", batch.tenant_id, idempotency_key),
            tenant_id=batch.tenant_id,
            task_type="vector_upsert",
            target_type="memory",
            target_id=memory_id,
            input_version=1,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            payload={"source_system": "honcho", "model_id": model_id},
            status="pending",
            priority=0,
        )
    )
    return True


def apply_migration(
    session: Session,
    batch: MigrationBatch,
    *,
    migration_batch_id: str,
    embedding_model_id: str = "BAAI/bge-m3",
) -> dict[str, Any]:
    correlation_id = stable_id("hcorr", batch.tenant_id, migration_batch_id)
    inserted = {
        "source_records": 0,
        "memory_items": 0,
        "evidence_edges": 0,
        "legacy_import_sources": 0,
        "tasks": 0,
    }

    for row in batch.messages:
        inserted["source_records"] += int(
            _insert_message(
                session,
                batch,
                row,
                migration_batch_id=migration_batch_id,
                correlation_id=correlation_id,
            )
        )
    session.flush()

    for row in batch.documents:
        inserted["memory_items"] += int(
            _insert_document(
                session,
                batch,
                row,
                migration_batch_id=migration_batch_id,
                correlation_id=correlation_id,
            )
        )
    session.flush()

    messages_by_id = {str(row["id"]): row for row in batch.messages}
    documents_by_id = {str(row["id"]): row for row in batch.documents}
    for row in batch.documents:
        document_id = str(row["id"])
        downstream_id = _memory_id(batch, document_id)
        group_id = _evidence_group_id(batch, document_id)
        metadata = row.get("internal_metadata") or {}
        requested_message_ids = {str(value) for value in _values(metadata.get("message_ids"))}
        requested_document_ids = {str(value) for value in _values(row.get("source_ids"))}

        for message_id in sorted(requested_message_ids & set(messages_by_id)):
            message = messages_by_id[message_id]
            upstream_id = _message_source_id(batch, str(message["public_id"]))
            inserted["evidence_edges"] += int(
                _ensure_evidence(
                    session,
                    batch,
                    relationship_type="supports",
                    upstream_id=upstream_id,
                    downstream_id=downstream_id,
                    group_id=group_id,
                    snippet=str(message["content"]),
                    locator={"honcho_message_id": message["id"]},
                    correlation_id=correlation_id,
                )
            )

        for source_document_id in sorted(requested_document_ids & set(documents_by_id)):
            source_document = documents_by_id[source_document_id]
            upstream_id = _memory_id(batch, source_document_id)
            inserted["evidence_edges"] += int(
                _ensure_evidence(
                    session,
                    batch,
                    relationship_type="derives",
                    upstream_id=upstream_id,
                    downstream_id=downstream_id,
                    group_id=group_id,
                    snippet=str(source_document["content"]),
                    locator={"honcho_document_id": source_document_id},
                    correlation_id=correlation_id,
                )
            )

        missing_message_ids = sorted(requested_message_ids - set(messages_by_id))
        missing_document_ids = sorted(requested_document_ids - batch.all_document_ids)
        has_resolved_evidence = bool(
            (requested_message_ids & set(messages_by_id))
            or (requested_document_ids & set(documents_by_id))
        )
        if missing_message_ids or missing_document_ids or not has_resolved_evidence:
            legacy_source_id, created = _ensure_legacy_source(
                session,
                batch,
                row,
                missing_document_ids=missing_document_ids,
                missing_message_ids=missing_message_ids,
                correlation_id=correlation_id,
            )
            inserted["source_records"] += int(created)
            inserted["legacy_import_sources"] += int(created)
            inserted["evidence_edges"] += int(
                _ensure_evidence(
                    session,
                    batch,
                    relationship_type="supports",
                    upstream_id=legacy_source_id,
                    downstream_id=downstream_id,
                    group_id=group_id,
                    snippet=_legacy_content(row),
                    locator={
                        "honcho_document_id": document_id,
                        "missing_honcho_document_ids": missing_document_ids,
                        "missing_honcho_message_ids": missing_message_ids,
                    },
                    correlation_id=correlation_id,
                )
            )

        inserted["tasks"] += int(
            _ensure_vector_task(
                session,
                batch,
                memory_id=downstream_id,
                correlation_id=correlation_id,
                model_id=embedding_model_id,
            )
        )

    session.flush()
    return {
        "executed_at": datetime.now(UTC),
        "migration_batch_id": migration_batch_id,
        "correlation_id": correlation_id,
        "plan": batch_plan_report(batch),
        "inserted": inserted,
        "skipped": {
            "messages": len(batch.messages)
            - min(
                len(batch.messages), inserted["source_records"] - inserted["legacy_import_sources"]
            ),
            "documents": len(batch.documents) - inserted["memory_items"],
        },
    }


def expected_ids(batch: MigrationBatch) -> dict[str, set[str]]:
    message_sources = {_message_source_id(batch, str(row["public_id"])) for row in batch.messages}
    memories = {_memory_id(batch, str(row["id"])) for row in batch.documents}
    evidence: set[str] = set()
    legacy_sources: set[str] = set()
    messages_by_id = {str(row["id"]): row for row in batch.messages}
    documents_by_id = {str(row["id"]): row for row in batch.documents}
    for row in batch.documents:
        document_id = str(row["id"])
        downstream_id = _memory_id(batch, document_id)
        metadata = row.get("internal_metadata") or {}
        message_ids = {str(value) for value in _values(metadata.get("message_ids"))}
        document_ids = {str(value) for value in _values(row.get("source_ids"))}
        for message_id in message_ids & set(messages_by_id):
            upstream_id = _message_source_id(batch, str(messages_by_id[message_id]["public_id"]))
            evidence.add(_evidence_id(batch, "supports", upstream_id, downstream_id))
        for source_document_id in document_ids & set(documents_by_id):
            upstream_id = _memory_id(batch, source_document_id)
            evidence.add(_evidence_id(batch, "derives", upstream_id, downstream_id))
        if (
            message_ids - set(messages_by_id)
            or document_ids - batch.all_document_ids
            or not ((message_ids & set(messages_by_id)) or (document_ids & set(documents_by_id)))
        ):
            legacy_id = _legacy_source_id(batch, document_id)
            legacy_sources.add(legacy_id)
            evidence.add(_evidence_id(batch, "supports", legacy_id, downstream_id))
    return {
        "message_sources": message_sources,
        "legacy_sources": legacy_sources,
        "memories": memories,
        "evidence": evidence,
    }


def validate_migration(session: Session, batch: MigrationBatch) -> dict[str, Any]:
    expected = expected_ids(batch)
    expected_sources = expected["message_sources"] | expected["legacy_sources"]
    source_ids = set(
        session.scalars(select(SourceRecord.id).where(SourceRecord.id.in_(expected_sources))).all()
    )
    memory_ids = set(
        session.scalars(select(MemoryItem.id).where(MemoryItem.id.in_(expected["memories"]))).all()
    )
    evidence_ids = set(
        session.scalars(
            select(MemoryEvidence.id).where(MemoryEvidence.id.in_(expected["evidence"]))
        ).all()
    )
    mapping_count = session.scalar(
        select(func.count())
        .select_from(ExternalEntityMapping)
        .where(
            ExternalEntityMapping.tenant_id == batch.tenant_id,
            ExternalEntityMapping.source_system == "honcho",
            ExternalEntityMapping.source_namespace == batch.source_namespace,
        )
    )
    task_rows = session.execute(
        select(MemoryTask.target_id, MemoryTask.priority, MemoryTask.correlation_id).where(
            MemoryTask.tenant_id == batch.tenant_id,
            MemoryTask.task_type == "vector_upsert",
            MemoryTask.target_id.in_(expected["memories"]),
        )
    ).all()
    audited_target_ids = expected_sources | expected["memories"] | expected["evidence"]
    audit_rows = session.execute(
        select(MemoryAuditLog.target_id, MemoryAuditLog.correlation_id).where(
            MemoryAuditLog.tenant_id == batch.tenant_id,
            MemoryAuditLog.action == "MIGRATE",
            MemoryAuditLog.reason_code == "HONCHO_MIGRATION",
            MemoryAuditLog.target_id.in_(audited_target_ids),
        )
    ).all()
    orphan_evidence = session.execute(
        text(
            """
            SELECT count(*)
            FROM memory_evidence e
            LEFT JOIN source_record s
              ON s.tenant_id = e.tenant_id AND s.id = e.upstream_source_id
            LEFT JOIN memory_item um
              ON um.tenant_id = e.tenant_id AND um.id = e.upstream_memory_id
            LEFT JOIN memory_item dm
              ON dm.tenant_id = e.tenant_id AND dm.id = e.downstream_memory_id
            WHERE e.tenant_id = :tenant_id
              AND (
                  (e.upstream_source_id IS NOT NULL AND s.id IS NULL)
                  OR (e.upstream_memory_id IS NOT NULL AND um.id IS NULL)
                  OR (e.downstream_memory_id IS NOT NULL AND dm.id IS NULL)
              )
            """
        ),
        {"tenant_id": batch.tenant_id},
    ).scalar_one()
    expected_mapping_count = len(batch.messages) + len(batch.documents)
    checks = {
        "source_records_complete": source_ids == expected_sources,
        "memory_items_complete": memory_ids == expected["memories"],
        "evidence_complete": evidence_ids == expected["evidence"],
        "mapping_count_matches": mapping_count == expected_mapping_count,
        "vector_tasks_complete": (
            len(task_rows) == len(expected["memories"])
            and {row.target_id for row in task_rows} == expected["memories"]
            and {row.priority for row in task_rows} == {0}
        ),
        "audit_logs_complete": (
            len(audit_rows) == len(audited_target_ids)
            and {row.target_id for row in audit_rows} == audited_target_ids
        ),
        "correlation_id_consistent": (
            len({row.correlation_id for row in task_rows}) == 1
            and {row.correlation_id for row in task_rows}
            == {row.correlation_id for row in audit_rows}
        ),
        "orphan_evidence_zero": orphan_evidence == 0,
    }
    return {
        "validated_at": datetime.now(UTC),
        "tenant_id": batch.tenant_id,
        "source_namespace": batch.source_namespace,
        "expected": {
            "source_records": len(expected_sources),
            "memory_items": len(expected["memories"]),
            "evidence_edges": len(expected["evidence"]),
            "external_mappings": expected_mapping_count,
            "legacy_import_sources": len(expected["legacy_sources"]),
            "vector_tasks": len(expected["memories"]),
            "audit_logs": len(audited_target_ids),
        },
        "actual": {
            "source_records": len(source_ids),
            "memory_items": len(memory_ids),
            "evidence_edges": len(evidence_ids),
            "external_mappings": mapping_count,
            "orphan_evidence": orphan_evidence,
            "vector_tasks": len(task_rows),
            "audit_logs": len(audit_rows),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }
