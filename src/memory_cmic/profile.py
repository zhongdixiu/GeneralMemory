from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.extraction_input import input_size
from memory_cmic.governance_state import lock_user
from memory_cmic.models import (
    MemoryAuditLog,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.providers import ProviderError, QwenFactModel, normalize_business_domain
from memory_cmic.task_queue import claim_tasks, enqueue_task, fail_and_retry_task, finish_task
from memory_cmic.worker import LeaseLostError, _lease_heartbeat, _leased_task

logger = logging.getLogger(__name__)

PROFILE_PROMPT = (
    "Extract explicit, current, durable user properties into these Chinese keys only: "
    "姓名 (stated name), 职位 (stated occupation or job title), "
    "邮件回复偏好 (lasting email reply style), 输出要求 (lasting output format or detail). "
    "Use a complete, concise Chinese sentence about the user as each value. For example, "
    "'用户名叫大钟，是一名算法研发' yields 姓名='用户叫大钟' and 职位='用户是算法研发'. "
    "For 职位, omit classifiers such as 一名 from the value. "
    "Recurring preferences for document types, such as how to write project reports, count as "
    "lasting 输出要求; preserve the document type in each value. Keep distinct properties "
    "separate. Do not infer, generalize across document types, or use project-specific roles, "
    "one-off task states, temporary requests, or historical facts. "
    "Return JSON {properties:[{memory_id:string,key:string,value:string,"
    "quote:string}]}. Quote must be an exact substring of that memory's content and must "
    "directly support the value. Use only supplied memory IDs. Return an empty array when "
    "nothing qualifies. Preserve the original language."
)
ENTITY_PREFIXES = {"姓名": "用户叫", "职位": "用户是"}
KEYS = {*ENTITY_PREFIXES, "邮件回复偏好", "输出要求"}
LEGACY_KEYS = {"identity.preferred_name", "pref.email.reply_style", "pref.output.requirements"}


class ProfileProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    key: str
    value: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=1)


class ProfileModel(Protocol):
    def extract(self, memories: list[dict[str, str]]) -> list[ProfileProposal]: ...


class QwenProfileModel:
    def __init__(self, *, api_key: str, base_url: str, model: str, output_tokens: int):
        self._model = QwenFactModel(
            api_key=api_key, base_url=base_url, model=model, output_tokens=output_tokens
        )

    def extract(self, memories: list[dict[str, str]]) -> list[ProfileProposal]:
        try:
            raw = self._model._json_completion(PROFILE_PROMPT, {"memories": memories})
            return [ProfileProposal.model_validate(item) for item in raw["properties"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"invalid profile output: {exc}") from exc


def _user_memories(session: Session, tenant_id: str, user_id: str) -> list[MemoryItem]:
    return session.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == user_id,
            MemoryItem.cognitive_type == "fact",
        )
        .order_by(MemoryItem.id)
    ).all()


