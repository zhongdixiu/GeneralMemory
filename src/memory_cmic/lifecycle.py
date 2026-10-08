from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.governance_state import lock_user, mark_dirty
from memory_cmic.models import (
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.task_queue import enqueue_task


def _active_and_current(
    status: str,
    version: int,
    expected: int,
    expired_at: datetime | None,
    now: datetime,
) -> bool:
    return status == "active" and version == expected and (expired_at is None or expired_at > now)


def evidence_group_is_complete(
    session: Session, evidence_group_id: str, *, now: datetime | None = None
) -> bool:
    check_time = now or datetime.now(UTC)
    edges = session.scalars(
        select(MemoryEvidence).where(MemoryEvidence.evidence_group_id == evidence_group_id)
    ).all()
    if not edges:
        return False

    for edge in edges:
        if edge.status != "active":
            return False
        if edge.upstream_source_id is not None:
            upstream = session.get(SourceRecord, edge.upstream_source_id)
            if upstream is None or not _active_and_current(
                upstream.status, upstream.version, edge.upstream_version, None, check_time
            ):
                return False
        else:
            upstream_memory = session.get(MemoryItem, edge.upstream_memory_id)
            if upstream_memory is None or not _active_and_current(
                upstream_memory.status,
                upstream_memory.version,
                edge.upstream_version,
                upstream_memory.expired_at,
                check_time,
            ):
                return False
    return True


def _audit_invalidation(
    session: Session,
    *,
    tenant_id: str,
    target_type: str,
    target_id: str,
    old_version: int,
    new_version: int,
    correlation_id: str | None,
    now: datetime,
) -> None:
    session.add(
        MemoryAuditLog(
            tenant_id=tenant_id,
            action="INVALIDATE",
            target_type=target_type,
            target_id=target_id,
            operator_type="system",
            operator_id="memory_cmic",
            reason_code="EVIDENCE_INCOMPLETE",
            correlation_id=correlation_id,
            state_before={"status": "active", "version": old_version},
            state_after={"status": "invalidated", "version": new_version},
            created_at=now,
        )
    )


def _enqueue_vector_deletes(
    session: Session,
    *,
    memory: MemoryItem,
    correlation_id: str | None,
    now: datetime,
) -> None:
    embeddings = session.scalars(
        select(MemoryEmbedding).where(
            MemoryEmbedding.tenant_id == memory.tenant_id,
            MemoryEmbedding.memory_id == memory.id,
            MemoryEmbedding.status == "active",
        )
    ).all()
    for embedding in embeddings:
        embedding.status = "stale"
        embedding.updated_at = now
        enqueue_task(
            session,
            {
                "id": uuid4().hex,
                "tenant_id": memory.tenant_id,
                "task_type": "vector_delete",
                "target_type": "memory",
                "target_id": memory.id,
                "input_version": memory.version,
                "idempotency_key": (
                    f"vector_delete:memory:{memory.id}:v{memory.version}:{embedding.model_id}"
                ),
                "correlation_id": correlation_id,
                "payload": {
                    "model_id": embedding.model_id,
                    "content_hash": embedding.content_hash,
                },
                "priority": 100,
                "available_at": now,
            },
        )


def _propagate_memory_change(
    session: Session,
    *,
    memory: MemoryItem,
    correlation_id: str | None,
    now: datetime,
) -> None:
    from memory_cmic.profile import enqueue_profile_rebuild

    outgoing_edges = session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.tenant_id == memory.tenant_id,
            MemoryEvidence.upstream_memory_id == memory.id,
        )
    ).all()
    dependent_memories = {
        edge.downstream_memory_id
        for edge in outgoing_edges
        if edge.downstream_memory_id is not None
    }
    dependent_profiles = {
        edge.downstream_profile_id
        for edge in outgoing_edges
        if edge.downstream_profile_id is not None
    }
    for edge in outgoing_edges:
        if edge.status == "active":
            edge.status = "invalidated"
            edge.invalidated_at = now

    _enqueue_vector_deletes(
        session,
        memory=memory,
        correlation_id=correlation_id,
        now=now,
    )
    session.flush()

    for downstream_id in dependent_memories:
        recheck_downstream(
            session,
            tenant_id=memory.tenant_id,
            downstream_memory_id=downstream_id,
            correlation_id=correlation_id,
            now=now,
        )
    for downstream_id in dependent_profiles:
        recheck_downstream(
            session,
            tenant_id=memory.tenant_id,
            downstream_profile_id=downstream_id,
            correlation_id=correlation_id,
            now=now,
        )
    if memory.subject_type == "user" and memory.cognitive_type == "fact":
        enqueue_profile_rebuild(
            session, tenant_id=memory.tenant_id, user_id=memory.subject_id,
            correlation_id=correlation_id,
        )


