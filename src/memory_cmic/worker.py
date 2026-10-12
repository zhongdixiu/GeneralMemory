from __future__ import annotations

import argparse
import hashlib
import json
import logging
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from memory_cmic.audit import record_audit
from memory_cmic.business_scenes import normalize_fact_scenes
from memory_cmic.db import create_database_engine
from memory_cmic.extraction_input import (
    ExtractionBudget,
    build_slices,
    input_size,
    select_history,
    split_slice,
)
from memory_cmic.governance_state import mark_dirty
from memory_cmic.lifecycle import govern_memory
from memory_cmic.models import (
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)
from memory_cmic.providers import (
    EXTRACTION_PROMPT,
    GOVERNANCE_PROMPT,
    PROMPT_VERSION,
    Embedder,
    ExtractedFact,
    ExtractionAssessment,
    FactModel,
    GovernanceDecision,
    OutputLimitError,
    ProviderError,
    QwenFactModel,
    SiliconFlowEmbedder,
    candidate_skip_reason,
    validate_fact_evidence,
)
from memory_cmic.settings import Settings
from memory_cmic.task_queue import (
    claim_tasks,
    fail_and_retry_task,
    finish_exhausted_leases,
    finish_task,
    renew_task_lease,
)
from memory_cmic.ttl import calculate_expired_at, explicit_expired_at

logger = logging.getLogger(__name__)


class WorkerInputError(RuntimeError):
    def __init__(self, message: str, *, committed_changes: bool = False):
        super().__init__(message)
        self.committed_changes = committed_changes


class LeaseLostError(RuntimeError):
    pass


class ModelCallBudgetError(WorkerInputError):
    pass


@dataclass(frozen=True)
class TaskInput:
    task_id: str
    tenant_id: str
    caller_agent_id: str
    user_id: str
    source_system: str
    session_id: str
    batch_seq: int
    targets: list[dict[str, Any]]
    history: list[dict[str, Any]]
    source_ids: dict[str, str]
    task_signature: str
    associations: tuple[tuple[str, str, int, int], ...]
    source_states: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class FactDecision:
    fact: ExtractedFact
    embedding: list[float]
    governance: GovernanceDecision
    scope_fingerprint: str
    memory_id: str
    conflict_group_id: str | None
    skip_reason: str | None = None


@contextmanager
def _lease_heartbeat(
    session_factory: sessionmaker[Session],
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    lease_token: str,
    lease_seconds: int,
) -> Iterator[None]:
    stopping = Event()

    def renew():
        while not stopping.wait(lease_seconds / 3):
            try:
                with session_factory.begin() as session:
                    renewed = renew_task_lease(
                        session,
                        tenant_id=tenant_id,
                        task_id=task_id,
                        worker_id=worker_id,
                        lease_token=lease_token,
                        lease_seconds=lease_seconds,
                    )
                if not renewed:
                    return
            except Exception:
                logger.exception("lease renewal failed for task %s", task_id)
                return

    thread = Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopping.set()
        thread.join()


def _normalized_fact(value: str) -> str:
    return " ".join(value.casefold().split())


def _fact_hash(value: str) -> str:
    return hashlib.sha256(_normalized_fact(value).encode()).hexdigest()


def _memory_content_hash(summary: str, content: str) -> str:
    return hashlib.sha256(f"{summary}\n{content}".encode()).hexdigest()


def _source_message(source: SourceRecord) -> dict[str, Any]:
    metadata = source.metadata_json or {}
    return {
        "message_id": source.message_id,
        "role": metadata.get("role"),
        "content": source.raw_content,
        "occurred_at": metadata.get("original_occurred_at", source.occurred_at.isoformat()),
        "author_id": source.author_id,
        "tool_status": metadata.get("tool_status"),
        "reply_to_message_id": metadata.get("reply_to_message_id"),
        "turn_id": metadata.get("turn_id"),
        "timestamp_origin": metadata.get("timestamp_origin", "source"),
        "tool_name": metadata.get("tool_name"),
        "tool_call_id": metadata.get("tool_call_id"),
        "tool_calls": metadata.get("tool_calls", []),
        "attachments": metadata.get("attachments", []),
    }


