from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from memory_cmic.governance_worker import TASK_TYPES, enqueue_expired_memories, run_once
from memory_cmic.lifecycle import invalidate_memory, update_or_invalidate_source
from memory_cmic.models import (
    GovernanceOperation,
    GovernancePending,
    GovernancePolicy,
    GovernanceSubjectState,
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    SourceRecord,
)
from memory_cmic.repositories import add_evidence_group, create_memory, create_source
from memory_cmic.task_queue import enqueue_task, finish_exhausted_leases


class FakeEmbedder:
    model_id = "fake-embedding-v1"

    def __init__(self):
        self.calls = 0
        self.fail_next = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("temporary provider failure")
        return [[1.0, *([0.0] * 1023)] for _ in texts]


@pytest.fixture
def governance_context(engine: Engine):
    tenant_id = f"tenant_governance_{uuid4().hex}"
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory, tenant_id
    with engine.begin() as connection:
        for model in (
            MemoryAuditLog,
            GovernanceOperation,
            GovernancePending,
            GovernanceSubjectState,
            GovernancePolicy,
            MemoryEvidence,
            MemoryEmbedding,
            MemoryTask,
            MemoryItem,
            SourceRecord,
        ):
            connection.execute(delete(model).where(model.tenant_id == tenant_id))


def _memory(session, tenant_id: str, *, expired_at: datetime | None = None) -> MemoryItem:
    return create_memory(
        session,
        {
            "id": uuid4().hex,
            "tenant_id": tenant_id,
            "subject_type": "user",
            "subject_id": "user_governance",
            "cognitive_type": "fact",
            "summary": "测试事实",
            "content": "测试事实",
            "effective_at": datetime.now(UTC) - timedelta(days=1),
            "expired_at": expired_at,
            "created_by": "test",
        },
    )


def _task(
    session,
    tenant_id: str,
    task_type: str,
    target_id: str,
    version: int,
    *,
    model_id: str | None = None,
) -> MemoryTask:
    return enqueue_task(
        session,
        {
            "id": uuid4().hex,
            "tenant_id": tenant_id,
            "task_type": task_type,
            "target_type": "memory",
            "target_id": target_id,
            "input_version": version,
            "idempotency_key": uuid4().hex,
            "correlation_id": f"corr_{target_id}",
            "payload": {"model_id": model_id} if model_id else {},
            "priority": 100 if task_type == "vector_delete" else 50,
        },
    )


def _drain(factory, embedder: FakeEmbedder, *, limit: int = 10) -> None:
    deadline = monotonic() + 5
    handled = 0
    while monotonic() < deadline and handled < limit:
        if run_once(factory, worker_id="governance_test", embedder=embedder):
            handled += 1
            continue
        with factory() as session:
            if not session.scalar(select(MemoryTask.id).where(
                MemoryTask.task_type.in_(TASK_TYPES),
                MemoryTask.status.in_(["pending", "processing"]),
            ).limit(1)):
                return
        sleep(0.02)
    pytest.fail("governance queue did not drain")


def test_exhausted_processing_lease_reaches_terminal_status(governance_context):
    factory, tenant_id = governance_context
    with factory.begin() as session:
        task = enqueue_task(session, {
            "id": uuid4().hex, "tenant_id": tenant_id, "task_type": "vector_upsert",
            "target_type": "memory", "target_id": "unused", "input_version": 1,
            "idempotency_key": uuid4().hex, "status": "processing",
            "worker_id": "dead_worker", "attempt_count": 3, "max_attempts": 3,
            "locked_until": datetime.now(UTC) - timedelta(minutes=1),
        })
        task_id = task.id
    with factory.begin() as session:
        assert finish_exhausted_leases(session) >= 1
    with factory() as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "failed"
        assert task.error_json["code"] == "LEASE_EXHAUSTED"


