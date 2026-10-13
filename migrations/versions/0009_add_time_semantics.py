"""Separate first receipt from unknown source time without rewriting memory lifetimes."""

from alembic import op

revision = "0009_add_time_semantics"
down_revision = "0008_add_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("LOCK TABLE source_record IN ACCESS EXCLUSIVE MODE")
    op.execute("ALTER TABLE source_record ADD COLUMN received_at TIMESTAMPTZ")
    op.execute("""
        UPDATE source_record SET received_at = created_at,
          metadata = coalesce(metadata, '{}'::jsonb) ||
            '{"received_at_origin":"legacy_created_at"}'::jsonb
    """)
    op.execute("ALTER TABLE source_record ALTER COLUMN received_at SET NOT NULL")
    op.execute("ALTER TABLE source_record ALTER COLUMN received_at SET DEFAULT CURRENT_TIMESTAMP")
    op.execute("ALTER TABLE source_record ALTER COLUMN occurred_at DROP NOT NULL")


def downgrade() -> None:
    # Unknown original timestamps cannot safely be reconstructed during downgrade.
    op.execute("ALTER TABLE source_record ALTER COLUMN occurred_at SET NOT NULL")
    op.execute("ALTER TABLE source_record DROP COLUMN received_at")
