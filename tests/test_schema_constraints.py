from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError


def insert_source(
    connection: Connection,
    *,
    source_id: str,
    tenant_id: str = "tenant_a",
    external_ref_id: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO source_record (
                id, tenant_id, source_system, source_type, external_ref_id,
                author_type, author_id, raw_content, content_hash, occurred_at
            ) VALUES (
                :id, :tenant_id, 'test', 'chat', :external_ref_id,
                'user', 'user_1', 'test content', 'hash', CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": source_id,
            "tenant_id": tenant_id,
            "external_ref_id": external_ref_id or source_id,
        },
    )


def insert_memory(
    connection: Connection,
    *,
    memory_id: str,
    tenant_id: str = "tenant_a",
    values: Mapping[str, Any] | None = None,
) -> None:
    parameters: dict[str, Any] = {
        "id": memory_id,
        "tenant_id": tenant_id,
        "subject_type": "user",
        "subject_id": "user_1",
        "cognitive_type": "fact",
        "business_domains": None,
        "project_domains": None,
        "summary": "test summary",
        "content": "test content",
        "confidence": 1.0,
        "supersedes_id": None,
        "created_by": "test",
    }
    parameters.update(values or {})
    connection.execute(
        text(
            """
            INSERT INTO memory_item (
                id, tenant_id, subject_type, subject_id, cognitive_type,
                business_domains, project_domains, summary, content,
                confidence, supersedes_id, effective_at, created_by
            ) VALUES (
                :id, :tenant_id, :subject_type, :subject_id, :cognitive_type,
                :business_domains, :project_domains, :summary, :content,
                :confidence, :supersedes_id, CURRENT_TIMESTAMP, :created_by
            )
            """
        ),
        parameters,
    )


def insert_profile(
    connection: Connection,
    *,
    profile_id: str,
    tenant_id: str = "tenant_a",
    business_domains: list[str] | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO profile_property (
                id, tenant_id, user_id, business_domains, property_key,
                property_value, value_type, effective_at
            ) VALUES (
                :id, :tenant_id, 'user_1', :business_domains, 'pref.reply_style',
                '"concise"'::jsonb, 'string', CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": profile_id,
            "tenant_id": tenant_id,
            "business_domains": business_domains,
        },
    )


def test_expected_tables_extensions_and_indexes(connection: Connection) -> None:
    extension_version = connection.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar_one()
    assert extension_version

    public_tables = set(
        connection.execute(
            text(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                """
            )
        ).scalars()
    )
    assert {
        "business_scene",
        "source_record",
        "memory_item",
        "profile_property",
        "memory_evidence",
        "memory_embedding",
        "memory_task",
        "memory_audit_log",
    } <= public_tables

    migration_tables = set(
        connection.execute(
            text(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'migration'
                """
            )
        ).scalars()
    )
    assert "external_entity_mapping" in migration_tables

    indexes = set(
        connection.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname IN ('public', 'migration')
                """
            )
        ).scalars()
    )
    assert {
        "ix_business_scene_embedding_hnsw",
        "ix_memory_item_business_domains_gin",
        "ix_memory_item_project_domains_gin",
        "ix_memory_embedding_active_hnsw",
        "ix_memory_task_pending_claim",
        "uq_profile_property_active_key",
        "uq_evidence_active_supports",
        "uq_evidence_active_derives",
        "uq_evidence_active_profile_basis",
    } <= indexes


def test_tenant_id_has_no_default(connection: Connection) -> None:
    defaults = connection.execute(
        text(
            """
            SELECT table_schema, table_name, column_default
            FROM information_schema.columns
            WHERE column_name = 'tenant_id'
              AND table_schema IN ('public', 'migration')
            """
        )
    ).all()
    assert defaults
    assert all(row.column_default is None for row in defaults)


def test_source_requires_tenant(connection: Connection) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO source_record (
                    id, tenant_id, source_system, source_type, external_ref_id,
                    author_type, author_id, raw_content, content_hash, occurred_at
                ) VALUES (
                    'src_no_tenant', NULL, 'test', 'chat', 'ref',
                    'user', 'user_1', 'content', 'hash', CURRENT_TIMESTAMP
                )
                """
            )
        )


def test_source_external_reference_is_idempotent_per_tenant(connection: Connection) -> None:
    insert_source(connection, source_id="src_1", external_ref_id="same_ref")
    with pytest.raises(IntegrityError):
        insert_source(connection, source_id="src_2", external_ref_id="same_ref")


def test_memory_rejects_confidence_out_of_range(connection: Connection) -> None:
    with pytest.raises(IntegrityError):
        insert_memory(connection, memory_id="mem_bad_confidence", values={"confidence": 1.1})


