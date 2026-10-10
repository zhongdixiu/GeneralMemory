from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.audit import record_audit
from memory_cmic.evidence import evidence_fingerprint, memory_has_valid_evidence
from memory_cmic.extraction_input import input_size
from memory_cmic.governance_state import lock_user, schedule_due
from memory_cmic.lifecycle import evidence_group_is_complete, govern_memory, invalidate_memory
from memory_cmic.models import (
    BusinessScene,
    GovernanceOperation,
    GovernancePending,
    GovernancePolicy,
    GovernanceSubjectState,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    SourceRecord,
)
from memory_cmic.providers import ProviderError, QwenFactModel
from memory_cmic.task_queue import (
    claim_tasks,
    fail_and_retry_task,
    finish_exhausted_leases,
    finish_task,
)
from memory_cmic.worker import LeaseLostError, _lease_heartbeat, _leased_task

logger = logging.getLogger(__name__)

PROMPT = (
    "Review memories for one user and one applicability scope according to phase. "
    "Facts/inference phases use active facts; conflicts phase includes disputed members. "
    "Return JSON with "
    "merge_ids, inferences, invalidations. Only merge statements with identical durable meaning, "
    "conditions and validity period. Never merge complementary requirements. "
    "Only infer actionable task preferences or habits supported by at least two independent "
    "user sources from different underlying statements. Do not infer personality or identity. "
    "An inference must add a useful pattern rather than repeat a fact. "
    "Write inferred content as a short, standalone declarative sentence with an explicit "
    "subject, predicate and object or complement. Refer to this user as 用户 in Chinese "
    "(the user in English), never 我, 我们, 你, I, we or you. Resolve remaining references "
    "only from supplied memories and evidence; do not guess referents or change literal "
    "names, titles or verbatim quotations. Keep all supporting conditions when shortening. "
    "Do not turn two complementary explicit rules for the same artifact into a bundled template "
    "or conjunction. For example, 'conclusion first' plus 'attach data' for weekly reports "
    "must remain two facts, not a third inferred memory. Infer only a generalization of a "
    "repeated tendency across distinct task contexts, such as weekly reports and project "
    "retrospectives sharing an output pattern. "
    "Preserve explicit business, project, customer, occasion and temporal conditions in the "
    "inferred content. Provide canonical business_domains using the supplied scene definitions; "
    "null means no business restriction in the evidence. Do not reject a supported pattern "
    "solely because it has no business label. Inferred domains must match the basis domains; "
    "never broaden restricted evidence into a general preference. "
    "When an inference is scene-limited, state that activity in the content itself; do not "
    "describe it as applying to all documents or tasks. "
    "Do not copy a source label without checking the inferred meaning. "
    "Explicit user facts override guesses. Never repeat a rejected conclusion, even "
    "with different wording, unless its underlying independent evidence changed. "
    "Only invalidate a memory when a later explicit user correction quoted verbatim in an "
    "active source proves it obsolete. Do not invalidate solely due to age or model suspicion. "
    "For merely suspected staleness, list a suspicion and leave the memory active. "
    "merge_ids lists other memory IDs, excluding anchor_id. Explain the exact shared meaning "
    "in merge_reason. Inference source_ids must be IDs of basis memories from memories[].id, "
    "never IDs of raw sources from sources[].id. Use only supplied IDs. "
    "Return {merge_ids:[memory_id], merge_reason:string, "
    "inferences:[{content,source_ids:[memory_id],confidence:0..1,reason,"
    "business_domains:[scene_name]|null}], "
    "invalidations:[{memory_id,source_id,quote,reason}], "
    "suspicions:[{memory_id,reason}]}. Spell these common keys exactly; phase-specific keys "
    "are described below. "
    "Empty arrays are valid."
    " Obey phase: facts permits merges/invalidations/suspicions only; inference permits "
    "inferences and inference_reviews only, using the freshly supplied facts. "
    "When generate_inferences is false return no new inferences; still review existing ones. "
    "inference_reviews:[{memory_id,outcome:retain|invalidate,source_id:null|string,"
    "quote:null|string,reason:string}]. Only invalidate an existing inference when a supplied "
    "explicit user fact with a verbatim source quote contradicts it in the same conditions. "
    "phase conflicts reviews the complete conflict group. Never pick the newest or majority "
    "claim without explicit correction/clarification. Return conflict_review:{"
    "outcome:unresolved|resolved|conditions_distinguished,reason:string,question:string|null,"
    "facts:[{content:string,basis_memory_id:string,source_id:string,quote:string}]}. "
    "For resolved require a later explicit user clarification and create a sourced replacement; "
    "for conditions_distinguished quote evidence supporting the complete separate conditions. "
    "Never silently reactivate disputed records. If evidence is insufficient return unresolved "
    "with the specific question needed from the user. Other result arrays must be empty."
)


class InferenceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4096)
    source_ids: list[str] = Field(min_length=2)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=512)
    business_domains: list[str] | None = Field(min_length=1)


class InvalidationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    source_id: str
    quote: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=512)


class SuspicionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    reason: str = Field(min_length=1, max_length=512)


class ConflictFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4096)
    basis_memory_id: str
    source_id: str
    quote: str = Field(min_length=1)


class ConflictReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["unresolved", "resolved", "conditions_distinguished"]
    reason: str = Field(min_length=1, max_length=512)
    question: str | None = Field(default=None, max_length=512)
    facts: list[ConflictFact] = Field(default_factory=list)


class InferenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    outcome: Literal["retain", "invalidate"]
    source_id: str | None = None
    quote: str | None = None
    reason: str = Field(min_length=1, max_length=512)


class ConsolidationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merge_ids: list[str] = Field(default_factory=list)
    merge_reason: str = Field(default="", max_length=512)
    inferences: list[InferenceProposal] = Field(default_factory=list)
    invalidations: list[InvalidationProposal] = Field(default_factory=list)
    suspicions: list[SuspicionProposal] = Field(default_factory=list)
    conflict_review: ConflictReview | None = None
    inference_reviews: list[InferenceReview] = Field(default_factory=list)


class ConsolidationModel(Protocol):
    def decide(self, payload: dict) -> ConsolidationDecision: ...


class QwenConsolidationModel:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        output_tokens: int,
        input_tokens: int = 12000,
    ):
        if input_tokens <= output_tokens:
            raise ValueError("model input budget must exceed output reserve")
        self.input_limit = input_tokens - output_tokens
        self._model = QwenFactModel(
            api_key=api_key, base_url=base_url, model=model, output_tokens=output_tokens
        )

    def decide(self, payload: dict) -> ConsolidationDecision:
        try:
            raw = self._model._json_completion(PROMPT, payload)
            if "suspections" in raw and "suspicions" not in raw:
                raw["suspicions"] = raw.pop("suspections")
            result = ConsolidationDecision.model_validate(raw)
            self.last_usage = self._model.last_usage
            return result
        except Exception as exc:
            raise ProviderError(f"invalid consolidation decision: {exc}") from exc


def _scene_aliases(session: Session, tenant_id: str) -> dict[str, str]:
    aliases = {}
    for scene in session.scalars(select(BusinessScene).where(BusinessScene.tenant_id == tenant_id)):
        aliases[scene.name] = scene.name
        aliases.update({alias.strip().lower(): scene.name for alias in scene.aliases})
    return aliases


def _scope_key(memory: MemoryItem, aliases: dict[str, str] | None = None) -> tuple:
    meta = memory.metadata_json or {}
    domains = memory.business_domains
    return (
        memory.cognitive_type,
        tuple(sorted({(aliases or {}).get(value.strip().lower(), value) for value in domains}))
        if domains is not None
        else None,
        tuple(sorted(memory.project_domains or [])) if memory.project_domains is not None else None,
        meta.get("project_context"),
        meta.get("project_limited"),
    )


