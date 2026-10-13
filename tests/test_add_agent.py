import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_add_api import _body, _headers
from test_add_api import api_context as _api_context
from test_add_migration import old_add_schema as _old_add_schema
from test_add_worker import FakeFactModel, _run
from test_add_worker import worker_context as _worker_context

from memory_cmic.add_management import reprocess_add
from memory_cmic.auth import AuthContext
from memory_cmic.models import MemoryAuditLog, MemoryTask, SourceRecord
from memory_cmic.retrieval import _matching_source_ids
from memory_cmic.task_queue import claim_tasks
from memory_cmic.worker import _load_task_input

api_context = _api_context
worker_context = _worker_context
old_add_schema = _old_add_schema


@pytest.mark.parametrize("agent_id", [None, "", "   ", "a" * 129, 123])
def test_invalid_agent_id_is_rejected(api_context, agent_id):
    client, _ = api_context
    assert (
        client.post(
            "/api/v1/memories:add", headers=_headers(), json={**_body(), "agent_id": agent_id}
        ).status_code
        == 422
    )


def test_omitted_and_explicit_default_agent_replay_same_task(api_context, engine):
    client, tenant = api_context
    body = _body()
    first = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    assert first.status_code == 202
    explicit = client.post(
        "/api/v1/memories:add", headers=_headers(), json={**body, "agent_id": "agent_email"}
    )
    assert explicit.json()["task_id"] == first.json()["task_id"]
    with Session(engine) as session:
        source = session.scalars(select(SourceRecord).where(SourceRecord.tenant_id == tenant)).one()
        assert source.agent_id == source.caller_agent_id == "agent_email"


def test_one_credential_isolates_agent_sources_idempotency_and_order(api_context, engine):
    client, tenant = api_context
    ids = []
    for agent_id in ("agent_a", "agent_b"):
        body = {**_body(content=f"来自{agent_id}。"), "agent_id": agent_id}
        response = client.post("/api/v1/memories:add", headers=_headers(), json=body)
        assert response.status_code == 202
        ids.append(response.json()["task_id"])
        replay = client.post("/api/v1/memories:add", headers=_headers(), json=body)
        assert replay.json()["task_id"] == ids[-1]
        assert client.get(f"/api/v1/tasks/{ids[-1]}", headers=_headers()).status_code == 200
    assert len(set(ids)) == 2
    body = {**_body(message_id="next"), "agent_id": "agent_a"}
    next_task = client.post("/api/v1/memories:add", headers=_headers(key="next"), json=body).json()[
        "task_id"
    ]
    conflict = client.post(
        "/api/v1/memories:add", headers=_headers(), json={**body, "agent_id": "agent_b"}
    )
    assert conflict.status_code == 409
    with Session(engine) as session, session.begin():
        tasks = session.scalars(select(MemoryTask).where(MemoryTask.id.in_(ids))).all()
        assert {t.agent_id for t in tasks} == {"agent_a", "agent_b"}
        assert all(t.caller_agent_id == "agent_email" and t.batch_seq == 1 for t in tasks)
        assert session.get(MemoryTask, next_task).batch_seq == 2
        sources = session.scalars(
            select(SourceRecord).where(SourceRecord.tenant_id == tenant)
        ).all()
        assert len(sources) == 3
        claimed = claim_tasks(session, tenant_id=tenant, worker_id="claim", limit=3)
        assert {t.id for t in claimed} == set(ids)


def test_self_reported_agent_preserves_credential_access_and_permissions(api_context, engine):
    client, tenant = api_context
    body = {**_body(), "agent_id": "agent_email"}
    own = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    other = client.post("/api/v1/memories:add", headers=_headers("other-token"), json=body)
    assert own.status_code == other.status_code == 202
    assert own.json()["task_id"] != other.json()["task_id"]
    assert (
        client.get(
            f"/api/v1/tasks/{own.json()['task_id']}", headers=_headers("other-token")
        ).status_code
        == 404
    )
    body["agent_id"] = "administrator"
    body["source_system"] = "forbidden_system"
    assert client.post("/api/v1/memories:add", headers=_headers(), json=body).status_code == 403
    assert (
        client.post(
            "/api/v1/governance/runs", headers=_headers(), json={"user_id": "user_1"}
        ).status_code
        == 403
    )
    with Session(engine) as session:
        sources = session.scalars(
            select(SourceRecord).where(SourceRecord.tenant_id == tenant)
        ).all()
        assert {s.caller_agent_id for s in sources} == {"agent_email", "agent_other"}
        assert all(s.agent_id == "agent_email" for s in sources)
        other_input = _load_task_input(session, session.get(MemoryTask, other.json()["task_id"]))
        assert other_input.history == [] and other_input.related_memories == []