@pytest.mark.parametrize("column", ["business_domains", "project_domains"])
def test_memory_rejects_empty_scope_arrays(connection: Connection, column: str) -> None:
    with pytest.raises(IntegrityError):
        insert_memory(connection, memory_id=f"mem_empty_{column}", values={column: []})


def test_evidence_rejects_relationship_shape_mismatch(connection: Connection) -> None:
    insert_source(connection, source_id="src_shape")
    insert_memory(connection, memory_id="mem_shape")
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO memory_evidence (
                    id, tenant_id, relationship_type, evidence_group_id,
                    upstream_source_id, downstream_memory_id, upstream_version
                ) VALUES (
                    'evd_shape', 'tenant_a', 'derives', 'grp_shape',
                    'src_shape', 'mem_shape', 1
                )
                """
            )
        )


def test_evidence_rejects_duplicate_active_edge(connection: Connection) -> None:
    insert_source(connection, source_id="src_duplicate")
    insert_memory(connection, memory_id="mem_duplicate")
    statement = text(
        """
        INSERT INTO memory_evidence (
            id, tenant_id, relationship_type, evidence_group_id,
            upstream_source_id, downstream_memory_id, upstream_version
        ) VALUES (
            :id, 'tenant_a', 'supports', 'grp_duplicate',
            'src_duplicate', 'mem_duplicate', 1
        )
        """
    )
    connection.execute(statement, {"id": "evd_duplicate_1"})
    with pytest.raises(IntegrityError):
        connection.execute(statement, {"id": "evd_duplicate_2"})


def test_evidence_rejects_self_loop(connection: Connection) -> None:
    insert_memory(connection, memory_id="mem_self")
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO memory_evidence (
                    id, tenant_id, relationship_type, evidence_group_id,
                    upstream_memory_id, downstream_memory_id, upstream_version
                ) VALUES (
                    'evd_self', 'tenant_a', 'derives', 'grp_self',
                    'mem_self', 'mem_self', 1
                )
                """
            )
        )


def test_evidence_rejects_cross_tenant_source_link(connection: Connection) -> None:
    insert_source(connection, source_id="src_cross", tenant_id="tenant_a")
    insert_memory(connection, memory_id="mem_cross", tenant_id="tenant_b")
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO memory_evidence (
                    id, tenant_id, relationship_type, evidence_group_id,
                    upstream_source_id, downstream_memory_id, upstream_version
                ) VALUES (
                    'evd_cross', 'tenant_b', 'supports', 'grp_cross',
                    'src_cross', 'mem_cross', 1
                )
                """
            )
        )


def test_profile_has_one_active_unscoped_property(connection: Connection) -> None:
    insert_profile(connection, profile_id="prf_1")
    with pytest.raises(IntegrityError):
        insert_profile(connection, profile_id="prf_2")


def test_embedding_primary_key_prevents_duplicate_model(connection: Connection) -> None:
    insert_memory(connection, memory_id="mem_embedding")
    statement = text(
        """
        INSERT INTO memory_embedding (
            tenant_id, memory_id, model_id, content_hash, embedding
        ) VALUES (
            'tenant_a', 'mem_embedding', 'test-model', 'hash',
            array_fill(0::real, ARRAY[1024])::vector
        )
        """
    )
    connection.execute(statement)
    with pytest.raises(IntegrityError):
        connection.execute(statement)


@pytest.mark.parametrize(
    ("column", "value"),
    [("priority", 25), ("attempt_count", -1)],
)
def test_task_rejects_invalid_scheduling_values(
    connection: Connection, column: str, value: int
) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                f"""
                INSERT INTO memory_task (
                    id, tenant_id, task_type, target_type, target_id,
                    idempotency_key, {column}
                ) VALUES (
                    'tsk_invalid_{column}', 'tenant_a', 'fact_extract', 'source',
                    'src_1', 'key_invalid_{column}', :value
                )
                """
            ),
            {"value": value},
        )


def test_external_mapping_primary_key_is_idempotent(connection: Connection) -> None:
    statement = text(
        """
        INSERT INTO migration.external_entity_mapping (
            tenant_id, source_system, source_namespace, entity_type,
            external_id, target_type, target_id, migration_batch_id
        ) VALUES (
            'tenant_a', 'honcho', 'local/workspace_a', 'message',
            'external_1', 'source', 'src_1', 'batch_1'
        )
        """
    )
    connection.execute(statement)
    with pytest.raises(IntegrityError):
        connection.execute(statement)


def test_supersedes_rejects_cross_tenant_memory(connection: Connection) -> None:
    insert_memory(connection, memory_id="mem_old", tenant_id="tenant_a")
    with pytest.raises(IntegrityError):
        insert_memory(
            connection,
            memory_id="mem_new",
            tenant_id="tenant_b",
            values={"supersedes_id": "mem_old"},
        )
