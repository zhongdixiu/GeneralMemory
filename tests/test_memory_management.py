from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from test_add_api import _settings
from test_read_api import FakeEmbedder, _fact, _inference, _profile, _source

import memory_cmic.api as api_module
import memory_cmic.lifecycle as lifecycle_module
from memory_cmic.auth import StaticCredentialStore
from memory_cmic.governance_worker import _apply_task, run_once
from memory_cmic.lifecycle import (
    MemoryVersionConflict,
    delete_memory,
    govern_memory,
    update_or_invalidate_source,
)
from memory_cmic.models import (
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    SourceRecord,
)
from memory_cmic.profile import read_profiles
from memory_cmic.task_queue import enqueue_task


@pytest.fixture
def management_api(connection, monkeypatch):
    connection.execute(text("SET LOCAL enable_indexscan = off"))
    tenant = f"tenant_manage_{uuid4().hex}"
    credentials = StaticCredentialStore([
        {"token": "editor", "tenant_id": tenant, "caller_agent_id": "editor",
         "can_manage_memories": True},
        {"token": "reader", "tenant_id": tenant, "caller_agent_id": "reader",
         "allowed_source_systems": ["test"]},
        {"token": "governor", "tenant_id": tenant, "caller_agent_id": "governor",
         "can_manage_governance": True},
        {"token": "foreign-editor", "tenant_id": tenant + "_other",
         "caller_agent_id": "editor", "can_manage_memories": True},
    ])
    settings = replace(
        _settings(tenant), credentials=credentials, siliconflow_api_key="fake",
        siliconflow_embedding_model=FakeEmbedder.model_id,
    )
    monkeypatch.setattr(api_module, "SiliconFlowEmbedder", lambda **kwargs: FakeEmbedder())
    with connection.begin_nested(), TestClient(
        api_module.create_app(settings, engine=connection)
    ) as client:
        yield client, tenant


def _headers(token="editor"):
    return {"Authorization": f"Bearer {token}"}


def _search(client, **filters):
    response = client.post(
        "/api/v1/memories:search", headers=_headers("reader"),
        json={"user_id": "user_1", "query": "用户的要求", **filters},
    )
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["results"]]


def _audits(session, tenant, action, target_id=None):
    return session.scalars(select(MemoryAuditLog).where(
        MemoryAuditLog.tenant_id == tenant, MemoryAuditLog.action == action,
        MemoryAuditLog.target_type == "memory",
        True if target_id is None else MemoryAuditLog.target_id == target_id,
    )).all()


@pytest.mark.parametrize("status", ["active", "disputed", "invalidated", "expired"])
def test_delete_keeps_history_and_is_idempotent(management_api, session, status):
    client, tenant = management_api
    memory = _fact(session, tenant, "用户要求周报先列结论")
    memory.status = status
    source = _source(session, memory)
    session.flush()
    memory_id, source_id, content = memory.id, source.id, memory.content
    path = f"/api/v1/memories/{memory_id}?user_id=user_1&expected_version=1"
    first = client.delete(path, headers=_headers())
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "deleted" and first.json()["version"] == 2
    session.expire_all()
    memory = session.get(MemoryItem, memory_id)
    assert memory.status == "deleted" and memory.content == content
    assert session.get(SourceRecord, source_id).status == "active"
    audit = _audits(session, tenant, "DELETE", memory_id)
    assert len(audit) == 1 and audit[0].operator_id == "editor"
    assert audit[0].state_before == {"status": status, "version": 1}
    assert audit[0].state_after == {"status": "deleted", "version": 2}
    assert memory_id not in _search(client)
    assert client.delete(path, headers=_headers()).json() == first.json()
    session.expire_all()
    assert len(_audits(session, tenant, "DELETE", memory_id)) == 1
    assert session.scalar(select(func.count()).select_from(MemoryTask).where(
        MemoryTask.tenant_id == tenant, MemoryTask.task_type == "vector_delete",
    )) == 1


