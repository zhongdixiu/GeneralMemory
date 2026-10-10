from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from memory_cmic.models import (
    GovernancePending,
    GovernancePolicy,
    GovernanceSubjectState,
    MemoryItem,
    MemoryTask,
)
from memory_cmic.task_queue import enqueue_task


def lock_user(session: Session, tenant_id: str, user_id: str) -> None:
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"fact:{tenant_id}:{user_id}", 0)))
    )


def mark_dirty(session: Session, memory: MemoryItem, *, from_add: bool = False) -> None:
    if memory.subject_type != "user":
        return
    now = datetime.now(UTC)
    session.execute(
        pg_insert(GovernancePending)
        .values(
            tenant_id=memory.tenant_id,
            user_id=memory.subject_id,
            memory_id=memory.id,
            first_change_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "user_id", "memory_id"],
            set_={
                "generation": GovernancePending.generation + 1,
                "updated_at": now,
            },
        )
    )
    if from_add:
        session.execute(
            pg_insert(GovernanceSubjectState)
            .values(tenant_id=memory.tenant_id, user_id=memory.subject_id, last_add_at=now)
            .on_conflict_do_update(
                index_elements=["tenant_id", "user_id"],
                set_={"last_add_at": now, "updated_at": now},
            )
        )


def backfill_history(session: Session, tenant_id: str) -> int:
    now = datetime.now(UTC)
    result = session.execute(
        text(
            """
            INSERT INTO governance_pending
                (tenant_id, user_id, memory_id, generation, first_change_at, updated_at)
            SELECT tenant_id, subject_id, id, 1, :now, :now
            FROM memory_item
            WHERE tenant_id = :tenant_id AND subject_type = 'user'
              AND status IN ('active', 'disputed') AND (expired_at IS NULL OR expired_at > :now)
            ON CONFLICT (tenant_id, user_id, memory_id) DO NOTHING
            """
        ),
        {"tenant_id": tenant_id, "now": now},
    )
    return result.rowcount


def backfill_user(session: Session, tenant_id: str, user_id: str) -> int:
    now = datetime.now(UTC)
    result = session.execute(
        text(
            """
            INSERT INTO governance_pending
                (tenant_id, user_id, memory_id, generation, first_change_at, updated_at)
            SELECT tenant_id, subject_id, id, 1, :now, :now
            FROM memory_item
            WHERE tenant_id = :tenant_id AND subject_type = 'user' AND subject_id = :user_id
              AND status IN ('active', 'disputed') AND (expired_at IS NULL OR expired_at > :now)
            ON CONFLICT (tenant_id, user_id, memory_id) DO NOTHING
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id, "now": now},
    )
    return result.rowcount


def ensure_policy(session: Session, tenant_id: str) -> GovernancePolicy:
    session.execute(
        pg_insert(GovernancePolicy)
        .values(tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    return session.get(GovernancePolicy, tenant_id)


def active_run(session: Session, tenant_id: str, user_id: str) -> MemoryTask | None:
    return session.scalars(
        select(MemoryTask).where(
            MemoryTask.tenant_id == tenant_id,
            MemoryTask.task_type == "consolidate",
            MemoryTask.user_id == user_id,
            MemoryTask.status.in_(["pending", "processing"]),
        )
    ).one_or_none()


def create_run(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    operator_id: str | None = None,
    idempotency_key: str | None = None,
    automatic: bool = False,
    trigger_reason: str = "manual",
) -> MemoryTask:
    lock_user(session, tenant_id, user_id)
    existing = active_run(session, tenant_id, user_id)
    if existing is not None:
        return existing
    policy = ensure_policy(session, tenant_id)
    now = datetime.now(UTC)
    return enqueue_task(
        session,
        {
            "id": f"gvr_{uuid4().hex}",
            "tenant_id": tenant_id,
            "task_type": "consolidate",
            "target_type": "subject",
            "target_id": user_id,
            "user_id": user_id,
            "caller_agent_id": operator_id,
            "idempotency_key": idempotency_key or f"consolidate:{user_id}:{uuid4().hex}",
            "correlation_id": uuid4().hex,
            "priority": 0,
            "available_at": now,
            "payload": {
                "automatic": automatic,
                "trigger_reason": trigger_reason,
                "policy_version": policy.version,
                "prompt_version": "governance-v2-phases",
                "generate_inferences": policy.generate_inferences,
                "limits": {
                    "memories": policy.max_memories,
                    "model_calls": policy.max_model_calls,
                },
            },
        },
    )


def schedule_due(session: Session) -> int:
    now = datetime.now(UTC)
    policies = session.scalars(
        select(GovernancePolicy).where(GovernancePolicy.auto_enabled.is_(True))
    ).all()
    scheduled = 0
    for policy in policies:
        rows = session.execute(
            select(
                GovernancePending.user_id,
                func.count(GovernancePending.memory_id),
                func.min(GovernancePending.first_change_at),
            )
            .where(GovernancePending.tenant_id == policy.tenant_id)
            .group_by(GovernancePending.user_id)
            .order_by(func.min(GovernancePending.first_change_at))
        ).all()
        for user_id, count, oldest in rows:
            state = session.get(GovernanceSubjectState, (policy.tenant_id, user_id))
            latest_task = session.scalars(
                select(MemoryTask)
                .where(
                    MemoryTask.tenant_id == policy.tenant_id,
                    MemoryTask.user_id == user_id,
                    MemoryTask.task_type == "consolidate",
                    MemoryTask.completed_at.is_not(None),
                )
                .order_by(MemoryTask.completed_at.desc())
                .limit(1)
            ).first()
            last_run = (
                latest_task.completed_at if latest_task else (state.last_run_at if state else None)
            )
            if last_run and last_run > now - timedelta(minutes=policy.cooldown_minutes):
                continue
            ready = oldest <= now - timedelta(minutes=policy.max_wait_minutes)
            idle = (
                not state
                or not state.last_add_at
                or state.last_add_at <= now - timedelta(minutes=policy.idle_minutes)
            )
            if not ready and (count < policy.change_threshold or not idle):
                continue
            if active_run(session, policy.tenant_id, user_id) is None:
                create_run(
                    session,
                    tenant_id=policy.tenant_id,
                    user_id=user_id,
                    automatic=True,
                    trigger_reason="max_wait" if ready else "change_threshold_idle",
                )
                scheduled += 1
    return scheduled
