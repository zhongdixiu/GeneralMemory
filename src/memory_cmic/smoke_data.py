from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from memory_cmic.db import create_database_engine
from memory_cmic.deterministic_embedding import deterministic_embedding
from memory_cmic.models import MemoryEmbedding, MemoryEvidence, MemoryItem, SourceRecord

DEFAULT_MEMORY_COUNT = 3_000
SMOKE_TENANT_ID = "tenant_wp5_smoke"
SMOKE_MODEL_ID = "fake-embedding-v1"
SECONDARY_MODEL_ID = "fake-embedding-v2"
_BATCH_SIZE = 100


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _batches(total: int) -> Iterator[range]:
    for start in range(0, total, _BATCH_SIZE):
        yield range(start, min(start + _BATCH_SIZE, total))


def _subject(index: int) -> tuple[str, str]:
    bucket = index % 20
    if bucket < 12:
        return "user", "user_wp5"
    if bucket < 17:
        return "agent", "agent_wp5"
    return "company", "company_wp5"


def _business_domains(index: int) -> list[str] | None:
    return (
        None,
        ["email"],
        ["disk"],
        ["communication"],
        ["disk", "email"],
        ["communication", "email"],
    )[index % 6]


def _project_domains(index: int) -> list[str] | None:
    return (
        None,
        ["project_a"],
        ["project_b"],
        ["project_c"],
        ["project_a", "project_b"],
    )[index % 5]


def _memory_state(index: int, generated_at: datetime) -> tuple[str, datetime | None]:
    bucket = index % 100
    if bucket == 0:
        return "invalidated", None
    if bucket == 1:
        return "disputed", None
    if bucket == 2:
        return "expired", generated_at - timedelta(days=1)
    if bucket == 3:
        return "active", generated_at - timedelta(minutes=1)
    if bucket == 4:
        return "active", generated_at + timedelta(days=30)
    return "active", None


def _delete_previous_data(session: Session) -> None:
    session.execute(delete(MemoryEvidence).where(MemoryEvidence.tenant_id == SMOKE_TENANT_ID))
    session.execute(delete(MemoryEmbedding).where(MemoryEmbedding.tenant_id == SMOKE_TENANT_ID))
    session.execute(delete(MemoryItem).where(MemoryItem.tenant_id == SMOKE_TENANT_ID))
    session.execute(delete(SourceRecord).where(SourceRecord.tenant_id == SMOKE_TENANT_ID))


def prepare_smoke_data(
    session: Session,
    *,
    memory_count: int = DEFAULT_MEMORY_COUNT,
    generated_at: datetime | None = None,
) -> dict[str, int | str]:
    """Replace the dedicated WP5 synthetic dataset and return inserted row counts."""
    if memory_count <= 0:
        raise ValueError("memory_count must be positive")

    generated_at = generated_at or datetime.now(UTC)
    _delete_previous_data(session)

    embedding_count = 0
    for indexes in _batches(memory_count):
        source_rows = []
        memory_rows = []
        evidence_rows = []
        embedding_rows = []

        for index in indexes:
            sequence = index + 1
            suffix = f"{sequence:08d}"
            source_id = f"wp5-src-{suffix}"
            memory_id = f"wp5-mem-{suffix}"
            subject_type, subject_id = _subject(index)
            business_domains = _business_domains(index)
            project_domains = _project_domains(index)
            status, expired_at = _memory_state(index, generated_at)
            topic = (business_domains or ["general"])[0]
            summary = f"虚构 {topic} 记忆 {suffix}"
            content = f"用于第五批范围和向量检索冒烟的虚构内容，序号 {suffix}。"
            memory_text = f"{summary}\n{content}"
            content_hash = _sha256(memory_text)

            source_rows.append(
                {
                    "id": source_id,
                    "tenant_id": SMOKE_TENANT_ID,
                    "source_system": "wp5_smoke_generator",
                    "source_type": "chat",
                    "external_ref_id": f"wp5-message-{suffix}",
                    "author_type": "user",
                    "author_id": "user_wp5",
                    "raw_content": content,
                    "content_hash": _sha256(content),
                    "metadata_json": {"synthetic": True, "sequence": sequence},
                    "status": "active",
                    "version": 1,
                    "occurred_at": generated_at - timedelta(seconds=index),
                }
            )
            memory_rows.append(
                {
                    "id": memory_id,
                    "tenant_id": SMOKE_TENANT_ID,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "cognitive_type": ("fact", "inference", "skill", "rule")[index % 4],
                    "business_domains": business_domains,
                    "project_domains": project_domains,
                    "summary": summary,
                    "content": content,
                    "metadata_json": {"synthetic": True, "sequence": sequence},
                    "confidence": 0.9,
                    "status": status,
                    "version": 1,
                    "effective_at": generated_at - timedelta(seconds=index),
                    "expired_at": expired_at,
                    "invalidated_at": generated_at if status == "invalidated" else None,
                    "created_by": "wp5_smoke_generator",
                }
            )
            evidence_rows.append(
                {
                    "id": f"wp5-evd-{suffix}",
                    "tenant_id": SMOKE_TENANT_ID,
                    "relationship_type": "supports",
                    "evidence_group_id": f"wp5-grp-{suffix}",
                    "upstream_source_id": source_id,
                    "downstream_memory_id": memory_id,
                    "upstream_version": 1,
                    "evidence_snippet": content,
                    "status": "active",
                }
            )

            embedding_rows.append(
                {
                    "tenant_id": SMOKE_TENANT_ID,
                    "memory_id": memory_id,
                    "model_id": SMOKE_MODEL_ID,
                    "content_hash": "0" * 64 if index % 100 == 6 else content_hash,
                    "embedding": deterministic_embedding(memory_text, SMOKE_MODEL_ID),
                    "status": "stale" if index % 100 == 5 else "active",
                }
            )
            if index % 100 == 7:
                embedding_rows.append(
                    {
                        "tenant_id": SMOKE_TENANT_ID,
                        "memory_id": memory_id,
                        "model_id": SECONDARY_MODEL_ID,
                        "content_hash": content_hash,
                        "embedding": deterministic_embedding(memory_text, SECONDARY_MODEL_ID),
                        "status": "active",
                    }
                )

        session.execute(insert(SourceRecord), source_rows)
        session.execute(insert(MemoryItem), memory_rows)
        session.execute(insert(MemoryEvidence), evidence_rows)
        session.execute(insert(MemoryEmbedding), embedding_rows)
        embedding_count += len(embedding_rows)

    session.flush()
    return {
        "tenant_id": SMOKE_TENANT_ID,
        "memory_items": memory_count,
        "source_records": memory_count,
        "evidence_edges": memory_count,
        "embeddings": embedding_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare synthetic data for the WP5 smoke run.")
    parser.add_argument("--count", type=int, default=DEFAULT_MEMORY_COUNT)
    args = parser.parse_args()

    database_url = os.environ.get("MEMORY_DATABASE_URL")
    if not database_url:
        parser.error("MEMORY_DATABASE_URL must be set")

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            result = prepare_smoke_data(session, memory_count=args.count)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
