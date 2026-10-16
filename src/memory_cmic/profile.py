from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.audit import record_audit
from memory_cmic.evidence import (
    evidence_fingerprint,
    evidence_group_is_complete,
    memory_has_valid_evidence,
)
from memory_cmic.extraction_input import input_size
from memory_cmic.governance_state import lock_user
from memory_cmic.models import (
    GovernanceSubjectState,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
)
from memory_cmic.providers import ProviderError, QwenFactModel, normalize_business_domain
from memory_cmic.task_queue import (
    claim_tasks,
    enqueue_task,
    fail_and_retry_task,
    finish_exhausted_leases,
    finish_task,
)
from memory_cmic.worker import LeaseLostError, _lease_heartbeat, _leased_task

logger = logging.getLogger(__name__)

PROFILE_PROMPT = (
    "Select explicit, current, durable information about the user: identity, occupations, "
    "lasting preferences, habits and recurring requirements. Each value must express one "
    "independently supported statement about the user. Splitting independent statements takes "
    "priority over reusing a whole fact. Each value must be a short, standalone declarative "
    "sentence with an explicit subject, predicate and object or complement. Refer to this "
    "user as 用户 in Chinese (the user in English), not 我, 我们, 你, I, we or you. "
    "Resolve legacy first-person facts to 用户, and other references only when the supplied "
    "fact identifies the referent. Do not guess identities or change literal names and titles. "
    "The quote remains verbatim even when the value resolves a pronoun. "
    "Reuse wording only when the fact is already atomic and has a resolved explicit subject; "
    "otherwise make the minimal edits needed for separate complete statements. "
    "For example, '用户希望以后项目周报先给结论，并附上数据依据' MUST produce two items: "
    "'用户希望以后项目周报先给结论' and '用户希望以后项目周报附上数据依据'. "
    "Both items cite the same memory_id, and each quote must retain enough source context "
    "to support its document type and conditions; quoting the whole fact is allowed. "
    "Keep qualifications, exceptions and conditions with "
    "the statement they constrain. Preserve document types, business conditions and negations. "
    "For example, '用户希望周报简洁，但风险说明需要详细' stays one qualified statement. "
    "Complementary preferences such as concise emails and conclusion-first emails are separate "
    "statements. Never bundle different requirements into a list or a combined template. "
    "Do not infer, broaden applicability, or include project-specific roles, temporary requests, "
    "one-off task states or historical facts. Do not assign property keys or categories. "
    "Return JSON {properties:[{memory_id:string,value:string,quote:string}]}. "
    "Quote must be an exact substring of that memory's content and directly support the whole "
    "value, including its conditions. Use only supplied memory IDs. Return an empty array when "
    "nothing qualifies. Preserve the original language."
    " Inputs identify cognitive_type. For fact inputs retain explicit statements only. "
    "For inference inputs select only already stated, stable, useful preferences or habits "
    "supported across independent tasks. Reject temporary patterns and unsupported traits. "
    "Preserve inferred uncertainty (例如 用户倾向于), never turn it into an explicit user "
    "instruction, and never perform additional inference. When the same qualified statement "
    "has explicit and inferred support, prefer the explicit statement."
    " Explicit facts and negations in the same conditions override inferred patterns; reject "
    "an inferred property contradicted by any supplied explicit fact."
)
LEGACY_KEYS = {
    "姓名",
    "职位",
    "邮件回复偏好",
    "输出要求",
    "identity.preferred_name",
    "pref.email.reply_style",
    "pref.output.requirements",
}
ProfileIdentity = tuple[tuple[str, ...] | None, str, datetime, datetime | None, str]
PROFILE_QUIET = timedelta(minutes=2)
PROFILE_MAX_WAIT = timedelta(minutes=30)
PROFILE_POLICY_VERSION = 1


class ProfileProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
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
            MemoryItem.cognitive_type.in_(["fact", "inference"]),
        )
        .order_by(MemoryItem.id)
    ).all()