def test_failed_ttl_task_is_rearmed_after_cooldown(governance_context):
    factory, tenant_id = governance_context
    with factory.begin() as session:
        _memory(session, tenant_id, expired_at=datetime.now(UTC) - timedelta(minutes=1))
        assert enqueue_expired_memories(session) == 1
        task = session.scalars(select(MemoryTask).where(
            MemoryTask.tenant_id == tenant_id, MemoryTask.task_type == "ttl_expire"
        )).one()
        task.status = "failed"
        task.attempt_count = task.max_attempts
        task.updated_at = datetime.now(UTC) - timedelta(hours=2)
        task_id = task.id
    with factory.begin() as session:
        assert enqueue_expired_memories(session) == 1
    with factory() as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "pending"
        assert task.attempt_count == 0


def test_vector_upsert_skips_same_content_and_cancels_old_version(governance_context):
    factory, tenant_id = governance_context
    embedder = FakeEmbedder()
    with factory.begin() as session:
        memory = _memory(session, tenant_id)
        memory_id = memory.id
        first = _task(session, tenant_id, "vector_upsert", memory_id, 1, model_id=embedder.model_id)
        first_id = first.id
    _drain(factory, embedder)
    assert embedder.calls == 1

    with factory.begin() as session:
        second = _task(
            session, tenant_id, "vector_upsert", memory_id, 1, model_id=embedder.model_id
        )
        second_id = second.id
    _drain(factory, embedder)
    assert embedder.calls == 1

    with factory.begin() as session:
        stale = _task(session, tenant_id, "vector_upsert", memory_id, 1, model_id=embedder.model_id)
        stale_id = stale.id
        memory = session.get(MemoryItem, memory_id)
        memory.content = "更新后的事实"
        memory.version += 1
    _drain(factory, embedder)
    assert embedder.calls == 1
    with factory.begin() as session:
        current = _task(
            session, tenant_id, "vector_upsert", memory_id, 2, model_id=embedder.model_id
        )
        current_id = current.id
    _drain(factory, embedder)
    assert embedder.calls == 2
    with factory() as session:
        assert session.get(MemoryTask, first_id).status == "succeeded"
        assert session.get(MemoryTask, second_id).status == "succeeded"
        assert session.get(MemoryTask, stale_id).status == "cancelled"
        assert session.get(MemoryTask, current_id).status == "succeeded"
        embedding = session.get(MemoryEmbedding, (tenant_id, memory_id, embedder.model_id))
        assert embedding.status == "active"
        assert embedding.content_hash != "0" * 64
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryAuditLog)
                .where(
                    MemoryAuditLog.tenant_id == tenant_id,
                    MemoryAuditLog.target_type == "embedding",
                    MemoryAuditLog.correlation_id == f"corr_{memory_id}",
                )
            )
            == 2
        )


