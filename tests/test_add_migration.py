import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def old_add_schema(connection):
    schema = "add_migration_" + uuid4().hex
    connection.execute(text(f"CREATE SCHEMA {schema}"))
    connection.execute(text(f"SET LOCAL search_path TO {schema}, public"))
    ddl = [
        """CREATE TABLE source_record (
          id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(64), source_system VARCHAR(64),
          user_id VARCHAR(128), session_id VARCHAR(128), message_id VARCHAR(256),
          external_ref_id VARCHAR(128), raw_content TEXT,
          CONSTRAINT uq_source_record_message UNIQUE
            (tenant_id, source_system, user_id, session_id, message_id))""",
        "CREATE INDEX ix_source_record_conversation ON source_record "
        "(tenant_id, source_system, user_id, session_id)",
        """CREATE TABLE memory_task (
          id VARCHAR(64) PRIMARY KEY, tenant_id VARCHAR(64), caller_agent_id VARCHAR(128),
          source_system VARCHAR(64), user_id VARCHAR(128), session_id VARCHAR(128),
          task_type VARCHAR(32), batch_seq BIGINT, idempotency_key VARCHAR(256), payload JSONB,
          CONSTRAINT uq_memory_task_idempotency UNIQUE
            (tenant_id, caller_agent_id, idempotency_key))""",
        "CREATE INDEX ix_memory_task_conversation ON memory_task "
        "(tenant_id, source_system, user_id, session_id, batch_seq)",
        """CREATE TABLE memory_task_source (
          tenant_id VARCHAR(64), task_id VARCHAR(64), source_id VARCHAR(64))""",
        """CREATE TABLE conversation_session (
          tenant_id VARCHAR(64), source_system VARCHAR(64), session_id VARCHAR(128),
          user_id VARCHAR(128), next_batch_seq BIGINT,
          CONSTRAINT pk_conversation_session PRIMARY KEY (tenant_id, source_system, session_id),
          CONSTRAINT ck_conversation_next_batch_seq_positive CHECK(next_batch_seq > 0))""",
        """INSERT INTO source_record VALUES
          ('source', 'tenant', 'chat', 'user', 'session', 'message', 'stable-ref', '原文')""",
        """INSERT INTO memory_task VALUES
          ('task', 'tenant', 'agent', 'chat', 'user', 'session', 'fact_extract', 7,
           'key', '{"execution": {"next_slice": 1}}')""",
        "INSERT INTO memory_task_source VALUES ('tenant', 'task', 'source')",
        "INSERT INTO conversation_session VALUES ('tenant', 'chat', 'session', 'user', 8)",
    ]
    for statement in ddl:
        connection.execute(text(statement))
    path = Path(__file__).parents[1] / "migrations/versions/0008_add_context.py"
    spec = importlib.util.spec_from_file_location("add_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        yield connection, module


def test_add_migration_preserves_source_task_and_progress(old_add_schema):
    connection, migration = old_add_schema
    migration.upgrade()
    source = connection.execute(text("SELECT * FROM source_record")).mappings().one()
    assert source["id"] == "source" and source["caller_agent_id"] == "agent"
    assert source["external_ref_id"] == "stable-ref" and source["raw_content"] == "原文"
    task = connection.execute(text("SELECT * FROM memory_task")).mappings().one()
    assert task["id"] == "task" and task["payload"]["execution"]["next_slice"] == 1
    conversation = connection.execute(text("SELECT * FROM conversation_session")).mappings().one()
    assert conversation["caller_agent_id"] == "agent" and conversation["next_batch_seq"] == 8
    migration.downgrade()
    assert connection.scalar(text("SELECT next_batch_seq FROM conversation_session")) == 8
    assert connection.scalar(text("SELECT external_ref_id FROM source_record")) == "stable-ref"


def test_add_migration_rejects_ambiguous_ownership_without_modification(old_add_schema):
    connection, migration = old_add_schema
    connection.execute(text("""INSERT INTO memory_task VALUES
      ('task2', 'tenant', 'other-agent', 'chat', 'user', 'session', 'fact_extract', 8, 'key2', '{}')
      """))
    connection.execute(text("INSERT INTO memory_task_source VALUES ('tenant', 'task2', 'source')"))
    with pytest.raises(DBAPIError, match="ambiguous source Agent ownership"):
        with connection.begin_nested():
            migration.upgrade()
    assert connection.scalar(text("SELECT count(*) FROM source_record")) == 1
    assert connection.scalar(text("SELECT raw_content FROM source_record")) == "原文"
    assert connection.scalar(text("SELECT count(*) FROM memory_task")) == 2


def test_add_downgrade_refuses_new_namespace_collisions(old_add_schema):
    connection, migration = old_add_schema
    migration.upgrade()
    connection.execute(text("""INSERT INTO conversation_session
      (tenant_id, caller_agent_id, source_system, user_id, session_id, next_batch_seq)
      VALUES ('tenant', 'other-agent', 'chat', 'user', 'session', 1)"""))
    with pytest.raises(DBAPIError, match="namespace collision"):
        with connection.begin_nested():
            migration.downgrade()
    assert connection.scalar(text("SELECT count(*) FROM conversation_session")) == 2
    assert connection.scalar(text("SELECT caller_agent_id FROM source_record")) == "agent"


@pytest.fixture
def old_time_schema(connection):
    schema = "time_migration_" + uuid4().hex
    connection.execute(text(f"CREATE SCHEMA {schema}"))
    connection.execute(text(f"SET LOCAL search_path TO {schema}, public"))
    connection.execute(text("""CREATE TABLE source_record (
        id TEXT PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        metadata JSONB)"""))
    connection.execute(text("""INSERT INTO source_record VALUES
        ('source', '2024-01-01T00:00:00Z', '2026-10-13T00:00:00Z',
         '{"timestamp_origin":"adapter_observed","original_occurred_at":"2024-01-01T00:00:00Z"}')
    """))
    path = Path(__file__).parents[1] / "migrations/versions/0009_add_time_semantics.py"
    spec = importlib.util.spec_from_file_location("time_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        yield connection, module


def test_time_migration_preserves_old_observation_and_labels_receipt_estimate(old_time_schema):
    connection, migration = old_time_schema
    migration.upgrade()
    source = connection.execute(text("SELECT * FROM source_record")).mappings().one()
    assert source["received_at"] == source["created_at"]
    assert source["metadata"]["received_at_origin"] == "legacy_created_at"
    assert source["metadata"]["timestamp_origin"] == "adapter_observed"
    assert source["occurred_at"].year == 2024
    migration.downgrade()
    assert connection.scalar(text("SELECT occurred_at FROM source_record")).year == 2024


def test_time_downgrade_refuses_to_invent_unknown_source_date(old_time_schema):
    connection, migration = old_time_schema
    migration.upgrade()
    connection.execute(text("UPDATE source_record SET occurred_at = NULL"))
    with pytest.raises(DBAPIError):
        with connection.begin_nested():
            migration.downgrade()
    assert connection.scalar(text("SELECT occurred_at FROM source_record")) is None
    assert connection.scalar(text("SELECT received_at FROM source_record")) is not None
