from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from runpy import run_path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import sessionmaker
from test_add_worker import FakeEmbedder, _add
from test_add_worker import worker_context as _worker_context
from test_fact_governance import ScriptedModel

import memory_cmic.profile as profile_module
from memory_cmic.lifecycle import update_or_invalidate_source
from memory_cmic.models import (
    GovernancePending,
    GovernanceSubjectState,
    MemoryAuditLog,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
    TenantProfileConfig,
)
from memory_cmic.profile import ProfileProposal, enqueue_profile_rebuild, read_profiles, run_once
from memory_cmic.providers import QwenFactModel
from memory_cmic.repositories import (
    add_evidence_group,
    create_memory,
    create_profile,
    create_source,
)
from memory_cmic.task_queue import enqueue_task
from memory_cmic.worker import run_once as run_fact_once

worker_context = _worker_context


class ExplicitProfileModel:
    def __init__(self):
        self.calls = 0

    def extract(self, memories, *, dimensions):
        self.calls += 1
        proposals = []
        for memory in memories:
            if "邮件回复简洁" in memory["content"]:
                proposals.append(
                    ProfileProposal(
                        property_key="communication",
                        memory_id=memory["id"],
                        value="用户希望邮件回复简洁",
                        quote="邮件回复简洁",
                    )
                )
            if "邮件回复详细" in memory["content"]:
                proposals.append(
                    ProfileProposal(
                        property_key="communication",
                        memory_id=memory["id"],
                        value="用户希望邮件回复详细",
                        quote="邮件回复详细",
                    )
                )
        return proposals


class VerbatimProfileModel:
    def extract(self, memories, *, dimensions):
        return [
            ProfileProposal(
                property_key="communication", memory_id=m["id"],
                value=m["content"], quote=m["content"],
            )
            for m in memories
        ]


def _drain_profile(factory, tenant_id, model):
    deadline = monotonic() + 15
    while monotonic() < deadline:
        with factory() as session:
            pending = session.scalar(
                select(MemoryTask.id)
                .where(
                    MemoryTask.tenant_id == tenant_id,
                    MemoryTask.task_type.in_(["profile_rebuild", "profile_config_apply"]),
                    MemoryTask.status.in_(["pending", "processing"]),
                    or_(
                        MemoryTask.payload["activation_only"].as_boolean().is_not(True),
                        MemoryTask.available_at <= datetime.now(UTC),
                    ),
                )
                .limit(1)
            )
        if pending is None:
            return
        # These tests exercise distillation, not the automatic waiting policy.
        with factory.begin() as session:
            for task in session.scalars(
                select(MemoryTask).where(
                    MemoryTask.tenant_id == tenant_id,
                    MemoryTask.task_type == "profile_rebuild",
                    MemoryTask.status == "pending",
                    or_(
                        MemoryTask.payload["activation_only"].as_boolean().is_not(True),
                        MemoryTask.available_at <= datetime.now(UTC),
                    ),
                )
            ):
                task.available_at = datetime.now(UTC) - timedelta(seconds=1)
        if not run_once(factory, worker_id="profile_test", model=model):
            sleep(0.05)
    with factory() as session:
        tasks = session.execute(
            select(MemoryTask.id, MemoryTask.status, MemoryTask.last_error).where(
                MemoryTask.tenant_id == tenant_id, MemoryTask.task_type == "profile_rebuild"
            )
        ).all()
    pytest.fail(f"profile task queue did not drain: {tasks}")


def _run_fact(factory, model):
    deadline = monotonic() + 15
    while monotonic() < deadline:
        if run_fact_once(
            factory,
            worker_id="profile_fact_test",
            fact_model=model,
            embedder=FakeEmbedder(),
            duplicate_threshold=0.85,
        ):
            return
        sleep(0.05)
    pytest.fail("fact task did not become available")


@pytest.fixture
def profile_context(engine):
    tenant_id = f"tenant_profile_{uuid4().hex}"
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory, tenant_id
    with engine.begin() as connection:
        for model in (
            MemoryAuditLog,
            GovernanceSubjectState,
            GovernancePending,
            MemoryEvidence,
            MemoryTask,
            ProfileProperty,
            MemoryItem,
            SourceRecord,
            TenantProfileConfig,
        ):
            connection.execute(delete(model).where(model.tenant_id == tenant_id))


