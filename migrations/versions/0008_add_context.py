"""Isolate Add conversations and idempotency without rewriting source evidence.

Revision ID: 0008_add_context
Revises: 0007_profile_governance
"""

from alembic import op

revision = "0008_add_context"
down_revision = "0007_profile_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deployment must stop API/Worker writes. Locks also fence accidental concurrent writes.
    op.execute("LOCK TABLE source_record, memory_task, memory_task_source, "
               "conversation_session IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM conversation_session c WHERE NOT EXISTS (
              SELECT 1 FROM memory_task t WHERE t.tenant_id = c.tenant_id
                AND t.source_system = c.source_system AND t.user_id = c.user_id
                AND t.session_id = c.session_id AND t.task_type = 'fact_extract'
                AND t.caller_agent_id IS NOT NULL AND t.batch_seq IS NOT NULL
            )
          ) THEN
            RAISE EXCEPTION 'Add migration: conversation has no verifiable Agent mapping';
          END IF;
          IF EXISTS (
            SELECT a.source_id FROM memory_task_source a
            JOIN memory_task t ON t.tenant_id = a.tenant_id AND t.id = a.task_id
            WHERE t.task_type = 'fact_extract'
            GROUP BY a.tenant_id, a.source_id
            HAVING count(DISTINCT t.caller_agent_id) <> 1
                OR bool_or(t.caller_agent_id IS NULL)
          ) THEN
            RAISE EXCEPTION 'Add migration: ambiguous source Agent ownership; '
                            'inspect memory_task_source and memory_task, resolve before retry';
          END IF;
        END $$
    """)
    op.execute("ALTER TABLE source_record ADD COLUMN caller_agent_id VARCHAR(128)")
    op.execute("""
        UPDATE source_record s SET caller_agent_id = owners.agent
        FROM (
          SELECT a.tenant_id, a.source_id, min(t.caller_agent_id) agent
          FROM memory_task_source a
          JOIN memory_task t ON t.tenant_id = a.tenant_id AND t.id = a.task_id
          WHERE t.task_type = 'fact_extract' GROUP BY a.tenant_id, a.source_id
        ) owners WHERE s.tenant_id = owners.tenant_id AND s.id = owners.source_id
    """)
    op.execute("ALTER TABLE source_record DROP CONSTRAINT uq_source_record_message")
    op.execute("ALTER TABLE source_record ADD CONSTRAINT uq_source_record_message UNIQUE "
               "(tenant_id, source_system, user_id, session_id, message_id, caller_agent_id)")
    op.execute("DROP INDEX ix_source_record_conversation")
    op.execute("CREATE INDEX ix_source_record_conversation ON source_record "
               "(tenant_id, source_system, caller_agent_id, user_id, session_id)")
    op.execute("ALTER TABLE conversation_session RENAME TO conversation_session_v1")
    op.execute("""
        CREATE TABLE conversation_session (
          tenant_id VARCHAR(64) NOT NULL, caller_agent_id VARCHAR(128) NOT NULL,
          source_system VARCHAR(64) NOT NULL, user_id VARCHAR(128) NOT NULL,
          session_id VARCHAR(128) NOT NULL, next_batch_seq BIGINT NOT NULL DEFAULT 1,
          created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
          CONSTRAINT pk_conversation_session_v2 PRIMARY KEY
            (tenant_id, caller_agent_id, source_system, user_id, session_id),
          CONSTRAINT ck_conversation_next_batch_seq_positive_v2 CHECK (next_batch_seq > 0)
        )
    """)
    op.execute("""
        INSERT INTO conversation_session
          (tenant_id, caller_agent_id, source_system, user_id, session_id, next_batch_seq)
        SELECT t.tenant_id, t.caller_agent_id, t.source_system, t.user_id, t.session_id,
               greatest(max(t.batch_seq) + 1, max(c.next_batch_seq)) FROM memory_task t
        LEFT JOIN conversation_session_v1 c ON c.tenant_id = t.tenant_id
          AND c.source_system = t.source_system AND c.user_id = t.user_id
          AND c.session_id = t.session_id
        WHERE t.task_type = 'fact_extract' AND t.batch_seq IS NOT NULL
        GROUP BY t.tenant_id, t.caller_agent_id, t.source_system, t.user_id, t.session_id
    """)
    op.execute("DROP TABLE conversation_session_v1")
    op.execute("ALTER TABLE conversation_session RENAME CONSTRAINT "
               "pk_conversation_session_v2 TO pk_conversation_session")
    op.execute("ALTER TABLE conversation_session RENAME CONSTRAINT "
               "ck_conversation_next_batch_seq_positive_v2 "
               "TO ck_conversation_next_batch_seq_positive")
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT uq_memory_task_idempotency")
    op.execute("ALTER TABLE memory_task ADD CONSTRAINT uq_memory_task_idempotency "
               "UNIQUE (tenant_id, caller_agent_id, idempotency_key, user_id)")
    op.execute("CREATE UNIQUE INDEX uq_service_task_key ON memory_task "
               "(tenant_id, caller_agent_id, idempotency_key) "
               "WHERE caller_agent_id IS NOT NULL AND user_id IS NULL")
    op.execute("DROP INDEX ix_memory_task_conversation")
    op.execute("CREATE INDEX ix_memory_task_conversation ON memory_task "
               "(tenant_id, source_system, caller_agent_id, user_id, session_id, batch_seq)")


def downgrade() -> None:
    # Old constraints reject new cross-Agent/user namespaces. Transactional DDL rolls back
    # the downgrade on any collision; operators must not delete evidence to force it through.
    op.execute("LOCK TABLE source_record, memory_task, memory_task_source, "
               "conversation_session IN ACCESS EXCLUSIVE MODE")
    op.execute("ALTER TABLE source_record DROP CONSTRAINT uq_source_record_message")
    op.execute("ALTER TABLE source_record ADD CONSTRAINT uq_source_record_message UNIQUE "
               "(tenant_id, source_system, user_id, session_id, message_id)")
    op.execute("DROP INDEX ix_source_record_conversation")
    op.execute("CREATE INDEX ix_source_record_conversation ON source_record "
               "(tenant_id, source_system, user_id, session_id)")
    op.execute("ALTER TABLE source_record DROP COLUMN caller_agent_id")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM conversation_session
            GROUP BY tenant_id, source_system, session_id HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'Add downgrade: conversation namespace collision';
          END IF;
        END $$
    """)
    op.execute("ALTER TABLE conversation_session DROP CONSTRAINT pk_conversation_session")
    op.execute("ALTER TABLE conversation_session ADD CONSTRAINT pk_conversation_session "
               "PRIMARY KEY (tenant_id, source_system, session_id)")
    op.execute("ALTER TABLE conversation_session DROP COLUMN caller_agent_id")
    op.execute("DROP INDEX uq_service_task_key")
    op.execute("ALTER TABLE memory_task DROP CONSTRAINT uq_memory_task_idempotency")
    op.execute("ALTER TABLE memory_task ADD CONSTRAINT uq_memory_task_idempotency "
               "UNIQUE (tenant_id, caller_agent_id, idempotency_key)")
    op.execute("DROP INDEX ix_memory_task_conversation")
    op.execute("CREATE INDEX ix_memory_task_conversation ON memory_task "
               "(tenant_id, source_system, user_id, session_id, batch_seq)")
