from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.models import MemoryEvidence, MemoryItem, ProfileProperty, SourceRecord


class EvidenceGroupError(ValueError):
    pass


class EvidenceGroupDownstreamMismatch(EvidenceGroupError):
    pass


class EvidenceGroupImmutable(EvidenceGroupError):
    pass


def normalize_scope(values: Sequence[str] | None) -> list[str] | None:
    if values is None:
        return None
    return sorted(set(values))


def _values(data: Mapping[str, Any], *, metadata: bool = False) -> dict[str, Any]:
    values = dict(data)
    if metadata and "metadata" in values:
        values["metadata_json"] = values.pop("metadata")
    return values


def create_source(session: Session, data: Mapping[str, Any]) -> SourceRecord:
    source = SourceRecord(**_values(data, metadata=True))
    session.add(source)
    session.flush()
    return source


def create_memory(session: Session, data: Mapping[str, Any]) -> MemoryItem:
    values = _values(data, metadata=True)
    values["business_domains"] = normalize_scope(values.get("business_domains"))
    values["project_domains"] = normalize_scope(values.get("project_domains"))
    memory = MemoryItem(**values)
    session.add(memory)
    session.flush()
    return memory


def create_profile(session: Session, data: Mapping[str, Any]) -> ProfileProperty:
    values = dict(data)
    values["business_domains"] = normalize_scope(values.get("business_domains"))
    profile = ProfileProperty(**values)
    session.add(profile)
    session.flush()
    return profile


def _downstream(edge: Mapping[str, Any] | MemoryEvidence) -> tuple[str | None, str | None]:
    if isinstance(edge, MemoryEvidence):
        return edge.downstream_memory_id, edge.downstream_profile_id
    return edge.get("downstream_memory_id"), edge.get("downstream_profile_id")


def add_evidence_group(
    session: Session, edges: Sequence[Mapping[str, Any]]
) -> list[MemoryEvidence]:
    if not edges:
        raise EvidenceGroupError("an evidence group must contain at least one edge")

    group_ids = {str(edge["evidence_group_id"]) for edge in edges}
    downstreams = {_downstream(edge) for edge in edges}
    if len(group_ids) != 1 or len(downstreams) != 1:
        raise EvidenceGroupError("all edges must belong to one group and one downstream")

    group_id = next(iter(group_ids))
    existing = session.scalars(
        select(MemoryEvidence).where(MemoryEvidence.evidence_group_id == group_id)
    ).all()
    if existing:
        if {_downstream(edge) for edge in existing} != downstreams:
            raise EvidenceGroupDownstreamMismatch(
                f"evidence group {group_id} already belongs to another downstream"
            )
        raise EvidenceGroupImmutable(f"evidence group {group_id} is immutable")

    records = [MemoryEvidence(**dict(edge)) for edge in edges]
    session.add_all(records)
    session.flush()
    return records