def _load_task_input(session: Session, task: MemoryTask) -> TaskInput:
    if not all(
        [
            task.caller_agent_id,
            task.user_id,
            task.source_system,
            task.session_id,
            task.batch_seq is not None,
        ]
    ):
        raise WorkerInputError("fact extraction task is missing conversation identity")

    associations = session.execute(
        select(MemoryTaskSource, SourceRecord)
        .join(
            SourceRecord,
            (SourceRecord.tenant_id == MemoryTaskSource.tenant_id)
            & (SourceRecord.id == MemoryTaskSource.source_id),
        )
        .where(
            MemoryTaskSource.tenant_id == task.tenant_id,
            MemoryTaskSource.task_id == task.id,
        )
        .order_by(MemoryTaskSource.kind, MemoryTaskSource.position)
    ).all()
    target_rows = [row for row in associations if row[0].kind == "target"]
    history_rows = [row for row in associations if row[0].kind == "history"]
    if not target_rows:
        raise WorkerInputError("fact extraction task has no target sources")

    targets = []
    repeated = []
    for association, source in target_rows:
        if source.status != "active" or source.version != association.source_version:
            raise WorkerInputError("target source is unavailable")
        if source.message_id is None:
            raise WorkerInputError("target source has no message_id")
        if source.processed_version == source.version:
            association.status = "skipped"
            repeated.append(source)
        else:
            targets.append(_source_message(source))

    # A task boundary is its accepted batch sequence, independent of extraction success.
    query = (
        select(MemoryTaskSource, SourceRecord, MemoryTask.batch_seq)
        .join(SourceRecord, (SourceRecord.tenant_id == MemoryTaskSource.tenant_id)
              & (SourceRecord.id == MemoryTaskSource.source_id))
        .join(MemoryTask, (MemoryTask.tenant_id == MemoryTaskSource.tenant_id)
              & (MemoryTask.id == MemoryTaskSource.task_id))
        .where(
            MemoryTask.tenant_id == task.tenant_id,
            MemoryTask.caller_agent_id == task.caller_agent_id,
            MemoryTask.source_system == task.source_system,
            MemoryTask.user_id == task.user_id,
            MemoryTask.session_id == task.session_id,
            MemoryTask.batch_seq < task.batch_seq,
            SourceRecord.caller_agent_id == task.caller_agent_id,
            SourceRecord.status == "active",
            SourceRecord.version == MemoryTaskSource.source_version,
        )
    )
    first_association = aliased(MemoryTaskSource)
    first_task = aliased(MemoryTask)
    first_target_batch = (
        select(func.min(first_task.batch_seq))
        .join(first_association, first_association.task_id == first_task.id)
        .where(first_association.source_id == SourceRecord.id,
               first_association.kind == "target")
        .correlate(SourceRecord).scalar_subquery()
    )
    recent = session.execute(
        query.where(MemoryTaskSource.kind == "target",
                    MemoryTask.batch_seq == first_target_batch)
        .order_by(MemoryTask.batch_seq.desc(), MemoryTaskSource.position.desc()).limit(30)
    ).all()
    anchor = aliased(MemoryTaskSource)
    anchor_source = aliased(SourceRecord)
    anchored_history = (
        select(anchor.source_id)
        .join(anchor_source, anchor_source.id == anchor.source_id)
        .where(
            anchor.task_id == MemoryTaskSource.task_id,
            anchor.kind == "target",
            anchor.source_version == anchor_source.version,
            anchor_source.metadata_json["timestamp_origin"].as_string() == "source",
            anchor_source.occurred_at >= SourceRecord.occurred_at,
        ).correlate(MemoryTaskSource, SourceRecord).exists()
    )
    supplements = session.execute(
        query.where(MemoryTaskSource.kind == "history",
                    SourceRecord.metadata_json["timestamp_origin"].as_string() == "source",
                    anchored_history)
        .order_by(SourceRecord.occurred_at.desc()).limit(30)
    ).all()
    explicit = [source for association, source in history_rows
                if source.status == "active" and source.version == association.source_version]
    selected = {source.message_id: (source, seq, association.position)
                for association, source, seq in reversed(recent)}
    for source in [*explicit, *repeated]:
        selected.setdefault(source.message_id, (source, task.batch_seq, -1))

    # Timestamped history is inserted before its chronological anchor, never appended as
    # a new utterance. Observed history remains explicit-only unless referenced later.
    for association, source, seq in supplements:
        selected.setdefault(source.message_id, (source, seq, association.position))

    target_source_ids = {source.id for _, source, _ in recent}
    pending = list(targets)
    examined = set()
    while pending:
        ids = {m.get("reply_to_message_id") for m in pending} - {None}
        turns = {m.get("turn_id") for m in pending} - {None}
        calls = {m.get("tool_call_id") for m in pending} - {None}
        calls.update(call["tool_call_id"] for m in pending for call in m.get("tool_calls", []))
        conditions = [SourceRecord.message_id.in_(ids)]
        if turns:
            conditions.append(SourceRecord.metadata_json["turn_id"].as_string().in_(turns))
        if calls:
            conditions.extend([
                SourceRecord.metadata_json["tool_call_id"].as_string().in_(calls),
                *[SourceRecord.metadata_json["tool_calls"].contains(
                    [{"tool_call_id": call}]) for call in calls],
            ])
        linked = session.execute(query.where(or_(*conditions)).order_by(
            MemoryTask.batch_seq, MemoryTaskSource.position)).all()
        pending = []
        for association, source, seq in linked:
            if association.kind == "target":
                target_source_ids.add(source.id)
            selected.setdefault(source.message_id, (source, seq, association.position))
            if source.id not in examined:
                examined.add(source.id)
                pending.append(_source_message(source))

    prior_ids = set(selected)
    for message in targets:
        reply = message.get("reply_to_message_id")
        if reply and reply not in prior_ids:
            message["context_gaps"] = [{"code": "UNKNOWN_REPLY", "message_id": reply}]
        prior_ids.add(message["message_id"])
    target_ids = {m["message_id"] for m in targets}
    recent_ids = {row[1].id for row in recent}
    ordered_targets = sorted(
        (item for item in selected.values() if item[0].id in recent_ids),
        key=lambda item: (item[1], item[2]),
    )
    def context_order(item):
        source, seq, position = item
        if source.id in target_source_ids or source in repeated:
            return (seq, position, 1, "", 0)
        if (source.metadata_json or {}).get("timestamp_origin", "source") == "source":
            following = next((row for row in ordered_targets
                              if row[0].occurred_at >= source.occurred_at), None)
            if following:
                return (following[1], following[2], 0, source.occurred_at.isoformat(), position)
            if ordered_targets and source.occurred_at < ordered_targets[0][0].occurred_at:
                return (0, -1, 0, source.occurred_at.isoformat(), position)
        return (seq, -1, 0, source.occurred_at.isoformat(), position)

    history = [_source_message(source) for source, _, _ in
               sorted(selected.values(), key=context_order)
               if source.message_id not in target_ids]
    used_sources = [source for _, source in target_rows]
    used_sources.extend(source for source, _, _ in selected.values())
    source_ids = {source.message_id: source.id for source in used_sources}
    return TaskInput(
        task_id=task.id,
        tenant_id=task.tenant_id,
        caller_agent_id=task.caller_agent_id,
        user_id=task.user_id,
        source_system=task.source_system,
        session_id=task.session_id,
        batch_seq=task.batch_seq,
        targets=targets,
        history=history,
        source_ids=source_ids,
        task_signature=json.dumps(
            {
                "type": task.task_type,
                "target_type": task.target_type,
                "target_id": task.target_id,
                "input_version": task.input_version,
                "payload": {k: v for k, v in task.payload.items() if k != "execution"},
            },
            sort_keys=True,
        ),
        source_states={
            source.id: {
                "status": source.status,
                "version": source.version,
                "message": _source_message(source),
            }
            for source in used_sources
        },
        associations=tuple(
            (
                association.source_id,
                association.kind,
                association.position,
                association.source_version,
            )
            for association, _ in associations
        ),
    )


def _similar_candidates(
    session: Session,
    *,
    task_input: TaskInput,
    model_id: str,
    embedding: list[float],
    fact: ExtractedFact,
    limit: int = 5,
) -> list[tuple[MemoryItem, float]]:
    distance = MemoryEmbedding.embedding.cosine_distance(embedding).label("distance")
    current_hash = func.encode(
        func.sha256(
            func.convert_to(func.concat(MemoryItem.summary, "\n", MemoryItem.content), "UTF8")
        ),
        "hex",
    )
    rows = session.execute(
        select(MemoryItem, distance)
        .join(
            MemoryEmbedding,
            (MemoryEmbedding.tenant_id == MemoryItem.tenant_id)
            & (MemoryEmbedding.memory_id == MemoryItem.id),
        )
        .where(
            MemoryItem.tenant_id == task_input.tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == task_input.user_id,
            MemoryItem.cognitive_type == "fact",
            MemoryItem.status.in_(["active", "disputed"]),
            *_scope_filters(fact),
            or_(MemoryItem.expired_at.is_(None), MemoryItem.expired_at > datetime.now(UTC)),
            MemoryEmbedding.model_id == model_id,
            MemoryEmbedding.status == "active",
            MemoryEmbedding.content_hash == current_hash,
        )
        .order_by(distance)
        .limit(limit)
    ).all()
    return [(memory, 1.0 - float(value)) for memory, value in rows]