def _snapshot(session: Session, tenant_id: str, user_id: str) -> tuple[str, list[MemoryItem]]:
    from memory_cmic.lifecycle import evidence_group_is_complete

    now = datetime.now(UTC)
    memories = _user_memories(session, tenant_id, user_id)
    ids = [memory.id for memory in memories]
    edges = (
        session.scalars(
            select(MemoryEvidence)
            .where(
                MemoryEvidence.tenant_id == tenant_id, MemoryEvidence.downstream_memory_id.in_(ids)
            )
            .order_by(MemoryEvidence.id)
        ).all()
        if ids
        else []
    )
    fingerprint: list[Any] = [
        (
            m.id,
            m.version,
            m.status,
            m.content,
            m.business_domains,
            m.project_domains,
            m.metadata_json,
            m.effective_at.isoformat(),
            m.expired_at.isoformat() if m.expired_at else None,
        )
        for m in memories
    ]
    complete: dict[str, bool] = {}
    for edge in edges:
        upstream = (
            session.get(SourceRecord, edge.upstream_source_id) if edge.upstream_source_id else None
        )
        fingerprint.append(
            (
                edge.id,
                edge.status,
                edge.evidence_group_id,
                edge.upstream_version,
                upstream.status if upstream else None,
                upstream.version if upstream else None,
            )
        )
        if edge.evidence_group_id not in complete:
            complete[edge.evidence_group_id] = evidence_group_is_complete(
                session, edge.evidence_group_id, now=now
            )
    valid_ids = {
        edge.downstream_memory_id
        for edge in edges
        if edge.relationship_type == "supports" and complete[edge.evidence_group_id]
    }
    eligible = [
        m
        for m in memories
        if m.id in valid_ids
        and m.status == "active"
        and m.effective_at <= now
        and (m.expired_at is None or m.expired_at > now)
        and m.project_domains is None
        and not (m.metadata_json or {}).get("project_limited")
        and not (m.metadata_json or {}).get("project_context")
        and (m.metadata_json or {}).get("temporal_kind", "current") == "current"
    ]
    digest = hashlib.sha256(
        json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return digest, eligible


def enqueue_profile_rebuild(
    session: Session, *, tenant_id: str, user_id: str, correlation_id: str | None = None
) -> MemoryTask:
    lock_user(session, tenant_id, user_id)
    pending = session.scalars(
        select(MemoryTask)
        .where(
            MemoryTask.tenant_id == tenant_id,
            MemoryTask.task_type == "profile_rebuild",
            MemoryTask.target_type == "subject",
            MemoryTask.target_id == user_id,
            MemoryTask.status == "pending",
        )
        .order_by(MemoryTask.created_at)
        .limit(1)
    ).first()
    if pending is not None:
        return pending
    watermark, _ = _snapshot(session, tenant_id, user_id)
    return enqueue_task(
        session,
        {
            "id": f"tsk_{uuid4().hex}",
            "tenant_id": tenant_id,
            "task_type": "profile_rebuild",
            "target_type": "subject",
            "target_id": user_id,
            "user_id": user_id,
            "idempotency_key": f"profile_rebuild:v2:{user_id}:{watermark}",
            "correlation_id": correlation_id,
            "payload": {"watermark": watermark},
            "priority": 50,
        },
    )


def _scope(memory: MemoryItem) -> tuple[str, ...] | None:
    return tuple(sorted(set(memory.business_domains))) if memory.business_domains else None


def _expiration(key: str, basis: list[MemoryItem]) -> datetime | None:
    dates = [memory.expired_at for memory in basis]
    if key == "输出要求":
        return min((date for date in dates if date is not None), default=None)
    return max(dates) if all(date is not None for date in dates) else None


def _desired(
    memories: list[MemoryItem], proposals: list[ProfileProposal]
) -> dict[tuple[tuple[str, ...] | None, str], tuple[Any, list[MemoryItem]]]:
    by_id = {m.id: m for m in memories}
    grouped: dict[tuple[tuple[str, ...] | None, str], list[tuple[str, MemoryItem]]] = defaultdict(
        list
    )
    for item in proposals:
        memory = by_id.get(item.memory_id)
        if (
            memory is None
            or item.key not in KEYS
            or item.quote not in memory.content
            or not item.value.strip()
        ):
            raise ProviderError("profile output is not grounded in an eligible fact")
        if item.key in ENTITY_PREFIXES and memory.business_domains is not None:
            raise ProviderError("identity and occupation must have general applicability")
        if item.key in ENTITY_PREFIXES:
            prefix = ENTITY_PREFIXES[item.key]
            entity = item.value.removeprefix(prefix).strip(" 。！!")
            if not item.value.startswith(prefix):
                raise ProviderError("identity or occupation value is not a complete property")
            if item.key == "职位":
                entity = entity.removeprefix("一名").removeprefix("一个")
            if not entity or entity not in item.quote:
                raise ProviderError("identity or occupation value is not supported by quote")
        if not item.value.startswith("用户"):
            raise ProviderError("profile value must be a sentence about the user")
        if item.key == "邮件回复偏好" and _scope(memory) != ("email",):
            raise ProviderError("email reply style requires email applicability")
        value = f"{prefix}{entity}" if item.key in ENTITY_PREFIXES else item.value.strip()
        grouped[(_scope(memory), item.key)].append((value, memory))
    desired = {}
    for identity, items in grouped.items():
        values = sorted({value for value, _ in items})
        if identity[1] != "输出要求" and len(values) != 1:
            continue  # Unresolved conflict: do not silently select a winner.
        basis = list({memory.id: memory for _, memory in items}.values())
        desired[identity] = (
            values if identity[1] == "输出要求" else values[0],
            basis,
        )
    return desired


def _replace(
    session: Session,
    task: MemoryTask,
    desired: dict[tuple[tuple[str, ...] | None, str], tuple[Any, list[MemoryItem]]],
) -> None:
    from memory_cmic.lifecycle import evidence_group_is_complete

    now = datetime.now(UTC)
    profiles = session.scalars(
        select(ProfileProperty)
        .where(
            ProfileProperty.tenant_id == task.tenant_id,
            ProfileProperty.user_id == task.user_id,
            ProfileProperty.property_key.in_(KEYS | LEGACY_KEYS),
        )
        .with_for_update()
    ).all()
    old = {(_scope(p), p.property_key): p for p in profiles if p.status == "active"}
    versions = defaultdict(int)
    for profile in profiles:
        identity = (_scope(profile), profile.property_key)
        versions[identity] = max(versions[identity], profile.version)
    for identity in set(old) | set(desired):
        current = old.get(identity)
        proposed = desired.get(identity)
        old_edges = (
            session.scalars(
                select(MemoryEvidence).where(
                    MemoryEvidence.tenant_id == task.tenant_id,
                    MemoryEvidence.downstream_profile_id == current.id,
                    MemoryEvidence.status == "active",
                )
            ).all()
            if current
            else []
        )
        if (
            proposed
            and current
            and current.property_value == proposed[0]
            and {e.upstream_memory_id for e in old_edges} == {m.id for m in proposed[1]}
            and current.effective_at == max(m.effective_at for m in proposed[1])
            and current.expired_at == _expiration(identity[1], proposed[1])
            and all(
                evidence_group_is_complete(session, e.evidence_group_id, now=now) for e in old_edges
            )
        ):
            continue
        if current:
            current.status = "invalidated"
            current.version += 1
            current.updated_at = now
            for edge in old_edges:
                edge.status = "invalidated"
                edge.invalidated_at = now
            session.add(
                MemoryAuditLog(
                    tenant_id=task.tenant_id,
                    action="INVALIDATE",
                    target_type="profile",
                    target_id=current.id,
                    operator_type="system",
                    operator_id="memory_cmic",
                    reason_code="PROFILE_REBUILT",
                    correlation_id=task.correlation_id,
                    state_before={"version": current.version - 1},
                    state_after={"version": current.version, "status": "invalidated"},
                )
            )
            session.flush()
        if not proposed:
            continue
        value, basis = proposed
        profile = ProfileProperty(
            id=f"prf_{uuid4().hex}",
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            business_domains=list(identity[0]) if identity[0] else None,
            property_key=identity[1],
            property_value=value,
            value_type="list" if isinstance(value, list) else "string",
            confidence=1,
            version=versions[identity] + 1,
            effective_at=max(m.effective_at for m in basis),
            expired_at=_expiration(identity[1], basis),
        )
        session.add(profile)
        session.flush()
        shared_group = f"evg_{uuid4().hex}" if identity[1] == "输出要求" else None
        for memory in basis:
            session.add(
                MemoryEvidence(
                    id=f"evi_{uuid4().hex}",
                    tenant_id=task.tenant_id,
                    relationship_type="profile_basis",
                    evidence_group_id=shared_group or f"evg_{uuid4().hex}",
                    upstream_memory_id=memory.id,
                    downstream_profile_id=profile.id,
                    upstream_version=memory.version,
                    evidence_snippet=memory.content[:512],
                )
            )
        session.add(
            MemoryAuditLog(
                tenant_id=task.tenant_id,
                action="INSERT",
                target_type="profile",
                target_id=profile.id,
                operator_type="system",
                operator_id="memory_cmic",
                reason_code="PROFILE_REBUILT",
                correlation_id=task.correlation_id,
                state_after={
                    "key": profile.property_key,
                    "value": value,
                    "basis_ids": [m.id for m in basis],
                    "version": profile.version,
                },
            )
        )


def read_profiles(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    business_domain: str,
    now: datetime | None = None,
) -> list[ProfileProperty]:
    from memory_cmic.lifecycle import evidence_group_is_complete

    check_time = now or datetime.now(UTC)
    business_domain = normalize_business_domain(business_domain)
    profiles = session.scalars(
        select(ProfileProperty)
        .where(
            ProfileProperty.tenant_id == tenant_id,
            ProfileProperty.user_id == user_id,
            ProfileProperty.status == "active",
            ProfileProperty.effective_at <= check_time,
            or_(ProfileProperty.expired_at.is_(None), ProfileProperty.expired_at > check_time),
            or_(
                ProfileProperty.business_domains.is_(None),
                ProfileProperty.business_domains.any(business_domain),
            ),
        )
        .order_by(ProfileProperty.property_key)
    ).all()
    result = []
    for profile in profiles:
        edges = session.scalars(
            select(MemoryEvidence).where(
                MemoryEvidence.tenant_id == tenant_id,
                MemoryEvidence.downstream_profile_id == profile.id,
                MemoryEvidence.relationship_type == "profile_basis",
            )
        ).all()
        source_valid = {}
        for edge in edges:
            memory = session.get(MemoryItem, edge.upstream_memory_id)
            if memory is None or memory.tenant_id != tenant_id or memory.cognitive_type != "fact":
                source_valid[edge.id] = False
                continue
            source_groups = set(
                session.scalars(
                    select(MemoryEvidence.evidence_group_id).where(
                        MemoryEvidence.tenant_id == tenant_id,
                        MemoryEvidence.downstream_memory_id == memory.id,
                        MemoryEvidence.relationship_type == "supports",
                    )
                )
            )
            source_valid[edge.id] = any(
                evidence_group_is_complete(session, group, now=check_time)
                for group in source_groups
            )
        if any(
            evidence_group_is_complete(session, group, now=check_time)
            and all(source_valid[edge.id] for edge in edges if edge.evidence_group_id == group)
            for group in {edge.evidence_group_id for edge in edges}
        ):
            result.append(profile)
    return result


def run_once(
    session_factory: sessionmaker[Session], *, worker_id: str, model: ProfileModel
) -> bool:
    with session_factory() as session:
        tenants = session.scalars(
            select(MemoryTask.tenant_id)
            .where(
                MemoryTask.task_type == "profile_rebuild",
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
    for tenant_id in tenants:
        with session_factory.begin() as session:
            claimed = claim_tasks(
                session,
                tenant_id=tenant_id,
                worker_id=worker_id,
                lease_seconds=180,
                task_types=("profile_rebuild",),
            )
            if not claimed:
                continue
            task = claimed[0]
            ownership = dict(
                tenant_id=tenant_id,
                task_id=task.id,
                worker_id=worker_id,
                lease_token=task.lease_token,
            )
        try:
            with _lease_heartbeat(session_factory, **ownership, lease_seconds=180):
                with session_factory.begin() as session:
                    task = _leased_task(session, **ownership)
                    if task.target_type == "subject":
                        user_id = task.user_id or task.target_id
                    elif task.target_type == "profile":
                        profile = session.get(ProfileProperty, task.target_id)
                        if profile is None or profile.tenant_id != tenant_id:
                            raise ValueError("profile target does not exist in tenant")
                        user_id = profile.user_id
                    else:
                        raise ValueError("invalid profile rebuild target")
                    snapshot, memories = _snapshot(session, tenant_id, user_id)
                    inputs = [{"id": m.id, "content": m.content} for m in memories]
                proposals: list[ProfileProposal] = []
                batch: list[dict[str, str]] = []
                for item in inputs:
                    candidate = [*batch, item]
                    if (
                        len(candidate) > 20
                        or input_size(PROFILE_PROMPT, {"memories": candidate}) > 7000
                    ):
                        if not batch:
                            raise ValueError("one profile fact exceeds the model input budget")
                        proposals.extend(model.extract(batch))
                        batch = [item]
                    else:
                        batch = candidate
                if batch:
                    proposals.extend(model.extract(batch))
                desired = _desired(memories, proposals)
                with session_factory.begin() as session:
                    task = _leased_task(session, **ownership)
                    lock_user(session, tenant_id, user_id)
                    current, _ = _snapshot(session, tenant_id, user_id)
                    if current != snapshot:
                        raise ProviderError("profile evidence changed during rebuild")
                    task.user_id = user_id
                    _replace(session, task, desired)
                    if not finish_task(session, **ownership, status="succeeded", results=[]):
                        raise LeaseLostError("task lease was lost")
        except ValueError as exc:
            with session_factory.begin() as session:
                finish_task(
                    session,
                    **ownership,
                    status="failed",
                    results=[],
                    error={"code": "INVALID_TASK", "message": str(exc)},
                )
        except LeaseLostError:
            logger.warning("profile task %s lost its lease", ownership["task_id"])
        except Exception as exc:
            logger.exception("profile task %s failed", ownership["task_id"])
            with session_factory.begin() as session:
                fail_and_retry_task(
                    session,
                    **ownership,
                    error=str(exc),
                    error_code="PROFILE_REBUILD_FAILED",
                    base_backoff_seconds=2,
                )
        return True
    return False