def _same_interval(left: MemoryItem, right: MemoryItem) -> bool:
    if left.expired_at != right.expired_at:
        return False
    if (left.metadata_json or {}).get("temporal_kind") == "historical":
        return left.effective_at == right.effective_at
    return (right.metadata_json or {}).get("temporal_kind") != "historical"


def _scope_memories(session: Session, anchor: MemoryItem) -> list[MemoryItem]:
    now = datetime.now(UTC)
    aliases = _scene_aliases(session, anchor.tenant_id)
    return [
        value
        for value in session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == anchor.tenant_id,
                MemoryItem.subject_type == "user",
                MemoryItem.subject_id == anchor.subject_id,
                MemoryItem.cognitive_type == "fact",
                MemoryItem.status == "active",
                MemoryItem.effective_at <= now,
                (MemoryItem.expired_at.is_(None)) | (MemoryItem.expired_at > now),
            )
        )
        if _scope_key(value, aliases) == _scope_key(anchor, aliases)
        and memory_has_valid_evidence(
            session, value, tenant_id=anchor.tenant_id, user_id=anchor.subject_id, now=now
        )
    ]


def _evidence_snapshot(
    session: Session, memories: list[MemoryItem]
) -> tuple[str, dict[str, list[MemoryEvidence]]]:
    ids = [memory.id for memory in memories]
    edges = (
        session.scalars(
            select(MemoryEvidence)
            .where(
                MemoryEvidence.tenant_id == memories[0].tenant_id,
                MemoryEvidence.downstream_memory_id.in_(ids),
            )
            .order_by(MemoryEvidence.id)
        ).all()
        if ids
        else []
    )
    by_memory: dict[str, list[MemoryEvidence]] = {memory_id: [] for memory_id in ids}
    entries = [
        (
            m.id,
            m.version,
            m.status,
            m.content,
            m.summary,
            m.business_domains,
            m.project_domains,
            m.metadata_json,
            m.effective_at.isoformat(),
            m.expired_at.isoformat() if m.expired_at else None,
        )
        for m in sorted(memories, key=lambda item: item.id)
    ]
    for edge in edges:
        by_memory[edge.downstream_memory_id].append(edge)
        upstream = (
            session.get(SourceRecord, edge.upstream_source_id)
            if edge.upstream_source_id
            else session.get(MemoryItem, edge.upstream_memory_id)
        )
        entries.append(
            (
                edge.id,
                edge.status,
                edge.evidence_group_id,
                edge.upstream_version,
                upstream.status if upstream else None,
                upstream.version if upstream else None,
                upstream.content_hash if isinstance(upstream, SourceRecord) else None,
            )
        )
    value = hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()
    return value, by_memory


def _valid_sources(session: Session, edges: list[MemoryEvidence]) -> list[SourceRecord]:
    sources = []
    for edge in edges:
        if edge.status != "active" or not edge.upstream_source_id:
            continue
        if not evidence_group_is_complete(session, edge.evidence_group_id):
            continue
        source = session.get(SourceRecord, edge.upstream_source_id)
        if (
            source is not None
            and source.status == "active"
            and source.version == edge.upstream_version
        ):
            sources.append(source)
    return sources


def _roots(session: Session, memory_id: str, *, excluded: set[str] | None = None) -> str:
    edges = session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.downstream_memory_id == memory_id,
            MemoryEvidence.status == "active",
        )
    ).all()
    values = sorted(
        {
            (s.id, s.version, s.content_hash)
            for s in _valid_sources(session, [e for e in edges if e.id not in (excluded or set())])
        }
    )
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _payload(
    session: Session,
    anchor: MemoryItem,
    memories: list[MemoryItem],
    edges: dict[str, list[MemoryEvidence]],
    input_limit: int,
) -> dict | None:
    ordered = sorted(
        memories,
        key=lambda m: (
            m.id != anchor.id,
            m.semantic_hash != anchor.semantic_hash,
            m.created_at,
            m.id,
        ),
    )[:50]
    rejected = session.scalars(
        select(GovernanceOperation)
        .where(
            GovernanceOperation.tenant_id == anchor.tenant_id,
            GovernanceOperation.user_id == anchor.subject_id,
            GovernanceOperation.kind == "rejection",
        )
        .order_by(GovernanceOperation.created_at.desc())
        .limit(30)
    ).all()
    payload = {
        "phase": "facts",
        "anchor_id": anchor.id,
        "scenes": [
            {"name": scene.name, "definition": scene.definition, "aliases": scene.aliases}
            for scene in session.scalars(
                select(BusinessScene).where(BusinessScene.tenant_id == anchor.tenant_id)
            )
        ],
        "memories": [
            {
                "id": m.id,
                "status": m.status,
                "conflict_group_id": m.conflict_group_id,
                "content": m.content,
                "effective_at": m.effective_at.isoformat(),
                "expired_at": m.expired_at.isoformat() if m.expired_at else None,
                "business_domains": m.business_domains,
                "project_domains": m.project_domains,
                "project_context": (m.metadata_json or {}).get("project_context"),
                "sources": [
                    {
                        "id": s.id,
                        "text": s.raw_content,
                        "occurred_at": s.occurred_at.isoformat(),
                    }
                    for s in _valid_sources(session, edges[m.id])[:2]
                ],
            }
            for m in ordered
        ],
        "rejected": [
            {
                "kind": op.details.get("kind"),
                "content": (op.details.get("content") or "")[:256],
                "winner_id": op.details.get("winner_id"),
                "loser_id": op.details.get("loser_id"),
            }
            for op in rejected[:10]
        ],
    }
    while len(payload["memories"]) > 1 and input_size(PROMPT, payload) > input_limit:
        payload["memories"].pop()
    if input_size(PROMPT, payload) > input_limit:
        return None
    return payload


def _record(
    session: Session, task: MemoryTask, kind: str, memory_id: str | None, details: dict
) -> GovernanceOperation:
    op = GovernanceOperation(
        id=f"gop_{uuid4().hex}",
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        task_id=task.id,
        kind=kind,
        memory_id=memory_id,
        details=details,
    )
    session.add(op)
    if kind in {"invalidate", "suspected_stale"}:
        # Actual invalidation is audited by lifecycle; suspicion is only a decision.
        return op
    target_id = details.get("winner_id") if kind == "merge" else memory_id
    memory = session.get(MemoryItem, target_id) if target_id else None
    before, after = None, None
    if kind == "merge":
        before = {"evidence_count": details["evidence_count_before"]}
        after = {"evidence_count": details["evidence_count_after"]}
    elif kind == "inference":
        after = {
            "content": memory.content,
            "status": memory.status,
            "version": memory.version,
            "effective_at": memory.effective_at.isoformat(),
            "expired_at": memory.expired_at.isoformat() if memory.expired_at else None,
        }
    record_audit(
        session,
        tenant_id=task.tenant_id,
        action="INSERT" if kind == "inference" else "UPDATE",
        target_type="memory" if target_id else "task",
        target_id=target_id or task.id,
        operator_type="system",
        operator_id="memory_cmic",
        reason_code=f"GOVERNANCE_{kind.upper()}",
        task=task,
        reason=details.get("reason"),
        context={
            "operation_id": op.id,
            "related_memory_id": memory_id,
            "source_ids": details.get("source_ids", []),
        },
        state_before=before,
        state_after=after,
    )
    return op


