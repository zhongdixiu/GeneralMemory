"""Add explicit audit task/context links without rewriting historical events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_audit_events"
down_revision = "0005_atomic_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_audit_log", sa.Column("task_id", sa.String(64), nullable=True))
    op.add_column("memory_audit_log", sa.Column("context", postgresql.JSONB(), nullable=True))
    op.drop_constraint("ck_audit_target_type", "memory_audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_target_type",
        "memory_audit_log",
        "target_type IN ('source', 'memory', 'evidence', 'profile', 'embedding', 'task', "
        "'governance_policy')",
    )
    op.create_index("ix_audit_task", "memory_audit_log", ["tenant_id", "task_id", "id"])
    op.create_index(
        "ix_audit_timeline",
        "memory_audit_log",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    # The old CHECK deliberately rejects downgrade while policy events still exist.
    op.drop_constraint("ck_audit_target_type", "memory_audit_log", type_="check")
    op.create_check_constraint(
        "ck_audit_target_type",
        "memory_audit_log",
        "target_type IN ('source', 'memory', 'evidence', 'profile', 'embedding', 'task')",
    )
    op.drop_index("ix_audit_timeline", table_name="memory_audit_log")
    op.drop_index("ix_audit_task", table_name="memory_audit_log")
    op.drop_column("memory_audit_log", "context")
    op.drop_column("memory_audit_log", "task_id")
