from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from memory_cmic.models import MemoryItem, MemoryTask


def enqueue_task(session: Session, data: Mapping[str, Any]) -> MemoryTask:
    caller_agent_id = data.get("caller_agent_id")
    caller_filter = (
        MemoryTask.caller_agent_id == caller_agent_id
        if caller_agent_id is not None
        else MemoryTask.caller_agent_id.is_(None)
    )
    existing = session.scalars(
        select(MemoryTask).where(
            MemoryTask.tenant_id == data["tenant_id"],
            caller_filter,
            MemoryTask.idempotency_key == data["idempotency_key"],
        )
    ).one_or_none()
    if existing is not None:
        return existing

    if caller_agent_id is None:
        inserted_id = session.scalar(
            pg_insert(MemoryTask)
            .values(**dict(data))
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "idempotency_key"],
                index_where=MemoryTask.caller_agent_id.is_(None),
            )
            .returning(MemoryTask.id)
        )
        if inserted_id is None:
            return session.scalars(
                select(MemoryTask).where(
                    MemoryTask.tenant_id == data["tenant_id"],
                    MemoryTask.caller_agent_id.is_(None),
                    MemoryTask.idempotency_key == data["idempotency_key"],
                )
            ).one()
        return session.get(MemoryTask, inserted_id)

    task = MemoryTask(**dict(data))
    session.add(task)
    session.flush()
    return task


def finish_exhausted_leases(session: Session) -> int:
    result = session.execute(
        text(
            """
            UPDATE memory_task
            SET status = CASE WHEN payload->'execution'->>'changed' = 'true'
                              THEN 'partial' ELSE 'failed' END,
                completed_at = clock_timestamp(),
                error = jsonb_build_object('code', 'LEASE_EXHAUSTED',
                                           'message', 'task lease expired after final attempt'),
                last_error = 'task lease expired after final attempt',
                worker_id = NULL, lease_token = NULL, locked_until = NULL,
                updated_at = clock_timestamp()
            WHERE status = 'processing'
              AND locked_until < clock_timestamp()
              AND attempt_count >= max_attempts
            """
        )
    )
    return result.rowcount


def claim_tasks(
    session: Session,
    *,
    tenant_id: str,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 60,
    task_types: tuple[str, ...] | None = None,
) -> list[MemoryTask]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if task_types == ():
        return []

    type_filter = "AND candidate.task_type IN :task_types" if task_types else ""

    claimed_ids = list(
        session.execute(
            text(
                f"""
                WITH candidates AS (
                    SELECT id
                    FROM memory_task AS candidate
                    WHERE candidate.tenant_id = :tenant_id
                      {type_filter}
                      AND (
                           (candidate.status = 'pending'
                            AND candidate.available_at <= CURRENT_TIMESTAMP)
                        OR (candidate.status = 'processing'
                            AND candidate.locked_until < CURRENT_TIMESTAMP
                            AND candidate.attempt_count < candidate.max_attempts)
                      )
                      AND (
                          candidate.session_id IS NULL
                          OR NOT EXISTS (
                              SELECT 1
                              FROM memory_task AS prior
                              WHERE prior.tenant_id = candidate.tenant_id
                                AND prior.source_system = candidate.source_system
                                AND prior.user_id = candidate.user_id
                                AND prior.session_id = candidate.session_id
                                AND prior.batch_seq < candidate.batch_seq
                                AND prior.status IN ('pending', 'processing')
                          )
                      )
                      AND (candidate.task_type <> 'profile_rebuild' OR (
                          pg_try_advisory_xact_lock(hashtextextended(
                              'profile-claim:' || candidate.tenant_id || ':' ||
                              COALESCE(candidate.user_id, candidate.target_id), 0))
                          AND NOT EXISTS (
                              SELECT 1 FROM memory_task prior
                              WHERE prior.tenant_id = candidate.tenant_id
                                AND prior.user_id = candidate.user_id
                                AND prior.task_type = 'profile_rebuild'
                                AND prior.status = 'processing' AND prior.id <> candidate.id
                          )
                      ))
                    ORDER BY candidate.priority DESC,
                             candidate.available_at ASC,
                             candidate.created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT :limit
                )
                UPDATE memory_task AS task
                SET status = 'processing',
                    worker_id = :worker_id,
                    lease_token = md5(random()::text || clock_timestamp()::text || task.id),
                    locked_until = CURRENT_TIMESTAMP
                                   + make_interval(secs => :lease_seconds),
                    attempt_count = CASE
                        WHEN task.status = 'processing' THEN task.attempt_count + 1
                        ELSE task.attempt_count
                    END,
                    started_at = COALESCE(task.started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                FROM candidates
                WHERE task.id = candidates.id
                RETURNING task.id
                """
            ).bindparams(*([bindparam("task_types", expanding=True)] if task_types else [])),
            {
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "limit": limit,
                "lease_seconds": lease_seconds,
                **({"task_types": task_types} if task_types else {}),
            },
        ).scalars()
    )
    if not claimed_ids:
        return []

    session.expire_all()
    tasks_by_id = {
        task.id: task
        for task in session.scalars(select(MemoryTask).where(MemoryTask.id.in_(claimed_ids)))
    }
    return sorted(
        (tasks_by_id[task_id] for task_id in claimed_ids),
        key=lambda task: (-task.priority, task.available_at, task.created_at),
    )