def _merge(
    session: Session,
    task: MemoryTask,
    anchor: MemoryItem,
    chosen: list[MemoryItem],
    edges: dict[str, list[MemoryEvidence]],
    reason: str,
) -> list[dict]:
    ordered = sorted(
        {m.id: m for m in [anchor, *chosen]}.values(), key=lambda m: (m.created_at, m.id)
    )
    winner = ordered[0]
    results = []
    for loser in ordered[1:]:
        if winner.status != "active" or loser.status != "active":
            continue
        winner_roots = _roots(session, winner.id)
        loser_roots = _roots(session, loser.id)
        rejected_merge = session.scalars(
            select(GovernanceOperation).where(
                GovernanceOperation.tenant_id == task.tenant_id,
                GovernanceOperation.kind == "rejection",
                GovernanceOperation.user_id == task.user_id,
            )
        ).all()
        if any(
            op.details.get("kind") == "merge"
            and op.details.get("winner_id") == winner.id
            and op.details.get("loser_id") == loser.id
            and op.details.get("winner_roots") == winner_roots
            and op.details.get("loser_roots") == loser_roots
            for op in rejected_merge
        ):
            continue
        copied_ids = []
        evidence_count = session.scalar(select(func.count()).select_from(MemoryEvidence).where(
            MemoryEvidence.tenant_id == task.tenant_id,
            MemoryEvidence.downstream_memory_id == winner.id,
            MemoryEvidence.status == "active",
        ))
        groups = {}
        for edge in edges[loser.id]:
            groups.setdefault(edge.evidence_group_id, []).append(edge)
        if not groups or any(
            any(e.relationship_type != "supports" or e.status != "active" for e in group)
            or not evidence_group_is_complete(session, group_id)
            for group_id, group in groups.items()
        ):
            continue
        for group in groups.values():
            new_group = f"evg_{uuid4().hex}"
            for edge in group:
                copy = MemoryEvidence(
                    id=f"evi_{uuid4().hex}",
                    tenant_id=task.tenant_id,
                    relationship_type="supports",
                    evidence_group_id=new_group,
                    upstream_source_id=edge.upstream_source_id,
                    downstream_memory_id=winner.id,
                    upstream_version=edge.upstream_version,
                    evidence_snippet=edge.evidence_snippet,
                    evidence_locator=edge.evidence_locator,
                )
                session.add(copy)
                copied_ids.append(copy.id)
        session.flush()
        govern_memory(
            session,
            memory=loser,
            status="invalidated",
            reason_code="GOVERNANCE_MERGED",
            correlation_id=task.correlation_id,
            task=task,
            reason=reason or None,
        )
        scope_after, _ = _evidence_snapshot(session, _scope_memories(session, winner))
        op = _record(
            session,
            task,
            "merge",
            loser.id,
            {
                "winner_id": winner.id,
                "loser_id": loser.id,
                "winner_content": winner.content,
                "loser_content": loser.content,
                "winner_version": winner.version,
                "loser_version": loser.version,
                "copied_edge_ids": copied_ids,
                "evidence_count_before": evidence_count,
                "evidence_count_after": evidence_count + len(copied_ids),
                "source_ids": sorted(
                    {
                        e.upstream_source_id
                        for group in groups.values()
                        for e in group
                        if e.upstream_source_id
                    }
                ),
                "winner_roots": winner_roots,
                "loser_roots": loser_roots,
                "scope_after": scope_after,
                "reason": reason or "same durable meaning and applicability",
            },
        )
        results.append(
            {"operation_id": op.id, "kind": "merge", "memory_ids": [winner.id, loser.id]}
        )
    return results


def _infer(
    session: Session,
    task: MemoryTask,
    proposal: InferenceProposal,
    available: dict[str, MemoryItem],
    edges: dict[str, list[MemoryEvidence]],
    rejected: list[GovernanceOperation],
    model_id: str,
) -> dict | None:
    bases = [available.get(mid) for mid in dict.fromkeys(proposal.source_ids)]
    if len(bases) < 2 or any(m is None or m.status != "active" for m in bases):
        return None
    aliases = _scene_aliases(session, task.tenant_id)
    if len({_scope_key(m, aliases) for m in bases}) != 1:
        return None
    basis_domains = _scope_key(bases[0], aliases)[1]
    proposed_domains = (
        tuple(
            sorted({aliases.get(name.strip().lower(), name) for name in proposal.business_domains})
        )
        if proposal.business_domains is not None
        else None
    )
    if proposed_domains != basis_domains:
        return None
    if basis_domains is not None and any(name not in aliases.values() for name in basis_domains):
        return None
    source_hashes = set()
    source_sessions = set()
    for memory in bases:
        for source in _valid_sources(session, edges[memory.id]):
            if source.author_type == "user" and source.user_id == task.user_id:
                # Different submission metadata does not make copied text independent evidence.
                source_hashes.add(source.content_hash)
                if source.session_id:
                    source_sessions.add((source.source_system, source.session_id))
    if len(source_hashes) < 2 or len(source_sessions) < 2:
        return None
    if proposal.confidence < 0.6:
        return None
    normalized = " ".join(proposal.content.split()).casefold()
    if any(" ".join(m.content.split()).casefold() == normalized for m in bases):
        return None
    if any(
        op.details.get("kind") == "inference"
        and set(op.details.get("source_ids", [])) == set(proposal.source_ids)
        and op.details.get("basis_roots")
        == {memory.id: _roots(session, memory.id) for memory in bases}
        for op in rejected
    ):
        return None
    duplicate = session.scalars(
        select(MemoryItem).where(
            MemoryItem.tenant_id == task.tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == task.user_id,
            MemoryItem.cognitive_type == "inference",
            MemoryItem.status == "active",
            MemoryItem.semantic_hash == hashlib.sha256(normalized.encode()).hexdigest(),
        )
    ).first()
    if duplicate is not None:
        return None
    existing_inferences = session.scalars(
        select(MemoryItem).where(
            MemoryItem.tenant_id == task.tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == task.user_id,
            MemoryItem.cognitive_type == "inference",
            MemoryItem.status == "active",
        )
    ).all()
    for old in existing_inferences:
        old_bases = set(
            session.scalars(
                select(MemoryEvidence.upstream_memory_id).where(
                    MemoryEvidence.tenant_id == task.tenant_id,
                    MemoryEvidence.downstream_memory_id == old.id,
                    MemoryEvidence.relationship_type == "derives",
                    MemoryEvidence.status == "active",
                )
            ).all()
        )
        if old_bases == {m.id for m in bases}:
            return None
    first = bases[0]
    expiries = [m.expired_at for m in bases if m.expired_at is not None]
    inference = MemoryItem(
        id=f"gin_{uuid4().hex}",
        tenant_id=task.tenant_id,
        subject_type="user",
        subject_id=task.user_id,
        cognitive_type="inference",
        business_domains=list(basis_domains) if basis_domains is not None else None,
        project_domains=first.project_domains,
        summary=proposal.content[:512],
        content=proposal.content,
        semantic_hash=hashlib.sha256(normalized.encode()).hexdigest(),
        confidence=Decimal(str(proposal.confidence)),
        status="active",
        effective_at=max(m.effective_at for m in bases),
        expired_at=min(expiries) if expiries else None,
        metadata_json={
            "project_context": (first.metadata_json or {}).get("project_context"),
            "governance_reason": proposal.reason,
        },
        created_by="memory_cmic_governance",
    )
    session.add(inference)
    session.flush()
    group_id = f"evg_{uuid4().hex}"
    for base in bases:
        session.add(
            MemoryEvidence(
                id=f"evi_{uuid4().hex}",
                tenant_id=task.tenant_id,
                relationship_type="derives",
                evidence_group_id=group_id,
                upstream_memory_id=base.id,
                downstream_memory_id=inference.id,
                upstream_version=base.version,
            )
        )
    from memory_cmic.profile import enqueue_profile_rebuild
    from memory_cmic.task_queue import enqueue_task

    session.flush()
    enqueue_profile_rebuild(
        session, tenant_id=task.tenant_id, user_id=task.user_id, correlation_id=task.correlation_id
    )

    enqueue_task(
        session,
        {
            "id": uuid4().hex,
            "tenant_id": task.tenant_id,
            "task_type": "vector_upsert",
            "target_type": "memory",
            "target_id": inference.id,
            "input_version": 1,
            "idempotency_key": f"vector_upsert:memory:{inference.id}:v1:{model_id}",
            "correlation_id": task.correlation_id,
            "payload": {"model_id": model_id},
            "priority": 50,
        },
    )
    op = _record(
        session,
        task,
        "inference",
        inference.id,
        {
            "content": proposal.content,
            "source_ids": proposal.source_ids,
            "basis": [{"id": base.id, "content": base.content} for base in bases],
            "source_refs": sorted(
                {source.id for base in bases for source in _valid_sources(session, edges[base.id])}
            ),
            "reason": proposal.reason,
            "version": inference.version,
        },
    )
    return {"operation_id": op.id, "kind": "inference", "memory_ids": [inference.id]}