def test_source_recheck_preserves_independent_evidence_then_cleans_vector(governance_context):
    factory, tenant_id = governance_context
    embedder = FakeEmbedder()
    with factory.begin() as session:
        memory = _memory(session, tenant_id)
        memory_id = memory.id
        source_ids = []
        for index in range(2):
            source = create_source(
                session,
                {
                    "id": uuid4().hex,
                    "tenant_id": tenant_id,
                    "source_system": "test",
                    "source_type": "chat",
                    "external_ref_id": f"source_{index}_{memory_id}",
                    "author_type": "user",
                    "author_id": "user_governance",
                    "raw_content": "测试事实",
                    "content_hash": "0" * 64,
                    "occurred_at": datetime.now(UTC),
                },
            )
            source_ids.append(source.id)
            add_evidence_group(
                session,
                [
                    {
                        "id": uuid4().hex,
                        "tenant_id": tenant_id,
                        "relationship_type": "supports",
                        "evidence_group_id": uuid4().hex,
                        "upstream_source_id": source.id,
                        "downstream_memory_id": memory_id,
                        "upstream_version": source.version,
                    }
                ],
            )
        _task(session, tenant_id, "vector_upsert", memory_id, 1, model_id=embedder.model_id)
    _drain(factory, embedder)

    with factory.begin() as session:
        update_or_invalidate_source(
            session,
            tenant_id=tenant_id,
            source_id=source_ids[0],
            status="deleted",
            correlation_id="corr_first_source",
        )
    _drain(factory, embedder)
    with factory() as session:
        assert session.get(MemoryItem, memory_id).status == "active"
        assert session.get(MemoryEmbedding, (tenant_id, memory_id, embedder.model_id)) is not None

    with factory.begin() as session:
        update_or_invalidate_source(
            session,
            tenant_id=tenant_id,
            source_id=source_ids[1],
            status="deleted",
            correlation_id="corr_second_source",
        )
        assert session.get(MemoryItem, memory_id).status == "invalidated"
    _drain(factory, embedder)
    with factory() as session:
        assert session.get(MemoryEmbedding, (tenant_id, memory_id, embedder.model_id)) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryTask)
                .where(
                    MemoryTask.tenant_id == tenant_id,
                    MemoryTask.correlation_id == "corr_second_source",
                    MemoryTask.status == "succeeded",
                )
            )
            == 3
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryAuditLog)
                .where(
                    MemoryAuditLog.tenant_id == tenant_id,
                    MemoryAuditLog.correlation_id == "corr_second_source",
                    MemoryAuditLog.reason_code == "VECTOR_DELETE",
                )
            )
            == 1
        )


def test_ttl_scan_and_duplicate_task_are_idempotent(governance_context):
    factory, tenant_id = governance_context
    embedder = FakeEmbedder()
    with factory.begin() as session:
        memory = _memory(session, tenant_id, expired_at=datetime.now(UTC) - timedelta(seconds=1))
        memory_id = memory.id
        session.add(
            MemoryEmbedding(
                tenant_id=tenant_id,
                memory_id=memory_id,
                model_id=embedder.model_id,
                content_hash="0" * 64,
                embedding=embedder.embed(["test"])[0],
            )
        )
    _drain(factory, embedder)
    with factory.begin() as session:
        task = _task(session, tenant_id, "ttl_expire", memory_id, 1)
        duplicate_id = task.id
    _drain(factory, embedder)
    with factory() as session:
        assert session.get(MemoryItem, memory_id).status == "expired"
        assert session.get(MemoryEmbedding, (tenant_id, memory_id, embedder.model_id)) is None
        assert session.get(MemoryTask, duplicate_id).status == "cancelled"
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryAuditLog)
                .where(
                    MemoryAuditLog.tenant_id == tenant_id,
                    MemoryAuditLog.target_id == memory_id,
                    MemoryAuditLog.action == "EXPIRE",
                )
            )
            == 1
        )


def test_ttl_scan_skips_already_queued_memory_when_limited(governance_context):
    factory, tenant_id = governance_context
    with factory.begin() as session:
        first = _memory(session, tenant_id, expired_at=datetime.now(UTC) - timedelta(days=2))
        second = _memory(session, tenant_id, expired_at=datetime.now(UTC) - timedelta(days=1))
        assert enqueue_expired_memories(session, limit=1) == 1
        assert enqueue_expired_memories(session, limit=1) == 1
        queued = set(
            session.scalars(
                select(MemoryTask.target_id).where(
                    MemoryTask.tenant_id == tenant_id,
                    MemoryTask.task_type == "ttl_expire",
                )
            )
        )
        assert queued == {first.id, second.id}


def test_late_vector_delete_cannot_remove_current_embedding(governance_context):
    factory, tenant_id = governance_context
    embedder = FakeEmbedder()
    with factory.begin() as session:
        memory = _memory(session, tenant_id)
        memory_id = memory.id
        stale = _task(session, tenant_id, "vector_delete", memory_id, 1, model_id=embedder.model_id)
        stale_id = stale.id
        memory.version = 2
        session.add(
            MemoryEmbedding(
                tenant_id=tenant_id,
                memory_id=memory_id,
                model_id=embedder.model_id,
                content_hash="0" * 64,
                embedding=embedder.embed(["test"])[0],
            )
        )
    _drain(factory, embedder)
    with factory() as session:
        assert session.get(MemoryTask, stale_id).status == "cancelled"
        assert session.get(MemoryEmbedding, (tenant_id, memory_id, embedder.model_id)) is not None


