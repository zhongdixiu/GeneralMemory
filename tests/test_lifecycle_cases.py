from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory_cmic.lifecycle import (
    evidence_group_is_complete,
    expire_memory,
    invalidate_memory,
    update_or_invalidate_source,
)
from memory_cmic.models import (
    MemoryAuditLog,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.repositories import add_evidence_group, create_memory, create_source
from memory_cmic.retrieval import search_memories
from memory_cmic.task_queue import enqueue_task

FIXTURE_NOW = datetime(2026, 9, 21, 8, tzinfo=UTC)


def _search_ids(
    session: Session,
    *,
    business_domain: str,
    project_id: str | None,
) -> set[str]:
    return {
        memory.id
        for memory in search_memories(
            session,
            tenant_id="tenant_orbit",
            user_id="user_ada",
            business_domain=business_domain,
            project_id=project_id,
            authorized_agent_ids=[],
            company_ids=[],
            now=FIXTURE_NOW,
        )
    }


def test_t01_single_source_fact_transaction_is_complete(session: Session) -> None:
    correlation_id = "corr_t01"
    source = create_source(
        session,
        {
            "id": "src_t01",
            "tenant_id": "tenant_t01",
            "source_system": "fixture",
            "source_type": "chat",
            "external_ref_id": "message_t01",
            "author_type": "user",
            "author_id": "user_t01",
            "raw_content": "虚构的单来源事实。",
            "content_hash": "0" * 64,
            "occurred_at": FIXTURE_NOW,
        },
    )
    memory = create_memory(
        session,
        {
            "id": "mem_t01",
            "tenant_id": "tenant_t01",
            "subject_type": "user",
            "subject_id": "user_t01",
            "cognitive_type": "fact",
            "business_domains": ["email", "email"],
            "project_domains": ["project_a"],
            "summary": "虚构单来源事实",
            "content": "用于 T01 验证的虚构事实。",
            "confidence": 1,
            "effective_at": FIXTURE_NOW,
            "created_by": "test_t01",
        },
    )
    edges = add_evidence_group(
        session,
        [
            {
                "id": "evd_t01",
                "tenant_id": "tenant_t01",
                "relationship_type": "supports",
                "evidence_group_id": "grp_t01",
                "upstream_source_id": source.id,
                "downstream_memory_id": memory.id,
                "upstream_version": source.version,
            }
        ],
    )
    task = enqueue_task(
        session,
        {
            "id": "tsk_t01_vector",
            "tenant_id": "tenant_t01",
            "task_type": "vector_upsert",
            "target_type": "memory",
            "target_id": memory.id,
            "input_version": memory.version,
            "idempotency_key": "vector_upsert:memory:mem_t01:v1",
            "correlation_id": correlation_id,
            "payload": {"model_id": "fake-embedding-v1"},
        },
    )
    session.add(
        MemoryAuditLog(
            tenant_id="tenant_t01",
            action="INSERT",
            target_type="memory",
            target_id=memory.id,
            operator_type="system",
            operator_id="test_t01",
            reason_code="MEMORY_CREATED",
            correlation_id=correlation_id,
            state_after={"status": "active", "version": 1},
        )
    )
    session.flush()

    assert memory.business_domains == ["email"]
    assert len(edges) == 1
    assert task.task_type == "vector_upsert"
    assert session.scalar(
        select(func.count()).select_from(MemoryAuditLog).where(
            MemoryAuditLog.correlation_id == correlation_id
        )
    ) == 1


def test_t02_second_source_adds_evidence_without_duplicate_memory(
    lifecycle_session: Session,
) -> None:
    memory_count = lifecycle_session.scalar(
        select(func.count()).select_from(MemoryItem).where(
            MemoryItem.id == "mem_orbit_email_bullets"
        )
    )
    group_ids = set(
        lifecycle_session.scalars(
            select(MemoryEvidence.evidence_group_id).where(
                MemoryEvidence.downstream_memory_id == "mem_orbit_email_bullets"
            )
        )
    )
    assert memory_count == 1
    assert group_ids == {
        "grp_orbit_m1_independent_01",
        "grp_orbit_m1_independent_02",
    }


def test_t03_one_independent_source_loss_keeps_memory_active(
    lifecycle_session: Session,
) -> None:
    update_or_invalidate_source(
        lifecycle_session,
        tenant_id="tenant_orbit",
        source_id="src_orbit_email_pref_01",
        status="invalidated",
        correlation_id="corr_t03",
        now=FIXTURE_NOW,
    )
    memory = lifecycle_session.get(MemoryItem, "mem_orbit_email_bullets")
    edge = lifecycle_session.get(MemoryEvidence, "evd_orbit_m1_s1")
    assert edge.status == "invalidated"
    assert memory.status == "active"
    assert "mem_orbit_email_bullets" in _search_ids(
        lifecycle_session, business_domain="email", project_id="project_a"
    )


def test_t04_all_groups_incomplete_stops_retrieval_and_enqueues_cleanup(
    lifecycle_session: Session,
) -> None:
    for source_id in ("src_orbit_email_pref_01", "src_orbit_email_pref_02"):
        update_or_invalidate_source(
            lifecycle_session,
            tenant_id="tenant_orbit",
            source_id=source_id,
            status="invalidated",
            correlation_id="corr_t04",
            now=FIXTURE_NOW,
        )

    memory = lifecycle_session.get(MemoryItem, "mem_orbit_email_bullets")
    assert memory.status == "invalidated"
    assert "mem_orbit_email_bullets" not in _search_ids(
        lifecycle_session, business_domain="email", project_id="project_a"
    )
    task_types = set(
        lifecycle_session.scalars(
            select(MemoryTask.task_type).where(MemoryTask.correlation_id == "corr_t04")
        )
    )
    assert {"dependency_recheck", "vector_delete"} <= task_types


def test_t05_multiple_facts_form_traceable_inference(lifecycle_session: Session) -> None:
    edges = lifecycle_session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.evidence_group_id == "grp_orbit_m3_joint_01"
        )
    ).all()
    assert {edge.upstream_memory_id for edge in edges} == {
        "mem_orbit_email_bullets",
        "mem_orbit_project_b_review",
    }
    assert {edge.relationship_type for edge in edges} == {"derives"}
    assert evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m3_joint_01", now=FIXTURE_NOW
    )