def _invalidate(
    session: Session,
    task: MemoryTask,
    proposal: InvalidationProposal,
    available: dict[str, MemoryItem],
) -> dict | None:
    target = available.get(proposal.memory_id)
    source = session.get(SourceRecord, proposal.source_id)
    if (
        target is None
        or target.status != "active"
        or source is None
        or source.tenant_id != task.tenant_id
        or source.user_id != task.user_id
        or source.author_type != "user"
        or source.status != "active"
        or proposal.quote not in source.raw_content
        or source.occurred_at <= target.effective_at
    ):
        return None
    # The cited correction must already support another current memory in this scope.
    corroborated = session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.tenant_id == task.tenant_id,
            MemoryEvidence.upstream_source_id == source.id,
            MemoryEvidence.status == "active",
            MemoryEvidence.downstream_memory_id != target.id,
        )
    ).all()
    aliases = _scene_aliases(session, task.tenant_id)
    if not any(
        (other := session.get(MemoryItem, edge.downstream_memory_id)) is not None
        and other.status == "active"
        and _scope_key(other, aliases) == _scope_key(target, aliases)
        for edge in corroborated
    ):
        return None
    rejected = session.scalars(
        select(GovernanceOperation).where(
            GovernanceOperation.tenant_id == task.tenant_id,
            GovernanceOperation.user_id == task.user_id,
            GovernanceOperation.kind == "rejection",
        )
    ).all()
    if any(
        op.details.get("kind") == "invalidate"
        and op.details.get("memory_id") == target.id
        and op.details.get("source_id") == source.id
        and op.details.get("source_version") == source.version
        for op in rejected
    ):
        return None
    op = _record(
        session,
        task,
        "invalidate",
        target.id,
        {
            "memory_id": target.id,
            "old_content": target.content,
            "source_id": source.id,
            "quote": proposal.quote,
            "reason": proposal.reason,
        },
    )
    invalidate_memory(
        session, tenant_id=task.tenant_id, memory_id=target.id,
        correlation_id=task.correlation_id, task=task,
        reason_code="GOVERNANCE_INVALIDATE", reason=proposal.reason,
        context={"operation_id": op.id},
    )
    scope_after, _ = _evidence_snapshot(session, _scope_memories(session, target))
    op.details = {**op.details, "version": target.version, "scope_after": scope_after}
    return {"operation_id": op.id, "kind": "invalidate", "memory_ids": [target.id]}


def _consume_call(session_factory, ownership: dict, model: ConsolidationModel, payload: dict):
    with session_factory.begin() as session:
        task = _leased_task(session, **ownership)
        if task.payload["model_calls"] >= task.payload["limits"]["model_calls"]:
            return None
        task.payload = {**task.payload, "model_calls": task.payload["model_calls"] + 1}
    decision = model.decide(payload)
    usage = getattr(model, "last_usage", None)
    if usage:
        with session_factory.begin() as session:
            task = _leased_task(session, **ownership)
            previous = task.payload.get("token_usage") or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
            task.payload = {
                **task.payload,
                "token_usage": {key: previous[key] + usage.get(key, 0) for key in previous},
            }
    return decision


def _conflict_input(session: Session, task: MemoryTask, group: str) -> list[MemoryItem]:
    members = session.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == task.tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == task.user_id,
            MemoryItem.conflict_group_id == group,
        )
        .order_by(MemoryItem.id)
    ).all()
    aliases = _scene_aliases(session, task.tenant_id)
    scopes = {_scope_key(m, aliases) for m in members}
    related = [
        m
        for m in session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == task.tenant_id,
                MemoryItem.subject_type == "user",
                MemoryItem.subject_id == task.user_id,
                MemoryItem.cognitive_type == "fact",
                MemoryItem.status.in_(["active", "disputed"]),
            )
        )
        if _scope_key(m, aliases) in scopes
    ]
    return sorted({m.id: m for m in [*members, *related]}.values(), key=lambda m: m.id)