def _fact(
    session, tenant_id, content, *, project=False, expired_at=None, business_domains=("email",)
):
    source = create_source(
        session,
        {
            "id": f"src_{uuid4().hex}",
            "tenant_id": tenant_id,
            "source_system": "test",
            "source_type": "chat",
            "external_ref_id": uuid4().hex,
            "author_type": "user",
            "author_id": "user_profile",
            "raw_content": content,
            "content_hash": "0" * 64,
            "occurred_at": datetime.now(UTC) - timedelta(days=1),
        },
    )
    memory = create_memory(
        session,
        {
            "id": f"mem_{uuid4().hex}",
            "tenant_id": tenant_id,
            "subject_type": "user",
            "subject_id": "user_profile",
            "cognitive_type": "fact",
            "business_domains": list(business_domains) if business_domains else None,
            "project_domains": None,
            "summary": content,
            "content": content,
            "metadata_json": {
                "project_limited": project,
                "project_context": "项目甲" if project else None,
                "temporal_kind": "current",
            },
            "effective_at": datetime.now(UTC) - timedelta(days=1),
            "expired_at": expired_at,
            "created_by": "test",
        },
    )
    add_evidence_group(
        session,
        [
            {
                "id": f"evi_{uuid4().hex}",
                "tenant_id": tenant_id,
                "relationship_type": "supports",
                "evidence_group_id": f"evg_{uuid4().hex}",
                "upstream_source_id": source.id,
                "downstream_memory_id": memory.id,
                "upstream_version": source.version,
            }
        ],
    )
    return source, memory


def test_name_and_job_are_separate_sentence_properties(profile_context):
    factory, tenant_id = profile_context

    class IdentityModel:
        def extract(self, memories, *, dimensions):
            return [
                ProfileProposal(
                    property_key="identity",
                    memory_id=memories[0]["id"],
                    value="用户叫大钟",
                    quote="用户名叫大钟",
                ),
                ProfileProposal(
                    property_key="occupation",
                    memory_id=memories[0]["id"],
                    value="用户是算法研发",
                    quote="是一名算法研发",
                ),
            ]

    with factory.begin() as session:
        _, memory = _fact(session, tenant_id, "用户名叫大钟，是一名算法研发", business_domains=None)
        legacy = ProfileProperty(
            id=f"prf_{uuid4().hex}",
            tenant_id=tenant_id,
            user_id="user_profile",
            property_key="identity.preferred_name",
            property_value="大钟",
            value_type="string",
            effective_at=memory.effective_at,
        )
        session.add(legacy)
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, IdentityModel())
    with factory() as session:
        profiles = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert {p.property_value for p in profiles} == {"用户叫大钟", "用户是算法研发"}
        assert {p.property_key for p in profiles} == {"identity", "occupation"}
        assert session.get(ProfileProperty, legacy.id).status == "invalidated"
        assert all(
            session.scalar(
                select(MemoryEvidence).where(
                    MemoryEvidence.downstream_profile_id == profile.id,
                    MemoryEvidence.upstream_memory_id == memory.id,
                )
            )
            for profile in profiles
        )


def test_output_requirements_have_independent_basis(profile_context):
    factory, tenant_id = profile_context

    class RequirementsModel:
        def extract(self, memories, *, dimensions):
            return [
                ProfileProposal(
                    property_key="communication",
                    memory_id=memory["id"],
                    value=f"用户{memory['content']}",
                    quote=memory["content"],
                )
                for memory in memories
            ]

    with factory.begin() as session:
        source, _ = _fact(
            session, tenant_id, "写项目周报时先写结论", business_domains=("方案编写",)
        )
        _fact(session, tenant_id, "写项目复盘时先写结论", business_domains=("方案编写",))
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, RequirementsModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="方案编写"
        )
        assert {row.property_value for row in rows} == {
            "用户写项目周报时先写结论",
            "用户写项目复盘时先写结论",
        }
        assert all(row.property_key is not None and row.value_type == "string" for row in rows)
        edges = session.scalars(
            select(MemoryEvidence).where(
                MemoryEvidence.downstream_profile_id.in_([row.id for row in rows])
            )
        ).all()
        assert len(edges) == 2
        assert len({edge.evidence_group_id for edge in edges}) == 2
        remaining_id = next(
            row.id for row in rows if row.property_value == "用户写项目复盘时先写结论"
        )
    with factory.begin() as session:
        update_or_invalidate_source(
            session, tenant_id=tenant_id, source_id=source.id, status="deleted"
        )
        remaining = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="方案编写"
        )
        assert [row.id for row in remaining] == [remaining_id]
    _drain_profile(factory, tenant_id, RequirementsModel())
    with factory() as session:
        remaining = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="方案编写"
        )
        assert [row.id for row in remaining] == [remaining_id]


