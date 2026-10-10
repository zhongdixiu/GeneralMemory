from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.audit import record_audit
from memory_cmic.governance_state import lock_user
from memory_cmic.lifecycle import expire_memory, recheck_downstream
from memory_cmic.models import (
    MemoryEmbedding,
    MemoryItem,
    MemoryTask,
    SourceRecord,
)
from memory_cmic.providers import Embedder
from memory_cmic.task_queue import (
    claim_tasks,
    enqueue_task,
    fail_and_retry_task,
    finish_exhausted_leases,
    finish_task,
)
from memory_cmic.worker import LeaseLostError, _lease_heartbeat, _leased_task

logger = logging.getLogger(__name__)
TASK_TYPES = ("vector_upsert", "vector_delete", "ttl_expire", "dependency_recheck")


class ObsoleteTask(Exception):
    pass


def enqueue_expired_memories(session: Session, *, limit: int = 100) -> int:
    now = datetime.now(UTC)
    memories = session.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.status == "active",
            MemoryItem.expired_at <= now,
            ~select(MemoryTask.id)
            .where(
                MemoryTask.tenant_id == MemoryItem.tenant_id,
                MemoryTask.task_type == "ttl_expire",
                MemoryTask.target_id == MemoryItem.id,
                MemoryTask.input_version == MemoryItem.version,
                or_(
                    MemoryTask.status != "failed",
                    MemoryTask.updated_at > now - timedelta(hours=1),
                ),
            )
            .exists(),
        )
        .order_by(MemoryItem.expired_at, MemoryItem.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    queued = 0
    for memory in memories:
        key = f"ttl_expire:memory:{memory.id}:v{memory.version}"
        existing = session.scalars(
            select(MemoryTask)
            .where(
                MemoryTask.tenant_id == memory.tenant_id,
                MemoryTask.caller_agent_id.is_(None),
                MemoryTask.idempotency_key == key,
            )
            .with_for_update()
        ).one_or_none()
        if existing is not None:
            if existing.status == "failed" and existing.updated_at <= now - timedelta(hours=1):
                existing.status = "pending"
                existing.attempt_count = 0
                existing.worker_id = None
                existing.lease_token = None
                existing.locked_until = None
                existing.error_json = None
                existing.last_error = None
                existing.available_at = now
                existing.updated_at = now
                queued += 1
            continue
        enqueue_task(
            session,
            {
                "id": uuid4().hex,
                "tenant_id": memory.tenant_id,
                "task_type": "ttl_expire",
                "target_type": "memory",
                "target_id": memory.id,
                "input_version": memory.version,
                "idempotency_key": key,
                "correlation_id": uuid4().hex,
                "priority": 100,
                "available_at": now,
            },
        )
        queued += 1
    return queued


def _current_memory(session: Session, task: MemoryTask) -> MemoryItem:
    if task.target_type != "memory" or task.input_version is None:
        raise ValueError("memory task requires a target version")
    memory = session.scalars(
        select(MemoryItem)
        .where(MemoryItem.tenant_id == task.tenant_id, MemoryItem.id == task.target_id)
        .with_for_update()
    ).one_or_none()
    if memory is None or memory.version != task.input_version:
        raise ObsoleteTask
    return memory


def _model_id(task: MemoryTask, *, legacy_model_id: str | None = None) -> str:
    model_id = task.payload.get("model_id")
    if not model_id and task.payload.get("source_system") == "honcho":
        model_id = legacy_model_id
    if not isinstance(model_id, str) or not model_id:
        raise ValueError("vector task requires model_id")
    return model_id


def _audit_embedding(
    session: Session, task: MemoryTask, *, action: str, model_id: str, content_hash: str
) -> None:
    record_audit(
        session,
        tenant_id=task.tenant_id,
        action=action,
        target_type="embedding",
        target_id=task.target_id,
        operator_type="system",
        operator_id="memory_cmic",
        reason_code="VECTOR_UPSERT" if action != "DELETE" else "VECTOR_DELETE",
        task=task,
        context={
            "model_id": model_id,
            **{
                key: task.payload[key]
                for key in ("operation_id", "original_correlation_id")
                if key in task.payload
            },
        },
        state_after={"model_id": model_id, "content_hash": content_hash}
        if action != "DELETE"
        else None,
        state_before={"model_id": model_id, "content_hash": content_hash}
        if action == "DELETE"
        else None,
    )


def _upsert_vector(
    session_factory: sessionmaker[Session], *, ownership: dict[str, str], embedder: Embedder
) -> None:
    with session_factory.begin() as session:
        task = _leased_task(session, **ownership)
        model_id = _model_id(task, legacy_model_id=embedder.model_id)
        if model_id != embedder.model_id:
            raise ValueError(f"no embedder for model_id {model_id}")
        memory = _current_memory(session, task)
        if memory.status != "active" or (
            memory.expired_at is not None and memory.expired_at <= datetime.now(UTC)
        ):
            raise ObsoleteTask
        text = f"{memory.summary}\n{memory.content}"
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        embedding = session.get(MemoryEmbedding, (task.tenant_id, memory.id, model_id))
        if embedding is not None and embedding.content_hash == content_hash:
            if embedding.status == "active":
                if not finish_task(session, **ownership, status="succeeded", results=[]):
                    raise LeaseLostError("task lease was lost")
                return
            vector = None
        else:
            vector = "generate"

    generated = embedder.embed([text])[0] if vector == "generate" else None
    with session_factory.begin() as session:
        task = _leased_task(session, **ownership)
        if _model_id(task, legacy_model_id=embedder.model_id) != model_id:
            raise ObsoleteTask
        memory = _current_memory(session, task)
        current_hash = hashlib.sha256(f"{memory.summary}\n{memory.content}".encode()).hexdigest()
        if (
            memory.status != "active"
            or (memory.expired_at is not None and memory.expired_at <= datetime.now(UTC))
            or current_hash != content_hash
        ):
            raise ObsoleteTask
        embedding = session.scalars(
            select(MemoryEmbedding)
            .where(
                MemoryEmbedding.tenant_id == task.tenant_id,
                MemoryEmbedding.memory_id == memory.id,
                MemoryEmbedding.model_id == model_id,
            )
            .with_for_update()
        ).one_or_none()
        if embedding is None:
            if generated is None:
                raise ObsoleteTask
            session.add(
                MemoryEmbedding(
                    tenant_id=task.tenant_id,
                    memory_id=memory.id,
                    model_id=model_id,
                    content_hash=content_hash,
                    embedding=generated,
                )
            )
            action = "INSERT"
        elif embedding.content_hash == content_hash and embedding.status == "active":
            action = None
        else:
            if embedding.content_hash != content_hash:
                if generated is None:
                    raise ObsoleteTask
                embedding.embedding = generated
                embedding.content_hash = content_hash
            embedding.status = "active"
            embedding.updated_at = datetime.now(UTC)
            action = "UPDATE"
        if action is not None:
            _audit_embedding(
                session, task, action=action, model_id=model_id, content_hash=content_hash
            )
        if not finish_task(session, **ownership, status="succeeded", results=[]):
            raise LeaseLostError("task lease was lost")


def _apply_task(session: Session, task: MemoryTask) -> None:
    if task.task_type == "vector_delete":
        model_id = _model_id(task)
        if task.target_type != "memory" or task.input_version is None:
            raise ValueError("memory task requires a target version")
        memory = session.scalars(
            select(MemoryItem)
            .where(MemoryItem.tenant_id == task.tenant_id, MemoryItem.id == task.target_id)
            .with_for_update()
        ).one_or_none()
        if memory is None or memory.version < task.input_version:
            raise ObsoleteTask
        if memory.status == "active" and (
            memory.expired_at is None or memory.expired_at > datetime.now(UTC)
        ):
            raise ObsoleteTask
        embedding = session.scalars(
            select(MemoryEmbedding)
            .where(
                MemoryEmbedding.tenant_id == task.tenant_id,
                MemoryEmbedding.memory_id == memory.id,
                MemoryEmbedding.model_id == model_id,
            )
            .with_for_update()
        ).one_or_none()
        if embedding is not None:
            expected_hash = task.payload.get("content_hash")
            if (expected_hash is not None and embedding.content_hash != expected_hash) or (
                expected_hash is None and memory.version != task.input_version
            ):
                raise ObsoleteTask
            _audit_embedding(
                session,
                task,
                action="DELETE",
                model_id=model_id,
                content_hash=embedding.content_hash,
            )
            session.delete(embedding)
    elif task.task_type == "ttl_expire":
        candidate = session.get(MemoryItem, task.target_id)
        if (
            candidate is not None
            and candidate.tenant_id == task.tenant_id
            and candidate.subject_type == "user"
        ):
            lock_user(session, task.tenant_id, candidate.subject_id)
        memory = _current_memory(session, task)
        if (
            memory.status != "active"
            or memory.expired_at is None
            or memory.expired_at > datetime.now(UTC)
        ):
            raise ObsoleteTask
        expire_memory(
            session,
            tenant_id=task.tenant_id,
            memory_id=memory.id,
            correlation_id=task.correlation_id,
            task=task,
        )
    elif task.task_type == "dependency_recheck":
        if task.input_version is None:
            raise ValueError("dependency task requires a target version")
        if task.target_type == "source":
            source = session.scalars(
                select(SourceRecord)
                .where(SourceRecord.tenant_id == task.tenant_id, SourceRecord.id == task.target_id)
                .with_for_update()
            ).one_or_none()
            if source is None or source.version != task.input_version:
                raise ObsoleteTask
            memory_ids = task.payload.get("affected_memory_ids")
            if not isinstance(memory_ids, list) or not all(
                isinstance(value, str) for value in memory_ids
            ):
                raise ValueError("source dependency task requires affected_memory_ids")
        elif task.target_type == "memory":
            memory_ids = [_current_memory(session, task).id]
        else:
            raise ValueError("unsupported dependency target")
        for memory_id in sorted(set(memory_ids)):
            memory = session.scalars(
                select(MemoryItem)
                .where(MemoryItem.tenant_id == task.tenant_id, MemoryItem.id == memory_id)
                .with_for_update()
            ).one_or_none()
            if memory is not None:
                recheck_downstream(
                    session,
                    tenant_id=task.tenant_id,
                    downstream_memory_id=memory_id,
                    correlation_id=task.correlation_id,
                    task=task,
                )
    else:
        raise ValueError("unsupported governance task type")


def run_once(session_factory: sessionmaker[Session], *, worker_id: str, embedder: Embedder) -> bool:
    with session_factory.begin() as session:
        finish_exhausted_leases(session)
        enqueue_expired_memories(session)
    with session_factory() as session:
        tenant_ids = session.scalars(
            select(MemoryTask.tenant_id)
            .where(
                MemoryTask.task_type.in_(TASK_TYPES),
                or_(
                    (MemoryTask.status == "pending")
                    & (MemoryTask.available_at <= func.current_timestamp()),
                    (MemoryTask.status == "processing")
                    & (MemoryTask.locked_until < func.current_timestamp())
                    & (MemoryTask.attempt_count < MemoryTask.max_attempts),
                ),
            )
            .distinct()
        ).all()
    for tenant_id in tenant_ids:
        with session_factory.begin() as session:
            claimed = claim_tasks(
                session,
                tenant_id=tenant_id,
                worker_id=worker_id,
                limit=1,
                lease_seconds=180,
                task_types=TASK_TYPES,
            )
            task = claimed[0] if claimed else None
            if task is not None:
                ownership = {
                    "tenant_id": tenant_id,
                    "task_id": task.id,
                    "worker_id": worker_id,
                    "lease_token": task.lease_token,
                }
                task_type = task.task_type
        if task is None:
            continue
        try:
            with _lease_heartbeat(session_factory, **ownership, lease_seconds=180):
                if task_type == "vector_upsert":
                    _upsert_vector(session_factory, ownership=ownership, embedder=embedder)
                else:
                    with session_factory.begin() as session:
                        task = _leased_task(session, **ownership)
                        _apply_task(session, task)
                        if not finish_task(session, **ownership, status="succeeded", results=[]):
                            raise LeaseLostError("task lease was lost")
        except (ObsoleteTask, ValueError) as exc:
            with session_factory.begin() as session:
                finish_task(
                    session,
                    **ownership,
                    status="cancelled" if isinstance(exc, ObsoleteTask) else "failed",
                    results=[],
                    error=None
                    if isinstance(exc, ObsoleteTask)
                    else {"code": "INVALID_TASK", "message": str(exc)},
                )
        except LeaseLostError:
            logger.warning("governance task %s lost its lease", ownership["task_id"])
        except Exception as exc:
            logger.exception("governance task %s failed", ownership["task_id"])
            with session_factory.begin() as session:
                fail_and_retry_task(
                    session,
                    **ownership,
                    error=str(exc),
                    error_code="GOVERNANCE_FAILED",
                    base_backoff_seconds=2,
                )
        return True
    return False