def _apply_conflict_review(
    session: Session,
    task: MemoryTask,
    group: str,
    memories: list[MemoryItem],
    fingerprint: str,
    review: ConflictReview,
    model_id: str,
) -> dict:
    members = [m for m in memories if m.conflict_group_id == group and m.status == "disputed"]
    by_id = {m.id: m for m in memories}
    replacements = []
    outcome = review.outcome
    if outcome == "unresolved" and not review.question:
        review.question = "请明确这些争议陈述当前分别适用于哪些条件，或哪条已被纠正。"
    if outcome != "unresolved":
        if not review.facts or (outcome == "conditions_distinguished" and len(review.facts) < 2):
            raise ProviderError("conflict resolution requires grounded replacement facts")
        prepared = []
        for proposal in review.facts:
            basis = by_id.get(proposal.basis_memory_id)
            source = session.get(SourceRecord, proposal.source_id)
            if (
                basis is None
                or source is None
                or source.tenant_id != task.tenant_id
                or (source.user_id or source.author_id) != task.user_id
                or source.author_type != "user"
                or source.status != "active"
                or not proposal.quote.strip()
                or proposal.quote not in source.raw_content
                or source.id
                not in {
                    s.id
                    for s in _valid_sources(
                        session,
                        session.scalars(
                            select(MemoryEvidence).where(
                                MemoryEvidence.downstream_memory_id == basis.id
                            )
                        ).all(),
                    )
                }
            ):
                raise ProviderError("conflict resolution cites invalid user evidence")
            if outcome == "resolved" and any(source.occurred_at <= m.effective_at for m in members):
                # A newer claim alone still needs semantic clarification, enforced in the prompt.
                raise ProviderError("conflict clarification must follow all disputed assertions")
            if outcome == "resolved" and any(_scope_key(m) != _scope_key(basis) for m in members):
                raise ProviderError("conflict clarification cannot replace unrelated scopes")
            if not _eligible_interval(basis):
                raise ProviderError(
                    "conflict resolution cannot restore expired or historical facts"
                )
            prepared.append((proposal, basis, source))
        if outcome == "conditions_distinguished" and not {m.id for _, m, _ in prepared}.issuperset(
            m.id for m in members
        ):
            raise ProviderError("condition separation must explain every disputed member")
        for proposal, basis, source in prepared:
            memory = MemoryItem(
                id=f"gcf_{uuid4().hex}",
                tenant_id=task.tenant_id,
                subject_type="user",
                subject_id=task.user_id,
                cognitive_type="fact",
                summary=proposal.content[:512],
                content=proposal.content,
                business_domains=basis.business_domains,
                project_domains=basis.project_domains,
                effective_at=(source.occurred_at if outcome == "resolved" else basis.effective_at),
                expired_at=basis.expired_at,
                metadata_json={
                    **(basis.metadata_json or {}),
                    "governance_action": "CLARIFY",
                    "previous_memory_ids": [m.id for m in members],
                },
                created_by="memory_cmic_governance",
            )
            session.add(memory)
            session.flush()
            session.add(
                MemoryEvidence(
                    id=f"evi_{uuid4().hex}",
                    tenant_id=task.tenant_id,
                    relationship_type="supports",
                    evidence_group_id=f"evg_{uuid4().hex}",
                    upstream_source_id=source.id,
                    downstream_memory_id=memory.id,
                    upstream_version=source.version,
                    evidence_snippet=proposal.quote[:512],
                )
            )
            from memory_cmic.task_queue import enqueue_task

            enqueue_task(
                session,
                {
                    "id": uuid4().hex,
                    "tenant_id": task.tenant_id,
                    "task_type": "vector_upsert",
                    "target_type": "memory",
                    "target_id": memory.id,
                    "input_version": memory.version,
                    "idempotency_key": (
                        f"vector_upsert:memory:{memory.id}:v{memory.version}:{model_id}"
                    ),
                    "correlation_id": task.correlation_id,
                    "payload": {"model_id": model_id},
                },
            )
            replacements.append(memory.id)
        for member in members:
            govern_memory(
                session,
                memory=member,
                status="invalidated",
                reason_code="CONFLICT_RESOLVED",
                correlation_id=task.correlation_id,
                task=task,
                reason=review.reason,
            )
        from memory_cmic.profile import enqueue_profile_rebuild

        session.flush()
        enqueue_profile_rebuild(
            session,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            correlation_id=task.correlation_id,
        )
    details = {
        "conflict_group_id": group,
        "fingerprint": fingerprint,
        "outcome": outcome,
        "reason": review.reason,
        "question": review.question,
        "members": [{"id": m.id, "version": m.version} for m in members],
        "replacement_ids": replacements,
    }
    op = _record(session, task, "conflict_review", None, details)
    return {"kind": "conflict_review", "operation_id": op.id, **details}


def _eligible_interval(memory: MemoryItem) -> bool:
    now = datetime.now(UTC)
    return (
        memory.effective_at <= now
        and (memory.expired_at is None or memory.expired_at > now)
        and (memory.metadata_json or {}).get("temporal_kind", "current") != "historical"
    )


def _review_conflicts(session_factory, ownership, model, model_id):
    with session_factory() as session:
        task = session.get(MemoryTask, ownership["task_id"])
        groups = session.scalars(
            select(MemoryItem.conflict_group_id)
            .where(
                MemoryItem.tenant_id == task.tenant_id,
                MemoryItem.subject_type == "user",
                MemoryItem.subject_id == task.user_id,
                MemoryItem.status == "disputed",
                MemoryItem.conflict_group_id.is_not(None),
            )
            .distinct()
            .order_by(MemoryItem.conflict_group_id)
        ).all()
    for group in groups:
        with session_factory() as session:
            task = session.get(MemoryTask, ownership["task_id"])
            memories = _conflict_input(session, task, group)
            fingerprint, edges = _evidence_snapshot(session, memories)
            previous = session.scalars(
                select(GovernanceOperation)
                .where(
                    GovernanceOperation.tenant_id == task.tenant_id,
                    GovernanceOperation.user_id == task.user_id,
                    GovernanceOperation.kind == "conflict_review",
                )
                .order_by(GovernanceOperation.created_at.desc())
            ).all()
            cached = next(
                (
                    op
                    for op in previous
                    if op.details.get("conflict_group_id") == group
                    and op.details.get("fingerprint") == fingerprint
                ),
                None,
            )
            anchor = next(m for m in memories if m.conflict_group_id == group)
            payload = _payload(
                session, anchor, memories, edges, getattr(model, "input_limit", 9952)
            )
            if payload is not None:
                payload["phase"] = "conflicts"
                payload["conflict_group_id"] = group
            generations = {
                row.memory_id: row.generation
                for row in session.scalars(
                    select(GovernancePending).where(
                        GovernancePending.tenant_id == task.tenant_id,
                        GovernancePending.user_id == task.user_id,
                    )
                )
            }
            if (payload is None or len(payload["memories"]) != len(memories)
                    or len(memories) > task.payload["limits"]["memories"]):
                with session_factory.begin() as writer:
                    current_task = _leased_task(writer, **ownership)
                    current_task.result_json = [*(current_task.result_json or []), {
                        "kind": "skip", "reason": "CONFLICT_INPUT_BUDGET",
                        "conflict_group_id": group,
                    }]
                continue  # Never adjudicate a truncated conflict group.
        decision = None if cached else _consume_call(session_factory, ownership, model, payload)
        if not cached and decision is None:
            break
        with session_factory.begin() as session:
            task = _leased_task(session, **ownership)
            lock_user(session, task.tenant_id, task.user_id)
            current = _conflict_input(session, task, group)
            if _evidence_snapshot(session, current)[0] != fingerprint:
                task.result_json = [
                    *(task.result_json or []),
                    {"kind": "skip", "reason": "INPUT_CHANGED", "conflict_group_id": group},
                ]
                continue
            if cached:
                result = {
                    "kind": "conflict_review",
                    "operation_id": cached.id,
                    "cached": True,
                    **cached.details,
                }
            else:
                review = decision.conflict_review or ConflictReview(
                    outcome="unresolved",
                    reason="没有充分的用户澄清证据",
                    question="请明确这些争议陈述当前分别适用于哪些条件，或哪条已被纠正。",
                )
                result = _apply_conflict_review(
                    session, task, group, current, fingerprint, review, model_id
                )
            task.result_json = [*(task.result_json or []), result]
            for memory in current:
                if memory.conflict_group_id != group:
                    continue
                pending = session.get(GovernancePending, (task.tenant_id, task.user_id, memory.id))
                if pending and pending.generation == generations.get(memory.id):
                    session.delete(pending)


def _scope_inferences(session: Session, anchor: MemoryItem) -> list[MemoryItem]:
    aliases = _scene_aliases(session, anchor.tenant_id)
    return [
        m
        for m in session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == anchor.tenant_id,
                MemoryItem.subject_type == "user",
                MemoryItem.subject_id == anchor.subject_id,
                MemoryItem.cognitive_type == "inference",
                MemoryItem.status == "active",
            )
        )
        if _scope_key(m, aliases)[1:] == _scope_key(anchor, aliases)[1:]
    ]