def _eligible_fact(memory: MemoryItem, now: datetime) -> bool:
    metadata = memory.metadata_json or {}
    return (
        memory.status == "active"
        and memory.effective_at <= now
        and (memory.expired_at is None or memory.expired_at > now)
        and memory.project_domains is None
        and not metadata.get("project_limited")
        and not metadata.get("project_context")
        and metadata.get("temporal_kind", "current") == "current"
        and metadata.get("retention_kind", "long_term") == "long_term"
    )


def inference_profiles_enabled() -> bool:
    return os.environ.get("MEMORY_PROFILE_ALLOW_INFERRED", "false").lower() == "true"


def _basis_fingerprint(
    session: Session, memory: MemoryItem, *, now: datetime | None = None,
) -> str:
    entries = evidence_fingerprint(session, memory)
    now = now or datetime.now(UTC)
    # Block inferred profiles when new explicit evidence awaits semantic review.
    for related in _user_memories(session, memory.tenant_id, memory.subject_id):
        if (related.cognitive_type == "fact" and related.status in {"active", "disputed"}
                and related.effective_at <= now
                and (related.expired_at is None or related.expired_at > now)
                and _scope(related) == _scope(memory)):
            entries.extend(evidence_fingerprint(session, related))
    return hashlib.sha256(
        json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _eligible_inference(session: Session, memory: MemoryItem, now: datetime) -> bool:
    if not _eligible_fact(memory, now):
        return False
    # Known unresolved contradictions block inferred profiles in the relevant scope.
    if any(
        _scope(other) == _scope(memory)
        for other in session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == memory.tenant_id,
                MemoryItem.subject_type == "user",
                MemoryItem.subject_id == memory.subject_id,
                MemoryItem.status == "disputed",
                MemoryItem.cognitive_type == "fact",
                MemoryItem.effective_at <= now,
                or_(MemoryItem.expired_at.is_(None), MemoryItem.expired_at > now),
                MemoryItem.metadata_json["temporal_kind"].as_string().is_distinct_from("historical"),
            )
        )
    ):
        return False
    groups: dict[str, list[MemoryEvidence]] = defaultdict(list)
    for edge in session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.tenant_id == memory.tenant_id,
            MemoryEvidence.downstream_memory_id == memory.id,
            MemoryEvidence.relationship_type == "derives",
        )
    ):
        groups[edge.evidence_group_id].append(edge)
    for group, edges in groups.items():
        if not evidence_group_is_complete(session, group, now=now):
            continue
        bases = {
            edge.upstream_memory_id: session.get(MemoryItem, edge.upstream_memory_id)
            for edge in edges
        }
        if len(bases) < 2 or any(
            basis is None
            or basis.cognitive_type != "fact"
            or not _eligible_fact(basis, now)
            or _scope(basis) != _scope(memory)
            for basis in bases.values()
        ):
            continue
        hashes, contexts = set(), set()
        from memory_cmic.consolidation import _valid_sources

        for basis in bases.values():
            source_edges = session.scalars(
                select(MemoryEvidence).where(
                    MemoryEvidence.tenant_id == memory.tenant_id,
                    MemoryEvidence.downstream_memory_id == basis.id,
                )
            ).all()
            for source in _valid_sources(session, source_edges):
                if (
                    source.author_type == "user"
                    and (source.user_id or source.author_id) == memory.subject_id
                ):
                    hashes.add(source.content_hash)
                    if source.session_id:
                        contexts.add((source.source_system, source.session_id))
        if len(hashes) >= 2 and len(contexts) >= 2:
            return True
    return False


