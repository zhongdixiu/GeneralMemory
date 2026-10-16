from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_cmic.deterministic_embedding import deterministic_embedding
from memory_cmic.lifecycle import (
    evidence_group_is_complete,
    invalidate_memory,
    update_or_invalidate_source,
)
from memory_cmic.models import (
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.repositories import (
    EvidenceGroupDownstreamMismatch,
    add_evidence_group,
    create_memory,
    create_or_replace_profile,
    normalize_scope,
)
from memory_cmic.retrieval import search_memories
from memory_cmic.task_queue import enqueue_task

FIXTURE_NOW = datetime(2026, 9, 21, 8, tzinfo=UTC)


def test_fixture_declares_synthetic_data_and_required_coverage(
    lifecycle_fixture_data: dict[str, Any],
) -> None:
    assert lifecycle_fixture_data["synthetic_data_only"] is True
    assert set(lifecycle_fixture_data["subjects"]["tenants"]) == {
        "tenant_orbit",
        "tenant_harbor",
    }
    domains = {
        domain
        for memory in lifecycle_fixture_data["memory_item"]
        for domain in memory["business_domains"] or []
    }
    projects = {
        project
        for memory in lifecycle_fixture_data["memory_item"]
        for project in memory["project_domains"] or []
    }
    assert domains == {"communication", "disk", "email"}
    assert projects == {"project_a", "project_b", "project_c"}


def test_scope_normalization_examples(lifecycle_fixture_data: dict[str, Any]) -> None:
    for example in lifecycle_fixture_data["normalization_examples"]:
        assert normalize_scope(example["input"]) == example["expected"]


def test_deterministic_embedding_is_stable_and_normalized() -> None:
    first = deterministic_embedding("相同的虚构文本", "fake-embedding-v1")
    second = deterministic_embedding("相同的虚构文本", "fake-embedding-v1")
    other_model = deterministic_embedding("相同的虚构文本", "fake-embedding-v2")
    assert first == second
    assert first != other_model
    assert len(first) == 1024
    assert math.sqrt(sum(value * value for value in first)) == pytest.approx(1.0)

    similar = deterministic_embedding("相同的虚构文本用于测试", "fake-embedding-v1")
    unrelated = deterministic_embedding("云盘文件按更新时间排列", "fake-embedding-v1")
    similar_score = sum(left * right for left, right in zip(first, similar, strict=True))
    unrelated_score = sum(left * right for left, right in zip(first, unrelated, strict=True))
    assert similar_score > unrelated_score


def test_complete_fixture_loads_all_database_records(
    lifecycle_session: Session, lifecycle_fixture_data: dict[str, Any]
) -> None:
    fixture_tenant_ids = lifecycle_fixture_data["subjects"]["tenants"]
    expected_counts = {
        SourceRecord: len(lifecycle_fixture_data["source_record"]),
        MemoryItem: len(lifecycle_fixture_data["memory_item"]),
        ProfileProperty: len(lifecycle_fixture_data["profile_property"]),
        MemoryEvidence: len(lifecycle_fixture_data["memory_evidence"]),
        MemoryEmbedding: len(lifecycle_fixture_data["memory_embedding"]),
        MemoryTask: len(lifecycle_fixture_data["memory_task"]),
        MemoryAuditLog: len(lifecycle_fixture_data["memory_audit_log"]),
    }
    for model, expected in expected_counts.items():
        assert lifecycle_session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.tenant_id.in_(fixture_tenant_ids))
        ) == expected


@pytest.mark.parametrize("case_index", range(6))
def test_t08_fixture_search_expectations(
    lifecycle_session: Session,
    lifecycle_fixture_data: dict[str, Any],
    case_index: int,
) -> None:
    case = lifecycle_fixture_data["expected_searches"][case_index]
    result_ids = {
        memory.id
        for memory in search_memories(
            lifecycle_session,
            **case["input"],
            now=FIXTURE_NOW,
        )
    }
    assert set(case["must_include"]) <= result_ids, case["case_id"]
    assert not set(case["must_exclude"]) & result_ids, case["case_id"]


@pytest.mark.parametrize(
    "field,record_id",
    [
        ("business_domains", "mem_invalid_empty_business"),
        ("project_domains", "mem_invalid_empty_project"),
    ],
)
def test_create_memory_leaves_empty_scope_rejection_to_database(
    session: Session,
    lifecycle_fixture_data: dict[str, Any],
    field: str,
    record_id: str,
) -> None:
    source = next(
        record
        for record in lifecycle_fixture_data["memory_item"]
        if record["id"] == "mem_orbit_email_bullets"
    )
    values = dict(source)
    values.update({"id": record_id, field: []})
    values.pop("metadata")
    values["metadata_json"] = {"fixture": True}
    for key, value in list(values.items()):
        if key.endswith("_at") and isinstance(value, str):
            values[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))

    with pytest.raises(IntegrityError), session.begin_nested():
        create_memory(session, values)