def test_vector_delete_cleans_same_stale_vector_after_inactive_version_advances(
    governance_context,
):
    factory, tenant_id = governance_context
    embedder = FakeEmbedder()
    with factory.begin() as session:
        memory = _memory(session, tenant_id)
        memory_id = memory.id
        session.add(
            MemoryEmbedding(
                tenant_id=tenant_id,
                memory_id=memory_id,
                model_id=embedder.model_id,
                content_hash="0" * 64,
                embedding=embedder.embed(["test"])[0],
            )
        )
        session.flush()
        invalidate_memory(
            session,
            tenant_id=tenant_id,
            memory_id=memory_id,
            correlation_id="corr_inactive_version",
        )
        memory.version += 1
    _drain(factory, embedder)
    with factory() as session:
        assert session.get(MemoryEmbedding, (tenant_id, memory_id, embedder.model_id)) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryTask)
                .where(
                    MemoryTask.tenant_id == tenant_id,
                    MemoryTask.task_type == "vector_delete",
                    MemoryTask.status == "succeeded",
                )
            )
            == 1
        )


def test_dependency_recheck_does_not_restore_invalidated_memory(governance_context):
    factory, tenant_id = governance_context
    embedder = FakeEmbedder()
    with factory.begin() as session:
        memory = _memory(session, tenant_id)
        memory.status = "invalidated"
        source = create_source(
            session,
            {
                "id": uuid4().hex,
                "tenant_id": tenant_id,
                "source_system": "test",
                "source_type": "chat",
                "external_ref_id": uuid4().hex,
                "author_type": "user",
                "author_id": "user_governance",
                "raw_content": "测试事实",
                "content_hash": "0" * 64,
                "occurred_at": datetime.now(UTC),
            },
        )
        add_evidence_group(
            session,
            [
                {
                    "id": uuid4().hex,
                    "tenant_id": tenant_id,
                    "relationship_type": "supports",
                    "evidence_group_id": uuid4().hex,
                    "upstream_source_id": source.id,
                    "downstream_memory_id": memory.id,
                    "upstream_version": source.version,
                }
            ],
        )
        task = _task(session, tenant_id, "dependency_recheck", memory.id, memory.version)
        task_id, memory_id = task.id, memory.id
    _drain(factory, embedder)
    with factory() as session:
        assert session.get(MemoryTask, task_id).status == "succeeded"
        assert session.get(MemoryItem, memory_id).status == "invalidated"


def test_provider_failure_retries_and_profile_task_stays_pending(governance_context):
    factory, tenant_id = governance_context
    embedder = FakeEmbedder()
    embedder.fail_next = True
    with factory.begin() as session:
        memory = _memory(session, tenant_id)
        memory_id = memory.id
        vector = _task(
            session, tenant_id, "vector_upsert", memory_id, 1, model_id=embedder.model_id
        )
        vector_id = vector.id
        profile = enqueue_task(
            session,
            {
                "id": uuid4().hex,
                "tenant_id": tenant_id,
                "task_type": "profile_rebuild",
                "target_type": "subject",
                "target_id": "user_governance",
                "idempotency_key": uuid4().hex,
            },
        )
        profile_id = profile.id
    assert run_once(factory, worker_id="governance_test", embedder=embedder)
    with factory.begin() as session:
        task = session.get(MemoryTask, vector_id)
        assert task.status == "pending" and task.attempt_count == 1
        task.available_at = datetime.now(UTC) - timedelta(seconds=1)
    _drain(factory, embedder)
    with factory() as session:
        assert session.get(MemoryTask, vector_id).status == "succeeded"
        assert session.get(MemoryTask, profile_id).status == "pending"
