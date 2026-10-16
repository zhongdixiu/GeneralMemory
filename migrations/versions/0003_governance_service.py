"""Add governed consolidation state and task constraints.

Revision ID: 0003_governance_service
Revises: 0002_add_ingestion_pipeline
"""

from alembic import op

revision = "0003_governance_service"
down_revision = "0002_add_ingestion_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT ck_task_type")
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT ck_task_type CHECK "
        "(task_type IN ('fact_extract', 'inference_derive', 'profile_rebuild', "
        "'dependency_recheck', 'vector_upsert', 'vector_delete', 'ttl_expire', 'consolidate'))"
    )
    op.execute(
        """
        WITH duplicates AS (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, idempotency_key ORDER BY created_at, id
            ) AS n
            FROM memory_task WHERE caller_agent_id IS NULL
        )
        UPDATE memory_task AS task
        SET idempotency_key = left(task.idempotency_key, 175) || ':legacy:' || task.id
        FROM duplicates WHERE task.id = duplicates.id AND duplicates.n > 1
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_system_task_key ON memory_task "
        "(tenant_id, idempotency_key) WHERE caller_agent_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_governance_active_run ON memory_task "
        "(tenant_id, user_id) WHERE task_type = 'consolidate' "
        "AND status IN ('pending', 'processing')"
    )
    op.execute(
        """
        CREATE TABLE governance_policy (
            tenant_id VARCHAR(64) PRIMARY KEY,
            auto_enabled BOOLEAN NOT NULL DEFAULT false,
            change_threshold INTEGER NOT NULL DEFAULT 5,
            idle_minutes INTEGER NOT NULL DEFAULT 10,
            cooldown_minutes INTEGER NOT NULL DEFAULT 60,
            max_wait_minutes INTEGER NOT NULL DEFAULT 360,
            max_memories INTEGER NOT NULL DEFAULT 100,
            max_model_calls INTEGER NOT NULL DEFAULT 10,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        CREATE TABLE governance_subject_state (
            tenant_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            last_add_at TIMESTAMPTZ,
            last_run_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE governance_pending (
            tenant_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            memory_id VARCHAR(64) NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            first_change_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, user_id, memory_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_governance_pending_age ON governance_pending (tenant_id, first_change_at)"
    )
    op.execute(
        """
        CREATE TABLE governance_operation (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            task_id VARCHAR(64) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            memory_id VARCHAR(64),
            details JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reverted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_governance_operation_task ON governance_operation (tenant_id, task_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE governance_operation")
    op.execute("DROP TABLE governance_pending")
    op.execute("DROP TABLE governance_subject_state")
    op.execute("DROP TABLE governance_policy")
    op.execute("DROP INDEX uq_governance_active_run")
    op.execute("DROP INDEX uq_system_task_key")
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT ck_task_type")
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT ck_task_type CHECK "
        "(task_type IN ('fact_extract', 'inference_derive', 'profile_rebuild', "
        "'dependency_recheck', 'vector_upsert', 'vector_delete', 'ttl_expire'))"
    )
