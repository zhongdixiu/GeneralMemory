from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.models import MemoryEvidence, MemoryItem, ProfileProperty, SourceRecord


def memory_has_valid_evidence(
    session: Session,
    memory: MemoryItem,
    *,
    tenant_id: str,
    user_id: str,
    now: datetime | None = None,
    checked: dict[str, bool] | None = None,
    allowed_source_ids: set[str] | None = None,
    visiting: set[str] | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    checked = checked if checked is not None else {}
    visiting = visiting or set()
    if memory.id in visiting:
        return False
    if memory.id in checked:
        return checked[memory.id]
    if (
        memory.tenant_id != tenant_id
        or memory.subject_type != "user"
        or memory.subject_id != user_id
        or memory.cognitive_type not in {"fact", "inference"}
        or memory.status != "active"
        or memory.effective_at > now
        or (memory.expired_at is not None and memory.expired_at <= now)
    ):
        return False
    groups = set(
        session.scalars(
            select(MemoryEvidence.evidence_group_id).where(
                MemoryEvidence.tenant_id == tenant_id,
                MemoryEvidence.downstream_memory_id == memory.id,
            )
        )
    )
    valid = any(
        evidence_group_is_complete(
            session,
            group,
            now=now,
            checked=checked,
            allowed_source_ids=allowed_source_ids,
            visiting=visiting | {memory.id},
        )
        for group in groups
    )
    # A cycle-dependent failure is not reusable for another independent path.
    if valid:
        checked[memory.id] = True
    return valid


def evidence_group_is_complete(
    session: Session,
    evidence_group_id: str,
    *,
    now: datetime | None = None,
    checked: dict[str, bool] | None = None,
    allowed_source_ids: set[str] | None = None,
    visiting: set[str] | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    edges = session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.evidence_group_id == evidence_group_id,
        )
    ).all()
    if (
        not edges
        or len({(e.tenant_id, e.downstream_memory_id, e.downstream_profile_id) for e in edges}) != 1
    ):
        return False
    first = edges[0]
    downstream = session.get(
        MemoryItem if first.downstream_memory_id else ProfileProperty,
        first.downstream_memory_id or first.downstream_profile_id,
    )
    if downstream is None or downstream.tenant_id != first.tenant_id:
        return False
    user_id = (
        downstream.subject_id
        if isinstance(downstream, MemoryItem) and downstream.subject_type == "user"
        else downstream.user_id
        if isinstance(downstream, ProfileProperty)
        else None
    )
    relationship = (
        "profile_basis"
        if isinstance(downstream, ProfileProperty)
        else "supports"
        if downstream.cognitive_type == "fact"
        else "derives"
        if downstream.cognitive_type == "inference"
        else None
    )
    for edge in edges:
        if edge.status != "active" or (relationship and edge.relationship_type != relationship):
            return False
        if edge.upstream_source_id:
            upstream = session.get(SourceRecord, edge.upstream_source_id)
            if (
                upstream is None
                or upstream.tenant_id != edge.tenant_id
                or upstream.status != "active"
                or upstream.version != edge.upstream_version
                or (relationship in {"derives", "profile_basis"})
                or (allowed_source_ids is not None and upstream.id not in allowed_source_ids)
                or (user_id and upstream.user_id and upstream.user_id != user_id)
                or (user_id and upstream.author_type == "user" and upstream.author_id != user_id)
            ):
                return False
        else:
            upstream = session.get(MemoryItem, edge.upstream_memory_id)
            if (
                upstream is None
                or upstream.tenant_id != edge.tenant_id
                or upstream.status != "active"
                or upstream.version != edge.upstream_version
                or upstream.effective_at > now
                or (upstream.expired_at is not None and upstream.expired_at <= now)
                or relationship == "supports"
                or (relationship == "derives" and upstream.cognitive_type != "fact")
            ):
                return False
            if user_id and not memory_has_valid_evidence(
                session,
                upstream,
                tenant_id=edge.tenant_id,
                user_id=user_id,
                now=now,
                checked=checked,
                allowed_source_ids=allowed_source_ids,
                visiting=visiting,
            ):
                return False
    return True


def evidence_fingerprint(session: Session, memory: MemoryItem) -> list[tuple]:
    """Include necessary upstream versions and sources, even before propagation runs."""
    entries: list[tuple] = []
    seen: set[str] = set()

    def visit(current: MemoryItem) -> None:
        if current.id in seen:
            return
        seen.add(current.id)
        entries.append(
            (
                "memory",
                current.id,
                current.version,
                current.status,
                current.content,
                current.business_domains,
                current.project_domains,
                current.effective_at.isoformat(),
                current.expired_at.isoformat() if current.expired_at else None,
                {
                    key: (current.metadata_json or {}).get(key)
                    for key in (
                        "temporal_kind",
                        "retention_kind",
                        "project_context",
                        "project_limited",
                    )
                },
            )
        )
        for edge in session.scalars(
            select(MemoryEvidence)
            .where(
                MemoryEvidence.tenant_id == current.tenant_id,
                MemoryEvidence.downstream_memory_id == current.id,
            )
            .order_by(MemoryEvidence.id)
        ):
            entries.append(
                (
                    "edge",
                    edge.id,
                    edge.status,
                    edge.relationship_type,
                    edge.evidence_group_id,
                    edge.upstream_version,
                )
            )
            if edge.upstream_source_id:
                source = session.get(SourceRecord, edge.upstream_source_id)
                entries.append(
                    (
                        "source",
                        edge.upstream_source_id,
                        source.version if source else None,
                        source.status if source else None,
                        source.content_hash if source else None,
                    )
                )
            elif edge.upstream_memory_id:
                basis = session.get(MemoryItem, edge.upstream_memory_id)
                if basis:
                    visit(basis)

    visit(memory)
    return entries
