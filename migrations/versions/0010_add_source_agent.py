"""Separate self-reported source Agent from the authenticated caller."""

from alembic import op

revision = "0010_add_source_agent"
down_revision = "0009_add_time_semantics"
branch_labels = None
depends_on = None


def _replace_namespaces(*, include_agent: bool) -> None:
    agent = ", agent_id" if include_agent else ""
    op.execute("ALTER TABLE source_record DROP CONSTRAINT uq_source_record_message")
    op.execute(
        "ALTER TABLE source_record ADD CONSTRAINT uq_source_record_message UNIQUE "
        f"(tenant_id, source_system, user_id, session_id, message_id, caller_agent_id{agent})"
    )
    op.execute("ALTER TABLE conversation_session DROP CONSTRAINT pk_conversation_session")
    op.execute(
        "ALTER TABLE conversation_session ADD CONSTRAINT pk_conversation_session PRIMARY KEY "
        f"(tenant_id, caller_agent_id{agent}, source_system, user_id, session_id)"
    )
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT uq_memory_task_idempotency")
    op.execute(
        "ALTER TABLE memory_task ADD CONSTRAINT uq_memory_task_idempotency UNIQUE "
        f"(tenant_id, caller_agent_id{agent}, idempotency_key, user_id)"
    )
    for table, suffix in (("source_record", ""), ("memory_task", ", batch_seq")):
        op.execute(f"DROP INDEX ix_{table}_conversation")
        op.execute(
            f"CREATE INDEX ix_{table}_conversation ON {table} "
            f"(tenant_id, source_system, caller_agent_id{agent}, user_id, session_id{suffix})"
        )


def upgrade() -> None:
    # Pause API/Worker writes before deploying the new identity contract.
    op.execute("LOCK TABLE source_record, memory_task, conversation_session "
               "IN ACCESS EXCLUSIVE MODE")
    for table in ("source_record", "memory_task", "conversation_session"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN agent_id VARCHAR(128)")
        op.execute(f"UPDATE {table} SET agent_id = caller_agent_id")
    op.execute("ALTER TABLE conversation_session ALTER COLUMN agent_id SET NOT NULL")
    _replace_namespaces(include_agent=True)


def downgrade() -> None:
    op.execute("LOCK TABLE source_record, memory_task, conversation_session "
               "IN ACCESS EXCLUSIVE MODE")
    # Even without a key collision, dropping a non-default source identity loses provenance.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM source_record
                     WHERE agent_id IS DISTINCT FROM caller_agent_id)
            OR EXISTS (SELECT 1 FROM memory_task
                       WHERE agent_id IS DISTINCT FROM caller_agent_id)
            OR EXISTS (SELECT 1 FROM conversation_session
                       WHERE agent_id IS DISTINCT FROM caller_agent_id) THEN
            RAISE EXCEPTION 'Add Agent downgrade: explicit source Agent identities would be lost';
          END IF;
        END $$
    """)
    _replace_namespaces(include_agent=False)
    for table in ("source_record", "memory_task", "conversation_session"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN agent_id")
