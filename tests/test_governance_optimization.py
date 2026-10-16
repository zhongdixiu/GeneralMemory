from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker
from test_consolidation import FakeModel, seed_fact

import memory_cmic.profile as profile_module
from memory_cmic.consolidation import run_once as consolidate_once
from memory_cmic.evidence import evidence_group_is_complete, memory_has_valid_evidence
from memory_cmic.governance_state import backfill_user, create_run, ensure_policy
from memory_cmic.models import (
    BusinessScene,
    GovernanceOperation,
    GovernancePending,
    GovernancePolicy,
    GovernanceSubjectState,
    MemoryAuditLog,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.profile import ProfileProposal, enqueue_profile_rebuild, read_profiles, run_once
from memory_cmic.repositories import add_evidence_group, create_memory
from memory_cmic.task_queue import claim_tasks


@pytest.fixture
def context(engine):
    tenant = f"tenant_opt_{uuid4().hex}"
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory, tenant
    with engine.begin() as conn:
        for model in (
            BusinessScene,
            MemoryAuditLog,
            GovernanceOperation,
            GovernancePending,
            GovernanceSubjectState,
            GovernancePolicy,
            MemoryEvidence,
            MemoryTask,
            ProfileProperty,
            MemoryItem,
            SourceRecord,
        ):
            conn.execute(delete(model).where(model.tenant_id == tenant))


def seed_inference(session, tenant, bases):
    inference = create_memory(
        session,
        {
            "id": f"inf_{uuid4().hex}",
            "tenant_id": tenant,
            "subject_type": "user",
            "subject_id": "user_1",
            "cognitive_type": "inference",
            "business_domains": ["report"],
            "summary": "用户倾向于简洁的报告",
            "content": "用户倾向于简洁的报告",
            "confidence": 0.8,
            "effective_at": max(m.effective_at for m in bases),
            "created_by": "test",
        },
    )
    group = f"evg_{uuid4().hex}"
    add_evidence_group(
        session,
        [
            {
                "id": f"evi_{uuid4().hex}",
                "tenant_id": tenant,
                "relationship_type": "derives",
                "evidence_group_id": group,
                "upstream_memory_id": m.id,
                "downstream_memory_id": inference.id,
                "upstream_version": m.version,
            }
            for m in bases
        ],
    )
    return inference, group


class EmptyModel:
    calls = 0

    def extract(self, memories, *, dimensions):
        self.calls += 1
        return []


class InferredModel:
    def extract(self, memories, *, dimensions):
        return [
            ProfileProposal(
                property_key="communication", memory_id=m["id"],
                value=m["content"], quote=m["content"],
            )
            for m in memories
            if m["cognitive_type"] == "inference"
        ]


def test_profile_first_quiet_max_wait_and_irrelevant_changes(context, monkeypatch):
    factory, tenant = context
    start = datetime.now(UTC)

    class Clock(datetime):
        current = start

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(profile_module, "datetime", Clock)
    with factory.begin() as session:
        assert enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1") is None
        first = seed_fact(session, tenant, "用户希望报告先给结论")
        task = enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1")
        assert task.available_at == start + timedelta(minutes=2)
        assert not run_once(factory, worker_id="opt", model=EmptyModel())
        state = session.get(GovernanceSubjectState, (tenant, "user_1"))
        initial = state.profile_first_dirty_at
        Clock.current += timedelta(minutes=1)
        seed_fact(session, tenant, "项目甲报告需详细", project_context="项目甲")
        assert enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1").id == task.id
        assert task.available_at == start + timedelta(minutes=2)
        first.content = "用户希望报告附上数据"
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1")
        assert task.available_at == start + timedelta(minutes=3)
        Clock.current = start + timedelta(minutes=29)
        first.content = "用户希望报告列出行动项"
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1")
        assert state.profile_first_dirty_at == initial
        assert task.available_at == start + timedelta(minutes=30)
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1", force=True)
        assert task.available_at == Clock.current


def test_profile_empty_success_does_not_loop_and_change_requeues(context):
    factory, tenant = context
    model = EmptyModel()
    with factory.begin() as session:
        fact = seed_fact(session, tenant, "用户喜欢简洁报告")
        task = enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1", force=True)
    assert run_once(factory, worker_id="opt", model=model)
    with factory.begin() as session:
        state = session.get(GovernanceSubjectState, (tenant, "user_1"))
        assert state.profile_evaluated_input
        assert state.profile_first_dirty_at is None
        assert session.get(MemoryTask, task.id).status == "succeeded"
        assert enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1") is None
        session.get(MemoryItem, fact.id).content = "用户喜欢详细报告"
        next_task = enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1")
        assert next_task.id != task.id
    assert model.calls == 1


def test_profile_result_counts_created_retained_and_invalidated(context):
    from test_profile import VerbatimProfileModel

    factory, tenant = context
    for model, expected in (
        (VerbatimProfileModel(), {"created": 1, "retained": 0, "invalidated": 0}),
        (VerbatimProfileModel(), {"created": 0, "retained": 1, "invalidated": 0}),
        (EmptyModel(), {"created": 0, "retained": 0, "invalidated": 1}),
    ):
        with factory.begin() as session:
            if not session.scalar(select(MemoryItem.id).where(MemoryItem.tenant_id == tenant)):
                seed_fact(session, tenant, "用户希望报告先给结论")
            task = enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1", force=True)
        assert run_once(factory, worker_id="opt", model=model)
        with factory() as session:
            result = session.get(MemoryTask, task.id).result_json[0]
            assert {key: result[key] for key in expected} == expected


def test_inferred_profile_is_typed_and_new_evidence_blocks_old_approval(context):
    factory, tenant = context
    with factory.begin() as session:
        bases = [
            seed_fact(session, tenant, "周报需要简洁"),
            seed_fact(session, tenant, "复盘需要简洁"),
        ]
        inference, _ = seed_inference(session, tenant, bases)
        enqueue_profile_rebuild(
            session, tenant_id=tenant, user_id="user_1", force=True, allow_inferred=True
        )
    assert run_once(factory, worker_id="opt", model=InferredModel(), allow_inferred=True)
    with factory.begin() as session:
        assert read_profiles(session, tenant_id=tenant, user_id="user_1") == []
        profiles = read_profiles(session, tenant_id=tenant, user_id="user_1", include_inferred=True)
        assert len(profiles) == 1 and profiles[0].assertion_kind == "inferred"
        assert float(profiles[0].confidence) == 0.8
        assert session.get(MemoryItem, inference.id).metadata_json["profile_admission"]["accepted"]
        seed_fact(session, tenant, "用户现在要求报告详细说明全部步骤")
        assert (
            read_profiles(session, tenant_id=tenant, user_id="user_1", include_inferred=True) == []
        )


@pytest.mark.parametrize("conflict_time", ["current", "expired", "future", "historical"])
def test_only_current_conflicts_block_inference_and_profile(context, conflict_time):
    from memory_cmic.consolidation import ConsolidationDecision

    factory, tenant = context
    now = datetime.now(UTC)
    with factory.begin() as session:
        bases = [
            seed_fact(session, tenant, "周报需要简洁"),
            seed_fact(session, tenant, "复盘需要简洁"),
        ]
        seed_inference(session, tenant, bases)
        disputed = seed_fact(session, tenant, "报告需要详细说明全部步骤")
        disputed.status = "disputed"
        if conflict_time == "expired":
            disputed.expired_at = now - timedelta(seconds=1)
        elif conflict_time == "future":
            disputed.effective_at = now + timedelta(days=1)
        elif conflict_time == "historical":
            disputed.metadata_json = {"temporal_kind": "historical"}
        enqueue_profile_rebuild(
            session, tenant_id=tenant, user_id="user_1", force=True, allow_inferred=True
        )
    assert run_once(factory, worker_id="opt", model=InferredModel(), allow_inferred=True)
    with factory.begin() as session:
        profiles = read_profiles(session, tenant_id=tenant, user_id="user_1", include_inferred=True)
        assert len(profiles) == (0 if conflict_time == "current" else 1)
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1")

    class ReviewModel:
        def __init__(self):
            self.phases = []

        def decide(self, payload):
            self.phases.append(payload["phase"])
            return ConsolidationDecision()

    model = ReviewModel()
    assert consolidate_once(factory, worker_id="opt", model=model, model_id="test")
    assert ("inference" in model.phases) == (conflict_time != "current")


def test_recursive_evidence_blocks_unpropagated_source_change_and_future_basis(context):
    factory, tenant = context
    with factory.begin() as session:
        bases = [seed_fact(session, tenant, "周报简洁"), seed_fact(session, tenant, "复盘简洁")]
        inference, group = seed_inference(session, tenant, bases)
        assert evidence_group_is_complete(session, group)
        source_id = session.scalar(
            select(MemoryEvidence.upstream_source_id).where(
                MemoryEvidence.downstream_memory_id == bases[0].id
            )
        )
        source = session.get(SourceRecord, source_id)
        source.version += 1
        session.flush()
        assert not evidence_group_is_complete(session, group)
        assert not memory_has_valid_evidence(session, inference, tenant_id=tenant, user_id="user_1")
        source.version -= 1
        bases[0].effective_at = datetime.now(UTC) + timedelta(hours=1)
        session.flush()
        assert not evidence_group_is_complete(session, group)


def test_only_one_profile_worker_claims_a_user_with_followup(context):
    factory, tenant = context
    with factory.begin() as session:
        fact = seed_fact(session, tenant, "报告简洁")
        first = enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1", force=True)
    with factory.begin() as session:
        assert (
            claim_tasks(
                session, tenant_id=tenant, worker_id="one", task_types=("profile_rebuild",)
            )[0].id
            == first.id
        )
    with factory.begin() as session:
        session.get(MemoryItem, fact.id).content = "报告详细"
        second = enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1", force=True)
        assert second.id != first.id
    with factory.begin() as session:
        assert (
            claim_tasks(session, tenant_id=tenant, worker_id="two", task_types=("profile_rebuild",))
            == []
        )


def test_conflicts_are_reviewed_once_and_remain_disputed_without_clarification(context):
    factory, tenant = context
    group = uuid4().hex
    with factory.begin() as session:
        first = seed_fact(session, tenant, "邮件默认范围7天")
        second = seed_fact(session, tenant, "邮件默认范围3天")
        first.status = second.status = "disputed"
        first.conflict_group_id = second.conflict_group_id = group
        backfill_user(session, tenant, "user_1")
        run = create_run(session, tenant_id=tenant, user_id="user_1")
    model = FakeModel({})
    assert consolidate_once(factory, worker_id="opt", model=model, model_id="test")
    with factory.begin() as session:
        assert session.get(MemoryTask, run.id).status == "succeeded"
        assert session.get(MemoryItem, first.id).status == "disputed"
        review = session.scalars(
            select(GovernanceOperation).where(
                GovernanceOperation.tenant_id == tenant,
                GovernanceOperation.kind == "conflict_review",
            )
        ).one()
        assert review.details["outcome"] == "unresolved" and review.details["question"]
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1")
    assert consolidate_once(factory, worker_id="opt", model=model, model_id="test")
    assert model.calls == 1


def test_explicit_clarification_creates_new_fact_without_reactivating_conflict(context):
    factory, tenant = context
    group = uuid4().hex
    with factory.begin() as session:
        first = seed_fact(session, tenant, "邮件默认范围7天")
        second = seed_fact(session, tenant, "邮件默认范围3天")
        first.status = second.status = "disputed"
        first.conflict_group_id = second.conflict_group_id = group
        clarification = seed_fact(session, tenant, "澄清：以后邮件统一改为3天")
        clarification.effective_at = datetime.now(UTC)
        source_id = session.scalar(
            select(MemoryEvidence.upstream_source_id).where(
                MemoryEvidence.downstream_memory_id == clarification.id
            )
        )
        session.get(SourceRecord, source_id).occurred_at = clarification.effective_at
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1")

    class Model:
        def decide(self, payload):
            from memory_cmic.consolidation import ConsolidationDecision

            if payload["phase"] != "conflicts":
                return ConsolidationDecision()
            return ConsolidationDecision.model_validate(
                {
                    "conflict_review": {
                        "outcome": "resolved",
                        "reason": "用户明确澄清未来统一为3天",
                        "facts": [
                            {
                                "content": "用户要求以后邮件默认范围为3天",
                                "basis_memory_id": clarification.id,
                                "source_id": source_id,
                                "quote": "以后邮件统一改为3天",
                            }
                        ],
                    }
                }
            )

    assert consolidate_once(factory, worker_id="opt", model=Model(), model_id="test")
    with factory() as session:
        assert session.get(MemoryItem, first.id).status == "invalidated"
        assert session.get(MemoryItem, second.id).status == "invalidated"
        assert session.scalar(
            select(MemoryItem.id).where(
                MemoryItem.tenant_id == tenant,
                MemoryItem.id.like("gcf_%"),
                MemoryItem.status == "active",
            )
        )


def test_conflict_conditions_preserve_both_grounded_requirements(context):
    from memory_cmic.consolidation import ConsolidationDecision

    factory, tenant = context
    with factory.begin() as session:
        members = [
            seed_fact(session, tenant, "常规查邮件默认查7天"),
            seed_fact(session, tenant, "临时查漏仅查3天"),
        ]
        proposals = []
        for memory in members:
            memory.status = "disputed"
            memory.conflict_group_id = "condition_group"
            source_id = session.scalar(
                select(MemoryEvidence.upstream_source_id).where(
                    MemoryEvidence.downstream_memory_id == memory.id
                )
            )
            proposals.append(
                {
                    "content": memory.content,
                    "basis_memory_id": memory.id,
                    "source_id": source_id,
                    "quote": memory.content,
                }
            )
        session.flush()
        backfill_user(session, tenant, "user_1")
        task = create_run(session, tenant_id=tenant, user_id="user_1")

    class Model:
        def decide(self, payload):
            return (
                ConsolidationDecision.model_validate(
                    {
                        "conflict_review": {
                            "outcome": "conditions_distinguished",
                            "reason": "常规与临时查漏的条件不同",
                            "facts": proposals,
                        }
                    }
                )
                if payload["phase"] == "conflicts"
                else ConsolidationDecision()
            )

    assert consolidate_once(factory, worker_id="opt", model=Model(), model_id="test")
    with factory() as session:
        assert session.get(MemoryTask, task.id).status == "succeeded"
        assert all(session.get(MemoryItem, m.id).status == "invalidated" for m in members)
        replacements = session.scalars(
            select(MemoryItem).where(MemoryItem.tenant_id == tenant, MemoryItem.status == "active")
        ).all()
        assert {m.content for m in replacements} == {m.content for m in members}
        assert len(replacements) == 2
        assert all(
            memory_has_valid_evidence(session, m, tenant_id=tenant, user_id="user_1")
            for m in replacements
        )


@pytest.mark.parametrize("generate", [True, False])
def test_fact_and_inference_decisions_are_separate_and_counterevidence_rejects_replay(
    context,
    generate,
):
    factory, tenant = context
    with factory.begin() as session:
        bases = [seed_fact(session, tenant, "周报简洁"), seed_fact(session, tenant, "复盘简洁")]
        inference, _ = seed_inference(session, tenant, bases)
        counter = seed_fact(session, tenant, "用户明确要求今后所有报告必须详细")
        source_id = session.scalar(
            select(MemoryEvidence.upstream_source_id).where(
                MemoryEvidence.downstream_memory_id == counter.id
            )
        )
        session.get(SourceRecord, source_id).occurred_at = datetime.now(UTC)
        ensure_policy(session, tenant).generate_inferences = generate
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1")
    phases = []

    class Model:
        def decide(self, payload):
            from memory_cmic.consolidation import ConsolidationDecision

            phases.append(payload["phase"])
            if payload["phase"] == "facts":
                return ConsolidationDecision()
            return ConsolidationDecision.model_validate(
                {
                    "inference_reviews": [
                        {
                            "memory_id": inference.id,
                            "outcome": "invalidate",
                            "source_id": source_id,
                            "quote": "今后所有报告必须详细",
                            "reason": "明确要求覆盖推断",
                        }
                    ],
                    "inferences": [
                        {
                            "content": "用户偏爱简洁报告",
                            "source_ids": [m.id for m in bases],
                            "confidence": 0.8,
                            "reason": "旧模式",
                            "business_domains": ["report"],
                        }
                    ],
                }
            )

    assert consolidate_once(factory, worker_id="opt", model=Model(), model_id="test")
    assert phases[:2] == ["facts", "inference"]
    with factory() as session:
        assert session.get(MemoryItem, inference.id).status == "invalidated"
        assert (
            session.scalar(
                select(MemoryItem.id).where(
                    MemoryItem.tenant_id == tenant,
                    MemoryItem.cognitive_type == "inference",
                    MemoryItem.status == "active",
                )
            )
            is None
        )


def test_disabling_new_inferences_keeps_fact_processing(context):
    from memory_cmic.governance_state import ensure_policy

    factory, tenant = context
    with factory.begin() as session:
        bases = [seed_fact(session, tenant, "周报简洁"), seed_fact(session, tenant, "复盘简洁")]
        ensure_policy(session, tenant).generate_inferences = False
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1")
    model = FakeModel(
        {
            "inferences": [
                {
                    "content": "用户倾向于简洁报告",
                    "source_ids": [m.id for m in bases],
                    "confidence": 0.8,
                    "reason": "共同模式",
                    "business_domains": ["report"],
                }
            ]
        }
    )
    assert consolidate_once(factory, worker_id="opt", model=model, model_id="test")
    assert model.calls == 1
    with factory() as session:
        assert (
            session.scalar(
                select(MemoryItem.id).where(
                    MemoryItem.tenant_id == tenant, MemoryItem.cognitive_type == "inference"
                )
            )
            is None
        )


def test_backfill_preview_rolls_back_and_apply_is_idempotent(context):
    from pathlib import Path
    from runpy import run_path

    from test_profile import VerbatimProfileModel

    backfill = run_path(Path(__file__).parents[1] / "scripts/backfill_memory_governance.py")[
        "backfill"
    ]
    factory, tenant = context
    with factory.begin() as session:
        fact = seed_fact(session, tenant, "用户希望报告先给结论")
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1", force=True)
    assert run_once(factory, worker_id="opt", model=VerbatimProfileModel())
    with factory.begin() as session:
        profile = session.scalars(
            select(ProfileProperty).where(
                ProfileProperty.tenant_id == tenant, ProfileProperty.status == "active"
            )
        ).one()
        source_id = session.scalar(
            select(MemoryEvidence.upstream_source_id).where(
                MemoryEvidence.downstream_memory_id == fact.id
            )
        )
        session.get(SourceRecord, source_id).status = "deleted"
    with factory() as session:
        report = backfill(session, tenant)
        assert report[0]["invalidated_profile_ids"] == [profile.id]
        session.rollback()
    with factory.begin() as session:
        assert session.get(ProfileProperty, profile.id).status == "active"
        first = backfill(session, tenant)
    with factory.begin() as session:
        assert session.get(ProfileProperty, profile.id).status == "invalidated"
        second = backfill(session, tenant)
        assert second[0]["invalidated_profile_ids"] == []
        assert second[0]["governance_pending_added"] == 0
        assert second[0]["profile_task_id"] == first[0]["profile_task_id"]


def test_management_profile_rebuild_and_conflict_queries_are_scoped(context):
    from dataclasses import replace

    from fastapi.testclient import TestClient
    from test_add_api import _settings

    from memory_cmic.api import create_app
    from memory_cmic.auth import StaticCredentialStore

    factory, tenant = context
    group = uuid4().hex
    with factory.begin() as session:
        first = seed_fact(session, tenant, "邮件默认7天")
        second = seed_fact(session, tenant, "邮件默认3天")
        first.status = second.status = "disputed"
        first.conflict_group_id = second.conflict_group_id = group
    credentials = StaticCredentialStore(
        [
            {
                "token": "manager",
                "tenant_id": tenant,
                "caller_agent_id": "admin",
                "can_manage_governance": True,
            },
            {
                "token": "other",
                "tenant_id": "other-tenant",
                "caller_agent_id": "admin",
                "can_manage_governance": True,
            },
            {
                "token": "writer",
                "tenant_id": tenant,
                "caller_agent_id": "writer",
                "allowed_source_systems": ["test"],
            },
            {
                "token": "content-manager",
                "tenant_id": tenant,
                "caller_agent_id": "admin",
                "can_manage_governance": True,
                "can_read_audit_content": True,
            },
        ]
    )
    settings = replace(_settings(tenant), credentials=credentials)
    with TestClient(create_app(settings, engine=factory.kw["bind"])) as client:
        admin = {"Authorization": "Bearer manager"}
        assert (
            client.get(
                "/api/v1/governance/conflicts", headers={"Authorization": "Bearer writer"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/governance/conflicts/{group}", headers={"Authorization": "Bearer other"}
            ).status_code
            == 404
        )
        groups = client.get("/api/v1/governance/conflicts?user_id=user_1", headers=admin).json()
        assert groups["conflicts"][0]["unresolved"]
        assert len(groups["conflicts"][0]["members"]) == 2
        assert groups["conflicts"][0]["members"][0]["content"] is None
        visible = client.get(
            f"/api/v1/governance/conflicts/{group}",
            headers={"Authorization": "Bearer content-manager"},
        ).json()
        assert visible["members"][0]["content"]
        rebuild = client.post("/api/v1/profiles/rebuild", json={"user_id": "user_1"}, headers=admin)
        assert rebuild.status_code == 202 and rebuild.json()["status"] == "pending"
        replay = client.post("/api/v1/profiles/rebuild", json={"user_id": "user_1"}, headers=admin)
        assert replay.json()["task_id"] == rebuild.json()["task_id"]
        governance = client.post(
            "/api/v1/governance/runs", json={"user_id": "user_1"}, headers=admin
        ).json()
        with factory.begin() as session:
            current = session.get(MemoryTask, governance["task_id"])
            profile_task = session.get(MemoryTask, rebuild.json()["task_id"])
            profile_task.payload = {
                **profile_task.payload,
                "trigger_correlation_ids": [current.correlation_id],
            }
        linked = client.get(
            f"/api/v1/governance/runs/{governance['task_id']}", headers=admin
        ).json()["associated_tasks"]
        assert linked[0]["status"] == "pending" and linked[0]["results"] is None
        assert run_once(factory, worker_id="opt", model=EmptyModel())
        linked = client.get(
            f"/api/v1/governance/runs/{governance['task_id']}", headers=admin
        ).json()["associated_tasks"]
        assert linked[0]["status"] == "succeeded"
        assert linked[0]["results"][0]["created"] == 0


def test_merge_of_dirty_anchor_continues_inference_on_surviving_facts(context):
    from memory_cmic.consolidation import ConsolidationDecision
    from memory_cmic.governance_state import mark_dirty

    factory, tenant = context
    with factory.begin() as session:
        winner = seed_fact(session, tenant, "周报先给结论")
        duplicate = seed_fact(session, tenant, "周报把结论放在最前面")
        other = seed_fact(session, tenant, "复盘先写结论")
        winner.created_at = datetime.now(UTC) - timedelta(days=2)
        duplicate.created_at = datetime.now(UTC) - timedelta(days=1)
        old, _ = seed_inference(session, tenant, [duplicate, other])
        mark_dirty(session, duplicate)
        create_run(session, tenant_id=tenant, user_id="user_1")
    phases = []

    class Model:
        def decide(self, payload):
            phases.append(payload["phase"])
            if payload["phase"] == "facts":
                return ConsolidationDecision(merge_ids=[winner.id])
            return ConsolidationDecision.model_validate(
                {
                    "inferences": [
                        {
                            "content": "用户撰写报告时倾向于先给结论",
                            "source_ids": [winner.id, other.id],
                            "confidence": 0.8,
                            "reason": "两类报告的共同模式",
                            "business_domains": ["report"],
                        }
                    ]
                }
            )

    assert consolidate_once(factory, worker_id="opt", model=Model(), model_id="test")
    assert phases == ["facts", "inference"]
    with factory() as session:
        assert session.get(MemoryItem, duplicate.id).status == "invalidated"
        assert session.get(MemoryItem, old.id).status == "invalidated"
        assert (
            len(
                session.scalars(
                    select(MemoryItem).where(
                        MemoryItem.tenant_id == tenant,
                        MemoryItem.cognitive_type == "inference",
                        MemoryItem.status == "active",
                    )
                ).all()
            )
            == 1
        )
        assert (
            session.scalar(
                select(GovernancePending.memory_id).where(GovernancePending.tenant_id == tenant)
            )
            is None
        )


def test_truncated_scope_retains_previously_clean_candidates(context):
    from memory_cmic.governance_state import mark_dirty

    factory, tenant = context
    with factory.begin() as session:
        facts = [seed_fact(session, tenant, f"用户明确报告要求第{i}项") for i in range(51)]
        mark_dirty(session, facts[0])
        task = create_run(session, tenant_id=tenant, user_id="user_1")
    assert consolidate_once(factory, worker_id="opt", model=FakeModel({}), model_id="test")
    with factory() as session:
        current = session.get(MemoryTask, task.id)
        assert current.status == "partial"
        assert any(item.get("reason") == "INPUT_BUDGET" for item in current.result_json)
        pending_ids = set(
            session.scalars(
                select(GovernancePending.memory_id).where(GovernancePending.tenant_id == tenant)
            )
        )
        omitted_ids = {
            memory_id
            for item in current.result_json
            if item.get("kind") == "deferred_scope"
            for memory_id in item["memory_ids"]
        }
        assert pending_ids == omitted_ids and pending_ids
        assert current.payload["remaining"] == len(pending_ids)


@pytest.mark.parametrize("conflicted", [False, True])
def test_oversized_original_evidence_keeps_work_without_provider_retry(context, conflicted):
    factory, tenant = context
    with factory.begin() as session:
        facts = [
            seed_fact(session, tenant, "用户希望报告简洁"),
            seed_fact(session, tenant, "用户希望报告详细"),
        ]
        for memory in facts:
            source_id = session.scalar(
                select(MemoryEvidence.upstream_source_id).where(
                    MemoryEvidence.downstream_memory_id == memory.id
                )
            )
            source = session.get(SourceRecord, source_id)
            source.raw_content = memory.content + "\n" + "原始对话背景" * 10000
            source.content_hash = hashlib.sha256(source.raw_content.encode()).hexdigest()
            if conflicted:
                memory.status = "disputed"
                memory.conflict_group_id = "oversized_group"
        session.flush()
        backfill_user(session, tenant, "user_1")
        task = create_run(session, tenant_id=tenant, user_id="user_1")
    model = FakeModel({})
    assert consolidate_once(factory, worker_id="opt", model=model, model_id="test")
    with factory() as session:
        current = session.get(MemoryTask, task.id)
        assert current.status == "partial" and current.payload["remaining"] == 2
        assert current.attempt_count == 0 and current.last_error is None
        reason = "CONFLICT_INPUT_BUDGET" if conflicted else "INPUT_BUDGET"
        assert any(item.get("reason") == reason for item in current.result_json)
    assert model.calls == 0


def test_future_fact_schedules_profile_when_it_becomes_eligible(context, monkeypatch):
    factory, tenant = context
    now = datetime.now(UTC)
    future = now + timedelta(days=1)
    with factory.begin() as session:
        fact = seed_fact(session, tenant, "用户将担任后端工程师")
        fact.effective_at = future
        task = enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1")
        assert task.available_at == future + timedelta(minutes=2)
        assert task.payload["activation_only"]
        assert read_profiles(session, tenant_id=tenant, user_id="user_1") == []
        task.available_at = now - timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future + timedelta(minutes=2)

    class Model:
        def extract(self, memories, *, dimensions):
            return [
                ProfileProposal(
                    property_key="communication", memory_id=m["id"],
                    value=m["content"], quote=m["content"],
                )
                for m in memories
            ]

    monkeypatch.setattr(profile_module, "datetime", Clock)
    assert run_once(factory, worker_id="opt", model=Model())
    with factory() as session:
        assert len(read_profiles(session, tenant_id=tenant, user_id="user_1", now=Clock.now())) == 1
        assert (
            session.scalar(
                select(MemoryTask.id).where(
                    MemoryTask.tenant_id == tenant,
                    MemoryTask.task_type == "profile_rebuild",
                    MemoryTask.status == "pending",
                )
            )
            is None
        )


def test_future_activation_is_queued_after_current_input_succeeds(context):
    from test_profile import VerbatimProfileModel

    factory, tenant = context
    future = datetime.now(UTC) + timedelta(days=1)
    with factory.begin() as session:
        seed_fact(session, tenant, "用户喜欢简洁报告")
        fact = seed_fact(session, tenant, "用户下周开始担任工程师")
        fact.effective_at = future
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_1", force=True)
    assert run_once(factory, worker_id="opt", model=VerbatimProfileModel())
    with factory() as session:
        pending = session.scalars(
            select(MemoryTask).where(
                MemoryTask.tenant_id == tenant,
                MemoryTask.task_type == "profile_rebuild",
                MemoryTask.status == "pending",
            )
        ).one()
        assert pending.available_at == future + timedelta(minutes=2)
        assert pending.payload["activation_only"]