def test_delete_propagates_without_removing_independent_basis(management_api, session):
    client, tenant = management_api
    first = _fact(session, tenant, "用户要求周报先列结论")
    second = _fact(session, tenant, "用户要求复盘先列结论")
    dependent = _inference(session, tenant, [first, second])
    independent = _inference(session, tenant, [first])
    session.add(MemoryEvidence(
        id=uuid4().hex, tenant_id=tenant, relationship_type="derives",
        evidence_group_id=uuid4().hex, upstream_memory_id=second.id,
        upstream_version=second.version, downstream_memory_id=independent.id,
    ))
    profile = _profile(session, first)
    session.flush()
    ids = first.id, dependent.id, independent.id, profile.id
    response = client.delete(f"/api/v1/memories/{first.id}?user_id=user_1", headers=_headers())
    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(MemoryItem, ids[1]).status == "invalidated"
    assert session.get(MemoryItem, ids[2]).status == "active"
    assert read_profiles(session, tenant_id=tenant, user_id="user_1") == []
    assert set(_search(client)) == {second.id, ids[2]}
    response = client.delete(f"/api/v1/memories/{ids[2]}?user_id=user_1", headers=_headers())
    assert response.status_code == 200 and response.json()["status"] == "deleted"


@pytest.mark.parametrize("token,status", [(None, 401), ("reader", 403), ("governor", 403),
                                         ("foreign-editor", 404)])
@pytest.mark.parametrize("method", ["delete", "put"])
def test_management_requires_independent_permission(management_api, session, token, status, method):
    client, tenant = management_api
    memory = _fact(session, tenant, "用户住在北京")
    headers = _headers(token) if token else {}
    path = f"/api/v1/memories/{memory.id}"
    response = (client.delete(path, params={"user_id": "user_1"}, headers=headers)
                if method == "delete" else client.put(
                    path, json={"user_id": "user_1", "content": "用户住在上海"}, headers=headers,
                ))
    assert response.status_code == status, response.text
    session.refresh(memory)
    assert memory.status == "active" and memory.version == 1
    assert _audits(session, tenant, "DELETE") == _audits(session, tenant, "UPDATE") == []


@pytest.mark.parametrize("method", ["delete", "put"])
def test_management_checks_user_subject_and_version(management_api, session, method):
    client, tenant = management_api
    memory = _fact(session, tenant, "用户住在北京")
    path = f"/api/v1/memories/{memory.id}"

    def call(user="user_1", **values):
        if method == "delete":
            return client.delete(path, params={"user_id": user, **values}, headers=_headers())
        return client.put(path, json={"user_id": user, "content": "用户住在上海", **values},
                          headers=_headers())

    assert call("other-user").status_code == 404
    assert call(expected_version=2).status_code == 409
    assert call(" ").status_code == 422
    memory.subject_type = "agent"
    session.flush()
    assert call().status_code == 404


