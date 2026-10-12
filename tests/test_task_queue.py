from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from memory_cmic.models import (
    MemoryAuditLog,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.repositories import create_memory, create_or_replace_profile, create_source
from memory_cmic.task_queue import (
    apply_memory_content_if_current,
    claim_tasks,
    complete_task,
    enqueue_task,
    fail_and_retry_task,
    renew_task_lease,
)


def _database_now(session: Session):
    return session.scalar(select(func.current_timestamp()))


def _pending_task(
    *,
    task_id: str,
    tenant_id: str,
    priority: int = 50,
    available_at=None,
) -> dict:
    values = {
        "id": task_id,
        "tenant_id": tenant_id,
        "task_type": "vector_upsert",
        "target_type": "memory",
        "target_id": f"target_{task_id}",
        "input_version": 1,
        "idempotency_key": f"vector_upsert:{task_id}",
        "priority": priority,
    }
    if available_at is not None:
        values["available_at"] = available_at
    return values


@pytest.fixture
def committed_queue_tenant(engine: Engine) -> Iterator[str]:
    tenant_id = f"tenant_queue_{uuid4().hex}"
    try:
        yield tenant_id
    finally:
        with engine.begin() as connection:
            connection.execute(delete(MemoryTask).where(MemoryTask.tenant_id == tenant_id))


def test_two_connections_skip_locked_tasks_without_duplicate_claim(
    engine: Engine, committed_queue_tenant: str
) -> None:
    with Session(engine) as seed_session:
        enqueue_task(
            seed_session,
            _pending_task(
                task_id="tsk_concurrent_high",
                tenant_id=committed_queue_tenant,
                priority=100,
            ),
        )
        enqueue_task(
            seed_session,
            _pending_task(
                task_id="tsk_concurrent_low",
                tenant_id=committed_queue_tenant,
                priority=50,
            ),
        )
        seed_session.commit()

    connection_one = engine.connect()
    connection_two = engine.connect()
    transaction_one = connection_one.begin()
    transaction_two = connection_two.begin()
    session_one = Session(bind=connection_one)
    session_two = Session(bind=connection_two)
    try:
        first = claim_tasks(
            session_one,
            tenant_id=committed_queue_tenant,
            worker_id="worker_one",
            lease_seconds=60,
        )
        second = claim_tasks(
            session_two,
            tenant_id=committed_queue_tenant,
            worker_id="worker_two",
            lease_seconds=60,
        )
        assert [task.id for task in first] == ["tsk_concurrent_high"]
        assert [task.id for task in second] == ["tsk_concurrent_low"]
        assert first[0].id != second[0].id
    finally:
        session_two.close()
        session_one.close()
        transaction_two.rollback()
        transaction_one.rollback()
        connection_two.close()
        connection_one.close()


def test_t10_stale_task_cannot_overwrite_newer_memory(session: Session) -> None:
    now = _database_now(session)
    memory = create_memory(
        session,
        {
            "id": "mem_t10",
            "tenant_id": "tenant_t10",
            "subject_type": "user",
            "subject_id": "user_t10",
            "cognitive_type": "inference",
            "summary": "当前版本摘要",
            "content": "version 5 content",
            "confidence": 1,
            "status": "active",
            "version": 5,
            "effective_at": now,
            "created_by": "test_t10",
        },
    )
    task = enqueue_task(
        session,
        {
            "id": "tsk_t10_stale",
            "tenant_id": "tenant_t10",
            "task_type": "inference_derive",
            "target_type": "memory",
            "target_id": memory.id,
            "input_version": 4,
            "idempotency_key": "inference_derive:mem_t10:v4",
            "status": "processing",
            "worker_id": "worker_t10",
            "locked_until": now + timedelta(minutes=5),
            "started_at": now,
        },
    )

    updated = apply_memory_content_if_current(
        session,
        task_id=task.id,
        worker_id="worker_t10",
        new_content="stale version 4 result",
    )
    assert updated is False
    assert memory.content == "version 5 content"
    assert memory.version == 5
    assert task.status == "cancelled"


def test_current_version_task_updates_memory_and_completes(session: Session) -> None:
    now = _database_now(session)
    memory = create_memory(
        session,
        {
            "id": "mem_current_result",
            "tenant_id": "tenant_current_result",
            "subject_type": "user",
            "subject_id": "user_current_result",
            "cognitive_type": "inference",
            "summary": "当前任务结果",
            "content": "version 1 content",
            "confidence": 1,
            "version": 1,
            "effective_at": now,
            "created_by": "test_current_result",
        },
    )
    task = enqueue_task(
        session,
        {
            "id": "tsk_current_result",
            "tenant_id": memory.tenant_id,
            "task_type": "inference_derive",
            "target_type": "memory",
            "target_id": memory.id,
            "input_version": 1,
            "idempotency_key": "inference_derive:mem_current_result:v1",
            "status": "processing",
            "worker_id": "worker_current_result",
            "locked_until": now + timedelta(minutes=5),
            "started_at": now,
        },
    )

    assert apply_memory_content_if_current(
        session,
        task_id=task.id,
        worker_id="worker_current_result",
        new_content="version 2 content",
    )
    session.refresh(memory)
    assert memory.content == "version 2 content"
    assert memory.version == 2
    assert task.status == "succeeded"


def test_t12_duplicate_idempotency_key_returns_one_task(session: Session) -> None:
    task_data = _pending_task(task_id="tsk_t12_original", tenant_id="tenant_t12")
    original = enqueue_task(session, task_data)
    duplicate_data = dict(task_data)
    duplicate_data["id"] = "tsk_t12_duplicate"
    duplicate = enqueue_task(session, duplicate_data)

    assert duplicate.id == original.id
    assert session.scalar(
        select(func.count()).select_from(MemoryTask).where(
            MemoryTask.tenant_id == "tenant_t12",
            MemoryTask.idempotency_key == task_data["idempotency_key"],
        )
    ) == 1


def test_t17_lease_renewal_takeover_and_owner_checks(session: Session) -> None:
    now = _database_now(session)
    task = enqueue_task(
        session,
        {
            **_pending_task(task_id="tsk_t17", tenant_id="tenant_t17"),
            "status": "processing",
            "worker_id": "worker_old",
            "locked_until": now + timedelta(minutes=1),
            "started_at": now,
        },
    )

    original_locked_until = task.locked_until
    assert not renew_task_lease(
        session,
        tenant_id="tenant_t17",
        task_id=task.id,
        worker_id="worker_wrong",
        lease_seconds=120,
    )
    assert renew_task_lease(
        session,
        tenant_id="tenant_t17",
        task_id=task.id,
        worker_id="worker_old",
        lease_seconds=120,
    )
    assert task.locked_until > original_locked_until
    assert claim_tasks(
        session,
        tenant_id="tenant_t17",
        worker_id="worker_new",
        lease_seconds=60,
    ) == []

    task.locked_until = now - timedelta(seconds=1)
    session.flush()
    assert not complete_task(
        session,
        tenant_id="tenant_t17",
        task_id=task.id,
        worker_id="worker_old",
    )
    claimed = claim_tasks(
        session,
        tenant_id="tenant_t17",
        worker_id="worker_new",
        lease_seconds=60,
    )
    assert [claimed_task.id for claimed_task in claimed] == [task.id]
    assert claimed[0].attempt_count == 1
    assert not complete_task(
        session,
        tenant_id="tenant_t17",
        task_id=task.id,
        worker_id="worker_old",
    )
    assert complete_task(
        session,
        tenant_id="tenant_t17",
        task_id=task.id,
        worker_id="worker_new",
    )


def test_failure_retry_backoff_and_terminal_failure(session: Session) -> None:
    now = _database_now(session)
    task = enqueue_task(
        session,
        {
            **_pending_task(task_id="tsk_retry", tenant_id="tenant_retry"),
            "status": "processing",
            "worker_id": "worker_retry_1",
            "locked_until": now + timedelta(minutes=5),
            "started_at": now,
            "max_attempts": 4,
        },
    )

    current_worker = "worker_retry_1"
    for attempt, expected_backoff in enumerate((1, 2, 4), start=1):
        retry = fail_and_retry_task(
            session,
            tenant_id="tenant_retry",
            task_id=task.id,
            worker_id=current_worker,
            error=f"synthetic failure {attempt}",
            base_backoff_seconds=1,
        )
        assert retry.status == "pending"
        assert retry.attempt_count == attempt
        assert retry.worker_id is None
        assert retry.locked_until is None
        assert (retry.available_at - retry.updated_at).total_seconds() == expected_backoff

        retry.available_at = now - timedelta(seconds=1)
        session.flush()
        current_worker = f"worker_retry_{attempt + 1}"
        claimed = claim_tasks(
            session,
            tenant_id="tenant_retry",
            worker_id=current_worker,
            lease_seconds=60,
        )
        assert [claimed_task.id for claimed_task in claimed] == [task.id]

    terminal = fail_and_retry_task(
        session,
        tenant_id="tenant_retry",
        task_id=task.id,
        worker_id=current_worker,
        error="terminal synthetic failure",
        base_backoff_seconds=1,
    )
    assert terminal.status == "failed"
    assert terminal.attempt_count == 4
    assert terminal.worker_id == current_worker
    assert terminal.completed_at is not None


def test_t18_priority_order_and_future_availability(lifecycle_session: Session) -> None:
    now = _database_now(lifecycle_session)
    enqueue_task(
        lifecycle_session,
        _pending_task(
            task_id="tsk_orbit_future_100",
            tenant_id="tenant_orbit",
            priority=100,
            available_at=now + timedelta(days=1),
        ),
    )

    claimed_ids = []
    for _ in range(3):
        claimed = claim_tasks(
            lifecycle_session,
            tenant_id="tenant_orbit",
            worker_id="worker_priority",
            lease_seconds=60,
        )
        claimed_ids.append(claimed[0].id)
    assert claimed_ids == [
        "tsk_orbit_dependency_100",
        "tsk_orbit_vector_50",
        "tsk_orbit_vector_0",
    ]
    assert claim_tasks(
        lifecycle_session,
        tenant_id="tenant_orbit",
        worker_id="worker_priority",
        lease_seconds=60,
    ) == []


def test_same_priority_uses_available_at_order(session: Session) -> None:
    now = _database_now(session)
    enqueue_task(
        session,
        _pending_task(
            task_id="tsk_order_later",
            tenant_id="tenant_order",
            available_at=now - timedelta(seconds=1),
        ),
    )
    enqueue_task(
        session,
        _pending_task(
            task_id="tsk_order_earlier",
            tenant_id="tenant_order",
            available_at=now - timedelta(seconds=2),
        ),
    )
    first = claim_tasks(
        session,
        tenant_id="tenant_order",
        worker_id="worker_order",
        lease_seconds=60,
    )
    second = claim_tasks(
        session,
        tenant_id="tenant_order",
        worker_id="worker_order",
        lease_seconds=60,
    )
    assert [task.id for task in first + second] == ["tsk_order_earlier", "tsk_order_later"]


def test_t19_tasks_and_audits_inherit_root_correlation(session: Session) -> None:
    now = _database_now(session)
    correlation_id = "corr_t19_root"
    source = create_source(
        session,
        {
            "id": "src_t19",
            "tenant_id": "tenant_t19",
            "source_system": "fixture",
            "source_type": "chat",
            "external_ref_id": "message_t19",
            "author_type": "user",
            "author_id": "user_t19",
            "raw_content": "虚构的根操作来源。",
            "content_hash": "1" * 64,
            "occurred_at": now,
        },
    )
    memory = create_memory(
        session,
        {
            "id": "mem_t19",
            "tenant_id": "tenant_t19",
            "subject_type": "user",
            "subject_id": "user_t19",
            "cognitive_type": "fact",
            "summary": "虚构链路事实",
            "content": "用于 correlation_id 验证。",
            "confidence": 1,
            "effective_at": now,
            "created_by": "test_t19",
        },
    )
    profile = create_or_replace_profile(
        session,
        {
            "id": "prf_t19",
            "tenant_id": "tenant_t19",
            "user_id": "user_t19",
            "business_domains": None,
            "property_key": "pref.synthetic",
            "property_value": "enabled",
            "value_type": "string",
            "confidence": 1,
            "effective_at": now,
        },
    )

    task_specs = [
        ("tsk_t19_extract", "fact_extract", "source", source.id),
        ("tsk_t19_vector", "vector_upsert", "memory", memory.id),
        ("tsk_t19_profile", "profile_rebuild", "profile", profile.id),
    ]
    for task_id, task_type, target_type, target_id in task_specs:
        enqueue_task(
            session,
            {
                "id": task_id,
                "tenant_id": "tenant_t19",
                "task_type": task_type,
                "target_type": target_type,
                "target_id": target_id,
                "idempotency_key": f"{task_type}:{target_type}:{target_id}:t19",
                "correlation_id": correlation_id,
                "payload": {"trigger": "t19"},
            },
        )
        session.add(
            MemoryAuditLog(
                tenant_id="tenant_t19",
                action="INSERT",
                target_type=target_type,
                target_id=target_id,
                operator_type="system",
                operator_id="test_t19",
                reason_code="T19_CHAIN",
                correlation_id=correlation_id,
                state_after={"task_type": task_type},
            )
        )
    session.flush()

    tasks = session.scalars(
        select(MemoryTask).where(MemoryTask.correlation_id == correlation_id)
    ).all()
    audits = session.scalars(
        select(MemoryAuditLog).where(MemoryAuditLog.correlation_id == correlation_id)
    ).all()
    assert len(tasks) == 3
    assert {(audit.target_type, audit.target_id) for audit in audits} == {
        ("source", source.id),
        ("memory", memory.id),
        ("profile", profile.id),
    }
    assert "correlation_id" not in MemoryItem.__table__.columns
    assert "correlation_id" not in SourceRecord.__table__.columns
    assert "correlation_id" not in ProfileProperty.__table__.columns

def test_renewal_rejects_old_token_after_same_worker_takeover(session):
    now = _database_now(session)
    task = enqueue_task(
        session,
        {
            **_pending_task(task_id="token_renewal", tenant_id="tenant_token_renewal"),
            "status": "processing",
            "worker_id": "same_worker",
            "lease_token": "old_token",
            "locked_until": now - timedelta(seconds=1),
            "started_at": now,
        },
    )
    claimed = claim_tasks(session, tenant_id=task.tenant_id, worker_id="same_worker")
    current_token = claimed[0].lease_token
    assert current_token != "old_token"
    assert not renew_task_lease(
        session,
        tenant_id=task.tenant_id,
        task_id=task.id,
        worker_id="same_worker",
        lease_token="old_token",
        lease_seconds=120,
    )
    assert renew_task_lease(
        session,
        tenant_id=task.tenant_id,
        task_id=task.id,
        worker_id="same_worker",
        lease_token=current_token,
        lease_seconds=120,
    )
    assert not complete_task(
        session,
        tenant_id=task.tenant_id,
        task_id=task.id,
        worker_id="same_worker",
        lease_token="old_token",
    )


def test_claim_empty_supported_types_leaves_queue_pending(session):
    task = enqueue_task(session, _pending_task(task_id="empty_types", tenant_id="empty_types"))
    assert claim_tasks(session, tenant_id=task.tenant_id, worker_id="unused", task_types=()) == []
    assert task.status == "pending"


def test_renewal_cannot_shorten_an_existing_lease(session: Session) -> None:
    now = _database_now(session)
    original_until = now + timedelta(minutes=1)
    task = enqueue_task(session, {
        **_pending_task(task_id="lease_no_regression", tenant_id="tenant_lease_no_regression"),
        "status": "processing", "worker_id": "lease_worker", "locked_until": original_until,
    })
    assert renew_task_lease(session, tenant_id=task.tenant_id, task_id=task.id,
                            worker_id="lease_worker", lease_seconds=1)
    session.refresh(task)
    assert task.locked_until == original_until