def _review_inference(session, task, proposal, existing, available, edges):
    inference = existing.get(proposal.memory_id)
    if inference is None:
        raise ProviderError("inference review cites an unprovided inference")
    if proposal.outcome == "retain":
        return None
    source = session.get(SourceRecord, proposal.source_id) if proposal.source_id else None
    if (
        source is None
        or source.tenant_id != task.tenant_id
        or source.author_type != "user"
        or source.user_id != task.user_id
        or source.status != "active"
        or not proposal.quote
        or proposal.quote not in source.raw_content
        or source.occurred_at < inference.effective_at
        or not any(
            source.id in {s.id for s in _valid_sources(session, edges[m.id])}
            for m in available.values()
        )
    ):
        raise ProviderError("inference invalidation requires grounded explicit counterevidence")
    invalidate_memory(
        session,
        tenant_id=task.tenant_id,
        memory_id=inference.id,
        task=task,
        correlation_id=task.correlation_id,
        reason_code="INFERENCE_CONTRADICTED",
        reason=proposal.reason,
    )
    _record(
        session,
        task,
        "rejection",
        inference.id,
        {
            "kind": "inference",
            "content": inference.content,
            "source_ids": list(
                session.scalars(
                    select(MemoryEvidence.upstream_memory_id).where(
                        MemoryEvidence.tenant_id == task.tenant_id,
                        MemoryEvidence.downstream_memory_id == inference.id,
                        MemoryEvidence.relationship_type == "derives",
                    )
                )
            ),
            "basis_roots": {
                mid: _roots(session, mid)
                for mid in session.scalars(
                    select(MemoryEvidence.upstream_memory_id).where(
                        MemoryEvidence.tenant_id == task.tenant_id,
                        MemoryEvidence.downstream_memory_id == inference.id,
                    )
                )
            },
            "source_id": source.id,
            "quote": proposal.quote,
            "reason": proposal.reason,
        },
    )
    op = _record(
        session,
        task,
        "inference_review",
        inference.id,
        {
            "outcome": "invalidated",
            "reason": proposal.reason,
            "source_id": source.id,
            "quote": proposal.quote,
        },
    )
    return {"kind": "inference_invalidated", "operation_id": op.id, "memory_ids": [inference.id]}


def _process_anchor(
    session_factory: sessionmaker[Session],
    ownership: dict[str, str],
    memory_id: str,
    generation: int,
    model: ConsolidationModel,
    model_id: str,
) -> tuple[str, list[dict]]:
    results = []
    captured = {}
    covered = True
    for phase in ("facts", "inference"):
        with session_factory() as session:
            task = session.get(MemoryTask, ownership["task_id"])
            pending = session.get(GovernancePending, (task.tenant_id, task.user_id, memory_id))
            anchor = session.get(MemoryItem, memory_id)
            if pending is None:
                return "skip", results
            if pending.generation != generation or anchor is None:
                return "changed", results
            if anchor.status == "disputed":
                return "conflict_pending", results
            if anchor.cognitive_type == "inference":
                groups = list(
                    session.scalars(
                        select(MemoryEvidence.evidence_group_id).where(
                            MemoryEvidence.tenant_id == task.tenant_id,
                            MemoryEvidence.downstream_memory_id == anchor.id,
                        )
                    )
                )
                for group in groups:
                    if evidence_group_is_complete(session, group):
                        basis_id = session.scalar(
                            select(MemoryEvidence.upstream_memory_id)
                            .where(MemoryEvidence.evidence_group_id == group)
                            .limit(1)
                        )
                        anchor = session.get(MemoryItem, basis_id)
                        break
            if anchor.cognitive_type == "fact" and anchor.status != "active":
                survivors = _scope_memories(session, anchor)
                if survivors:
                    # Continue inference on the surviving scope after a merge/invalidation.
                    anchor = survivors[0]
            if (
                anchor.status != "active"
                or anchor.cognitive_type != "fact"
                or not _eligible_interval(anchor)
            ):
                return "skip", results
            memories = _scope_memories(session, anchor)
            existing = _scope_inferences(session, anchor) if phase == "inference" else []
            if len(memories) < 2 and not existing:
                continue
            fingerprint, edges = _evidence_snapshot(session, memories)
            inference_fingerprint = [evidence_fingerprint(session, m) for m in existing]
            payload = _payload(
                session, anchor, memories, edges, getattr(model, "input_limit", 9952)
            )
            if payload is None:
                return "input_budget", results
            payload["phase"] = phase
            if phase == "inference":
                payload["generate_inferences"] = task.payload.get("generate_inferences", True)
                if not payload["generate_inferences"] and not existing:
                    continue
                payload["existing_inferences"] = [
                    {"id": m.id, "content": m.content} for m in existing
                ]
                if input_size(PROMPT, payload) > getattr(model, "input_limit", 9952):
                    return "budget", results
                # Known unresolved contradictions must not feed new conclusions.
                if session.scalar(
                    select(MemoryItem.id)
                    .where(
                        MemoryItem.tenant_id == task.tenant_id,
                        MemoryItem.subject_type == "user",
                        MemoryItem.subject_id == task.user_id,
                        MemoryItem.status == "disputed",
                        MemoryItem.cognitive_type == "fact",
                        MemoryItem.effective_at <= datetime.now(UTC),
                        or_(MemoryItem.expired_at.is_(None),
                            MemoryItem.expired_at > datetime.now(UTC)),
                        MemoryItem.metadata_json["temporal_kind"].as_string()
                        .is_distinct_from("historical"),
                        MemoryItem.business_domains.is_not_distinct_from(anchor.business_domains),
                    )
                    .limit(1)
                ):
                    continue
            allowed_ids = {item["id"] for item in payload["memories"]}
            allowed_sources = {
                source["id"] for item in payload["memories"] for source in item["sources"]
            }
            for row in session.scalars(
                select(GovernancePending).where(
                    GovernancePending.tenant_id == task.tenant_id,
                    GovernancePending.user_id == task.user_id,
                    GovernancePending.memory_id.in_(allowed_ids | {m.id for m in existing}),
                )
            ):
                captured.setdefault(row.memory_id, row.generation)
            omitted = {m.id for m in memories} - allowed_ids
        if omitted:
            # Keep out-of-budget candidates visible even when they were not originally dirty.
            with session_factory.begin() as session:
                task = _leased_task(session, **ownership)
                lock_user(session, task.tenant_id, task.user_id)
                for mid in omitted:
                    session.execute(pg_insert(GovernancePending).values(
                        tenant_id=task.tenant_id, user_id=task.user_id, memory_id=mid,
                    ).on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "memory_id"]))
                if phase == "facts":
                    task.result_json = [*(task.result_json or []), {
                        "kind": "deferred_scope", "reason": "INPUT_BUDGET",
                        "memory_ids": sorted(omitted),
                    }]
        decision = _consume_call(session_factory, ownership, model, payload)
        if decision is None:
            return "budget", results
        if (
            phase == "facts"
            and (
                not set(decision.merge_ids).issubset(allowed_ids)
                or any(
                    p.memory_id not in allowed_ids or p.source_id not in allowed_sources
                    for p in decision.invalidations
                )
                or any(p.memory_id not in allowed_ids for p in decision.suspicions)
            )
            or phase == "inference"
            and (
                any(not set(p.source_ids).issubset(allowed_ids) for p in decision.inferences)
                or any(
                    p.memory_id not in {m.id for m in existing}
                    or (p.source_id and p.source_id not in allowed_sources)
                    for p in decision.inference_reviews
                )
            )
        ):
            raise ProviderError("consolidation decision references unprovided evidence")
        with session_factory.begin() as session:
            task = _leased_task(session, **ownership)
            lock_user(session, task.tenant_id, task.user_id)
            pending = session.get(GovernancePending, (task.tenant_id, task.user_id, memory_id))
            if pending is None or pending.generation != generation:
                return "changed", results
            anchor = session.get(MemoryItem, anchor.id)
            current = _scope_memories(session, anchor)
            current_fingerprint, current_edges = _evidence_snapshot(session, current)
            fresh_inferences = _scope_inferences(session, anchor) if phase == "inference" else []
            if (
                current_fingerprint != fingerprint
                or [evidence_fingerprint(session, m) for m in fresh_inferences]
                != inference_fingerprint
            ):
                return "changed", results
            available = {m.id: m for m in current if m.id in allowed_ids}
            changes = []
            if phase == "facts":
                chosen = [available[mid] for mid in decision.merge_ids if mid != anchor.id]
                if decision.merge_ids and not chosen:
                    covered = False
                if (
                    chosen
                    and len(decision.merge_ids) == len(set(decision.merge_ids))
                    and all(_same_interval(anchor, m) for m in chosen)
                ):
                    changes.extend(
                        _merge(session, task, anchor, chosen, current_edges, decision.merge_reason)
                    )
                for proposal in decision.invalidations:
                    result = _invalidate(session, task, proposal, available)
                    if result:
                        changes.append(result)
                for proposal in decision.suspicions:
                    op = _record(
                        session,
                        task,
                        "suspected_stale",
                        proposal.memory_id,
                        {
                            "reason": proposal.reason,
                            "version": available[proposal.memory_id].version,
                        },
                    )
                    changes.append(
                        {
                            "kind": "suspected_stale",
                            "operation_id": op.id,
                            "memory_ids": [proposal.memory_id],
                        }
                    )
            else:
                rejected = session.scalars(
                    select(GovernanceOperation).where(
                        GovernanceOperation.tenant_id == task.tenant_id,
                        GovernanceOperation.user_id == task.user_id,
                        GovernanceOperation.kind == "rejection",
                    )
                ).all()
                for proposal in decision.inference_reviews:
                    result = _review_inference(
                        session,
                        task,
                        proposal,
                        {m.id: m for m in fresh_inferences},
                        available,
                        current_edges,
                    )
                    if result:
                        changes.append(result)
                session.flush()
                rejected = session.scalars(select(GovernanceOperation).where(
                    GovernanceOperation.tenant_id == task.tenant_id,
                    GovernanceOperation.user_id == task.user_id,
                    GovernanceOperation.kind == "rejection",
                )).all()
                for proposal in decision.inferences:
                    if not task.payload.get("generate_inferences", True):
                        break
                    result = _infer(
                        session, task, proposal, available, current_edges, rejected, model_id
                    )
                    if result:
                        changes.append(result)
            results.extend(changes)
            task.result_json = [*(task.result_json or []), *changes]
            task.payload = {
                **task.payload,
                "execution": {
                    "changed": bool(changes)
                    or task.payload.get("execution", {}).get("changed", False)
                },
            }
    with session_factory.begin() as session:
        task = _leased_task(session, **ownership)
        lock_user(session, task.tenant_id, task.user_id)
        for mid, expected in {memory_id: generation, **(captured if covered else {})}.items():
            pending = session.get(GovernancePending, (task.tenant_id, task.user_id, mid))
            if pending is not None and pending.generation == expected:
                session.delete(pending)
        if not results:
            task.result_json = [
                *(task.result_json or []),
                {"kind": "skip", "memory_ids": [memory_id], "reason": "NO_VALID_CHANGE"},
            ]
        task.payload = {**task.payload, "cursor": task.payload["cursor"] + 1}
    return "committed", results