def test_update_creates_new_evidence_and_invalidates_old_dependents(management_api, session):
    client, tenant = management_api
    old = _fact(session, tenant, "用户要求周报先列结论", agent="agent_a", domains=["report"])
    old.metadata_json = {"retention_kind": "long_term", "temporal_kind": "current"}
    old.expired_at = datetime.now(UTC) + timedelta(days=90)
    old_id, old_time, old_expiry = old.id, old.effective_at, old.expired_at
    old_source_id = _source(session, old).id
    inference = _inference(session, tenant, [old])
    inference_id = inference.id
    _profile(session, old)
    session.flush()
    response = client.put(
        f"/api/v1/memories/{old_id}", headers=_headers(),
        json={"user_id": "user_1", "content": "用户要求周报先提供风险说明",
              "expected_version": 1, "reason": "用户修正要求"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["id"] != old_id and result["supersedes_id"] == old_id
    session.expire_all()
    new = session.get(MemoryItem, result["id"])
    old = session.get(MemoryItem, old_id)
    assert old.status == "invalidated" and old.content == "用户要求周报先列结论"
    assert new.content == "用户要求周报先提供风险说明" and new.status == "active"
    assert new.business_domains == ["report"] and new.expired_at == old_expiry
    assert new.effective_at == old_time
    assert session.get(MemoryItem, inference_id).status == "invalidated"
    assert read_profiles(session, tenant_id=tenant, user_id="user_1") == []
    assert session.get(SourceRecord, old_source_id).status == "active"
    source = _source(session, new)
    assert source.source_type == "manual" and source.author_type == "agent"
    assert source.raw_content == new.content and source.caller_agent_id == "editor"
    assert source.id != old_source_id
    history = _audits(session, tenant, "UPDATE", old_id)
    assert len(history) == 1 and history[0].state_before["content"] == old.content
    assert history[0].state_after["content"] == new.content
    assert history[0].context["replacement_id"] == new.id
    assert _search(client) == []  # The replacement waits for its vector; old values stay hidden.
    # This fixture holds one outer transaction: CURRENT_TIMESTAMP predates API enqueue times.
    session.execute(update(MemoryTask).where(
        MemoryTask.tenant_id == tenant, MemoryTask.status == "pending",
        MemoryTask.task_type != "profile_rebuild",
    ).values(available_at=func.current_timestamp()))
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    for _ in range(10):
        if not run_once(factory, worker_id="management-test", embedder=FakeEmbedder()):
            break
    session.expire_all()
    assert _search(client) == [new.id]
    assert _search(client, agent_id="editor", source_system="memory_management") == [new.id]
    assert _search(client, agent_id="agent_a") == []
    assert session.scalar(select(MemoryEmbedding).where(
        MemoryEmbedding.memory_id == old_id,
    )) is None


@pytest.mark.parametrize("state", ["deleted", "invalidated", "disputed", "expired",
                                   "ttl_elapsed", "future", "inference"])
def test_update_cannot_restore_or_convert_ineligible_memories(management_api, session, state):
    client, tenant = management_api
    memory = _fact(session, tenant, "用户要求周报先列结论")
    if state == "ttl_elapsed":
        memory.expired_at = datetime.now(UTC) - timedelta(seconds=1)
    elif state == "future":
        memory.effective_at = datetime.now(UTC) + timedelta(days=1)
    elif state == "inference":
        memory.cognitive_type = "inference"
    else:
        memory.status = state
    session.flush()
    response = client.put(
        f"/api/v1/memories/{memory.id}", headers=_headers(),
        json={"user_id": "user_1", "content": "用户要求周报先提供风险说明"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "MEMORY_NOT_EDITABLE"
    assert _audits(session, tenant, "UPDATE") == []


def test_unchanged_update_is_noop_and_invalid_body_is_rejected(management_api, session):
    client, tenant = management_api
    memory = _fact(session, tenant, "用户住在北京")
    path = f"/api/v1/memories/{memory.id}"
    response = client.put(path, headers=_headers(),
                          json={"user_id": "user_1", "content": memory.content})
    assert response.status_code == 200 and response.json()["id"] == memory.id
    assert _audits(session, tenant, "UPDATE") == []
    for extra in ({"content": " "}, {"content": "x" * 8001}, {"expected_version": 0},
                  {"expected_version": True}, {"status": "active"}, {"tenant_id": "other"}):
        response = client.put(path, headers=_headers(),
                              json={"user_id": "user_1", "content": "用户住在上海", **extra})
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("method", ["delete", "put"])
def test_failed_enqueue_rolls_back_mutation_and_audit(management_api, session, monkeypatch, method):
    client, tenant = management_api
    memory = _fact(session, tenant, "用户住在北京")
    memory_id = memory.id

    def unavailable(*args, **kwargs):
        raise RuntimeError("simulated queue write failure")

    monkeypatch.setattr(lifecycle_module, "enqueue_task", unavailable)
    with pytest.raises(RuntimeError, match="queue write failure"):
        if method == "delete":
            client.delete(f"/api/v1/memories/{memory_id}?user_id=user_1", headers=_headers())
        else:
            client.put(f"/api/v1/memories/{memory_id}", headers=_headers(),
                       json={"user_id": "user_1", "content": "用户住在上海"})
    session.expire_all()
    memory = session.get(MemoryItem, memory_id)
    assert memory.status == "active" and memory.version == 1 and memory.content == "用户住在北京"
    assert _audits(session, tenant, "DELETE") == _audits(session, tenant, "UPDATE") == []
    assert session.scalar(select(func.count()).select_from(SourceRecord).where(
        SourceRecord.tenant_id == tenant,
    )) == 1


def test_deleted_memory_is_terminal_and_late_cleanup_still_works(management_api, session):
    _, tenant = management_api
    memory = _fact(session, tenant, "用户住在北京")
    source = _source(session, memory)
    delete_memory(session, tenant_id=tenant, user_id="user_1", memory_id=memory.id,
                  operator_id="editor")
    with pytest.raises(ValueError, match="deleted memory"):
        govern_memory(session, memory=memory, status="invalidated", reason_code="test",
                      correlation_id=None)
    update_or_invalidate_source(
        session, tenant_id=tenant, source_id=source.id, status="invalidated",
    )
    assert memory.status == "deleted" and memory.version == 2
    cleanup = session.scalar(select(MemoryTask).where(
        MemoryTask.tenant_id == tenant, MemoryTask.task_type == "vector_delete",
        MemoryTask.target_id == memory.id,
    ))
    _apply_task(session, cleanup)
    session.flush()
    assert session.scalar(select(MemoryEmbedding).where(
        MemoryEmbedding.memory_id == memory.id,
    )) is None


def test_delete_refreshes_stale_session_and_rejects_old_version(management_api, session):
    _, tenant = management_api
    memory = _fact(session, tenant, "用户住在北京")
    session.execute(text("UPDATE memory_item SET version=2 WHERE id=:id"), {"id": memory.id})
    assert memory.version == 1
    with pytest.raises(MemoryVersionConflict):
        delete_memory(session, tenant_id=tenant, user_id="user_1", memory_id=memory.id,
                      operator_id="editor", expected_version=1)
    assert memory.status == "active" and memory.version == 2


def test_late_vector_upsert_cannot_restore_deleted_memory(management_api, session):
    _, tenant = management_api
    memory = _fact(session, tenant, "用户住在北京")
    task = enqueue_task(session, {
        "id": uuid4().hex, "tenant_id": tenant, "task_type": "vector_upsert",
        "target_type": "memory", "target_id": memory.id, "input_version": 1,
        "idempotency_key": uuid4().hex, "payload": {"model_id": FakeEmbedder.model_id},
    })
    task_id, memory_id = task.id, memory.id
    delete_memory(session, tenant_id=tenant, user_id="user_1", memory_id=memory_id,
                  operator_id="editor")
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    for _ in range(10):
        if not run_once(factory, worker_id="late-vector-test", embedder=FakeEmbedder()):
            break
    session.expire_all()
    assert session.get(MemoryTask, task_id).status == "cancelled"
    assert session.get(MemoryItem, memory_id).status == "deleted"


def test_memory_management_permission_is_explicit_and_strict():
    for value in ("true", 1, None):
        with pytest.raises(ValueError, match="can_manage_memories"):
            StaticCredentialStore([{"token": "t", "tenant_id": "t", "caller_agent_id": "a",
                                    "can_manage_memories": value}])


def test_deleted_status_migration_preserves_rows_and_guards_downgrade(connection):
    migration = run_path(Path(__file__).parents[1] / "migrations/versions/0011_memory_deleted.py")
    schema = f"delete_migration_{uuid4().hex}"
    connection.execute(text(f"CREATE SCHEMA {schema}"))
    connection.execute(text(f"SET LOCAL search_path TO {schema}, public"))
    connection.execute(text("CREATE TABLE memory_item (id TEXT PRIMARY KEY, content TEXT, "
                            "status TEXT, CONSTRAINT ck_memory_status CHECK "
                            "(status IN ('active', 'invalidated', 'disputed', 'expired')))"))
    connection.execute(text("INSERT INTO memory_item VALUES ('m', '原文', 'active')"))
    with Operations.context(MigrationContext.configure(connection)):
        migration["upgrade"]()
        connection.execute(text("UPDATE memory_item SET status='deleted' WHERE id='m'"))
        with pytest.raises(IntegrityError), connection.begin_nested():
            migration["downgrade"]()
        assert connection.execute(text("SELECT content,status FROM memory_item")).one() == (
            "原文", "deleted",
        )
