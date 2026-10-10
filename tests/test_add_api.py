from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from memory_cmic.api import create_app
from memory_cmic.auth import StaticCredentialStore
from memory_cmic.models import (
    ConversationSession,
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)
from memory_cmic.settings import Settings


def _settings(tenant_id: str) -> Settings:
    return Settings(
        database_url="unused",
        credentials=StaticCredentialStore(
            [
                {
                    "token": "test-token",
                    "tenant_id": tenant_id,
                    "caller_agent_id": "agent_email",
                    "allowed_source_systems": ["email_agent"],
                },
                {
                    "token": "other-token",
                    "tenant_id": tenant_id,
                    "caller_agent_id": "agent_other",
                    "allowed_source_systems": ["email_agent"],
                },
            ]
        ),
        dashscope_api_key=None,
        dashscope_base_url="https://example.invalid/v1",
        dashscope_model="qwen3.8-max",
        siliconflow_api_key=None,
        siliconflow_base_url="https://example.invalid/v1",
        siliconflow_embedding_model="BAAI/bge-m3",
        duplicate_candidate_threshold=0.85,
    )


def _body(*, message_id: str = "msg_1", content: str = "以后周报使用项目符号。") -> dict:
    return {
        "source_system": "email_agent",
        "user_id": "user_1",
        "session_id": "session_1",
        "messages": [
            {
                "message_id": message_id,
                "role": "user",
                "content": content,
                "occurred_at": "2026-09-24T09:00:00+08:00",
            }
        ],
    }


@pytest.fixture
def api_context(engine: Engine) -> Iterator[tuple[TestClient, str]]:
    tenant_id = f"tenant_add_{uuid4().hex}"
    app = create_app(_settings(tenant_id), engine)
    with TestClient(app) as client:
        yield client, tenant_id
    with engine.begin() as connection:
        connection.execute(delete(MemoryAuditLog).where(MemoryAuditLog.tenant_id == tenant_id))
        connection.execute(delete(MemoryEvidence).where(MemoryEvidence.tenant_id == tenant_id))
        connection.execute(delete(MemoryEmbedding).where(MemoryEmbedding.tenant_id == tenant_id))
        connection.execute(delete(MemoryTaskSource).where(MemoryTaskSource.tenant_id == tenant_id))
        connection.execute(delete(MemoryTask).where(MemoryTask.tenant_id == tenant_id))
        connection.execute(delete(MemoryItem).where(MemoryItem.tenant_id == tenant_id))
        connection.execute(delete(SourceRecord).where(SourceRecord.tenant_id == tenant_id))
        connection.execute(
            delete(ConversationSession).where(ConversationSession.tenant_id == tenant_id)
        )


def _headers(token: str = "test-token", key: str = "request-1") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def test_add_persists_source_task_and_replays_idempotently(
    api_context: tuple[TestClient, str], engine: Engine
) -> None:
    client, tenant_id = api_context
    first = client.post("/api/v1/memories:add", headers=_headers(), json=_body())
    assert first.status_code == 202
    assert first.json()["status"] == "pending"
    assert first.json()["results"] is None
    assert first.headers["x-request-id"]

    second = client.post("/api/v1/memories:add", headers=_headers(), json=_body())
    assert second.status_code == 202
    assert second.json()["task_id"] == first.json()["task_id"]

    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(SourceRecord).where(
                SourceRecord.tenant_id == tenant_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MemoryTask).where(
                MemoryTask.tenant_id == tenant_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MemoryTaskSource).where(
                MemoryTaskSource.tenant_id == tenant_id
            )
        ) == 1


def test_reused_request_id_does_not_merge_processing_chains(api_context, engine):
    client, tenant_id = api_context
    request_id = "same-prefix-" + "a" * 100
    first = client.post(
        "/api/v1/memories:add",
        json=_body(message_id="first"),
        headers={**_headers(key="first"), "X-Request-ID": request_id},
    )
    second = client.post(
        "/api/v1/memories:add",
        json=_body(message_id="second"),
        headers={**_headers(key="second"), "X-Request-ID": request_id},
    )
    replay = client.post(
        "/api/v1/memories:add",
        json=_body(message_id="first"),
        headers={**_headers(key="first"), "X-Request-ID": "different-request"},
    )
    assert first.status_code == second.status_code == replay.status_code == 202
    assert first.json()["task_id"] == replay.json()["task_id"]
    with Session(engine) as session:
        tasks = session.scalars(select(MemoryTask).where(MemoryTask.tenant_id == tenant_id)).all()
        assert len(tasks) == 2
        assert len({task.correlation_id for task in tasks}) == 2
        assert all(task.correlation_id != request_id[:64] for task in tasks)
        assert all(task.payload["request_id"] == request_id for task in tasks)
        events = session.scalars(
            select(MemoryAuditLog).where(MemoryAuditLog.tenant_id == tenant_id)
        ).all()
        assert len(events) == 4
        assert {event.task_id for event in events} == {task.id for task in tasks}


def test_add_rejects_idempotency_and_message_conflicts(
    api_context: tuple[TestClient, str]
) -> None:
    client, _ = api_context
    assert client.post(
        "/api/v1/memories:add", headers=_headers(), json=_body()
    ).status_code == 202

    idempotency_conflict = client.post(
        "/api/v1/memories:add",
        headers=_headers(),
        json=_body(content="不同内容"),
    )
    assert idempotency_conflict.status_code == 409
    assert idempotency_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    message_conflict = client.post(
        "/api/v1/memories:add",
        headers=_headers(key="request-2"),
        json=_body(content="不同内容"),
    )
    assert message_conflict.status_code == 409
    assert message_conflict.json()["error"]["code"] == "MESSAGE_ID_CONFLICT"


def test_add_enforces_bearer_source_and_task_owner(
    api_context: tuple[TestClient, str]
) -> None:
    client, _ = api_context
    assert client.post(
        "/api/v1/memories:add", headers={"Idempotency-Key": "request-1"}, json=_body()
    ).status_code == 401

    forbidden = _body()
    forbidden["source_system"] = "other_source"
    denied = client.post("/api/v1/memories:add", headers=_headers(), json=forbidden)
    assert denied.status_code == 403

    accepted = client.post("/api/v1/memories:add", headers=_headers(), json=_body())
    task_id = accepted.json()["task_id"]
    hidden = client.get(
        f"/api/v1/tasks/{task_id}", headers={"Authorization": "Bearer other-token"}
    )
    assert hidden.status_code == 404


def test_add_validates_standard_message_contract(api_context: tuple[TestClient, str]) -> None:
    client, _ = api_context
    invalid_tool = _body()
    invalid_tool["messages"][0]["role"] = "tool"
    response = client.post(
        "/api/v1/memories:add", headers=_headers(), json=invalid_tool
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_MESSAGE"

    explicit_null = _body()
    explicit_null["messages"][0]["author_id"] = None
    response = client.post(
        "/api/v1/memories:add", headers=_headers(), json=explicit_null
    )
    assert response.status_code == 422
