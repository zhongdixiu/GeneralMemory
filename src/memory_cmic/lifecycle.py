from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.audit import record_audit
from memory_cmic.evidence import evidence_group_is_complete
from memory_cmic.governance_state import lock_user, mark_dirty
from memory_cmic.models import (
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.repositories import add_evidence_group
from memory_cmic.task_queue import enqueue_task


class MemoryVersionConflict(ValueError):
    pass


class MemoryUpdateConflict(ValueError):
    pass


def _lock_user_memory(
    session: Session, *, tenant_id: str, user_id: str, memory_id: str,
) -> MemoryItem:
    lock_user(session, tenant_id, user_id)
    memory = session.scalars(
        select(MemoryItem).where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == user_id,
            MemoryItem.cognitive_type.in_(["fact", "inference"]),
            MemoryItem.id == memory_id,
        ).with_for_update().execution_options(populate_existing=True)
    ).one_or_none()
    if memory is None:
        raise LookupError("memory does not exist for this user in tenant")
    return memory


def delete_memory(
    session: Session, *, tenant_id: str, user_id: str, memory_id: str,
    operator_id: str, operator_type: str = "agent", reason: str | None = None,
    expected_version: int | None = None, correlation_id: str | None = None,
    now: datetime | None = None,
) -> MemoryItem:
    memory = _lock_user_memory(
        session, tenant_id=tenant_id, user_id=user_id, memory_id=memory_id,
    )
    if memory.status == "deleted":
        return memory
    if expected_version is not None and memory.version != expected_version:
        raise MemoryVersionConflict("memory changed since it was read")
    check_time = now or datetime.now(UTC)
    correlation_id = correlation_id or uuid4().hex
    before = {"status": memory.status, "version": memory.version}
    memory.status = "deleted"
    memory.version += 1
    memory.invalidated_at = check_time
    memory.updated_at = check_time
    record_audit(
        session, tenant_id=tenant_id, action="DELETE", target_type="memory",
        target_id=memory.id, operator_type=operator_type, operator_id=operator_id,
        reason_code="MEMORY_DELETED", reason=reason, correlation_id=correlation_id,
        context={"user_id": user_id}, state_before=before,
        state_after={"status": "deleted", "version": memory.version}, created_at=check_time,
    )
    _propagate_memory_change(
        session, memory=memory, correlation_id=correlation_id, now=check_time,
    )
    session.flush()
    return memory


