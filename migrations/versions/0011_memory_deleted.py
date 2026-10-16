"""Distinguish explicit memory deletion from evidence invalidation."""

from alembic import op

revision = "0011_memory_deleted"
down_revision = "0010_add_source_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_memory_status", "memory_item", type_="check")
    op.create_check_constraint(
        "ck_memory_status", "memory_item",
        "status IN ('active', 'invalidated', 'disputed', 'expired', 'deleted')",
    )


def downgrade() -> None:
    # Existing deleted records deliberately prevent downgrade; never restore them implicitly.
    op.drop_constraint("ck_memory_status", "memory_item", type_="check")
    op.create_check_constraint(
        "ck_memory_status", "memory_item",
        "status IN ('active', 'invalidated', 'disputed', 'expired')",
    )
