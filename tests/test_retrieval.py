from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from memory_cmic.deterministic_embedding import deterministic_embedding
from memory_cmic.retrieval import search_memories
from memory_cmic.smoke_data import SMOKE_MODEL_ID, SMOKE_TENANT_ID, prepare_smoke_data

GENERATED_AT = datetime(2026, 9, 21, 8, tzinfo=UTC)


def _memory_text(sequence: int) -> str:
    suffix = f"{sequence:08d}"
    topic = ("general", "email", "disk", "communication", "disk", "communication")[
        (sequence - 1) % 6
    ]
    return f"虚构 {topic} 记忆 {suffix}\n用于第五批范围和向量检索冒烟的虚构内容，序号 {suffix}。"


def _vector_search(session: Session, sequence: int, **overrides: object) -> list[str]:
    parameters = {
        "tenant_id": SMOKE_TENANT_ID,
        "user_id": "user_wp5",
        "business_domain": "email",
        "project_id": "project_a",
        "authorized_agent_ids": ["agent_wp5"],
        "company_ids": ["company_wp5"],
        "model_id": SMOKE_MODEL_ID,
        "query_embedding": deterministic_embedding(_memory_text(sequence), SMOKE_MODEL_ID),
        "limit": 10,
        "now": GENERATED_AT,
    }
    parameters.update(overrides)
    return [memory.id for memory in search_memories(session, **parameters)]


def test_vector_search_ranks_exact_match_and_merges_subject_routes(session: Session) -> None:
    prepare_smoke_data(session, memory_count=60, generated_at=GENERATED_AT)

    assert _vector_search(session, 26)[0] == "wp5-mem-00000026"

    result_ids = _vector_search(
        session,
        26,
        business_domain="email",
        project_id="project_a",
        limit=60,
    )
    assert "wp5-mem-00000026" in result_ids  # user
    assert "wp5-mem-00000056" in result_ids  # agent
    assert "wp5-mem-00000020" in result_ids  # company


def test_vector_search_excludes_stale_hash_and_requires_model(session: Session) -> None:
    prepare_smoke_data(session, memory_count=20, generated_at=GENERATED_AT)

    stale_status_ids = _vector_search(
        session,
        6,
        business_domain="communication",
        project_id="project_a",
        limit=20,
    )
    assert "wp5-mem-00000006" not in stale_status_ids

    result_ids = _vector_search(
        session,
        7,
        business_domain="general",
        project_id="project_a",
        limit=20,
    )
    assert "wp5-mem-00000007" not in result_ids

    with pytest.raises(ValueError, match="model_id is required"):
        _vector_search(session, 2, model_id=None)


def test_vector_search_uses_requested_model(session: Session) -> None:
    prepare_smoke_data(session, memory_count=20, generated_at=GENERATED_AT)
    text = _memory_text(8)

    result_ids = _vector_search(
        session,
        8,
        business_domain="email",
        project_id="project_b",
        model_id="fake-embedding-v2",
        query_embedding=deterministic_embedding(text, "fake-embedding-v2"),
    )

    assert result_ids[0] == "wp5-mem-00000008"