def test_rebuild_creates_grounded_profile_and_coalesces(profile_context):
    factory, tenant_id = profile_context
    model = ExplicitProfileModel()
    with factory.begin() as session:
        source, memory = _fact(session, tenant_id, "以后邮件回复简洁")
        _fact(session, tenant_id, "我在项目甲负责设计，邮件回复详细", project=True)
        task = enqueue_profile_rebuild(
            session, tenant_id=tenant_id, user_id="user_profile", correlation_id="corr_profile"
        )
        same = enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
        assert task.id == same.id
        task_id = task.id
    _drain_profile(factory, tenant_id, model)
    with factory() as session:
        assert session.get(MemoryTask, task_id).status == "succeeded"
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert len(rows) == 1
        assert rows[0].property_key == "communication"
        assert rows[0].property_value == "用户希望邮件回复简洁"
        basis = session.scalars(
            select(MemoryEvidence).where(
                MemoryEvidence.downstream_profile_id == rows[0].id,
                MemoryEvidence.relationship_type == "profile_basis",
            )
        ).one()
        assert basis.upstream_memory_id == memory.id
        assert session.get(SourceRecord, source.id).raw_content == "以后邮件回复简洁"
        assert (
            session.scalars(
                select(MemoryAuditLog).where(
                    MemoryAuditLog.tenant_id == tenant_id,
                    MemoryAuditLog.target_id == rows[0].id,
                )
            )
            .one()
            .correlation_id
            == "corr_profile"
        )
        assert (
            read_profiles(
                session, tenant_id=tenant_id, user_id="user_profile", business_domain="disk"
            )
            == []
        )
    with factory.begin() as session:
        enqueue_task(
            session,
            {
                "id": f"tsk_{uuid4().hex}",
                "tenant_id": tenant_id,
                "task_type": "profile_rebuild",
                "target_type": "subject",
                "target_id": "user_profile",
                "user_id": "user_profile",
                "idempotency_key": uuid4().hex,
            },
        )
    _drain_profile(factory, tenant_id, model)
    with factory() as session:
        assert (
            session.scalar(
                select(ProfileProperty).where(
                    ProfileProperty.tenant_id == tenant_id, ProfileProperty.status == "active"
                )
            ).id
            == rows[0].id
        )


def test_source_loss_and_disputed_facts_remove_old_value(profile_context):
    factory, tenant_id = profile_context
    model = ExplicitProfileModel()
    with factory.begin() as session:
        source, old_memory = _fact(session, tenant_id, "以后邮件回复简洁")
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, model)
    with factory.begin() as session:
        update_or_invalidate_source(
            session, tenant_id=tenant_id, source_id=source.id, status="deleted"
        )
        assert (
            read_profiles(
                session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
            )
            == []
        )
    _drain_profile(factory, tenant_id, model)
    with factory.begin() as session:
        _, concise = _fact(session, tenant_id, "以后邮件回复简洁")
        _, detailed = _fact(session, tenant_id, "以后邮件回复详细")
        # Conflict resolution belongs to fact governance, not profile key grouping.
        concise.status = detailed.status = "disputed"
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, model)
    with factory() as session:
        assert (
            read_profiles(
                session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
            )
            == []
        )
        assert session.get(MemoryItem, old_memory.id).status == "invalidated"


def test_explicit_change_replaces_value(profile_context):
    factory, tenant_id = profile_context
    model = ExplicitProfileModel()
    with factory.begin() as session:
        source, _ = _fact(session, tenant_id, "以后邮件回复简洁")
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, model)
    with factory() as session:
        first_id = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )[0].id
    with factory.begin() as session:
        update_or_invalidate_source(
            session, tenant_id=tenant_id, source_id=source.id, status="deleted"
        )
        _fact(session, tenant_id, "以后邮件回复详细")
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, model)
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert len(rows) == 1
        assert rows[0].property_value == "用户希望邮件回复详细"
        assert session.get(ProfileProperty, first_id).status == "invalidated"