def process_run(
    session_factory: sessionmaker[Session],
    *,
    ownership: dict[str, str],
    model: ConsolidationModel,
    model_id: str,
) -> None:
    with session_factory.begin() as session:
        task = _leased_task(session, **ownership)
        if "selection" not in task.payload:
            rows = session.scalars(
                select(GovernancePending)
                .where(
                    GovernancePending.tenant_id == task.tenant_id,
                    GovernancePending.user_id == task.user_id,
                )
                .order_by(GovernancePending.first_change_at, GovernancePending.memory_id)
                .limit(task.payload["limits"]["memories"])
            ).all()
            task.payload = {
                **task.payload,
                "selection": [[r.memory_id, r.generation] for r in rows],
                "cursor": 0,
                "model_calls": 0,
            }
    _review_conflicts(session_factory, ownership, model, model_id)
    while True:
        with session_factory() as session:
            task = session.get(MemoryTask, ownership["task_id"])
            selection = task.payload["selection"]
            cursor = task.payload["cursor"]
            calls = task.payload["model_calls"]
            if cursor >= len(selection) or calls >= task.payload["limits"]["model_calls"]:
                break
            memory_id, generation = selection[cursor]
        disposition, _results = _process_anchor(
            session_factory, ownership, memory_id, generation, model, model_id
        )
        if disposition == "committed":
            continue
        with session_factory.begin() as session:
            task = _leased_task(session, **ownership)
            if disposition not in {"changed", "budget", "input_budget", "conflict_pending"}:
                pending = session.get(GovernancePending, (task.tenant_id, task.user_id, memory_id))
                if pending is not None and pending.generation == generation:
                    session.delete(pending)
            task.result_json = [
                *(task.result_json or []),
                {
                    "kind": "skip",
                    "memory_ids": [memory_id],
                    "reason": {"changed": "INPUT_CHANGED", "budget": "MODEL_BUDGET",
                               "input_budget": "INPUT_BUDGET",
                               "conflict_pending": "CONFLICT_PENDING"}.get(
                                   disposition, "NOT_ELIGIBLE"),
                },
            ]
            task.payload = {**task.payload, "cursor": task.payload["cursor"] + 1}
    with session_factory.begin() as session:
        task = _leased_task(session, **ownership)
        remaining = session.scalar(
            select(func.count())
            .select_from(GovernancePending)
            .where(
                GovernancePending.tenant_id == task.tenant_id,
                GovernancePending.user_id == task.user_id,
            )
        )
        state = session.get(GovernanceSubjectState, (task.tenant_id, task.user_id))
        if state is None:
            state = GovernanceSubjectState(tenant_id=task.tenant_id, user_id=task.user_id)
            session.add(state)
        state.last_run_at = datetime.now(UTC)
        status = "partial" if remaining else "succeeded"
        task.payload = {**task.payload, "remaining": remaining}
        if not finish_task(
            session,
            **ownership,
            status=status,
            results=task.result_json or [],
            error={"code": "WORK_REMAINING", "message": f"{remaining} memories remain"}
            if remaining
            else None,
        ):
            raise LeaseLostError("consolidation lease was lost")