def _add_evidence(
    session: Session,
    *,
    task_input: TaskInput,
    fact: ExtractedFact,
    memory_id: str,
    initial: bool = False,
) -> tuple[str, bool]:
    evidence_count = session.scalar(select(func.count()).select_from(MemoryEvidence).where(
        MemoryEvidence.tenant_id == task_input.tenant_id,
        MemoryEvidence.downstream_memory_id == memory_id,
        MemoryEvidence.status == "active",
    )) if not initial else 0
    support = []
    for message_id in dict.fromkeys(fact.message_ids):
        source_id = task_input.source_ids[message_id]
        state = task_input.source_states[source_id]
        pieces = [m for m in [*task_input.history, *task_input.targets]
                  if m["message_id"] == message_id]
        start = min(m.get("start_char", 0) for m in pieces)
        end = max(m.get("end_char", len(m["content"])) for m in pieces)
        support.append((source_id, state["version"], start, end))
    group_id = "evg_" + hashlib.sha256(json.dumps(
        [memory_id, sorted(support)], sort_keys=True).encode()).hexdigest()[:60]
    if session.scalar(select(MemoryEvidence.id).where(
        MemoryEvidence.tenant_id == task_input.tenant_id,
        MemoryEvidence.evidence_group_id == group_id,
    ).limit(1)):
        return group_id, False
    for message_id, (source_id, version, start, end) in zip(
        dict.fromkeys(fact.message_ids), support, strict=True
    ):
        source = session.get(SourceRecord, source_id)
        session.add(
            MemoryEvidence(
                id=f"evi_{uuid4().hex}",
                tenant_id=task_input.tenant_id,
                relationship_type="supports",
                evidence_group_id=group_id,
                upstream_source_id=source_id,
                downstream_memory_id=memory_id,
                upstream_version=version,
                evidence_snippet=source.raw_content[start:end],
                evidence_locator={
                    "message_id": message_id,
                    "start_char": start,
                    "end_char": end,
                },
            )
        )

    task = session.get(MemoryTask, task_input.task_id)
    if not initial:
        record_audit(
            session,
            tenant_id=task_input.tenant_id,
            action="UPDATE",
            target_type="memory",
            target_id=memory_id,
            reason_code="FACT_EVIDENCE_ADDED",
            task=task,
            state_before={"evidence_count": evidence_count},
            state_after={"evidence_count": evidence_count + len(set(fact.message_ids))},
            context={
                "evidence_group_id": group_id,
                "task_id": task_input.task_id,
                "message_ids": fact.message_ids,
            },
        )
    mark_dirty(session, session.get(MemoryItem, memory_id), from_add=True)
    return group_id, True


def _scope_filters(fact: ExtractedFact):
    return (
        MemoryItem.business_domains.is_(None)
        if fact.business_domains is None
        else MemoryItem.business_domains.is_not(None),
        MemoryItem.project_domains.is_(None),
        MemoryItem.metadata_json["project_context"].as_string().is_(None)
        if fact.project_context is None
        else MemoryItem.metadata_json["project_context"].as_string() == fact.project_context,
    )


def _compatible_business(fact: ExtractedFact, candidate: dict[str, Any]) -> bool:
    labels = candidate.get("business_domains")
    if (fact.business_domains is None) != (labels is None):
        return False
    initial = {"communication", "email", "disk"}
    # Unknown/new tags need semantic comparison; known distinct initial scopes do not.
    return not (
        fact.business_domains and labels
        and set(fact.business_domains).issubset(initial)
        and set(labels).issubset(initial)
        and set(fact.business_domains) != set(labels)
    )


def _scope_memories(session: Session, task_input: TaskInput, fact: ExtractedFact):
    return session.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == task_input.tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == task_input.user_id,
            MemoryItem.cognitive_type == "fact",
            MemoryItem.status.in_(["active", "disputed"]),
            *_scope_filters(fact),
            or_(MemoryItem.expired_at.is_(None), MemoryItem.expired_at > datetime.now(UTC)),
        )
        .order_by(MemoryItem.id)
    ).all()


def _fingerprint(memories: list[MemoryItem]) -> str:
    values = [
        {
            "id": m.id,
            "version": m.version,
            "content": m.content,
            "summary": m.summary,
            "status": m.status,
            "metadata": m.metadata_json,
            "group": m.conflict_group_id,
            "effective_at": m.effective_at.isoformat(),
            "expired_at": m.expired_at.isoformat() if m.expired_at else None,
        }
        for m in memories
    ]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _fact_time(fact: ExtractedFact, task_input: TaskInput) -> datetime:
    return fact.effective_at or max(
        datetime.fromisoformat(m["occurred_at"])
        for m in task_input.targets
        if m["message_id"] in fact.message_ids and m["role"] == "user"
    )


def _check_governance(
    decision: GovernanceDecision,
    fact: ExtractedFact,
    candidates: list[dict[str, Any]],
    task_input: TaskInput,
) -> None:
    by_id = {m["id"]: m for m in candidates}
    references = set(decision.memory_ids)
    if decision.historical_before_id:
        references.add(decision.historical_before_id)
    if not references.issubset(by_id):
        raise ProviderError("governance output cites an unknown memory ID")
    if decision.historical_before_id:
        boundary = by_id[decision.historical_before_id]
        source_time = max(
            datetime.fromisoformat(m["occurred_at"])
            for m in task_input.targets
            if m["message_id"] in fact.message_ids and m["role"] == "user"
        )
        newer_source = datetime.fromisoformat(
            boundary.get("source_occurred_at") or boundary["effective_at"]
        )
        if source_time >= newer_source:
            raise ProviderError("historical boundary cannot downgrade a newer assertion")
        if (
            fact.temporal_kind != "current"
            or boundary["temporal_kind"] != "current"
            or _fact_time(fact, task_input) >= datetime.fromisoformat(boundary["effective_at"])
        ):
            raise ProviderError(
                "historical boundary must follow the incoming current-state assertion"
            )
    selected = [by_id[mid] for mid in decision.memory_ids]
    for candidate in [by_id[mid] for mid in references]:
        if "business_domains" in candidate and not _compatible_business(fact, candidate):
            raise ProviderError("governance cannot cross business applicability")
        if candidate.get("project_context") != fact.project_context:
            raise ProviderError("governance cannot cross project context")
    if decision.action == "DUPLICATE":
        candidate = selected[0]
        if candidate["status"] != "active":
            raise ProviderError("disputed memory cannot be a duplicate")
        if candidate.get("temporal_kind", "current") != fact.temporal_kind or candidate.get(
            "explicit_expired_at", candidate["expired_at"]
        ) != (fact.expired_at.isoformat() if fact.expired_at else None):
            raise ProviderError("duplicate has different temporal applicability")
    if decision.action == "SUPERSEDE":
        if fact.change_kind == "none" or fact.temporal_kind == "historical":
            raise ProviderError("supersession requires an explicit current-state change")
        cited = next(
            (
                m
                for m in task_input.targets
                if m["message_id"] == decision.justification_message_id
                and m["role"] == "user"
                and m["message_id"] in fact.message_ids
            ),
            None,
        )
        if (
            cited is None
            or not decision.justification_quote
            or decision.justification_quote not in cited["content"]
        ):
            raise ProviderError("supersession requires a verbatim target user justification")
        incoming = datetime.fromisoformat(cited["occurred_at"])
        for candidate in selected:
            if candidate.get("temporal_kind", "current") == "historical":
                raise ProviderError("supersession cannot erase a historical interval")
            if cited.get("timestamp_origin") == "adapter_observed" or candidate.get(
                "timestamp_origin") == "adapter_observed":
                raise ProviderError("observed timestamps cannot establish supersession recency")
            source_time = candidate.get("source_occurred_at") or candidate["effective_at"]
            if incoming < datetime.fromisoformat(source_time):
                raise ProviderError("late input cannot supersede a newer fact")
            if fact.change_kind == "update" and _fact_time(
                fact, task_input
            ) < datetime.fromisoformat(candidate["effective_at"]):
                raise ProviderError("state update cannot move backwards in time")
            if candidate["status"] == "disputed" and fact.change_kind != "clarification":
                raise ProviderError("dispute recovery requires explicit user clarification")
    if decision.action == "DISPUTE":
        for candidate in selected:
            start = datetime.fromisoformat(candidate["effective_at"])
            end = (
                datetime.fromisoformat(candidate["expired_at"]) if candidate["expired_at"] else None
            )
            if (fact.expired_at is not None and fact.expired_at <= start) or (
                end is not None and end <= _fact_time(fact, task_input)
            ):
                raise ProviderError("non-overlapping historical periods cannot be disputed")
    if decision.action in {"SUPERSEDE", "DISPUTE"}:
        groups = {m["conflict_group_id"] for m in selected if m["conflict_group_id"]}
        group_ids = {m["id"] for m in candidates if m["conflict_group_id"] in groups}
        if not group_ids.issubset(decision.memory_ids):
            raise ProviderError("governance must include every member of a selected conflict group")


