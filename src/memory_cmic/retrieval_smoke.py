from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from memory_cmic.db import create_database_engine
from memory_cmic.deterministic_embedding import deterministic_embedding
from memory_cmic.models import MemoryEmbedding, MemoryEvidence, MemoryItem
from memory_cmic.retrieval import (
    _scope_filters,
    _subject_routes,
    _vector_statement,
    search_memories,
)
from memory_cmic.smoke_data import DEFAULT_MEMORY_COUNT, SMOKE_MODEL_ID, SMOKE_TENANT_ID

DEFAULT_ITERATIONS = 30


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _measure(action: Callable[[], object], iterations: int) -> dict[str, float | int]:
    samples = []
    for _ in range(iterations):
        started_at = time.perf_counter()
        action()
        samples.append((time.perf_counter() - started_at) * 1_000)
    return {
        "iterations": iterations,
        "p50_ms": round(_percentile(samples, 0.50), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
    }


def _explain(session: Session, statement: Select[Any]) -> dict[str, Any]:
    compiled = statement.compile(
        dialect=session.get_bind().dialect,
        compile_kwargs={"literal_binds": True},
    )
    return session.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}")
    ).scalar_one()[0]


def run_smoke(session: Session, *, iterations: int = DEFAULT_ITERATIONS) -> dict[str, Any]:
    if iterations <= 0:
        raise ValueError("iterations must be positive")

    memory_count = session.scalar(
        select(func.count()).select_from(MemoryItem).where(MemoryItem.tenant_id == SMOKE_TENANT_ID)
    )
    if memory_count < DEFAULT_MEMORY_COUNT:
        raise RuntimeError(
            f"expected at least {DEFAULT_MEMORY_COUNT} smoke memories, found {memory_count}"
        )

    now = datetime.now(UTC)
    query_text = "虚构 email 记忆 00000026\n用于第五批范围和向量检索冒烟的虚构内容，序号 00000026。"
    query_embedding = deterministic_embedding(query_text, SMOKE_MODEL_ID)
    common = {
        "session": session,
        "tenant_id": SMOKE_TENANT_ID,
        "user_id": "user_wp5",
        "model_id": SMOKE_MODEL_ID,
        "query_embedding": query_embedding,
        "limit": 10,
        "now": now,
    }

    def single_subject() -> list[MemoryItem]:
        return search_memories(
            **common,
            business_domain="email",
            project_id="project_a",
            authorized_agent_ids=[],
            company_ids=[],
        )

    def scoped_vector() -> list[MemoryItem]:
        return search_memories(
            **common,
            business_domain="disk",
            project_id="project_b",
            authorized_agent_ids=[],
            company_ids=[],
        )

    def multi_subject() -> list[MemoryItem]:
        return search_memories(
            **common,
            business_domain="email",
            project_id="project_a",
            authorized_agent_ids=["agent_wp5"],
            company_ids=["company_wp5"],
        )

    ttl_statement = select(MemoryItem.id).where(
        MemoryItem.tenant_id == SMOKE_TENANT_ID,
        MemoryItem.status == "active",
        MemoryItem.expired_at.is_not(None),
        MemoryItem.expired_at <= now,
    )
    reverse_statement = select(MemoryEvidence.id).where(
        MemoryEvidence.tenant_id == SMOKE_TENANT_ID,
        MemoryEvidence.upstream_source_id == "wp5-src-00000026",
    )

    scope_filters = _scope_filters(
        tenant_id=SMOKE_TENANT_ID,
        business_domain="email",
        project_id="project_a",
        now=now,
    )
    user_route = _subject_routes(user_id="user_wp5", authorized_agent_ids=[], company_ids=[])[0]
    vector_statement = _vector_statement(
        route=user_route,
        scope_filters=scope_filters,
        model_id=SMOKE_MODEL_ID,
        query_embedding=query_embedding,
        limit=10,
    )
    distance = MemoryEmbedding.embedding.cosine_distance(query_embedding).label("distance")
    hnsw_preflight_statement = (
        select(MemoryEmbedding.memory_id, distance)
        .where(MemoryEmbedding.status == "active")
        .order_by(distance)
        .limit(10)
    )

    return {
        "generated_at": now.isoformat(),
        "tenant_id": SMOKE_TENANT_ID,
        "memory_count": memory_count,
        "iterations": iterations,
        "timings": {
            "single_subject_vector": _measure(single_subject, iterations),
            "scoped_vector": _measure(scoped_vector, iterations),
            "multi_subject_vector": _measure(multi_subject, iterations),
            "ttl_scan": _measure(lambda: list(session.scalars(ttl_statement)), iterations),
            "source_reverse_dependency": _measure(
                lambda: list(session.scalars(reverse_statement)), iterations
            ),
        },
        "plans": {
            "hnsw_preflight": _explain(session, hnsw_preflight_statement),
            "representative_vector": _explain(session, vector_statement),
            "ttl_scan": _explain(session, ttl_statement),
            "source_reverse_dependency": _explain(session, reverse_statement),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the WP5 retrieval performance smoke.")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    database_url = os.environ.get("MEMORY_DATABASE_URL")
    if not database_url:
        parser.error("MEMORY_DATABASE_URL must be set")

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            result = run_smoke(session, iterations=args.iterations)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(f"{rendered}\n")
        print(rendered)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
