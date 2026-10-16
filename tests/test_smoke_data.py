from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory_cmic.models import MemoryEmbedding, MemoryEvidence, MemoryItem, SourceRecord
from memory_cmic.smoke_data import (
    SECONDARY_MODEL_ID,
    SMOKE_MODEL_ID,
    SMOKE_TENANT_ID,
    prepare_smoke_data,
)

GENERATED_AT = datetime(2026, 9, 21, 8, tzinfo=UTC)


def _count(session: Session, model: type[object]) -> int:
    return session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == SMOKE_TENANT_ID)
    )


def test_prepare_smoke_data_builds_required_distribution(session: Session) -> None:
    result = prepare_smoke_data(session, memory_count=120, generated_at=GENERATED_AT)

    assert result == {
        "tenant_id": SMOKE_TENANT_ID,
        "memory_items": 120,
        "source_records": 120,
        "evidence_edges": 120,
        "embeddings": 122,
    }
    assert _count(session, SourceRecord) == 120
    assert _count(session, MemoryItem) == 120
    assert _count(session, MemoryEvidence) == 120
    assert _count(session, MemoryEmbedding) == 122

    subject_counts = {
        subject_type: count
        for subject_type, count in session.execute(
            select(MemoryItem.subject_type, func.count())
            .where(MemoryItem.tenant_id == SMOKE_TENANT_ID)
            .group_by(MemoryItem.subject_type)
        ).tuples()
    }
    assert subject_counts == {"user": 72, "agent": 30, "company": 18}
    assert session.scalar(
        select(func.count()).select_from(MemoryItem).where(
            MemoryItem.tenant_id == SMOKE_TENANT_ID,
            MemoryItem.business_domains.is_(None),
        )
    ) == 20
    assert session.scalar(
        select(func.count()).select_from(MemoryItem).where(
            MemoryItem.tenant_id == SMOKE_TENANT_ID,
            MemoryItem.project_domains.is_(None),
        )
    ) == 24

    assert session.scalar(
        select(func.count()).select_from(MemoryEmbedding).where(
            MemoryEmbedding.tenant_id == SMOKE_TENANT_ID,
            MemoryEmbedding.model_id == SMOKE_MODEL_ID,
            MemoryEmbedding.status == "stale",
        )
    ) == 2
    assert session.scalar(
        select(func.count()).select_from(MemoryEmbedding).where(
            MemoryEmbedding.tenant_id == SMOKE_TENANT_ID,
            MemoryEmbedding.model_id == SMOKE_MODEL_ID,
            MemoryEmbedding.content_hash == "0" * 64,
        )
    ) == 2
    assert session.scalar(
        select(func.count()).select_from(MemoryEmbedding).where(
            MemoryEmbedding.tenant_id == SMOKE_TENANT_ID,
            MemoryEmbedding.model_id == SECONDARY_MODEL_ID,
        )
    ) == 2


def test_prepare_smoke_data_rejects_non_positive_count(session: Session) -> None:
    with pytest.raises(ValueError, match="memory_count must be positive"):
        prepare_smoke_data(session, memory_count=0, generated_at=GENERATED_AT)