def _candidate(memory: MemoryItem) -> dict[str, Any]:
    return {
        "id": memory.id,
        "memory": memory.content,
        "business_domains": memory.business_domains,
        "project_context": (memory.metadata_json or {}).get("project_context"),
        "status": memory.status,
        "effective_at": memory.effective_at.isoformat(),
        "expired_at": memory.expired_at.isoformat() if memory.expired_at else None,
        "explicit_expired_at": explicit_expired_at(memory),
        "conflict_group_id": memory.conflict_group_id,
        "temporal_kind": (memory.metadata_json or {}).get("temporal_kind", "current"),
        "source_occurred_at": (memory.metadata_json or {}).get("source_occurred_at"),
        "timestamp_origin": (memory.metadata_json or {}).get("timestamp_origin", "source"),
    }


def _plan_decisions(
    session_factory: sessionmaker[Session],
    *,
    task_input: TaskInput,
    facts: list[ExtractedFact],
    embeddings: list[list[float]],
    fact_model: FactModel,
    embedder: Embedder,
    duplicate_threshold: float,
    consume_call,
    budget: ExtractionBudget,
) -> list[FactDecision]:
    decisions = []
    working = {}
    fingerprints = {}
    for fact, embedding in zip(facts, embeddings, strict=True):
        scope = (fact.business_domains is not None, fact.project_context)
        with session_factory() as session:
            if scope not in working:
                memories = _scope_memories(session, task_input, fact)
                fingerprints[scope] = _fingerprint(memories)
                working[scope] = [
                    _candidate(m) for m in sorted(memories, key=lambda m: m.updated_at)
                ]
            available = working[scope]
            eligible = [m for m in available if _compatible_business(fact, m)]
            exact = next(
                (
                    m
                    for m in eligible
                    if m.get("business_domains") == fact.business_domains
                    and m["status"] == "active"
                    and _fact_hash(m["memory"]) == _fact_hash(fact.memory)
                    and m["temporal_kind"] == fact.temporal_kind
                    and m["explicit_expired_at"]
                    == (fact.expired_at.isoformat() if fact.expired_at else None)
                    and (
                        fact.temporal_kind != "historical"
                        or m["effective_at"] == _fact_time(fact, task_input).isoformat()
                    )
                ),
                None,
            )
            if exact is not None:
                decision = GovernanceDecision(action="DUPLICATE", memory_ids=[exact["id"]])
            else:
                similar = _similar_candidates(
                    session,
                    task_input=task_input,
                    model_id=embedder.model_id,
                    embedding=embedding,
                    fact=fact,
                )
                routed_ids = {m.id for m, score in similar if score >= duplicate_threshold}
                selected = {
                    m["id"]: m for m in eligible if m["id"] in routed_ids or m in eligible[-10:]
                }
                groups = {
                    m["conflict_group_id"] for m in selected.values() if m["conflict_group_id"]
                }
                selected.update({m["id"]: m for m in available if m["conflict_group_id"] in groups})
                candidates = list(selected.values())
                decision = None
        if decision is None:
            if candidates:
                payload = {
                    "fact": fact.model_dump(mode="json"),
                    "candidates": candidates,
                    "targets": task_input.targets,
                }
                if (
                    input_size(GOVERNANCE_PROMPT, payload)
                    > budget.input_tokens - budget.output_tokens
                ):
                    raise WorkerInputError("governance input exceeds model budget")
                consume_call()
                decision = fact_model.resolve(
                    fact=fact, candidates=candidates, targets=task_input.targets
                )
                unknown_recency = any(
                    m.get("timestamp_origin") == "adapter_observed"
                    for m in task_input.targets if m["message_id"] in fact.message_ids
                ) or any(m.get("timestamp_origin") == "adapter_observed"
                         for m in candidates if m["id"] in {
                             *decision.memory_ids, decision.historical_before_id,
                         })
                if unknown_recency and (
                    decision.action in {"SUPERSEDE", "DISPUTE"} or decision.historical_before_id
                ):
                    decisions.append(FactDecision(
                        fact, embedding, decision, fingerprints[scope], "", None,
                        skip_reason="UNKNOWN_EVENT_TIME",
                    ))
                    continue
                _check_governance(decision, fact, candidates, task_input)
            else:
                decision = GovernanceDecision(action="ADD")
        if decision.historical_before_id:
            boundary = next(m for m in candidates if m["id"] == decision.historical_before_id)
            start_time = _fact_time(fact, task_input)
            fact = fact.model_copy(
                update={
                    "memory": f"在 {start_time.isoformat()} 时：{fact.memory}",
                    "temporal_kind": "historical",
                    "effective_at": start_time,
                    "expired_at": datetime.fromisoformat(boundary["effective_at"]),
                }
            )
        memory_id = f"mem_{uuid4().hex}"
        group = None
        if decision.action == "DISPUTE":
            group = next(
                (
                    m["conflict_group_id"]
                    for m in available
                    if m["id"] in decision.memory_ids and m["conflict_group_id"]
                ),
                f"cfg_{uuid4().hex}",
            )
        if decision.action != "DUPLICATE":
            if decision.action == "SUPERSEDE":
                available[:] = [m for m in available if m["id"] not in decision.memory_ids]
            elif decision.action == "DISPUTE":
                for m in available:
                    if m["id"] in decision.memory_ids:
                        m.update(status="disputed", conflict_group_id=group)
            expiry = calculate_expired_at(
                retention_kind=fact.retention_kind,
                effective_at=_fact_time(fact, task_input),
                explicit_expired_at=fact.expired_at,
            )
            new_candidate = {
                "id": memory_id,
                "memory": fact.memory,
                "business_domains": fact.business_domains,
                "project_context": fact.project_context,
                "status": "disputed" if group else "active",
                "effective_at": _fact_time(fact, task_input).isoformat(),
                "expired_at": expiry.isoformat() if expiry else None,
                "explicit_expired_at": fact.expired_at.isoformat() if fact.expired_at else None,
                "conflict_group_id": group,
                "temporal_kind": fact.temporal_kind,
                "source_occurred_at": max(
                    datetime.fromisoformat(m["occurred_at"])
                    for m in task_input.targets
                    if m["message_id"] in fact.message_ids and m["role"] == "user"
                ).isoformat(),
            }
            if expiry is None or expiry > datetime.now(UTC):
                available.append(new_candidate)
        decisions.append(
            FactDecision(fact, embedding, decision, fingerprints[scope], memory_id, group)
        )
    return decisions


