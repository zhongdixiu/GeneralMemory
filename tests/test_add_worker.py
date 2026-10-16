from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.api import create_app
from memory_cmic.auth import StaticCredentialStore
from memory_cmic.models import (
    BusinessScene,
    ConversationSession,
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)
from memory_cmic.providers import ExtractedFact, GovernanceDecision
from memory_cmic.settings import Settings
from memory_cmic.worker import run_once


@dataclass
class FakeFactModel:
    duplicate_semantic_candidates: bool = False
    duplicate_checks: list[list[dict]] = field(default_factory=list)

    def extract(self, *, targets: list[dict], history: list[dict]) -> list[ExtractedFact]:
        user_target = next(message for message in targets if message["role"] == "user")
        return [
            ExtractedFact(
                memory=user_target["content"], message_ids=[user_target["message_id"]]
            )
        ]

    def find_duplicate(self, *, fact: str, candidates: list[dict]) -> str | None:
        self.duplicate_checks.append(candidates)
        return candidates[0]["id"] if self.duplicate_semantic_candidates else None


    def resolve(self, *, fact, candidates, targets):
        duplicate = self.find_duplicate(fact=fact.memory, candidates=candidates)
        return GovernanceDecision(action="DUPLICATE" if duplicate else "ADD",
                                  memory_ids=[duplicate] if duplicate else [])


class FakeEmbedder:
    model_id = "BAAI/bge-m3"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vector = [0.0] * 1024
        vector[0] = 1.0
        return [list(vector) for _ in texts]


def _settings(tenant_id: str) -> Settings:
    return Settings(
        database_url="unused",
        credentials=StaticCredentialStore(
            [
                {
                    "token": "worker-token",
                    "tenant_id": tenant_id,
                    "caller_agent_id": "agent_worker",
                    "allowed_source_systems": ["email_agent"],
                }
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


@pytest.fixture
def worker_context(engine: Engine) -> Iterator[tuple[TestClient, str]]:
    tenant_id = f"tenant_worker_{uuid4().hex}"
    with TestClient(create_app(_settings(tenant_id), engine)) as client:
        yield client, tenant_id
    with engine.begin() as connection:
        connection.execute(delete(BusinessScene).where(BusinessScene.tenant_id == tenant_id))
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


def _add(
    client: TestClient, *, key: str, message_id: str, content: str,
    occurred_at: str = "2026-09-24T09:00:00+08:00",
) -> str:
    response = client.post(
        "/api/v1/memories:add",
        headers={
            "Authorization": "Bearer worker-token",
            "Idempotency-Key": key,
        },
        json={
            "source_system": "email_agent",
            "user_id": "user_worker",
            "session_id": "session_worker",
            "messages": [
                {
                    "message_id": message_id,
                    "role": "user",
                    "content": content,
                    "occurred_at": occurred_at,
                }
            ],
        },
    )
    assert response.status_code == 202
    return response.json()["task_id"]


def _run(engine: Engine, model: FakeFactModel) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if run_once(
            sessionmaker(engine, expire_on_commit=False),
            worker_id=f"worker_{uuid4().hex}",
            fact_model=model,
            embedder=FakeEmbedder(),
            duplicate_threshold=0.85,
        ):
            return
        sleep(0.02)
    pytest.fail("fact task did not become available within 5 seconds")


def test_worker_creates_fact_evidence_embedding_and_public_result(
    worker_context: tuple[TestClient, str], engine: Engine
) -> None:
    client, tenant_id = worker_context
    task_id = _add(
        client,
        key="worker-new",
        message_id="msg_new",
        content="用户偏好使用项目符号写周报。",
    )
    _run(engine, FakeFactModel())

    response = client.get(
        f"/api/v1/tasks/{task_id}", headers={"Authorization": "Bearer worker-token"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["results"][0]["event"] == "ADD"

    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MemoryEvidence).where(
                MemoryEvidence.tenant_id == tenant_id
            )
        ) == 1
        embedding = session.scalars(
            select(MemoryEmbedding).where(MemoryEmbedding.tenant_id == tenant_id)
        ).one()
        assert len(embedding.embedding) == 1024
        events = session.scalars(select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == tenant_id, MemoryAuditLog.target_type == "memory"
        ).order_by(MemoryAuditLog.id)).all()
        assert [event.reason_code for event in events] == ["FACT_EXTRACTED"]
        assert events[0].task_id == task_id
        assert events[0].state_after["version"] == 1
        assert events[0].context["message_ids"] == ["msg_new"]


def test_worker_exact_duplicate_only_adds_evidence(
    worker_context: tuple[TestClient, str], engine: Engine
) -> None:
    client, tenant_id = worker_context
    content = "用户偏好使用项目符号写周报。"
    _add(client, key="exact-1", message_id="msg_exact_1", content=content)
    _run(engine, FakeFactModel())
    second_task = _add(client, key="exact-2", message_id="msg_exact_2", content=content)
    _run(engine, FakeFactModel())

    response = client.get(
        f"/api/v1/tasks/{second_task}", headers={"Authorization": "Bearer worker-token"}
    )
    assert response.json()["results"] == []
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MemoryEvidence).where(
                MemoryEvidence.tenant_id == tenant_id
            )
        ) == 2
        events = session.scalars(select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == tenant_id, MemoryAuditLog.target_type == "memory"
        ).order_by(MemoryAuditLog.id)).all()
        assert [event.reason_code for event in events] == ["FACT_EXTRACTED", "FACT_EVIDENCE_ADDED"]
        assert events[1].context["message_ids"] == ["msg_exact_2"]