def _snapshot(
    session: Session,
    tenant_id: str,
    user_id: str,
    *,
    allow_inferred: bool | None = None,
) -> tuple[str, list[MemoryItem]]:
    allow_inferred = inference_profiles_enabled() if allow_inferred is None else allow_inferred
    now = datetime.now(UTC)
    eligible = []
    fingerprint = []
    for memory in _user_memories(session, tenant_id, user_id):
        if not _eligible_fact(memory, now) or not memory_has_valid_evidence(
            session,
            memory,
            tenant_id=tenant_id,
            user_id=user_id,
            now=now,
        ):
            continue
        if memory.cognitive_type == "inference" and (
            not allow_inferred or not _eligible_inference(session, memory, now)
        ):
            continue
        eligible.append(memory)
        fingerprint.extend(evidence_fingerprint(session, memory))
    fingerprint.append(("policy", PROFILE_POLICY_VERSION, allow_inferred))
    digest = hashlib.sha256(
        json.dumps(
            fingerprint,
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return digest, eligible


def _profile_state(session: Session, tenant_id: str, user_id: str) -> GovernanceSubjectState:
    session.execute(
        pg_insert(GovernanceSubjectState)
        .values(
            tenant_id=tenant_id,
            user_id=user_id,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "user_id"])
    )
    return session.get(GovernanceSubjectState, (tenant_id, user_id))


def _due_at(state: GovernanceSubjectState, now: datetime) -> datetime:
    return max(
        now + timedelta(seconds=1),
        min(
            (state.profile_last_change_at or now) + PROFILE_QUIET,
            (state.profile_first_dirty_at or now) + PROFILE_MAX_WAIT,
        ),
    )


def _merge_triggers(task: MemoryTask, followup: MemoryTask) -> None:
    triggers = list(task.payload.get("trigger_correlation_ids", []))
    for trigger in [followup.correlation_id, *followup.payload.get("trigger_correlation_ids", [])]:
        if trigger and trigger not in triggers:
            triggers.append(trigger)
    task.payload = {**task.payload, "trigger_correlation_ids": triggers}


def _next_activation(session: Session, tenant_id: str, user_id: str,
                     allow_inferred: bool, now: datetime) -> datetime | None:
    times = []
    for memory in session.scalars(select(MemoryItem).where(
        MemoryItem.tenant_id == tenant_id, MemoryItem.subject_type == "user",
        MemoryItem.subject_id == user_id, MemoryItem.cognitive_type.in_(["fact", "inference"]),
        MemoryItem.status == "active", MemoryItem.effective_at > now,
    )):
        when = memory.effective_at
        if (not _eligible_fact(memory, when) or not memory_has_valid_evidence(
            session, memory, tenant_id=tenant_id, user_id=user_id, now=when,
        ) or memory.cognitive_type == "inference" and (
            not allow_inferred or not _eligible_inference(session, memory, when)
        )):
            continue
        times.append(when)
    return min(times) if times else None


def enqueue_profile_rebuild(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    correlation_id: str | None = None,
    force: bool = False,
    allow_inferred: bool | None = None,
) -> MemoryTask | None:
    lock_user(session, tenant_id, user_id)
    session.flush()
    allow_inferred = inference_profiles_enabled() if allow_inferred is None else allow_inferred
    digest, candidates = _snapshot(session, tenant_id, user_id, allow_inferred=allow_inferred)
    state = _profile_state(session, tenant_id, user_id)
    now = datetime.now(UTC)
    pending = session.scalars(
        select(MemoryTask)
        .where(
            MemoryTask.tenant_id == tenant_id,
            MemoryTask.task_type == "profile_rebuild",
            MemoryTask.user_id == user_id,
            MemoryTask.status == "pending",
        )
        .with_for_update()
    ).one_or_none()
    changed = state.profile_input != digest
    activation = _next_activation(session, tenant_id, user_id, allow_inferred, now)
    activation_only = False
    if not force and not changed:
        if pending is not None and correlation_id:
            triggers = list(pending.payload.get("trigger_correlation_ids", []))
            if correlation_id not in triggers:
                pending.payload = {**pending.payload,
                                   "trigger_correlation_ids": [*triggers, correlation_id]}
        if pending is not None and not pending.payload.get("activation_only"):
            return pending
        if activation is None:
            if pending is not None:
                pending.status = "cancelled"
                pending.completed_at = now
                state.profile_first_dirty_at = state.profile_last_change_at = None
                state.profile_evaluated_input = digest
            return None
        activation_only = True
    if (
        not force
        and not candidates
        and state.profile_input is None
        and activation is None
        and not session.scalar(
            select(ProfileProperty.id)
            .where(
                ProfileProperty.tenant_id == tenant_id,
                ProfileProperty.user_id == user_id,
            )
            .limit(1)
        )
    ):
        return None
    if not force and not candidates and activation is not None:
        activation_only = True
    if changed:
        state.profile_input = digest
        state.profile_last_change_at = now
        state.profile_first_dirty_at = min(state.profile_first_dirty_at or now, now)
    if activation_only:
        state.profile_first_dirty_at = state.profile_last_change_at = activation
    state.updated_at = now
    triggers = list((pending.payload or {}).get("trigger_correlation_ids", [])) if pending else []
    if correlation_id and correlation_id not in triggers:
        triggers.append(correlation_id)
    due = now if force else _due_at(state, now)
    payload = {
        "watermark": digest,
        "policy_version": PROFILE_POLICY_VERSION,
        "allow_inferred": allow_inferred,
        "activation_only": activation_only,
        "force": force or bool(pending and pending.payload.get("force")),
        "trigger_correlation_ids": triggers,
    }
    if pending:
        pending.payload = payload
        pending.available_at = now if payload["force"] else due
        return pending
    return enqueue_task(
        session,
        {
            "id": f"tsk_{uuid4().hex}",
            "tenant_id": tenant_id,
            "task_type": "profile_rebuild",
            "target_type": "subject",
            "target_id": user_id,
            "user_id": user_id,
            "idempotency_key": f"profile_rebuild:v4:{user_id}:{uuid4().hex}",
            "correlation_id": correlation_id or uuid4().hex,
            "payload": payload,
            "priority": 50,
            "available_at": due,
        },
    )


def _scope(memory: MemoryItem) -> tuple[str, ...] | None:
    return tuple(sorted(set(memory.business_domains))) if memory.business_domains else None


def _desired(
    memories: list[MemoryItem], proposals: list[ProfileProposal]
) -> dict[ProfileIdentity, tuple[str, list[MemoryItem]]]:
    by_id = {m.id: m for m in memories}
    grouped: dict[ProfileIdentity, dict[str, MemoryItem]] = defaultdict(dict)
    for item in proposals:
        memory = by_id.get(item.memory_id)
        value = item.value.strip()
        if (
            memory is None
            or not item.quote.strip()
            or item.quote not in memory.content
            or not value
        ):
            raise ProviderError("profile output is not grounded in an eligible memory")
        identity = (
            _scope(memory),
            value,
            memory.effective_at,
            memory.expired_at,
            "explicit" if memory.cognitive_type == "fact" else "inferred",
        )
        grouped[identity][memory.id] = memory
    explicit = {identity[:4] for identity in grouped if identity[4] == "explicit"}
    return {
        identity: (identity[1], list(basis.values()))
        for identity, basis in grouped.items()
        if identity[4] == "explicit" or identity[:4] not in explicit
    }


def _replace(
    session: Session,
    task: MemoryTask,
    desired: dict[ProfileIdentity, tuple[str, list[MemoryItem]]],
) -> dict[str, int]:
    now = datetime.now(UTC)
    counts = {"created": 0, "invalidated": 0, "retained": 0}
    profiles = session.scalars(
        select(ProfileProperty)
        .where(
            ProfileProperty.tenant_id == task.tenant_id,
            ProfileProperty.user_id == task.user_id,
            or_(
                ProfileProperty.property_key.is_(None),
                ProfileProperty.property_key.in_(LEGACY_KEYS),
            ),
        )
        .with_for_update()
    ).all()
    remaining = desired.copy()
    versions: dict[ProfileIdentity, int] = defaultdict(int)
    identities = {
        p.id: (_scope(p), p.property_value, p.effective_at, p.expired_at, p.assertion_kind)
        for p in profiles
        if p.property_key is None and isinstance(p.property_value, str)
    }
    for profile in profiles:
        identity = identities.get(profile.id)
        if identity is not None:
            versions[identity] = max(versions[identity], profile.version)
    for current in profiles:
        if current.status != "active":
            continue
        identity = identities.get(current.id)
        proposed = remaining.get(identity)
        old_edges = session.scalars(
            select(MemoryEvidence).where(
                MemoryEvidence.tenant_id == task.tenant_id,
                MemoryEvidence.downstream_profile_id == current.id,
                MemoryEvidence.status == "active",
            )
        ).all()
        if (
            proposed
            and current.property_value == proposed[0]
            and {e.upstream_memory_id for e in old_edges} == {m.id for m in proposed[1]}
            and all(
                evidence_group_is_complete(session, e.evidence_group_id, now=now) for e in old_edges
            )
        ):
            remaining.pop(identity)
            counts["retained"] += 1
            continue
        current.status = "invalidated"
        counts["invalidated"] += 1
        current.version += 1
        current.updated_at = now
        for edge in old_edges:
            edge.status = "invalidated"
            edge.invalidated_at = now
        record_audit(
            session,
            tenant_id=task.tenant_id,
            action="INVALIDATE",
            target_type="profile",
            target_id=current.id,
            operator_type="system",
            operator_id="memory_cmic",
            reason_code="PROFILE_REBUILT",
            task=task,
            state_before={"version": current.version - 1, "status": "active"},
            state_after={"version": current.version, "status": "invalidated"},
        )
        session.flush()
    for identity, (value, basis) in remaining.items():
        profile = ProfileProperty(
            id=f"prf_{uuid4().hex}",
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            business_domains=list(identity[0]) if identity[0] else None,
            property_key=None,
            property_value=value,
            value_type="string",
            assertion_kind=identity[4],
            confidence=1 if identity[4] == "explicit" else min(m.confidence for m in basis),
            version=versions[identity] + 1,
            effective_at=identity[2],
            expired_at=identity[3],
        )
        session.add(profile)
        counts["created"] += 1
        session.flush()
        for memory in basis:
            session.add(
                MemoryEvidence(
                    id=f"evi_{uuid4().hex}",
                    tenant_id=task.tenant_id,
                    relationship_type="profile_basis",
                    evidence_group_id=f"evg_{uuid4().hex}",
                    upstream_memory_id=memory.id,
                    downstream_profile_id=profile.id,
                    upstream_version=memory.version,
                    evidence_snippet=memory.content[:512],
                )
            )
        record_audit(
            session,
            tenant_id=task.tenant_id,
            action="INSERT",
            target_type="profile",
            target_id=profile.id,
            operator_type="system",
            operator_id="memory_cmic",
            reason_code="PROFILE_REBUILT",
            task=task,
            context={"basis_ids": [m.id for m in basis]},
            state_after={
                "key": profile.property_key,
                "value": value,
                "version": profile.version,
                "status": profile.status,
            },
        )
    return counts


def read_profiles(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    business_domain: str | None = None,
    now: datetime | None = None,
    include_inferred: bool = False,
) -> list[ProfileProperty]:
    check_time = now or datetime.now(UTC)
    business_filter = True
    if business_domain is not None:
        business_domain = normalize_business_domain(business_domain)
        business_filter = or_(
            ProfileProperty.business_domains.is_(None),
            ProfileProperty.business_domains.any(business_domain),
        )
    profiles = session.scalars(
        select(ProfileProperty)
        .where(
            ProfileProperty.tenant_id == tenant_id,
            ProfileProperty.user_id == user_id,
            ProfileProperty.status == "active",
            ProfileProperty.effective_at <= check_time,
            or_(ProfileProperty.expired_at.is_(None), ProfileProperty.expired_at > check_time),
            business_filter,
            True if include_inferred else ProfileProperty.assertion_kind == "explicit",
        )
        .order_by(
            ProfileProperty.property_key.nulls_last(),
            ProfileProperty.property_value,
            ProfileProperty.id,
        )
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
            valid = bool(
                memory is not None
                and _eligible_fact(memory, check_time)
                and memory.cognitive_type
                == ("fact" if profile.assertion_kind == "explicit" else "inference")
                and (profile.property_key is not None or _scope(memory) == _scope(profile))
                and memory_has_valid_evidence(
                    session, memory, tenant_id=tenant_id, user_id=user_id, now=check_time
                )
            )
            if valid and profile.assertion_kind == "inferred":
                approval = (memory.metadata_json or {}).get("profile_admission", {})
                valid = (
                    _eligible_inference(session, memory, check_time)
                    and approval.get("accepted") is True
                    and approval.get("policy_version") == PROFILE_POLICY_VERSION
                    and approval.get("fingerprint") == _basis_fingerprint(
                        session, memory, now=check_time)
                )
            source_valid[edge.id] = valid
        if any(
            evidence_group_is_complete(session, group, now=check_time)
            and all(source_valid[edge.id] for edge in edges if edge.evidence_group_id == group)
            for group in {edge.evidence_group_id for edge in edges}
        ):
            result.append(profile)
    return result


def run_once(
    session_factory: sessionmaker[Session],
    *,
    worker_id: str,
    model: ProfileModel,
    allow_inferred: bool | None = None,
) -> bool:
    with session_factory.begin() as session:
        finish_exhausted_leases(session)
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
                    selected_allow_inferred = task.payload.get("allow_inferred", allow_inferred)
                    snapshot, memories = _snapshot(
                        session, tenant_id, user_id, allow_inferred=selected_allow_inferred
                    )
                    inputs = [
                        {"id": m.id, "content": m.content, "cognitive_type": m.cognitive_type}
                        for m in memories
                    ]
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
                with session_factory.begin() as session:
                    task = _leased_task(session, **ownership)
                    lock_user(session, tenant_id, user_id)
                    current, current_memories = _snapshot(
                        session, tenant_id, user_id, allow_inferred=selected_allow_inferred
                    )
                    if current != snapshot:
                        # Merge a queued follow-up into this attempt, retaining the oldest wait.
                        state = _profile_state(session, tenant_id, user_id)
                        now = datetime.now(UTC)
                        state.profile_input = current
                        state.profile_first_dirty_at = state.profile_first_dirty_at or now
                        state.profile_last_change_at = now
                        for followup in session.scalars(
                            select(MemoryTask).where(
                                MemoryTask.tenant_id == tenant_id,
                                MemoryTask.user_id == user_id,
                                MemoryTask.task_type == "profile_rebuild",
                                MemoryTask.status == "pending",
                            )
                        ):
                            _merge_triggers(task, followup)
                            followup.status = "cancelled"
                            followup.completed_at = now
                        session.flush()
                        task.status = "pending"
                        task.worker_id = task.locked_until = task.lease_token = None
                        task.available_at = _due_at(state, now)
                        task.last_error = None
                        task.error_json = None
                        task.payload = {
                            **task.payload,
                            "watermark": current,
                            "force": False,
                            "deferred_reason": "INPUT_CHANGED",
                        }
                        return True
                    task.user_id = user_id
                    desired = _desired(current_memories, proposals)
                    accepted = {m.id for _, basis in desired.values() for m in basis}
                    for memory in current_memories:
                        if memory.cognitive_type == "inference":
                            memory.metadata_json = {
                                **(memory.metadata_json or {}),
                                "profile_admission": {
                                    "accepted": memory.id in accepted,
                                    "policy_version": PROFILE_POLICY_VERSION,
                                    "fingerprint": _basis_fingerprint(session, memory),
                                    "reason": "DURABLE_PROFILE"
                                    if memory.id in accepted
                                    else "NO_DURABLE_PROFILE",
                                    "evaluated_at": datetime.now(UTC).isoformat(),
                                },
                            }
                    counts = _replace(session, task, desired)
                    state = _profile_state(session, tenant_id, user_id)
                    state.profile_input = state.profile_evaluated_input = current
                    state.profile_first_dirty_at = state.profile_last_change_at = None
                    # Follow-ups for the same input are covered by this successful result.
                    for followup in session.scalars(
                        select(MemoryTask).where(
                            MemoryTask.tenant_id == tenant_id,
                            MemoryTask.user_id == user_id,
                            MemoryTask.task_type == "profile_rebuild",
                            MemoryTask.status == "pending",
                        )
                    ):
                        if not followup.payload.get("force"):
                            _merge_triggers(task, followup)
                            followup.status = "cancelled"
                            followup.completed_at = datetime.now(UTC)
                    if not finish_task(
                        session,
                        **ownership,
                        status="succeeded",
                        results=[
                            {
                                "kind": "profile_rebuild",
                                "input": current,
                                "properties": len(desired),
                                **counts,
                            }
                        ],
                    ):
                        raise LeaseLostError("task lease was lost")
                    enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id=user_id,
                                            allow_inferred=selected_allow_inferred,
                                            correlation_id=task.correlation_id)
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