def _commit_results(
    session: Session,
    *,
    task: MemoryTask,
    task_input: TaskInput,
    decisions: list[FactDecision],
    embedder: Embedder,
) -> tuple[list[dict[str, str]], bool, set[int]]:
    from memory_cmic.profile import enqueue_profile_rebuild

    results = []
    changed = False
    unchanged = set()
    session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"fact:{task_input.tenant_id}:{task_input.user_id}", 0)
            )
        )
    )
    # Check all scope snapshots before this slice mutates any memory.
    for decision in decisions:
        if decision.skip_reason:
            continue
        memories = _scope_memories(session, task_input, decision.fact)
        session.scalars(
            select(MemoryItem)
            .where(MemoryItem.id.in_([m.id for m in memories]))
            .order_by(MemoryItem.id)
            .with_for_update()
        ).all()
        for memory in memories:
            session.refresh(memory)
        if _fingerprint(memories) != decision.scope_fingerprint:
            raise ProviderError("governance candidates changed; slice must be retried")
    for index, decision in enumerate(decisions):
        if decision.skip_reason:
            continue
        fact, governance = decision.fact, decision.governance
        if governance.action == "DUPLICATE":
            _, added = _add_evidence(
                session, task_input=task_input, fact=fact, memory_id=governance.memory_ids[0]
            )
            changed = changed or added
            if not added:
                unchanged.add(index)
            continue
        changed = True
        existing = [session.get(MemoryItem, mid) for mid in governance.memory_ids]
        conflict_group = decision.conflict_group_id
        for old in existing:
            govern_memory(
                session,
                memory=old,
                status="disputed" if governance.action == "DISPUTE" else "invalidated",
                reason_code="FACT_DISPUTED"
                if governance.action == "DISPUTE"
                else "FACT_SUPERSEDED",
                correlation_id=task.correlation_id,
                task=task,
                reason=governance.justification_quote or None,
                conflict_group_id=conflict_group,
            )
            mark_dirty(session, old, from_add=True)
        effective_at = _fact_time(fact, task_input)
        expired_at = calculate_expired_at(
            retention_kind=fact.retention_kind,
            effective_at=effective_at,
            explicit_expired_at=fact.expired_at,
        )
        memory = MemoryItem(
            id=decision.memory_id,
            tenant_id=task_input.tenant_id,
            subject_type="user",
            subject_id=task_input.user_id,
            cognitive_type="fact",
            business_domains=fact.business_domains,
            project_domains=None,
            summary=fact.memory[:512],
            content=fact.memory,
            semantic_hash=_fact_hash(fact.memory),
            confidence=1,
            effective_at=effective_at,
            expired_at=expired_at,
            status="disputed"
            if conflict_group
            else "expired"
            if expired_at is not None and expired_at <= datetime.now(UTC)
            else "active",
            conflict_group_id=conflict_group,
            supersedes_id=existing[0].id if governance.action == "SUPERSEDE" else None,
            metadata_json={
                "project_limited": fact.project_limited,
                "project_context": fact.project_context,
                "temporal_kind": fact.temporal_kind,
                "retention_kind": fact.retention_kind,
                "explicit_expired_at": fact.expired_at.isoformat() if fact.expired_at else None,
                "source_occurred_at": max(
                    datetime.fromisoformat(m["occurred_at"])
                    for m in task_input.targets
                    if m["message_id"] in fact.message_ids and m["role"] == "user"
                ).isoformat(),
                "timestamp_origin": "adapter_observed" if any(
                    m.get("timestamp_origin") == "adapter_observed"
                    for m in task_input.targets
                    if m["message_id"] in fact.message_ids and m["role"] == "user"
                ) else "source",
                "governance_action": governance.action,
                "previous_memory_ids": governance.memory_ids,
            },
            created_by=task_input.caller_agent_id,
        )
        session.add(memory)
        session.flush()
        group_id, _ = _add_evidence(
            session, task_input=task_input, fact=fact, memory_id=memory.id, initial=True
        )
        if memory.status == "active" and (
            memory.expired_at is None or memory.expired_at > datetime.now(UTC)
        ):
            session.add(
                MemoryEmbedding(
                    tenant_id=task_input.tenant_id,
                    memory_id=memory.id,
                    model_id=embedder.model_id,
                    content_hash=_memory_content_hash(memory.summary, memory.content),
                    embedding=decision.embedding,
                )
            )
            results.append({"id": memory.id, "memory": fact.memory, "event": "ADD"})
        record_audit(
            session,
            tenant_id=task_input.tenant_id,
            action="INSERT",
            target_type="memory",
            target_id=memory.id,
            reason_code="FACT_EXTRACTED",
            task=task,
            context={
                "evidence_group_id": group_id,
                "message_ids": fact.message_ids,
                "timestamp_origin": "adapter_observed" if any(
                    m.get("timestamp_origin") == "adapter_observed"
                    for m in task_input.targets
                    if m["message_id"] in fact.message_ids and m["role"] == "user"
                ) else "source",
                "governance_action": governance.action,
            },
            state_after={
                "content": fact.memory,
                "status": memory.status,
                "version": memory.version,
                "evidence_count": len(set(fact.message_ids)),
                "effective_at": memory.effective_at.isoformat(),
                "expired_at": memory.expired_at.isoformat() if memory.expired_at else None,
            },
        )
    if changed:
        session.flush()
        enqueue_profile_rebuild(
            session, tenant_id=task_input.tenant_id, user_id=task_input.user_id,
            correlation_id=task.correlation_id,
        )
    return ([result for result in results
             if session.get(MemoryItem, result["id"]).status == "active"], changed, unchanged)