def test_high_similarity_is_classified_instead_of_hard_dropped(
    worker_context: tuple[TestClient, str], engine: Engine
) -> None:
    client, tenant_id = worker_context
    _add(client, key="semantic-1", message_id="msg_semantic_1", content="用户现在住在北京。")
    _run(engine, FakeFactModel())
    _add(client, key="semantic-2", message_id="msg_semantic_2", content="用户现在住在上海。")
    classifier = FakeFactModel(duplicate_semantic_candidates=False)
    _run(engine, classifier)

    assert classifier.duplicate_checks
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id
            )
        ) == 2
def test_fact_worker_leaves_high_priority_cleanup_pending(worker_context, engine):
    client, tenant_id = worker_context
    task_id = _add(client, key="mixed", message_id="mixed", content="我喜欢简洁邮件。")
    with Session(engine) as session:
        session.add(
            MemoryTask(
                id=f"cleanup_{tenant_id}",
                tenant_id=tenant_id,
                task_type="vector_delete",
                target_type="memory",
                target_id="unused",
                idempotency_key="cleanup",
                priority=100,
            )
        )
        session.commit()
    _run(engine, FakeFactModel())
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "succeeded"
        cleanup = session.get(MemoryTask, f"cleanup_{tenant_id}")
        assert cleanup.status == "pending"
        assert cleanup.worker_id is None


class ChangingFactModel(FakeFactModel):
    def __init__(self, change):
        super().__init__()
        self.change = change

    def extract(self, *, targets, history):
        self.change()
        return super().extract(targets=targets, history=history)


@pytest.mark.parametrize("change", ["status", "version", "payload", "association"])
def test_input_changes_during_model_call_cannot_commit(worker_context, engine, change):
    client, tenant_id = worker_context
    task_id = _add(client, key="stale", message_id="stale", content="我喜欢简洁邮件。")

    def mutate():
        with Session(engine) as session:
            if change in {"status", "version"}:
                source = session.scalars(
                    select(SourceRecord).where(
                        SourceRecord.tenant_id == tenant_id,
                    )
                ).one()
                if change == "status":
                    source.status = "deleted"
                else:
                    source.version += 1
            elif change == "payload":
                session.get(MemoryTask, task_id).payload = {"config_version": 2}
            else:
                association = session.scalars(
                    select(MemoryTaskSource).where(
                        MemoryTaskSource.task_id == task_id,
                    )
                ).one()
                association.position += 1
            session.commit()

    _run(engine, ChangingFactModel(mutate))
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "failed"
        assert session.get(MemoryTask, task_id).error_json["code"] == "INPUT_PROCESSING_FAILED"
        assert not session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id,
            )
        ).all()
        source = session.scalars(
            select(SourceRecord).where(
                SourceRecord.tenant_id == tenant_id,
            )
        ).one()
        assert source.processed_version is None