def test_changed_evidence_during_model_call_does_not_commit_stale_profile(profile_context):
    factory, tenant_id = profile_context
    with factory.begin() as session:
        source, _ = _fact(session, tenant_id, "以后邮件回复简洁")
        task = enqueue_profile_rebuild(
            session, tenant_id=tenant_id, user_id="user_profile", force=True,
        )

    class ChangingModel(ExplicitProfileModel):
        def extract(self, memories, *, dimensions):
            with factory.begin() as session:
                update_or_invalidate_source(
                    session, tenant_id=tenant_id, source_id=source.id, status="deleted"
                )
            return super().extract(memories, dimensions=dimensions)

    assert run_once(factory, worker_id="profile_test", model=ChangingModel())
    with factory() as session:
        assert session.get(MemoryTask, task.id).status == "pending"
        assert (
            read_profiles(
                session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
            )
            == []
        )


def test_complementary_preferences_and_conditions_are_preserved(profile_context):
    factory, tenant_id = profile_context
    values = {
        "用户希望邮件回复简洁，但风险说明需要详细",
        "用户希望邮件回复先给结论",
    }
    with factory.begin() as session:
        for value in values:
            _fact(session, tenant_id, value)
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert {row.property_value for row in rows} == values
        assert all(row.property_key is not None for row in rows)


def test_duplicate_statement_keeps_independent_evidence(profile_context):
    factory, tenant_id = profile_context
    with factory.begin() as session:
        first_source, first = _fact(session, tenant_id, "用户希望邮件回复简洁")
        second_source, second = _fact(session, tenant_id, "用户希望邮件回复简洁")
        second.effective_at = first.effective_at
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert len(rows) == 1
        profile_id = rows[0].id
        edges = session.scalars(
            select(MemoryEvidence).where(MemoryEvidence.downstream_profile_id == profile_id)
        ).all()
        assert len({edge.evidence_group_id for edge in edges}) == 2
    with factory.begin() as session:
        update_or_invalidate_source(
            session, tenant_id=tenant_id, source_id=first_source.id, status="deleted"
        )
        assert [
            p.id
            for p in read_profiles(
                session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
            )
        ] == [profile_id]
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory.begin() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert len(rows) == 1 and rows[0].property_value == "用户希望邮件回复简洁"
        assert rows[0].id == profile_id
        update_or_invalidate_source(
            session, tenant_id=tenant_id, source_id=second_source.id, status="deleted"
        )
        assert (
            read_profiles(
                session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
            )
            == []
        )


@pytest.mark.parametrize("difference", ["scope", "effective_at", "expired_at"])
def test_same_statement_with_different_applicability_is_not_merged(profile_context, difference):
    factory, tenant_id = profile_context
    with factory.begin() as session:
        _, first = _fact(session, tenant_id, "用户希望先给结论")
        _, second = _fact(session, tenant_id, "用户希望先给结论")
        second.effective_at = first.effective_at
        if difference == "scope":
            second.business_domains = None
        elif difference == "effective_at":
            second.effective_at -= timedelta(hours=1)
        else:
            second.expired_at = datetime.now(UTC) + timedelta(days=1)
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        assert (
            len(
                read_profiles(
                    session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
                )
            )
            == 2
        )


def test_profile_expiration_and_future_facts_are_filtered(profile_context):
    factory, tenant_id = profile_context
    now = datetime.now(UTC)
    with factory.begin() as session:
        _, memory = _fact(
            session, tenant_id, "用户希望邮件回复简洁", expired_at=now + timedelta(hours=1)
        )
        _, future = _fact(session, tenant_id, "用户希望邮件回复先给结论")
        future.effective_at = now + timedelta(days=1)
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert len(rows) == 1 and rows[0].expired_at == memory.expired_at
        assert (
            read_profiles(
                session,
                tenant_id=tenant_id,
                user_id="user_profile",
                business_domain="email",
                now=now + timedelta(hours=2),
            )
            == []
        )