def _slice_processing_results(session, task_input, decisions, assessments, skipped, unchanged):
    results = []
    by_id = {a.message_id: a for a in assessments}
    for message in task_input.targets:
        location = {k: message[k] for k in ("message_id", "start_char", "end_char")
                    if k in message}
        matching = [(i, d) for i, d in enumerate(decisions)
                    if message["message_id"] in d.fact.message_ids]
        for index, decision in matching:
            if decision.skip_reason:
                results.append({**location, "outcome": "time_unresolved",
                                "reason": decision.skip_reason})
                continue
            governance = decision.governance
            memory_id = (governance.memory_ids[0] if governance.action == "DUPLICATE"
                         else decision.memory_id)
            memory = session.get(MemoryItem, memory_id)
            outcome = {"ADD": "added", "DUPLICATE": "evidence_added",
                       "SUPERSEDE": "superseded", "DISPUTE": "disputed"}[governance.action]
            if index in unchanged:
                outcome = "already_exists"
            elif memory.status == "expired":
                outcome = "historical"
            results.append({**location, "outcome": outcome, "memory_ids": [memory_id]})
        for item in skipped:
            if message["message_id"] in item["message_ids"]:
                results.append({**location, "outcome": "time_unresolved", "reason": item["code"]})
        assessment = by_id.get(message["message_id"])
        if assessment and assessment.outcome != "evaluated":
            results.append({**location, "outcome": assessment.outcome, "reason": assessment.reason})
        elif not matching and not any(message["message_id"] in item["message_ids"]
                                      for item in skipped):
            outcome = "evaluated_no_fact" if message["role"] == "user" else "no_durable_value"
            results.append({**location, "outcome": outcome})
    return results


def _leased_task(session, *, tenant_id, task_id, worker_id, lease_token):
    task = session.scalars(
        select(MemoryTask)
        .where(
            MemoryTask.id == task_id,
            MemoryTask.tenant_id == tenant_id,
            MemoryTask.status == "processing",
            MemoryTask.worker_id == worker_id,
            MemoryTask.lease_token == lease_token,
            MemoryTask.locked_until > func.clock_timestamp(),
        )
        .with_for_update()
    ).one_or_none()
    if task is None:
        raise LeaseLostError("task lease was lost")
    return task


def _save_execution(task: MemoryTask, execution: dict[str, Any]) -> None:
    task.payload = {**task.payload, "execution": execution}


def _restore_input(value: dict[str, Any]) -> TaskInput:
    return TaskInput(
        **{**value, "associations": tuple(tuple(row) for row in value["associations"])}
    )


def _validate_input(
    session: Session,
    task: MemoryTask,
    task_input: TaskInput,
    *,
    committed_changes: bool = False,
) -> None:
    identity = (
        task.id,
        task.tenant_id,
        task.caller_agent_id,
        task.user_id,
        task.source_system,
        task.session_id,
        task.batch_seq,
    )
    expected = (
        task_input.task_id,
        task_input.tenant_id,
        task_input.caller_agent_id,
        task_input.user_id,
        task_input.source_system,
        task_input.session_id,
        task_input.batch_seq,
    )
    signature = json.dumps(
        {
            "type": task.task_type,
            "target_type": task.target_type,
            "target_id": task.target_id,
            "input_version": task.input_version,
            "payload": {k: v for k, v in task.payload.items() if k != "execution"},
        },
        sort_keys=True,
    )
    associations = session.scalars(
        select(MemoryTaskSource)
        .where(
            MemoryTaskSource.task_id == task.id,
            MemoryTaskSource.tenant_id == task.tenant_id,
        )
        .order_by(MemoryTaskSource.kind, MemoryTaskSource.position)
        .with_for_update()
    ).all()
    actual_associations = tuple(
        (a.source_id, a.kind, a.position, a.source_version) for a in associations
    )
    sources = session.scalars(
        select(SourceRecord)
        .where(
            SourceRecord.tenant_id == task.tenant_id,
            SourceRecord.id.in_(task_input.source_states),
        )
        .order_by(SourceRecord.id)
        .with_for_update()
    ).all()
    states = {
        source.id: {
            "status": source.status,
            "version": source.version,
            "message": _source_message(source),
        }
        for source in sources
    }
    if (
        identity != expected
        or signature != task_input.task_signature
        or actual_associations != task_input.associations
        or states != task_input.source_states
        or any((source.caller_agent_id, source.user_id, source.source_system, source.session_id)
               != (task_input.caller_agent_id, task_input.user_id,
                   task_input.source_system, task_input.session_id) for source in sources)
    ):
        raise WorkerInputError(
            "task input changed or source became unavailable during extraction",
            committed_changes=committed_changes,
        )