@pytest.mark.parametrize("role", ["assistant", "system", "tool"])
def test_worker_rejects_non_user_only_evidence(worker_context, engine, role):
    client, tenant_id = worker_context
    task_id = _add(client, key="role", message_id="role", content="我喜欢简洁邮件。")
    with Session(engine) as session:
        source = session.scalars(
            select(SourceRecord).where(
                SourceRecord.tenant_id == tenant_id,
            )
        ).one()
        source.metadata_json = {**source.metadata_json, "role": role}
        session.commit()

    class NonUserFactModel(FakeFactModel):
        def extract(self, *, targets, history):
            return [ExtractedFact(memory="用户喜欢简洁邮件。", message_ids=["role"])]

    _run(engine, NonUserFactModel())
    with Session(engine) as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 1
        assert not session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id,
            )
        ).all()


@pytest.mark.parametrize("change", ["version", "status", "content", "expired"])
def test_changed_duplicate_candidate_retries_without_evidence(worker_context, engine, change):
    from datetime import UTC, datetime, timedelta

    client, tenant_id = worker_context
    _add(client, key="candidate-1", message_id="candidate-1", content="我喜欢简洁邮件。")
    _run(engine, FakeFactModel())
    task_id = _add(client, key="candidate-2", message_id="candidate-2", content="请简洁回复邮件。")

    class ChangedDuplicateModel(FakeFactModel):
        def find_duplicate(self, *, fact, candidates):
            with Session(engine) as session:
                memory = session.get(MemoryItem, candidates[0]["id"])
                if change == "version":
                    memory.version += 1
                elif change == "status":
                    memory.status = "invalidated"
                elif change == "content":
                    memory.content = "我需要详细邮件。"
                else:
                    memory.expired_at = datetime.now(UTC) - timedelta(seconds=1)
                session.commit()
            return candidates[0]["id"]

    _run(engine, ChangedDuplicateModel())
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "pending"
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryEvidence)
                .where(
                    MemoryEvidence.tenant_id == tenant_id,
                )
            )
            == 1
        )


def test_same_worker_takeover_blocks_old_result(worker_context, engine):
    from datetime import UTC, datetime, timedelta

    from memory_cmic.task_queue import claim_tasks

    client, tenant_id = worker_context
    task_id = _add(client, key="takeover", message_id="takeover", content="我喜欢简洁邮件。")

    def takeover():
        with Session(engine) as session:
            task = session.get(MemoryTask, task_id)
            old_token = task.lease_token
            task.locked_until = datetime.now(UTC) - timedelta(seconds=1)
            session.flush()
            claimed = claim_tasks(
                session,
                tenant_id=tenant_id,
                worker_id=task.worker_id,
                task_types=("fact_extract",),
            )
            assert claimed[0].lease_token != old_token
            session.commit()

    _run(engine, ChangingFactModel(takeover))
    with Session(engine) as session:
        assert session.get(MemoryTask, task_id).status == "processing"
        assert not session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id,
            )
        ).all()


def test_long_model_call_renews_lease_and_completes(worker_context, engine):
    import time

    from memory_cmic.task_queue import claim_tasks
    from memory_cmic.worker import process_task

    client, tenant_id = worker_context
    task_id = _add(client, key="long", message_id="long", content="我喜欢简洁邮件。")
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        task = claim_tasks(
            session,
            tenant_id=tenant_id,
            worker_id="long_worker",
            lease_seconds=1,
            task_types=("fact_extract",),
        )[0]
        token = task.lease_token
        original_until = task.locked_until

    def wait_for_renewal():
        time.sleep(1.5)
        with factory() as session:
            assert session.get(MemoryTask, task_id).locked_until >= original_until
            assert claim_tasks(session, tenant_id=tenant_id, worker_id="takeover") == []

    process_task(
        factory,
        task_id=task_id,
        tenant_id=tenant_id,
        worker_id="long_worker",
        lease_token=token,
        fact_model=ChangingFactModel(wait_for_renewal),
        embedder=FakeEmbedder(),
        duplicate_threshold=0.85,
        lease_seconds=1,
    )
    with factory() as session:
        assert session.get(MemoryTask, task_id).status == "succeeded"