def run_once(
    session_factory: sessionmaker[Session],
    *,
    worker_id: str,
    model: ConsolidationModel,
    model_id: str,
) -> bool:
    with session_factory.begin() as session:
        finish_exhausted_leases(session)
        schedule_due(session)
        disabled = session.scalars(
            select(MemoryTask)
            .join(GovernancePolicy, GovernancePolicy.tenant_id == MemoryTask.tenant_id)
            .where(
                MemoryTask.task_type == "consolidate",
                MemoryTask.status == "pending",
                MemoryTask.payload["automatic"].as_boolean().is_(True),
                GovernancePolicy.auto_enabled.is_(False),
            )
        ).all()
        for task in disabled:
            task.status = "cancelled"
            task.completed_at = datetime.now(UTC)
    with session_factory() as session:
        tenants = session.scalars(
            select(MemoryTask.tenant_id)
            .where(
                MemoryTask.task_type == "consolidate",
                MemoryTask.status.in_(["pending", "processing"]),
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
                task_types=("consolidate",),
            )
            task = claimed[0] if claimed else None
            if task is not None:
                ownership = {
                    "tenant_id": tenant_id,
                    "task_id": task.id,
                    "worker_id": worker_id,
                    "lease_token": task.lease_token,
                }
        if task is None:
            continue
        try:
            with _lease_heartbeat(session_factory, **ownership, lease_seconds=180):
                process_run(session_factory, ownership=ownership, model=model, model_id=model_id)
        except LeaseLostError:
            logger.warning("consolidation task %s lost lease", ownership["task_id"])
        except Exception as exc:
            logger.exception("consolidation task %s failed", ownership["task_id"])
            with session_factory.begin() as session:
                fail_and_retry_task(
                    session,
                    **ownership,
                    error=str(exc),
                    error_code="CONSOLIDATION_FAILED",
                    base_backoff_seconds=2,
                )
        return True
    return False


class RevertConflict(ValueError):
    pass


def revert_operation(
    session: Session,
    *,
    operation: GovernanceOperation,
    expected_version: int,
    operator_id: str,
    reason: str,
    model_id: str,
    idempotency_key: str | None = None,
) -> GovernanceOperation:
    if operation.reverted_at is not None or operation.kind not in {
        "merge",
        "inference",
        "invalidate",
    }:
        raise RevertConflict("operation is not reversible")
    lock_user(session, operation.tenant_id, operation.user_id)
    details = operation.details
    memory = session.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == operation.tenant_id,
            MemoryItem.id == operation.memory_id,
        )
        .with_for_update()
    ).one_or_none()
    if (
        memory is None
        or memory.version != expected_version
        or memory.version
        != details.get("loser_version" if operation.kind == "merge" else "version")
    ):
        raise RevertConflict("memory changed since the operation")
    now = datetime.now(UTC)
    original_task = session.get(MemoryTask, operation.task_id)
    correlation_id = uuid4().hex
    before = {"status": memory.status, "version": memory.version}
    audit_context = {
        "operation_id": operation.id,
        "original_correlation_id": original_task.correlation_id if original_task else None,
    }
    if operation.kind == "inference":
        if memory.status != "active":
            raise RevertConflict("inference is no longer active")
        invalidate_memory(
            session,
            tenant_id=operation.tenant_id,
            memory_id=memory.id,
            correlation_id=correlation_id,
            task=original_task,
            reason_code="GOVERNANCE_REVERTED",
            reason=reason,
            operator_type="admin",
            operator_id=operator_id,
            context=audit_context,
        )
        rejection = {
            "kind": "inference",
            "content": details["content"],
            "source_ids": details["source_ids"],
            "basis_roots": {mid: _roots(session, mid) for mid in details["source_ids"]},
            "reason": reason,
        }
    else:
        if memory.status != "invalidated" or (
            memory.expired_at is not None and memory.expired_at <= now
        ):
            raise RevertConflict("memory is no longer eligible for restoration")
        scope_after, _ = _evidence_snapshot(session, _scope_memories(session, memory))
        if scope_after != details.get("scope_after"):
            raise RevertConflict("facts or evidence changed in this scope")
        if operation.kind == "merge":
            winner = session.scalars(
                select(MemoryItem)
                .where(
                    MemoryItem.tenant_id == operation.tenant_id,
                    MemoryItem.id == details["winner_id"],
                )
                .with_for_update()
            ).one_or_none()
            if (
                winner is None
                or winner.status != "active"
                or winner.version != details["winner_version"]
            ):
                raise RevertConflict("merge survivor changed")
            copied = session.scalars(
                select(MemoryEvidence)
                .where(
                    MemoryEvidence.tenant_id == operation.tenant_id,
                    MemoryEvidence.id.in_(details["copied_edge_ids"]),
                )
                .with_for_update()
            ).all()
            if len(copied) != len(details["copied_edge_ids"]) or any(
                e.status != "active" for e in copied
            ):
                raise RevertConflict("merged evidence changed")
            if (
                _roots(session, winner.id, excluded=set(details["copied_edge_ids"]))
                != details["winner_roots"]
                or _roots(session, memory.id) != details["loser_roots"]
            ):
                raise RevertConflict("original evidence changed")
            evidence_count = session.scalar(select(func.count()).select_from(MemoryEvidence).where(
                MemoryEvidence.tenant_id == operation.tenant_id,
                MemoryEvidence.downstream_memory_id == winner.id,
                MemoryEvidence.status == "active",
            ))
            for edge in copied:
                edge.status = "invalidated"
                edge.invalidated_at = now
            record_audit(
                session,
                tenant_id=operation.tenant_id,
                action="UPDATE",
                target_type="memory",
                target_id=winner.id,
                reason_code="GOVERNANCE_REVERTED",
                reason=reason,
                operator_type="admin",
                operator_id=operator_id,
                task=original_task,
                correlation_id=correlation_id,
                context=audit_context,
                state_before={"evidence_count": evidence_count},
                state_after={"evidence_count": evidence_count - len(copied)},
            )
            rejection = {
                "kind": "merge",
                "winner_id": winner.id,
                "loser_id": memory.id,
                "winner_roots": details["winner_roots"],
                "loser_roots": details["loser_roots"],
                "reason": reason,
            }
        else:
            edges = session.scalars(
                select(MemoryEvidence).where(
                    MemoryEvidence.tenant_id == operation.tenant_id,
                    MemoryEvidence.downstream_memory_id == memory.id,
                )
            ).all()
            groups = {edge.evidence_group_id for edge in edges}
            if not any(evidence_group_is_complete(session, group) for group in groups):
                raise RevertConflict("source evidence is no longer sufficient")
            rejection = {
                "kind": "invalidate",
                "memory_id": memory.id,
                "source_id": details["source_id"],
                "source_version": (
                    source.version
                    if (source := session.get(SourceRecord, details["source_id"]))
                    else None
                ),
                "reason": reason,
            }
        later = session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == operation.tenant_id,
                MemoryItem.subject_type == "user",
                MemoryItem.subject_id == operation.user_id,
                MemoryItem.cognitive_type == "fact",
                MemoryItem.status == "active",
                MemoryItem.created_at > operation.created_at,
            )
        ).all()
        aliases = _scene_aliases(session, operation.tenant_id)
        if any(_scope_key(item, aliases) == _scope_key(memory, aliases) for item in later):
            raise RevertConflict("new facts appeared in this scope")
        memory.status = "active"
        memory.version += 1
        memory.invalidated_at = None
        memory.updated_at = now
        from memory_cmic.task_queue import enqueue_task

        enqueue_task(
            session,
            {
                "id": uuid4().hex,
                "tenant_id": operation.tenant_id,
                "task_type": "vector_upsert",
                "target_type": "memory",
                "target_id": memory.id,
                "input_version": memory.version,
                "idempotency_key": f"vector_upsert:memory:{memory.id}:v{memory.version}:{model_id}",
                "correlation_id": correlation_id,
                "payload": {"model_id": model_id, **audit_context},
                "priority": 50,
            },
        )
    operation.reverted_at = now
    feedback = GovernanceOperation(
        id=f"gop_{uuid4().hex}",
        tenant_id=operation.tenant_id,
        user_id=operation.user_id,
        task_id=operation.task_id,
        kind="rejection",
        memory_id=memory.id,
        details={
            **rejection,
            "reverted_operation_id": operation.id,
            "idempotency_key": idempotency_key,
        },
    )
    session.add(feedback)
    if operation.kind != "inference":
        record_audit(
            session,
            tenant_id=operation.tenant_id,
            action="UPDATE",
            target_type="memory",
            target_id=memory.id,
            operator_type="admin",
            operator_id=operator_id,
            reason_code="GOVERNANCE_REVERTED",
            reason=reason,
            correlation_id=correlation_id,
            task=original_task,
            context=audit_context,
            state_before=before,
            state_after={
                "version": memory.version,
                "status": memory.status,
            },
        )
    return feedback