def recheck_downstream(
    session: Session,
    *,
    tenant_id: str,
    downstream_memory_id: str | None = None,
    downstream_profile_id: str | None = None,
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    if (downstream_memory_id is None) == (downstream_profile_id is None):
        raise ValueError("exactly one downstream id is required")

    check_time = now or datetime.now(UTC)
    edge_filter = (
        MemoryEvidence.downstream_memory_id == downstream_memory_id
        if downstream_memory_id is not None
        else MemoryEvidence.downstream_profile_id == downstream_profile_id
    )
    group_ids = set(
        session.scalars(
            select(MemoryEvidence.evidence_group_id).where(
                MemoryEvidence.tenant_id == tenant_id, edge_filter
            )
        )
    )
    if any(evidence_group_is_complete(session, group_id, now=check_time) for group_id in group_ids):
        return True

    if downstream_memory_id is not None:
        downstream = session.get(MemoryItem, downstream_memory_id)
        target_type = "memory"
        task_type = "dependency_recheck"
        priority = 100
    else:
        downstream = session.get(ProfileProperty, downstream_profile_id)
        target_type = "profile"
        task_type = "profile_rebuild"
        priority = 50
    if downstream is None or downstream.tenant_id != tenant_id:
        raise LookupError("downstream does not exist in tenant")
    if downstream.status != "active":
        return False

    old_version = downstream.version
    downstream.status = "invalidated"
    downstream.version += 1
    downstream.updated_at = check_time
    if isinstance(downstream, MemoryItem):
        downstream.invalidated_at = check_time

    _audit_invalidation(
        session,
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=downstream.id,
        old_version=old_version,
        new_version=downstream.version,
        correlation_id=correlation_id,
        now=check_time,
    )
    enqueue_task(
        session,
        {
            "id": uuid4().hex,
            "tenant_id": tenant_id,
            "task_type": task_type,
            "target_type": target_type,
            "target_id": downstream.id,
            "input_version": downstream.version,
            "idempotency_key": f"{task_type}:{target_type}:{downstream.id}:v{downstream.version}",
            "correlation_id": correlation_id,
            "payload": {"trigger_version": downstream.version},
            "priority": priority,
            "available_at": check_time,
        },
    )
    session.flush()

    if isinstance(downstream, MemoryItem):
        _propagate_memory_change(
            session,
            memory=downstream,
            correlation_id=correlation_id,
            now=check_time,
        )
    return False


def update_or_invalidate_source(
    session: Session,
    *,
    tenant_id: str,
    source_id: str,
    status: str,
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> SourceRecord:
    source = session.get(SourceRecord, source_id)
    if source is None or source.tenant_id != tenant_id:
        raise LookupError("source does not exist in tenant")
    affected_edges = session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.tenant_id == tenant_id,
            MemoryEvidence.upstream_source_id == source_id,
        )
    ).all()
    downstream_ids = {
        edge.downstream_memory_id
        for edge in affected_edges
        if edge.downstream_memory_id is not None
    }
    users = {
        memory.subject_id
        for memory in session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id, MemoryItem.id.in_(downstream_ids)
            )
        )
        if memory.subject_type == "user"
    }
    for user_id in sorted(users):
        lock_user(session, tenant_id, user_id)
    old_status = source.status
    old_version = source.version
    source.status = status
    source.version += 1
    source.updated_at = now or datetime.now(UTC)
    if status != "active":
        source.invalidated_at = source.updated_at
    for edge in affected_edges:
        if edge.status == "active":
            edge.status = "invalidated"
            edge.invalidated_at = source.updated_at
    session.add(
        MemoryAuditLog(
            tenant_id=tenant_id,
            action="INVALIDATE" if status != "active" else "UPDATE",
            target_type="source",
            target_id=source.id,
            operator_type="system",
            operator_id="memory_cmic",
            reason_code="SOURCE_STATUS_CHANGED",
            correlation_id=correlation_id,
            state_before={"status": old_status, "version": old_version},
            state_after={"status": source.status, "version": source.version},
            created_at=source.updated_at,
        )
    )
    enqueue_task(
        session,
        {
            "id": uuid4().hex,
            "tenant_id": tenant_id,
            "task_type": "dependency_recheck",
            "target_type": "source",
            "target_id": source.id,
            "input_version": source.version,
            "idempotency_key": f"dependency_recheck:source:{source.id}:v{source.version}",
            "correlation_id": correlation_id,
            "payload": {"affected_memory_ids": sorted(downstream_ids)},
            "priority": 100,
            "available_at": source.updated_at,
        },
    )
    session.flush()
    for memory_id in downstream_ids:
        recheck_downstream(
            session,
            tenant_id=tenant_id,
            downstream_memory_id=memory_id,
            correlation_id=correlation_id,
            now=source.updated_at,
        )
        affected = session.get(MemoryItem, memory_id)
        if affected is not None:
            mark_dirty(session, affected)
    return source


