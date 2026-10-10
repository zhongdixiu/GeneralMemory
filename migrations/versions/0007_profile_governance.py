"""Profile assertion kinds and persistent conditional scheduling."""

import sqlalchemy as sa
from alembic import op

revision = "0007_profile_governance"
down_revision = "0006_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "governance_policy",
        sa.Column(
            "generate_inferences",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "profile_property",
        sa.Column(
            "assertion_kind",
            sa.String(16),
            nullable=False,
            server_default="explicit",
        ),
    )
    op.create_check_constraint(
        "ck_profile_assertion", "profile_property", "assertion_kind IN ('explicit', 'inferred')"
    )
    for name in ("profile_input", "profile_evaluated_input"):
        op.add_column("governance_subject_state", sa.Column(name, sa.String(64)))
    for name in ("profile_first_dirty_at", "profile_last_change_at"):
        op.add_column("governance_subject_state", sa.Column(name, sa.DateTime(timezone=True)))
    # Legacy profile-target jobs join the same per-user queue. Stop old workers before upgrade.
    op.execute("""
        UPDATE memory_task t SET user_id = p.user_id
        FROM profile_property p WHERE t.task_type = 'profile_rebuild'
          AND t.target_type = 'profile' AND t.tenant_id = p.tenant_id AND t.target_id = p.id
    """)
    op.execute("""
        UPDATE memory_task SET user_id = target_id
        WHERE task_type = 'profile_rebuild' AND target_type = 'subject' AND user_id IS NULL
    """)
    op.execute("""
        WITH duplicates AS (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, user_id, status ORDER BY created_at, id
            ) AS n FROM memory_task WHERE task_type = 'profile_rebuild'
                AND user_id IS NOT NULL AND status IN ('pending', 'processing')
        )
        UPDATE memory_task t SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        FROM duplicates d WHERE t.id = d.id AND d.n > 1
    """)
    for status in ("pending", "processing"):
        op.create_index(
            f"uq_profile_{status}_user",
            "memory_task",
            ["tenant_id", "user_id"],
            unique=True,
            postgresql_where=sa.text(f"task_type = 'profile_rebuild' AND status = '{status}'"),
        )


def downgrade() -> None:
    # Downgrade cannot safely reinterpret inferred profiles as explicit facts.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM profile_property WHERE assertion_kind = 'inferred') THEN
                RAISE EXCEPTION 'inferred profiles must be reviewed before downgrade';
            END IF;
        END $$
    """)
    for status in ("pending", "processing"):
        op.drop_index(f"uq_profile_{status}_user", table_name="memory_task")
    for name in (
        "profile_input",
        "profile_evaluated_input",
        "profile_first_dirty_at",
        "profile_last_change_at",
    ):
        op.drop_column("governance_subject_state", name)
    op.drop_constraint("ck_profile_assertion", "profile_property", type_="check")
    op.drop_column("profile_property", "assertion_kind")
    op.drop_column("governance_policy", "generate_inferences")