def test_t06_fact_loss_stops_inference_and_enqueues_recheck(
    lifecycle_session: Session,
) -> None:
    invalidate_memory(
        lifecycle_session,
        tenant_id="tenant_orbit",
        memory_id="mem_orbit_email_bullets",
        correlation_id="corr_t06",
        now=FIXTURE_NOW,
    )
    inference = lifecycle_session.get(MemoryItem, "mem_orbit_email_reviewable_style")
    assert inference.status == "invalidated"
    assert "mem_orbit_email_reviewable_style" not in _search_ids(
        lifecycle_session, business_domain="email", project_id="project_a"
    )
    assert lifecycle_session.scalar(
        select(func.count()).select_from(MemoryTask).where(
            MemoryTask.correlation_id == "corr_t06",
            MemoryTask.task_type == "dependency_recheck",
            MemoryTask.target_id == inference.id,
        )
    ) == 1


def test_t07_profile_basis_loss_invalidates_profile_and_enqueues_rebuild(
    lifecycle_session: Session,
) -> None:
    invalidate_memory(
        lifecycle_session,
        tenant_id="tenant_orbit",
        memory_id="mem_orbit_email_reviewable_style",
        correlation_id="corr_t07",
        now=FIXTURE_NOW,
    )
    profile = lifecycle_session.get(ProfileProperty, "prf_orbit_email_reply_style")
    task = lifecycle_session.scalars(
        select(MemoryTask).where(
            MemoryTask.correlation_id == "corr_t07",
            MemoryTask.task_type == "profile_rebuild",
            MemoryTask.target_id == profile.user_id,
        )
    ).one()
    assert profile.status == "invalidated"
    assert task.target_type == "subject"
    assert task.payload["watermark"]