def _process_task(
    session_factory: sessionmaker[Session],
    *,
    task_id: str,
    tenant_id: str,
    worker_id: str,
    lease_token: str,
    fact_model: FactModel,
    embedder: Embedder,
    duplicate_threshold: float,
    budget: ExtractionBudget,
) -> None:
    ownership = {
        "tenant_id": tenant_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "lease_token": lease_token,
    }
    with session_factory.begin() as session:
        task = _leased_task(session, **ownership)
        if task.task_type != "fact_extract":
            raise WorkerInputError("unsupported task type")
        execution = task.payload.get("execution")
        if execution is None:
            task_input = _load_task_input(session, task)
            try:
                slices = build_slices(
                    task_input.targets, task_input.history, system=EXTRACTION_PROMPT, budget=budget
                )
            except ValueError as exc:
                raise WorkerInputError(str(exc)) from exc
            used_ids = {m["message_id"] for current in slices
                        for m in [*current["targets"], *current["history"]]}
            used_ids.update(task_input.source_states[source_id]["message"]["message_id"]
                            for source_id, kind, _, _ in task_input.associations
                            if kind == "target")
            source_ids = {mid: sid for mid, sid in task_input.source_ids.items() if mid in used_ids}
            task_input = replace(
                task_input,
                history=[m for m in task_input.history if m["message_id"] in used_ids],
                source_ids=source_ids,
                source_states={sid: state for sid, state in task_input.source_states.items()
                               if sid in set(source_ids.values())},
            )
            execution = {
                "input": asdict(task_input),
                "slices": slices,
                "next_slice": 0,
                "model_calls": 0,
                "changed": False,
                "diagnostics": [
                    {**gap, "target_message_id": message["message_id"],
                     "action_taken": "reported_to_extractor"}
                    for message in task_input.targets for gap in message.get("context_gaps", [])
                ],
                "processing_results": [
                    {"message_id": source.message_id, "outcome": "already_processed"}
                    for association, source in session.execute(
                        select(MemoryTaskSource, SourceRecord)
                        .join(SourceRecord, SourceRecord.id == MemoryTaskSource.source_id)
                        .where(MemoryTaskSource.task_id == task.id,
                               MemoryTaskSource.kind == "target",
                               MemoryTaskSource.status == "skipped")
                    )
                ],
                "budget": asdict(budget),
                "prompt_version": PROMPT_VERSION,
            }
            _save_execution(task, execution)
            task.result_json = []
        else:
            if execution["prompt_version"] != PROMPT_VERSION:
                raise WorkerInputError("task prompt version is no longer supported")
            task_input = _restore_input(execution["input"])
            budget = ExtractionBudget(**execution["budget"])
        _validate_input(session, task, task_input, committed_changes=execution["changed"])

    def consume_call():
        with session_factory.begin() as session:
            task = _leased_task(session, **ownership)
            if "execution" not in task.payload:
                raise WorkerInputError(
                    "task execution input was removed", committed_changes=execution["changed"]
                )
            _validate_input(session, task, task_input, committed_changes=execution["changed"])
            state = dict(task.payload["execution"])
            if state["model_calls"] >= budget.max_model_calls:
                raise ModelCallBudgetError("task model call budget exhausted")
            state["model_calls"] += 1
            _save_execution(task, state)
            execution["model_calls"] = state["model_calls"]

    while execution["next_slice"] < len(execution["slices"]):
        index = execution["next_slice"]
        current = execution["slices"][index]
        slice_input = replace(task_input, targets=current["targets"], history=current["history"])
        prepared = execution.get("prepared_slice")
        assessments = []
        if current.get("context_error"):
            facts = []
            assessments = [ExtractionAssessment(
                message_id=m["message_id"], outcome="context_insufficient",
                reason=current["context_error"],
            ) for m in current["targets"]]
        elif prepared and prepared["index"] == index:
            facts = [ExtractedFact.model_validate(value) for value in prepared["facts"]]
            assessments = [ExtractionAssessment.model_validate(value)
                           for value in prepared.get("assessments", [])]
        else:
            consume_call()
            try:
                extract_with_outcomes = getattr(fact_model, "extract_with_outcomes", None)
                if extract_with_outcomes is not None:
                    output = extract_with_outcomes(
                        targets=slice_input.targets, history=slice_input.history)
                    facts, assessments = output.facts, output.assessments
                else:
                    facts = fact_model.extract(
                        targets=slice_input.targets, history=slice_input.history)
            except OutputLimitError as exc:
                try:
                    children = split_slice(current)
                except ValueError:
                    raise ProviderError("output truncation recovery exhausted") from exc
                # Re-select from the frozen input, retaining required dependencies.
                for child in children:
                    for message in child["history"]:
                        if message["message_id"] in {m["message_id"] for m in child["targets"]}:
                            message = dict(message)
                            message["continuation"] = True
                            child["history"] = [message if m["message_id"] == message["message_id"]
                                                else m for m in child["history"]]
                    selected = select_history(
                        child["targets"], child["history"], system=EXTRACTION_PROMPT,
                        limit=budget.input_tokens - budget.output_tokens,
                    )
                    if selected is None:
                        child.update(history=[], context_error="CONTEXT_BUDGET_EXCEEDED")
                    else:
                        child["history"] = selected
                with session_factory.begin() as session:
                    task = _leased_task(session, **ownership)
                    execution["slices"] = [
                        *execution["slices"][:index],
                        *children,
                        *execution["slices"][index + 1 :],
                    ]
                    execution["diagnostics"] = [
                        *execution["diagnostics"],
                        {
                            "code": "OUTPUT_LIMIT_REACHED",
                            "slice_id": index,
                            "action_taken": "split",
                            "attempt": execution["model_calls"],
                        },
                    ]
                    _save_execution(task, execution)
                continue
            validate_fact_evidence(facts, slice_input.targets, slice_input.history)
            facts = [
                normalize_fact_scenes(
                    session_factory,
                    tenant_id=task_input.tenant_id,
                    fact=fact,
                    model=fact_model,
                    embedder=embedder,
                    consume_call=consume_call,
                    budget=budget,
                )
                for fact in facts
            ]
            with session_factory.begin() as session:
                task = _leased_task(session, **ownership)
                execution["prepared_slice"] = {
                    "index": index,
                    "facts": [fact.model_dump(mode="json") for fact in facts],
                    "assessments": [a.model_dump() for a in assessments],
                }
                _save_execution(task, execution)
        accepted = []
        skipped = []
        for fact in facts:
            reason = candidate_skip_reason(fact)
            if any(m.get("timestamp_origin") == "adapter_observed"
                   for m in slice_input.targets if m["message_id"] in fact.message_ids):
                if fact.change_kind != "none":
                    reason = "UNKNOWN_EVENT_TIME"
            if reason:
                skipped.append(
                    {
                        "code": reason,
                        "slice_id": index,
                        "message_ids": fact.message_ids,
                        "action_taken": "skipped",
                    }
                )
            else:
                accepted.append(fact)
        embeddings = []
        for start in range(0, len(accepted), 32):
            consume_call()
            embeddings.extend(embedder.embed([f.memory for f in accepted[start : start + 32]]))
        decisions = _plan_decisions(
            session_factory,
            task_input=slice_input,
            facts=accepted,
            embeddings=embeddings,
            fact_model=fact_model,
            embedder=embedder,
            duplicate_threshold=duplicate_threshold,
            consume_call=consume_call,
            budget=budget,
        )
        with session_factory.begin() as session:
            task = _leased_task(session, **ownership)
            _validate_input(session, task, task_input, committed_changes=execution["changed"])
            if task.payload["execution"]["next_slice"] != index:
                raise LeaseLostError("slice progress changed")
            results, changed, unchanged = _commit_results(
                session, task=task, task_input=slice_input, decisions=decisions, embedder=embedder
            )
            execution["next_slice"] = index + 1
            execution.pop("prepared_slice", None)
            execution["changed"] = execution["changed"] or changed
            execution["diagnostics"] = [*execution["diagnostics"], *skipped]
            processing_results = _slice_processing_results(
                session, slice_input, decisions, assessments, skipped, unchanged)
            execution["processing_results"] = [
                *execution.get("processing_results", []), *processing_results,
            ]
            _save_execution(task, execution)
            task.result_json = [
                result
                for result in [*(task.result_json or []), *results]
                if session.get(MemoryItem, result["id"]).status == "active"
            ]
            remaining = {
                m["message_id"]
                for pending in execution["slices"][index + 1 :]
                for m in pending["targets"]
            }
            for message in current["targets"]:
                if message["message_id"] not in remaining:
                    source = session.get(SourceRecord, task_input.source_ids[message["message_id"]])
                    incomplete = any(
                        result["message_id"] == message["message_id"] and result["outcome"] in
                        {"context_insufficient", "time_unresolved", "evaluated_no_fact"}
                        for result in execution["processing_results"]
                    )
                    if not incomplete:
                        source.processed_version = source.version
                    association = session.get(MemoryTaskSource, (tenant_id, task_id, source.id))
                    association.status = "processed"
            # Fence every slice, including non-terminal commits, against expired leases.
            if not session.scalar(
                select(MemoryTask.id).where(
                    MemoryTask.id == task_id,
                    MemoryTask.locked_until > func.clock_timestamp(),
                )
            ):
                raise LeaseLostError("task lease expired during slice commit")
            if index + 1 == len(execution["slices"]):
                if not finish_task(
                    session, **ownership, status="succeeded", results=task.result_json
                ):
                    raise LeaseLostError("task lease was lost")
    if not execution["slices"]:
        with session_factory.begin() as session:
            task = _leased_task(session, **ownership)
            _validate_input(session, task, task_input, committed_changes=execution["changed"])
            if not finish_task(session, **ownership, status="succeeded", results=[]):
                raise LeaseLostError("task lease was lost")