def update_memory(
    session: Session, *, tenant_id: str, user_id: str, memory_id: str, content: str,
    operator_id: str, model_id: str, expected_version: int | None = None,
    operator_type: str = "agent", reason: str | None = None,
    correlation_id: str | None = None, now: datetime | None = None,
) -> MemoryItem:
    old = _lock_user_memory(
        session, tenant_id=tenant_id, user_id=user_id, memory_id=memory_id,
    )
    check_time = now or datetime.now(UTC)
    if expected_version is not None and old.version != expected_version:
        raise MemoryVersionConflict("memory changed since it was read")
    if (old.cognitive_type != "fact" or old.status != "active"
            or old.effective_at > check_time
            or old.expired_at is not None and old.expired_at <= check_time):
        raise MemoryUpdateConflict("only a currently active user fact can be updated")
    if not content.strip() or len(content) > 8000:
        raise ValueError("memory content must contain 1-8000 nonblank characters")
    if content == old.content:
        return old
    correlation_id = correlation_id or uuid4().hex
    source = SourceRecord(
        id=f"src_{uuid4().hex}", tenant_id=tenant_id, user_id=user_id,
        source_system="memory_management", source_type="manual",
        caller_agent_id=operator_id, agent_id=operator_id,
        external_ref_id=correlation_id, author_type=operator_type, author_id=operator_id,
        raw_content=content, content_hash=hashlib.sha256(content.encode()).hexdigest(),
        occurred_at=check_time, received_at=check_time,
    )
    session.add(source)
    metadata = deepcopy(old.metadata_json or {})
    for key in ("source_message_ids", "statement_order", "governance_action",
                "previous_memory_ids", "profile_admission"):
        metadata.pop(key, None)
    metadata.update({"source_occurred_at": check_time.isoformat(), "timestamp_origin": "source"})
    replacement = MemoryItem(
        id=f"mem_{uuid4().hex}", tenant_id=tenant_id,
        subject_type="user", subject_id=user_id, cognitive_type="fact",
        business_domains=deepcopy(old.business_domains),
        project_domains=deepcopy(old.project_domains),
        summary=content[:512], content=content,
        semantic_hash=hashlib.sha256(" ".join(content.casefold().split()).encode()).hexdigest(),
        metadata_json=metadata, confidence=1,
        effective_at=old.effective_at, expired_at=old.expired_at,
        supersedes_id=old.id, created_by=operator_id,
    )
    session.add(replacement)
    session.flush()
    add_evidence_group(session, [{
        "id": f"evi_{uuid4().hex}", "tenant_id": tenant_id,
        "relationship_type": "supports", "evidence_group_id": f"evg_{uuid4().hex}",
        "upstream_source_id": source.id, "upstream_version": source.version,
        "downstream_memory_id": replacement.id, "evidence_snippet": content,
    }])
    record_audit(
        session, tenant_id=tenant_id, action="INSERT", target_type="source",
        target_id=source.id, operator_type=operator_type, operator_id=operator_id,
        reason_code="MANUAL_MEMORY_SOURCE", reason=reason, correlation_id=correlation_id,
        context={"user_id": user_id, "memory_id": replacement.id},
        state_after={"status": source.status, "version": source.version}, created_at=check_time,
    )
    before = {"status": old.status, "version": old.version, "content": old.content}
    invalidate_memory(
        session, tenant_id=tenant_id, memory_id=old.id, correlation_id=correlation_id,
        now=check_time, operator_type=operator_type, operator_id=operator_id,
        reason_code="MEMORY_UPDATED", reason=reason,
        context={"replacement_id": replacement.id},
    )
    record_audit(
        session, tenant_id=tenant_id, action="UPDATE", target_type="memory",
        target_id=old.id, operator_type=operator_type, operator_id=operator_id,
        reason_code="MEMORY_UPDATED", reason=reason, correlation_id=correlation_id,
        context={"user_id": user_id, "replacement_id": replacement.id, "source_id": source.id},
        state_before=before,
        state_after={"id": replacement.id, "status": replacement.status,
                     "version": replacement.version, "content": content},
        created_at=check_time,
    )
    record_audit(
        session, tenant_id=tenant_id, action="INSERT", target_type="memory",
        target_id=replacement.id, operator_type=operator_type, operator_id=operator_id,
        reason_code="MEMORY_CREATED", correlation_id=correlation_id,
        context={"supersedes_id": old.id, "source_id": source.id},
        state_after={"status": replacement.status, "version": replacement.version,
                     "content": content}, created_at=check_time,
    )
    enqueue_task(session, {
        "id": uuid4().hex, "tenant_id": tenant_id, "task_type": "vector_upsert",
        "target_type": "memory", "target_id": replacement.id,
        "input_version": replacement.version,
        "idempotency_key": (
            f"vector_upsert:memory:{replacement.id}:v{replacement.version}:{model_id}"
        ),
        "correlation_id": correlation_id, "payload": {"model_id": model_id}, "priority": 50,
    })
    mark_dirty(session, replacement)
    session.flush()
    return replacement


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
    task: MemoryTask | None = None,
    reason_code: str = "EVIDENCE_INCOMPLETE",
    reason: str | None = None,
    operator_type: str = "system",
    operator_id: str = "memory_cmic",
    context: dict | None = None,
) -> None:
    record_audit(
        session,
        tenant_id=tenant_id,
        action="INVALIDATE",
        target_type=target_type,
        target_id=target_id,
        operator_type=operator_type,
        operator_id=operator_id,
        reason_code=reason_code,
        reason=reason,
        task=task,
        context=context,
        correlation_id=correlation_id,
        state_before={"status": "active", "version": old_version},
        state_after={"status": "invalidated", "version": new_version},
        created_at=now,
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
    task: MemoryTask | None = None,
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
            task=task,
        )
    for downstream_id in dependent_profiles:
        recheck_downstream(
            session,
            tenant_id=memory.tenant_id,
            downstream_profile_id=downstream_id,
            correlation_id=correlation_id,
            now=now,
            task=task,
        )
    if memory.subject_type == "user" and memory.cognitive_type in {"fact", "inference"}:
        if task is None or task.task_type != "consolidate":
            mark_dirty(session, memory)
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
    task: MemoryTask | None = None,
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
        task=task,
    )
    if isinstance(downstream, ProfileProperty):
        from memory_cmic.profile import enqueue_profile_rebuild

        session.flush()
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id=downstream.user_id,
                                correlation_id=correlation_id)
    else:
        enqueue_task(
            session,
            {
                "id": uuid4().hex,
                "tenant_id": tenant_id,
                "task_type": task_type,
                "target_type": target_type,
                "target_id": downstream.id,
                "input_version": downstream.version,
                "idempotency_key": (
                    f"{task_type}:{target_type}:{downstream.id}:v{downstream.version}"
                ),
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
            task=task,
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
    record_audit(
        session,
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
    from memory_cmic.profile import enqueue_profile_rebuild

    for user_id in users:
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id=user_id,
                                correlation_id=correlation_id)
    return source


def invalidate_memory(
    session: Session,
    *,
    tenant_id: str,
    memory_id: str,
    correlation_id: str | None = None,
    now: datetime | None = None,
    task: MemoryTask | None = None,
    reason_code: str = "EVIDENCE_INCOMPLETE",
    reason: str | None = None,
    operator_type: str = "system",
    operator_id: str = "memory_cmic",
    context: dict | None = None,
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
            task=task,
            reason_code=reason_code,
            reason=reason,
            operator_type=operator_type,
            operator_id=operator_id,
            context=context,
        )
        _propagate_memory_change(
            session,
            memory=memory,
            correlation_id=correlation_id,
            now=check_time,
            task=task,
        )
    return memory


def govern_memory(
    session: Session, *, memory: MemoryItem, status: str, reason_code: str,
    correlation_id: str | None, conflict_group_id: str | None = None,
    task: MemoryTask | None = None, reason: str | None = None,
) -> None:
    if status not in {"invalidated", "disputed"}:
        raise ValueError("invalid governance status")
    if memory.status == "deleted":
        raise ValueError("deleted memory cannot be changed by governance")
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
    record_audit(
        session,
        tenant_id=memory.tenant_id,
        action="UPDATE" if status == "disputed" else "INVALIDATE",
        target_type="memory",
        target_id=memory.id,
        operator_type="system",
        operator_id="memory_cmic",
        reason_code=reason_code,
        correlation_id=correlation_id,
        task=task,
        reason=reason,
        state_before=before,
        state_after={
            "status": status,
            "version": memory.version,
            "conflict_group_id": conflict_group_id,
        },
    )
    _propagate_memory_change(
        session, memory=memory, correlation_id=correlation_id, now=now, task=task
    )


def expire_memory(
    session: Session,
    *,
    tenant_id: str,
    memory_id: str,
    correlation_id: str | None = None,
    now: datetime | None = None,
    task: MemoryTask | None = None,
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
    record_audit(
        session,
        tenant_id=tenant_id,
        action="EXPIRE",
        target_type="memory",
        target_id=memory.id,
        operator_type="system",
        operator_id="memory_cmic",
        reason_code="TTL_EXPIRED",
        task=task,
        correlation_id=correlation_id,
        state_before={"status": "active", "version": old_version},
        state_after={"status": "expired", "version": memory.version},
        created_at=check_time,
    )
    _propagate_memory_change(
        session,
        memory=memory,
        correlation_id=correlation_id,
        now=check_time,
        task=task,
    )
    return memory