@pytest.mark.parametrize("status", ["failed", "partial", "cancelled"])
def test_terminal_prior_batch_does_not_block_following(worker_context, engine, status):
    client, tenant_id = worker_context
    first = _add(client, key="prior-1", message_id="prior-1", content="我喜欢简洁邮件。")
    second = _add(client, key="prior-2", message_id="prior-2", content="我喜欢项目符号。")
    with Session(engine) as session:
        session.get(MemoryTask, first).status = status
        session.commit()

    class CheckingModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert any(m["message_id"] == "prior-1" for m in history)
            return super().extract(targets=targets, history=history)

    _run(engine, CheckingModel())
    with Session(engine) as session:
        assert session.get(MemoryTask, first).status == status
        assert session.get(MemoryTask, second).status == "succeeded"
        assert session.get(MemoryTask, second).error_json is None


def test_expired_lease_during_commit_rolls_back_all_results(worker_context, engine, monkeypatch):
    from memory_cmic import worker
    from memory_cmic.task_queue import claim_tasks

    client, tenant_id = worker_context
    task_id = _add(
        client, key="commit-expiry", message_id="commit-expiry", content="我喜欢简洁邮件。"
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        task = claim_tasks(
            session,
            tenant_id=tenant_id,
            worker_id="slow_commit",
            lease_seconds=1,
            task_types=("fact_extract",),
        )[0]
        token = task.lease_token
    original_commit = worker._commit_results

    def slow_commit(*args, **kwargs):
        results = original_commit(*args, **kwargs)
        # Expire after the transaction began, while this transaction holds the task lock.
        # A CURRENT_TIMESTAMP fence would incorrectly allow this commit.
        args[0].execute(
            text(
                "UPDATE memory_task SET locked_until = "
                "CURRENT_TIMESTAMP + INTERVAL '1 microsecond' "
                "WHERE id = :task_id"
            ),
            {"task_id": task_id},
        )
        return results

    monkeypatch.setattr(worker, "_commit_results", slow_commit)
    with pytest.raises(worker.LeaseLostError):
        worker.process_task(
            factory,
            task_id=task_id,
            tenant_id=tenant_id,
            worker_id="slow_commit",
            lease_token=token,
            fact_model=FakeFactModel(),
            embedder=FakeEmbedder(),
            duplicate_threshold=0.85,
            lease_seconds=1,
        )
    with factory() as session:
        for model in (MemoryItem, MemoryEvidence, MemoryEmbedding):
            assert not session.scalars(select(model).where(model.tenant_id == tenant_id)).all()
        assert not session.scalars(
            select(MemoryAuditLog).where(
                MemoryAuditLog.tenant_id == tenant_id,
                MemoryAuditLog.reason_code == "FACT_EXTRACTED",
            )
        ).all()
        source = session.scalars(
            select(SourceRecord).where(
                SourceRecord.tenant_id == tenant_id,
            )
        ).one()
        assert source.processed_version is None


@pytest.mark.parametrize("changed", [False, True])
def test_exhausted_prior_lease_does_not_block_following(worker_context, engine, changed):
    from datetime import UTC, datetime, timedelta

    client, tenant_id = worker_context
    first = _add(client, key="exhausted", message_id="exhausted", content="我喜欢简洁邮件。")
    second = _add(client, key="following", message_id="following", content="我喜欢项目符号。")
    with Session(engine) as session:
        task = session.get(MemoryTask, first)
        task.status = "processing"
        task.worker_id = "stopped-worker"
        task.lease_token = "old-token"
        task.locked_until = datetime.now(UTC) - timedelta(minutes=1)
        task.attempt_count = task.max_attempts
        task.payload = {**task.payload, "execution": {"changed": changed}}
        session.commit()

    class ContextModel(FakeFactModel):
        def extract(self, *, targets, history):
            assert any(m["message_id"] == "exhausted" for m in history)
            return super().extract(targets=targets, history=history)

    _run(engine, ContextModel())
    with Session(engine) as session:
        prior = session.get(MemoryTask, first)
        assert prior.status == ("partial" if changed else "failed")
        assert prior.error_json["code"] == "LEASE_EXHAUSTED"
        assert session.get(MemoryTask, second).status == "succeeded"