def test_assistant_author_defaults_to_source_agent(api_context, engine):
    client, tenant = api_context
    body = {**_body(), "agent_id": "business_agent"}
    body["messages"][0]["role"] = "assistant"
    response = client.post("/api/v1/memories:add", headers=_headers(), json=body)
    assert response.status_code == 202
    with Session(engine) as session:
        source = session.scalars(select(SourceRecord).where(SourceRecord.tenant_id == tenant)).one()
        assert source.author_id == source.agent_id == "business_agent"
        assert source.caller_agent_id == "agent_email"
    body["messages"][0]["author_id"] = "another_agent"
    assert (
        client.post(
            "/api/v1/memories:add", headers=_headers(key="invalid-author"), json=body
        ).status_code
        == 422
    )


def test_worker_context_and_search_filter_use_source_agent(worker_context, engine):
    client, tenant = worker_context
    headers = _headers("worker-token")
    first = {**_body(content="用户住在北京。"), "agent_id": "business_a"}
    response = client.post("/api/v1/memories:add", headers=headers, json=first)
    assert response.status_code == 202
    _run(engine, FakeFactModel())
    for agent_id in ("business_b", "business_a"):
        body = {**_body(message_id=f"next-{agent_id}"), "agent_id": agent_id}
        task_id = client.post(
            "/api/v1/memories:add", headers=_headers("worker-token", agent_id), json=body
        ).json()["task_id"]
        with Session(engine) as session:
            data = _load_task_input(session, session.get(MemoryTask, task_id))
            assert data.agent_id == agent_id and data.caller_agent_id == "agent_worker"
            assert bool(data.history) == bool(data.related_memories) == (agent_id == "business_a")
            assert {m["message_id"] for m in data.history} == (
                {"msg_1"} if agent_id == "business_a" else set()
            )
    with Session(engine) as session:
        source = session.scalars(
            select(SourceRecord).where(
                SourceRecord.tenant_id == tenant, SourceRecord.message_id == "msg_1"
            )
        ).one()
        assert source.id in _matching_source_ids(
            session, tenant_id=tenant, user_id="user_1", source_system=None, agent_id="business_a"
        )
        assert source.id not in _matching_source_ids(
            session, tenant_id=tenant, user_id="user_1", source_system=None, agent_id="agent_worker"
        )
        audits = session.scalars(
            select(MemoryAuditLog).where(
                MemoryAuditLog.task_id == response.json()["task_id"],
                MemoryAuditLog.reason_code == "FACT_EXTRACTION_ENQUEUED",
            )
        ).all()
        assert audits[0].operator_id == "agent_worker"
        assert audits[0].context["agent_id"] == "business_a"


def test_management_reprocess_preserves_both_agent_identities(worker_context, engine):
    client, tenant = worker_context
    task_id = client.post(
        "/api/v1/memories:add",
        headers=_headers("worker-token"),
        json={**_body(), "agent_id": "business_agent"},
    ).json()["task_id"]
    _run(engine, FakeFactModel())
    with Session(engine) as session, session.begin():
        task = reprocess_add(
            session,
            task_id=task_id,
            reason="测试身份保留",
            auth=AuthContext(tenant, "manager", frozenset(), can_manage_governance=True),
        )
        assert task.agent_id == "business_agent" and task.caller_agent_id == "agent_worker"
        assert task.batch_seq == 2


@pytest.fixture
def old_agent_schema(old_add_schema):
    connection, previous = old_add_schema
    previous.upgrade()
    path = Path(__file__).parents[1] / "migrations/versions/0010_add_source_agent.py"
    spec = importlib.util.spec_from_file_location("agent_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        yield connection, module


def test_agent_migration_preserves_old_namespaces_and_progress(old_agent_schema):
    connection, migration = old_agent_schema
    migration.upgrade()
    for table in ("source_record", "memory_task", "conversation_session"):
        row = connection.execute(text(f"SELECT * FROM {table}")).mappings().one()
        assert row["agent_id"] == row["caller_agent_id"] == "agent"
    assert connection.scalar(text("SELECT next_batch_seq FROM conversation_session")) == 8
    assert (
        connection.scalar(text("SELECT payload->'execution'->>'next_slice' FROM memory_task"))
        == "1"
    )
    migration.downgrade()
    assert connection.scalar(text("SELECT external_ref_id FROM source_record")) == "stable-ref"


def test_agent_downgrade_refuses_to_drop_explicit_identity(old_agent_schema):
    connection, migration = old_agent_schema
    migration.upgrade()
    connection.execute(text("UPDATE conversation_session SET agent_id = 'business_agent'"))
    with pytest.raises(DBAPIError, match="explicit source Agent identities would be lost"):
        with connection.begin_nested():
            migration.downgrade()
    assert connection.scalar(text("SELECT agent_id FROM conversation_session")) == "business_agent"