def renew_task_lease(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    lease_seconds: int,
    lease_token: str | None = None,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    result = session.execute(
        text(
            """
            UPDATE memory_task
            SET locked_until = clock_timestamp() + make_interval(secs => :lease_seconds),
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = :tenant_id
              AND id = :task_id
              AND status = 'processing'
              AND worker_id = :worker_id
              AND (CAST(:lease_token AS VARCHAR) IS NULL
                   OR lease_token = CAST(:lease_token AS VARCHAR))
              AND locked_until > clock_timestamp()
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "lease_token": lease_token,
        },
    )
    session.expire_all()
    return result.rowcount == 1


def complete_task(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    lease_token: str | None = None,
) -> bool:
    result = session.execute(
        text(
            """
            UPDATE memory_task
            SET status = 'succeeded',
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = :tenant_id
              AND id = :task_id
              AND status = 'processing'
              AND worker_id = :worker_id
              AND (CAST(:lease_token AS VARCHAR) IS NULL
                   OR lease_token = CAST(:lease_token AS VARCHAR))
              AND locked_until > CURRENT_TIMESTAMP
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "lease_token": lease_token,
        },
    )
    session.expire_all()
    return result.rowcount == 1


def fail_and_retry_task(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    error: str,
    error_code: str = "EXTRACTION_FAILED",
    lease_token: str | None = None,
    base_backoff_seconds: int = 1,
) -> MemoryTask | None:
    if base_backoff_seconds <= 0:
        raise ValueError("base_backoff_seconds must be positive")
    returned_id = session.execute(
        text(
            """
            UPDATE memory_task
            SET attempt_count = attempt_count + 1,
                status = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN
                        CASE WHEN payload->'execution'->>'changed' = 'true'
                             THEN 'partial' ELSE 'failed' END
                    ELSE 'pending'
                END,
                available_at = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN available_at
                    ELSE CURRENT_TIMESTAMP + make_interval(
                        secs => :base_backoff_seconds * power(2, attempt_count)
                    )
                END,
                worker_id = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN worker_id
                    ELSE NULL
                END,
                locked_until = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN locked_until
                    ELSE NULL
                END,
                lease_token = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN lease_token
                    ELSE NULL
                END,
                completed_at = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN CURRENT_TIMESTAMP
                    ELSE NULL
                END,
                last_error = :error,
                error = CASE
                    WHEN attempt_count + 1 >= max_attempts
                    THEN jsonb_build_object(
                        'code', CAST(:error_code AS TEXT),
                        'message', CAST(:error AS TEXT)
                    )
                    ELSE NULL
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = :tenant_id
              AND id = :task_id
              AND status = 'processing'
              AND worker_id = :worker_id
              AND (CAST(:lease_token AS VARCHAR) IS NULL
                   OR lease_token = CAST(:lease_token AS VARCHAR))
              AND locked_until > clock_timestamp()
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "error": error,
            "error_code": error_code,
            "lease_token": lease_token,
            "base_backoff_seconds": base_backoff_seconds,
        },
    ).scalar_one_or_none()
    if returned_id is None:
        return None
    session.expire_all()
    return session.get(MemoryTask, returned_id)


def finish_task(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    lease_token: str,
    status: str,
    results: list[dict[str, Any]],
    error: dict[str, str] | None = None,
) -> bool:
    if status not in {"succeeded", "partial", "failed", "cancelled"}:
        raise ValueError("invalid terminal task status")
    result = session.execute(
        update(MemoryTask)
        .where(
            MemoryTask.tenant_id == tenant_id,
            MemoryTask.id == task_id,
            MemoryTask.status == "processing",
            MemoryTask.worker_id == worker_id,
            MemoryTask.lease_token == lease_token,
            MemoryTask.locked_until > func.clock_timestamp(),
        )
        .values(
            status=status,
            result_json=results,
            error_json=error,
            completed_at=func.current_timestamp(),
            updated_at=func.current_timestamp(),
        )
    )
    session.expire_all()
    return result.rowcount == 1


def apply_memory_content_if_current(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    new_content: str,
) -> bool:
    task = session.scalars(
        select(MemoryTask)
        .where(
            MemoryTask.id == task_id,
            MemoryTask.status == "processing",
            MemoryTask.worker_id == worker_id,
            MemoryTask.locked_until > func.current_timestamp(),
        )
        .with_for_update()
    ).one_or_none()
    if task is None or task.target_type != "memory" or task.input_version is None:
        return False

    result = session.execute(
        update(MemoryItem)
        .where(
            MemoryItem.tenant_id == task.tenant_id,
            MemoryItem.id == task.target_id,
            MemoryItem.version == task.input_version,
        )
        .values(
            content=new_content,
            version=MemoryItem.version + 1,
            updated_at=func.current_timestamp(),
        )
    )
    if result.rowcount == 0:
        task.status = "cancelled"
    else:
        task.status = "succeeded"
    task.completed_at = func.current_timestamp()
    task.updated_at = func.current_timestamp()
    session.flush()
    return result.rowcount == 1
