"""Export unclassified facts for TTL review without changing stored memories."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.db import create_database_engine
from memory_cmic.models import MemoryEvidence, MemoryItem, ProfileProperty


def build_report(session: Session, tenant_id: str | None = None) -> list[dict]:
    statement = select(MemoryItem).where(
        MemoryItem.cognitive_type == "fact",
        MemoryItem.status == "active",
        MemoryItem.metadata_json["retention_kind"].as_string().is_(None),
    ).order_by(MemoryItem.tenant_id, MemoryItem.id)
    if tenant_id is not None:
        statement = statement.where(MemoryItem.tenant_id == tenant_id)
    report = []
    for memory in session.scalars(statement):
        profiles = session.scalars(
            select(ProfileProperty)
            .join(MemoryEvidence, (
                (MemoryEvidence.tenant_id == ProfileProperty.tenant_id)
                & (MemoryEvidence.downstream_profile_id == ProfileProperty.id)
            ))
            .where(
                MemoryEvidence.tenant_id == memory.tenant_id,
                MemoryEvidence.upstream_memory_id == memory.id,
                MemoryEvidence.relationship_type == "profile_basis",
                MemoryEvidence.status == "active",
                ProfileProperty.status == "active",
            )
            .order_by(ProfileProperty.id)
        ).all()
        report.append({
            "tenant_id": memory.tenant_id,
            "id": memory.id,
            "version": memory.version,
            "content": memory.content,
            "metadata": memory.metadata_json,
            "effective_at": memory.effective_at.isoformat(),
            "expired_at": memory.expired_at.isoformat() if memory.expired_at else None,
            "profiles": [
                {"id": profile.id, "value": profile.property_value} for profile in profiles
            ],
            "proposed_retention_kind": None,
            "reason": "需核对完整陈述；画像依据不代表整条事实都应长期保留",
        })
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id")
    parser.add_argument("--report", type=Path, default=Path("/tmp/memory_cmic_ttl_review.jsonl"))
    args = parser.parse_args()
    engine = create_database_engine()
    try:
        with Session(engine) as session:
            records = build_report(session, args.tenant_id)
        args.report.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
        )
        print(f"{len(records)} facts exported to {args.report}; database unchanged")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