def test_t09_expired_at_filters_before_ttl_task_and_expire_creates_cleanup(
    lifecycle_session: Session,
) -> None:
    memory = lifecycle_session.get(MemoryItem, "mem_orbit_expired_daily_digest")
    memory.status = "active"
    lifecycle_session.flush()

    assert "mem_orbit_expired_daily_digest" not in _search_ids(
        lifecycle_session, business_domain="email", project_id="project_c"
    )
    assert lifecycle_session.scalar(
        select(func.count()).select_from(MemoryTask).where(
            MemoryTask.task_type == "ttl_expire",
            MemoryTask.target_id == memory.id,
        )
    ) == 0

    expire_memory(
        lifecycle_session,
        tenant_id="tenant_orbit",
        memory_id=memory.id,
        correlation_id="corr_t09",
        now=FIXTURE_NOW,
    )
    assert memory.status == "expired"
    assert lifecycle_session.scalar(
        select(func.count()).select_from(MemoryTask).where(
            MemoryTask.correlation_id == "corr_t09",
            MemoryTask.task_type == "vector_delete",
        )
    ) == 1
    assert lifecycle_session.scalar(
        select(func.count()).select_from(MemoryAuditLog).where(
            MemoryAuditLog.correlation_id == "corr_t09",
            MemoryAuditLog.action == "EXPIRE",
        )
    ) == 1


def test_t15_one_missing_edge_breaks_and_group(lifecycle_session: Session) -> None:
    update_or_invalidate_source(
        lifecycle_session,
        tenant_id="tenant_orbit",
        source_id="src_orbit_project_b_context",
        status="invalidated",
        correlation_id="corr_t15",
        now=FIXTURE_NOW,
    )
    assert not evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m2_joint_01", now=FIXTURE_NOW
    )
    assert lifecycle_session.get(MemoryItem, "mem_orbit_project_b_review").status == (
        "invalidated"
    )


def test_t16_one_missing_or_group_leaves_other_group_complete(
    lifecycle_session: Session,
) -> None:
    update_or_invalidate_source(
        lifecycle_session,
        tenant_id="tenant_orbit",
        source_id="src_orbit_email_pref_01",
        status="invalidated",
        correlation_id="corr_t16",
        now=FIXTURE_NOW,
    )
    assert not evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m1_independent_01", now=FIXTURE_NOW
    )
    assert evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m1_independent_02", now=FIXTURE_NOW
    )
    assert lifecycle_session.get(MemoryItem, "mem_orbit_email_bullets").status == "active"


def test_group_completeness_checks_history_version_and_upstream_ttl(
    lifecycle_session: Session,
) -> None:
    edge = lifecycle_session.get(MemoryEvidence, "evd_orbit_m2_s3")
    edge.status = "invalidated"
    edge.invalidated_at = FIXTURE_NOW
    lifecycle_session.flush()
    assert not evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m2_joint_01", now=FIXTURE_NOW
    )

    edge.status = "active"
    edge.invalidated_at = None
    lifecycle_session.get(SourceRecord, "src_orbit_project_b_context").version = 2
    lifecycle_session.flush()
    assert not evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m2_joint_01", now=FIXTURE_NOW
    )

    upstream_memory = lifecycle_session.get(MemoryItem, "mem_orbit_email_bullets")
    upstream_memory.expired_at = datetime(2000, 1, 1, tzinfo=UTC)
    lifecycle_session.flush()
    assert not evidence_group_is_complete(
        lifecycle_session, "grp_orbit_m3_joint_01", now=FIXTURE_NOW
    )


def test_correction_replacement_and_dispute_states_are_not_retrieved(
    lifecycle_session: Session,
) -> None:
    old_source = lifecycle_session.get(SourceRecord, "src_orbit_old_folder")
    old_memory = lifecycle_session.get(MemoryItem, "mem_orbit_old_project_c_folder")
    current_memory = lifecycle_session.get(MemoryItem, "mem_orbit_current_project_c_folder")
    assert old_source.status == "invalidated"
    assert old_source.version == 2
    assert old_memory.status == "invalidated"
    assert current_memory.supersedes_id == old_memory.id

    project_ids = _search_ids(
        lifecycle_session, business_domain="disk", project_id="project_c"
    )
    assert current_memory.id in project_ids
    assert old_memory.id not in project_ids

    communication_ids = _search_ids(
        lifecycle_session, business_domain="communication", project_id="project_a"
    )
    assert "mem_orbit_quiet_after_20" not in communication_ids
    assert "mem_orbit_quiet_after_21" not in communication_ids