@pytest.mark.parametrize("kind", ["temporary", "regular", "long_term"])
def test_classified_profile_only_uses_long_term_facts(profile_context, kind):
    factory, tenant_id = profile_context
    with factory.begin() as session:
        source, memory = _fact(session, tenant_id, "用户希望邮件回复简洁")
        memory.metadata_json = {
            **memory.metadata_json, "retention_kind": kind, "explicit_expired_at": None
        }
        source_id = source.id
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        )
        assert len(rows) == (1 if kind == "long_term" else 0)
        if rows:
            assert rows[0].expired_at is None
    if kind == "long_term":
        with factory.begin() as session:
            update_or_invalidate_source(
                session, tenant_id=tenant_id, source_id=source_id, status="invalidated"
            )
        with factory() as session:
            assert read_profiles(
                session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
            ) == []


def test_ttl_report_lists_unclassified_facts_and_profiles_without_changes(profile_context):
    factory, tenant_id = profile_context
    with factory.begin() as session:
        _, legacy = _fact(session, tenant_id, "用户希望邮件回复简洁")
        _, classified = _fact(session, tenant_id, "本阶段邮件回复详细")
        classified.metadata_json = {
            **classified.metadata_json,
            "retention_kind": "temporary",
            "explicit_expired_at": None,
        }
        legacy_id, legacy_metadata = legacy.id, legacy.metadata_json
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    build_report = run_path(
        str(Path(__file__).parents[1] / "scripts/report_memory_ttl.py")
    )["build_report"]
    with factory() as session:
        report = build_report(session, tenant_id)
        assert [row["id"] for row in report] == [legacy_id]
        assert report[0]["proposed_retention_kind"] is None
        assert [profile["value"] for profile in report[0]["profiles"]] == [
            "用户希望邮件回复简洁"
        ]
        assert session.get(MemoryItem, legacy_id).metadata_json == legacy_metadata
        assert session.get(MemoryItem, legacy_id).expired_at is None
        assert not session.dirty and not session.deleted and not session.new


@pytest.fixture
def integrated_profile_context(worker_context, engine):
    client, tenant_id = worker_context
    factory = sessionmaker(engine, expire_on_commit=False)
    yield client, factory, tenant_id
    with factory.begin() as session:
        session.execute(delete(MemoryEvidence).where(MemoryEvidence.tenant_id == tenant_id))
        session.execute(delete(ProfileProperty).where(ProfileProperty.tenant_id == tenant_id))


@pytest.mark.parametrize("action", ["ADD", "SUPERSEDE", "DISPUTE"])
def test_fact_governance_drives_profile_updates(integrated_profile_context, action):
    client, factory, tenant_id = integrated_profile_context
    first = "用户希望邮件回复简洁"
    second = (
        "用户希望邮件回复先给结论"
        if action == "ADD"
        else "用户希望邮件回复详细"
        if action == "DISPUTE"
        else "之前说错了，用户希望邮件回复详细"
    )
    _add(client, key="first", message_id="first", content=first)
    _run_fact(factory, ScriptedModel({
        "business_domains": ["email"], "retention_kind": "long_term"
    }))
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_worker", business_domain="email"
        )
        assert len(rows) == 1
        first_id = rows[0].id
    _add(client, key="second", message_id="second", content=second)
    fields = {"business_domains": ["email"], "retention_kind": "long_term"}
    if action == "SUPERSEDE":
        fields["change_kind"] = "correction"
    _run_fact(factory, ScriptedModel(fields, action=action))
    with factory() as session:
        current = read_profiles(
            session, tenant_id=tenant_id, user_id="user_worker", business_domain="email"
        )
        assert [p.id for p in current] == ([first_id] if action == "ADD" else [])
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_worker", business_domain="email"
        )
        expected = (
            {first, second} if action == "ADD" else {second} if action == "SUPERSEDE" else set()
        )
        assert {row.property_value for row in rows} == expected
        if action == "ADD":
            assert next(row.id for row in rows if row.property_value == first) == first_id