def process_task(
    session_factory: sessionmaker[Session],
    *,
    task_id: str,
    tenant_id: str,
    worker_id: str,
    lease_token: str,
    fact_model: FactModel,
    embedder: Embedder,
    duplicate_threshold: float,
    lease_seconds: int = 180,
    budget: ExtractionBudget = ExtractionBudget(),
) -> None:
    with _lease_heartbeat(
        session_factory,
        tenant_id=tenant_id,
        task_id=task_id,
        worker_id=worker_id,
        lease_token=lease_token,
        lease_seconds=lease_seconds,
    ):
        _process_task(
            session_factory,
            task_id=task_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            lease_token=lease_token,
            fact_model=fact_model,
            embedder=embedder,
            duplicate_threshold=duplicate_threshold,
            budget=budget,
        )


def run_once(
    session_factory: sessionmaker[Session],
    *,
    worker_id: str,
    fact_model: FactModel,
    embedder: Embedder,
    duplicate_threshold: float,
    budget: ExtractionBudget = ExtractionBudget(),
) -> bool:
    with session_factory.begin() as session:
        finish_exhausted_leases(session)
        tenant_ids = session.scalars(
            select(MemoryTask.tenant_id)
            .where(
                MemoryTask.task_type == "fact_extract",
                or_(
                    (MemoryTask.status == "pending")
                    & (MemoryTask.available_at <= func.current_timestamp()),
                    (MemoryTask.status == "processing")
                    & (MemoryTask.locked_until < func.current_timestamp()),
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
                task_types=("fact_extract",),
            )
            task = claimed[0] if claimed else None
            if task is not None:
                task_id = task.id
                lease_token = task.lease_token
        if task is None:
            continue
        try:
            process_task(
                session_factory,
                task_id=task_id,
                tenant_id=tenant_id,
                worker_id=worker_id,
                lease_token=lease_token,
                fact_model=fact_model,
                embedder=embedder,
                duplicate_threshold=duplicate_threshold,
                budget=budget,
            )
        except WorkerInputError as exc:
            with session_factory.begin() as session:
                task = session.get(MemoryTask, task_id)
                changed = exc.committed_changes or (task.payload.get("execution") or {}).get(
                    "changed", False
                )
                finish_task(
                    session,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    status="partial" if changed else "failed",
                    results=task.result_json or [],
                    error={
                        "code": "EXTRACTION_FAILED"
                        if isinstance(exc, ModelCallBudgetError)
                        else "INPUT_PROCESSING_FAILED",
                        "message": str(exc),
                    },
                )
        except ProviderError as exc:
            with session_factory.begin() as session:
                fail_and_retry_task(
                    session,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    error=str(exc),
                    error_code="EXTRACTION_FAILED",
                    base_backoff_seconds=2,
                )
        except LeaseLostError:
            logger.warning("task %s lost its lease before completion", task_id)
        except Exception as exc:
            logger.exception("unexpected task failure for %s", task_id)
            with session_factory.begin() as session:
                fail_and_retry_task(
                    session,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    error=str(exc),
                    error_code="EXTRACTION_FAILED",
                    base_backoff_seconds=2,
                )
        return True
    return False


def main() -> None:
    from memory_cmic.consolidation import QwenConsolidationModel
    from memory_cmic.consolidation import run_once as run_consolidation_once
    from memory_cmic.governance_worker import run_once as run_governance_once

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="MemoryCMIC worker")
    parser.add_argument(
        "--role", choices=("add", "maintenance", "consolidation", "profile"), default="add"
    )
    role = parser.parse_args().role
    settings = Settings.from_env()
    if role in {"add", "consolidation", "profile"} and not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY must be set")
    if role in {"add", "maintenance"} and not settings.siliconflow_api_key:
        raise RuntimeError("SILICONFLOW_API_KEY must be set")
    engine = create_database_engine(settings.database_url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    fact_model = QwenFactModel(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
        output_tokens=settings.extraction_budget.output_tokens,
        input_tokens=settings.extraction_budget.input_tokens,
    ) if role == "add" else None
    embedder = SiliconFlowEmbedder(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model_id=settings.siliconflow_embedding_model,
    ) if role in {"add", "maintenance"} else None
    consolidation_model = QwenConsolidationModel(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
        output_tokens=settings.extraction_budget.output_tokens,
        input_tokens=settings.extraction_budget.input_tokens,
    ) if role == "consolidation" else None
    if role == "profile":
        from memory_cmic.profile import QwenProfileModel
        from memory_cmic.profile import run_once as run_profile_once

        profile_model = QwenProfileModel(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
            model=settings.dashscope_model,
            output_tokens=settings.extraction_budget.output_tokens,
        )
    worker_id = f"worker_{uuid4().hex}"
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        if role == "maintenance":
            handled = run_governance_once(session_factory, worker_id=worker_id, embedder=embedder)
        elif role == "consolidation":
            handled = run_consolidation_once(
                session_factory, worker_id=worker_id, model=consolidation_model,
                model_id=settings.siliconflow_embedding_model,
            )
        elif role == "profile":
            handled = run_profile_once(
                session_factory, worker_id=worker_id, model=profile_model,
                allow_inferred=settings.profile_allow_inferred,
            )
        else:
            handled = run_once(
                session_factory,
                worker_id=worker_id,
                fact_model=fact_model,
                embedder=embedder,
                duplicate_threshold=settings.duplicate_candidate_threshold,
                budget=settings.extraction_budget,
            )
        if not handled:
            time.sleep(1)


if __name__ == "__main__":
    main()