def invalidate_memory(
    session: Session,
    *,
    tenant_id: str,
    memory_id: str,
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> MemoryItem:
    memory = session.get(MemoryItem, memory_id)
    if memory is None or memory.tenant_id != tenant_id:
        raise LookupError("memory does not exist in tenant")
    if memory.subject_type == "user":
        lock_user(session, tenant_id, memory.subject_id)
    if memory.status == "active":
        check_time = now or datetime.now(UTC)
        old_version = memory.version
        memory.status = "invalidated"
        memory.version += 1
        memory.invalidated_at = check_time
        memory.updated_at = check_time
        _audit_invalidation(
            session,
            tenant_id=tenant_id,
            target_type="memory",
            target_id=memory.id,
            old_version=old_version,
            new_version=memory.version,
            correlation_id=correlation_id,
            now=check_time,
        )
        _propagate_memory_change(
            session,
            memory=memory,
            correlation_id=correlation_id,
            now=check_time,
        )
    return memory


def govern_memory(
    session: Session, *, memory: MemoryItem, status: str, reason_code: str,
    correlation_id: str | None, conflict_group_id: str | None = None,
) -> None:
    if status not in {"invalidated", "disputed"}:
        raise ValueError("invalid governance status")
    if memory.status == status and memory.conflict_group_id == conflict_group_id:
        return
    now = datetime.now(UTC)
    before = {"status": memory.status, "version": memory.version,
              "conflict_group_id": memory.conflict_group_id}
    memory.status = status
    memory.version += 1
    memory.conflict_group_id = conflict_group_id
    memory.invalidated_at = now if status == "invalidated" else None
    memory.updated_at = now
    session.add(MemoryAuditLog(
        tenant_id=memory.tenant_id, action="INVALIDATE", target_type="memory",
        target_id=memory.id, operator_type="system", operator_id="memory_cmic",
        reason_code=reason_code, correlation_id=correlation_id,
        state_before=before,
        state_after={"status": status, "version": memory.version,
                     "conflict_group_id": conflict_group_id},
    ))
    _propagate_memory_change(session, memory=memory, correlation_id=correlation_id, now=now)


def expire_memory(
    session: Session,
    *,
    tenant_id: str,
    memory_id: str,
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> MemoryItem:
    check_time = now or datetime.now(UTC)
    memory = session.get(MemoryItem, memory_id)
    if memory is None or memory.tenant_id != tenant_id:
        raise LookupError("memory does not exist in tenant")
    if memory.subject_type == "user":
        lock_user(session, tenant_id, memory.subject_id)
    if memory.status != "active":
        return memory
    if memory.expired_at is None or memory.expired_at > check_time:
        raise ValueError("memory has not reached expired_at")

    old_version = memory.version
    memory.status = "expired"
    memory.version += 1
    memory.updated_at = check_time
    session.add(
        MemoryAuditLog(
            tenant_id=tenant_id,
            action="EXPIRE",
            target_type="memory",
            target_id=memory.id,
            operator_type="system",
            operator_id="memory_cmic",
            reason_code="TTL_EXPIRED",
            correlation_id=correlation_id,
            state_before={"status": "active", "version": old_version},
            state_after={"status": "expired", "version": memory.version},
            created_at=check_time,
        )
    )
    _propagate_memory_change(
        session,
        memory=memory,
        correlation_id=correlation_id,
        now=check_time,
    )
    return memory