def test_evidence_group_cannot_be_reused_for_another_downstream(
    lifecycle_session: Session,
) -> None:
    with pytest.raises(EvidenceGroupDownstreamMismatch):
        add_evidence_group(
            lifecycle_session,
            [
                {
                    "id": "evd_invalid_reuse",
                    "tenant_id": "tenant_orbit",
                    "relationship_type": "supports",
                    "evidence_group_id": "grp_orbit_m1_independent_01",
                    "upstream_source_id": "src_orbit_company_share_rule",
                    "downstream_memory_id": "mem_orbit_company_share_link",
                    "upstream_version": 1,
                }
            ],
        )


def test_cross_tenant_evidence_is_rejected_by_database(lifecycle_session: Session) -> None:
    with pytest.raises(IntegrityError), lifecycle_session.begin_nested():
        add_evidence_group(
            lifecycle_session,
            [
                {
                    "id": "evd_invalid_cross_tenant",
                    "tenant_id": "tenant_orbit",
                    "relationship_type": "supports",
                    "evidence_group_id": "grp_invalid_cross_tenant",
                    "upstream_source_id": "src_harbor_disk_pref",
                    "downstream_memory_id": "mem_orbit_email_bullets",
                    "upstream_version": 1,
                }
            ],
        )


def test_enqueue_task_returns_existing_idempotent_task(lifecycle_session: Session) -> None:
    fixture_task = lifecycle_session.get(MemoryTask, "tsk_orbit_vector_50")
    duplicate = enqueue_task(
        lifecycle_session,
        {
            "id": "tsk_duplicate_idempotency",
            "tenant_id": fixture_task.tenant_id,
            "task_type": fixture_task.task_type,
            "target_type": fixture_task.target_type,
            "target_id": fixture_task.target_id,
            "idempotency_key": fixture_task.idempotency_key,
        },
    )
    assert duplicate.id == fixture_task.id


def test_create_or_replace_profile_invalidates_previous_active_value(
    lifecycle_session: Session,
) -> None:
    replacement = create_or_replace_profile(
        lifecycle_session,
        {
            "id": "prf_orbit_email_reply_style_v2",
            "tenant_id": "tenant_orbit",
            "user_id": "user_ada",
            "business_domains": ["email", "email"],
            "property_key": "pref.email.reply_style",
            "property_value": {"format": "short_paragraph"},
            "value_type": "object",
            "confidence": 0.9,
            "status": "active",
            "effective_at": FIXTURE_NOW,
        },
    )
    previous = lifecycle_session.get(ProfileProperty, "prf_orbit_email_reply_style")
    assert previous.status == "invalidated"
    assert replacement.business_domains == ["email"]
    assert replacement.version == 2


def test_or_groups_keep_memory_active_until_all_groups_are_incomplete(
    lifecycle_session: Session,
) -> None:
    assert evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m1_independent_01", now=FIXTURE_NOW
    )
    update_or_invalidate_source(
        lifecycle_session,
        tenant_id="tenant_orbit",
        source_id="src_orbit_email_pref_01",
        status="invalidated",
        now=FIXTURE_NOW,
    )
    memory = lifecycle_session.get(MemoryItem, "mem_orbit_email_bullets")
    assert memory.status == "active"

    update_or_invalidate_source(
        lifecycle_session,
        tenant_id="tenant_orbit",
        source_id="src_orbit_email_pref_02",
        status="invalidated",
        now=FIXTURE_NOW,
    )
    assert memory.status == "invalidated"


def test_and_group_loses_completeness_when_one_source_changes(
    lifecycle_session: Session,
) -> None:
    update_or_invalidate_source(
        lifecycle_session,
        tenant_id="tenant_orbit",
        source_id="src_orbit_project_b_context",
        status="invalidated",
        now=FIXTURE_NOW,
    )
    memory = lifecycle_session.get(MemoryItem, "mem_orbit_project_b_review")
    assert memory.status == "invalidated"


def test_memory_invalidation_propagates_to_inference_and_profile(
    lifecycle_session: Session,
) -> None:
    invalidate_memory(
        lifecycle_session,
        tenant_id="tenant_orbit",
        memory_id="mem_orbit_email_bullets",
        correlation_id="corr_propagation_test",
        now=FIXTURE_NOW,
    )
    inference = lifecycle_session.get(MemoryItem, "mem_orbit_email_reviewable_style")
    profile = lifecycle_session.get(ProfileProperty, "prf_orbit_email_reply_style")
    assert inference.status == "invalidated"
    assert profile.status == "invalidated"
    task_types = set(
        lifecycle_session.scalars(
            select(MemoryTask.task_type).where(
                MemoryTask.correlation_id == "corr_propagation_test"
            )
        )
    )
    assert {"dependency_recheck", "profile_rebuild"} <= task_types