@pytest.mark.parametrize(
    "content,normalized,domains,project_context",
    [
        ("我喜欢吃年糕。", "用户喜欢吃年糕。", None, None),
        ("早餐时我喜欢吃年糕。", "用户在早餐时喜欢吃年糕。", None, None),
        ("以后邮件回复都简洁一些。", "用户希望以后邮件回复简洁。", ["email"], None),
        ("项目甲的周报先写结论。", "用户要求项目甲的周报先写结论。", ["report"], "项目甲"),
    ],
)
def test_fact_without_scope_known_reaches_profile_with_conditions(
    integrated_profile_context, monkeypatch, content, normalized, domains, project_context
):
    client, factory, tenant_id = integrated_profile_context
    _add(client, key="profile-conditions", message_id="conditions", content=content)
    fact = {
        "memory": normalized,
        "message_ids": ["conditions"],
        "business_domains": domains,
        "project_limited": project_context is not None,
        "project_context": project_context,
        "temporal_kind": "current",
        "retention_kind": "long_term",
        "change_kind": "none",
        "effective_at": None,
        "expired_at": None,
    }
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    monkeypatch.setattr(model, "_json_completion", lambda *args: {
        "facts": [fact], "assessments": [{"message_id": "conditions",
                                         "outcome": "evaluated", "reason": "有明确依据"}]})
    _run_fact(factory, model)
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        for domain in ("email", "disk"):
            rows = read_profiles(
                session, tenant_id=tenant_id, user_id="user_worker", business_domain=domain
            )
            expected = [normalized] if project_context is None and (
                domains is None or domain in domains
            ) else []
            assert [row.property_value for row in rows] == expected
            for row in rows:
                assert row.business_domains == domains
                evidence = session.scalars(
                    select(MemoryEvidence).where(MemoryEvidence.downstream_profile_id == row.id)
                ).one()
                basis = session.get(MemoryItem, evidence.upstream_memory_id)
                assert basis.cognitive_type == "fact"
                assert basis.content == normalized


def test_inference_is_not_promoted_to_explicit_profile(profile_context):
    factory, tenant_id = profile_context
    with factory.begin() as session:
        _, memory = _fact(session, tenant_id, "用户编写周报和复盘时倾向先给结论")
        memory.cognitive_type = "inference"
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        assert read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="email"
        ) == []


def test_profile_rebuild_uses_explicit_facts_from_multiple_agents(profile_context):
    factory, tenant_id = profile_context
    contents = ["用户希望邮件回复简洁", "用户希望邮件先列结论"]
    with factory.begin() as session:
        for agent, content in zip(("agent_a", "agent_b"), contents, strict=True):
            _, memory = _fact(session, tenant_id, content)
            memory.created_by = agent
        _, inferred = _fact(session, tenant_id, "用户倾向简洁且结论先行的沟通")
        inferred.cognitive_type = "inference"
        enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(session, tenant_id=tenant_id, user_id="user_profile")
        assert {row.property_value for row in rows} == set(contents)


def test_repository_creates_same_dimension_statements_independently(session):
    rows = [
        create_profile(
            session,
            {
                "id": f"prf_{uuid4().hex}",
                "tenant_id": "tenant_dimension_repository",
                "property_key": "communication",
                "user_id": "u1",
                "business_domains": ["email", "email"],
                "property_value": value,
                "value_type": "string",
                "effective_at": datetime.now(UTC),
            },
        )
        for value in ("用户希望邮件简洁", "用户希望邮件先给结论")
    ]
    assert all(row.status == "active" and row.property_key is not None for row in rows)
    assert all(row.business_domains == ["email"] for row in rows)


def test_fact_expiring_during_model_call_cannot_commit_profile(profile_context, monkeypatch):
    factory, tenant_id = profile_context
    initial_time = datetime.now(UTC)

    class Clock(datetime):
        current = initial_time

        @classmethod
        def now(cls, tz=None):
            return cls.current

    class ExpiringModel(VerbatimProfileModel):
        def extract(self, memories, *, dimensions):
            Clock.current = initial_time + timedelta(hours=2)
            return super().extract(memories, dimensions=dimensions)

    monkeypatch.setattr(profile_module, "datetime", Clock)
    with factory.begin() as session:
        _fact(session, tenant_id, "用户希望邮件简洁", expired_at=initial_time + timedelta(hours=1))
        task = enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
        task.available_at = initial_time - timedelta(hours=1)
    assert run_once(factory, worker_id="profile_test", model=ExpiringModel())
    with factory() as session:
        assert session.get(MemoryTask, task.id).status == "pending"
        assert session.get(MemoryTask, task.id).payload["deferred_reason"] == "INPUT_CHANGED"
        assert session.get(MemoryTask, task.id).attempt_count == 0
        assert session.get(MemoryTask, task.id).last_error is None
        assert (
            session.scalar(select(ProfileProperty.id).where(ProfileProperty.tenant_id == tenant_id))
            is None
        )
