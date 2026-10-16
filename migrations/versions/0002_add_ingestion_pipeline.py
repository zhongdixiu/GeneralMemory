"""Add the standard conversation ingestion pipeline.

Revision ID: 0002_add_ingestion_pipeline
Revises: 0001_memory_model_v1
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import op

revision = "0002_add_ingestion_pipeline"
down_revision = "0001_memory_model_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE source_record ADD COLUMN user_id VARCHAR(128)")
    op.execute("ALTER TABLE source_record ADD COLUMN message_id VARCHAR(256)")
    op.execute("ALTER TABLE source_record ADD COLUMN semantic_hash VARCHAR(64)")
    op.execute("ALTER TABLE source_record ADD COLUMN processed_version INTEGER")
    op.execute("ALTER TABLE source_record ALTER COLUMN author_id TYPE VARCHAR(128)")
    op.execute(
        "ALTER TABLE source_record ADD CONSTRAINT ck_source_record_processed_version_positive "
        "CHECK (processed_version IS NULL OR processed_version > 0)"
    )
    op.execute(
        "ALTER TABLE source_record ADD CONSTRAINT uq_source_record_message UNIQUE "
        "(tenant_id, source_system, user_id, session_id, message_id)"
    )
    op.execute(
        "CREATE INDEX ix_source_record_conversation ON source_record "
        "(tenant_id, source_system, user_id, session_id)"
    )

    op.execute("ALTER TABLE memory_item ALTER COLUMN subject_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE memory_item ADD COLUMN semantic_hash VARCHAR(64)")
    op.execute(
        "CREATE INDEX ix_memory_item_fact_hash ON memory_item "
        "(tenant_id, subject_type, subject_id, semantic_hash) "
        "WHERE status = 'active' AND semantic_hash IS NOT NULL"
    )
    op.execute("ALTER TABLE profile_property ALTER COLUMN user_id TYPE VARCHAR(128)")
    op.execute("ALTER TABLE memory_audit_log ALTER COLUMN operator_id TYPE VARCHAR(128)")

    # Preserve validation-era 1536-dimensional vectors for audit/rebuild instead of deleting them.
    op.execute("DROP INDEX ix_memory_embedding_active_hnsw")
    op.execute("ALTER TABLE memory_embedding RENAME COLUMN embedding TO embedding_legacy")
    op.execute("ALTER TABLE memory_embedding ALTER COLUMN embedding_legacy DROP NOT NULL")
    op.execute("ALTER TABLE memory_embedding ADD COLUMN embedding VECTOR(1024)")
    op.execute("UPDATE memory_embedding SET status = 'stale'")
    op.execute(
        "CREATE INDEX ix_memory_embedding_active_hnsw ON memory_embedding "
        "USING hnsw (embedding vector_cosine_ops) "
        "WHERE status = 'active' AND embedding IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE conversation_session (
            tenant_id VARCHAR(64) NOT NULL,
            source_system VARCHAR(64) NOT NULL,
            session_id VARCHAR(128) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            next_batch_seq BIGINT NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT pk_conversation_session PRIMARY KEY
                (tenant_id, source_system, session_id),
            CONSTRAINT ck_conversation_next_batch_seq_positive CHECK (next_batch_seq > 0)
        )
        """
    )

    op.execute("ALTER TABLE memory_task DROP CONSTRAINT uq_memory_task_idempotency")
    op.execute("ALTER TABLE memory_task ADD COLUMN caller_agent_id VARCHAR(128)")
    op.execute("ALTER TABLE memory_task ADD COLUMN request_fingerprint VARCHAR(64)")
    op.execute("ALTER TABLE memory_task ADD COLUMN source_system VARCHAR(64)")
    op.execute("ALTER TABLE memory_task ADD COLUMN user_id VARCHAR(128)")
    op.execute("ALTER TABLE memory_task ADD COLUMN session_id VARCHAR(128)")
    op.execute("ALTER TABLE memory_task ADD COLUMN batch_seq BIGINT")
    op.execute("ALTER TABLE memory_task ADD COLUMN result JSONB")
    op.execute("ALTER TABLE memory_task ADD COLUMN error JSONB")
    op.execute("ALTER TABLE memory_task ADD COLUMN lease_token VARCHAR(64)")
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT uq_memory_task_tenant_id_id "
        "UNIQUE (tenant_id, id)"
    )
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT uq_memory_task_idempotency "
        "UNIQUE (tenant_id, caller_agent_id, idempotency_key)"
    )
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT ck_task_status")
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT ck_task_status CHECK "
        "(status IN ('pending', 'processing', 'succeeded', 'partial', 'failed', 'cancelled'))"
    )
    op.execute(
        "CREATE INDEX ix_memory_task_conversation ON memory_task "
        "(tenant_id, source_system, user_id, session_id, batch_seq)"
    )

    op.execute(
        """
        CREATE TABLE memory_task_source (
            tenant_id VARCHAR(64) NOT NULL,
            task_id VARCHAR(64) NOT NULL,
            source_id VARCHAR(64) NOT NULL,
            source_version INTEGER NOT NULL,
            kind VARCHAR(16) NOT NULL,
            position INTEGER NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT pk_memory_task_source PRIMARY KEY (tenant_id, task_id, source_id),
            CONSTRAINT fk_memory_task_source_task FOREIGN KEY (tenant_id, task_id)
                REFERENCES memory_task (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_memory_task_source_source FOREIGN KEY (tenant_id, source_id)
                REFERENCES source_record (tenant_id, id),
            CONSTRAINT ck_task_source_version_positive CHECK (source_version > 0),
            CONSTRAINT ck_task_source_kind CHECK (kind IN ('target', 'history')),
            CONSTRAINT ck_task_source_position_nonnegative CHECK (position >= 0),
            CONSTRAINT ck_task_source_status CHECK
                (status IN ('pending', 'processed', 'skipped'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_task_source_source ON memory_task_source "
        "(tenant_id, source_id, kind)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE memory_task_source")
    op.execute("DROP INDEX ix_memory_task_conversation")
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT ck_task_status")
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT ck_task_status CHECK "
        "(status IN ('pending', 'processing', 'succeeded', 'failed', 'cancelled'))"
    )
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT uq_memory_task_idempotency")
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT uq_memory_task_tenant_id_id")
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT uq_memory_task_idempotency "
        "UNIQUE (tenant_id, idempotency_key)"
    )
    for column in (
        "lease_token",
        "error",
        "result",
        "batch_seq",
        "session_id",
        "user_id",
        "source_system",
        "request_fingerprint",
        "caller_agent_id",
    ):
        op.execute(f'ALTER TABLE memory_task DROP COLUMN "{column}"')

    op.execute("DROP TABLE conversation_session")

    op.execute("DROP INDEX ix_memory_embedding_active_hnsw")
    op.execute("ALTER TABLE memory_embedding DROP COLUMN embedding")
    op.execute("ALTER TABLE memory_embedding RENAME COLUMN embedding_legacy TO embedding")
    op.execute("DELETE FROM memory_embedding WHERE embedding IS NULL")
    op.execute("ALTER TABLE memory_embedding ALTER COLUMN embedding SET NOT NULL")
    op.execute(
        "CREATE INDEX ix_memory_embedding_active_hnsw ON memory_embedding "
        "USING hnsw (embedding vector_cosine_ops) WHERE status = 'active'"
    )

    op.execute("ALTER TABLE profile_property ALTER COLUMN user_id TYPE VARCHAR(64)")
    op.execute("ALTER TABLE memory_audit_log ALTER COLUMN operator_id TYPE VARCHAR(64)")
    op.execute("DROP INDEX ix_memory_item_fact_hash")
    op.execute("ALTER TABLE memory_item DROP COLUMN semantic_hash")
    op.execute("ALTER TABLE memory_item ALTER COLUMN subject_id TYPE VARCHAR(64)")
    op.execute("DROP INDEX ix_source_record_conversation")
    op.execute("ALTER TABLE source_record DROP CONSTRAINT uq_source_record_message")
    op.execute(
        "ALTER TABLE source_record DROP CONSTRAINT "
        "ck_source_record_processed_version_positive"
    )
    op.execute("ALTER TABLE source_record ALTER COLUMN author_id TYPE VARCHAR(64)")
    for column in ("processed_version", "semantic_hash", "message_id", "user_id"):
        op.execute(f"ALTER TABLE source_record DROP COLUMN {column}")
