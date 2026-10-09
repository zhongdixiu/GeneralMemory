from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker
from test_add_worker import FakeEmbedder, _add
from test_add_worker import worker_context as _worker_context
from test_fact_governance import ScriptedModel

import memory_cmic.profile as profile_module
from memory_cmic.lifecycle import update_or_invalidate_source
from memory_cmic.models import (
    GovernancePending,
    MemoryAuditLog,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.profile import ProfileProposal, enqueue_profile_rebuild, read_profiles, run_once
from memory_cmic.repositories import (
    add_evidence_group,
    create_memory,
    create_or_replace_profile,
    create_source,
)
from memory_cmic.task_queue import enqueue_task
from memory_cmic.worker import run_once as run_fact_once

worker_context = _worker_context


class ExplicitProfileModel:
    def __init__(self):
        self.calls = 0

    def extract(self, memories):
        self.calls += 1
        proposals = []
        for memory in memories:
            if "邮件回复简洁" in memory["content"]:
                proposals.append(
                    ProfileProposal(
                        memory_id=memory["id"],
                        value="用户希望邮件回复简洁",
                        quote="邮件回复简洁",
                    )
                )
            if "邮件回复详细" in memory["content"]:
                proposals.append(
                    ProfileProposal(
                        memory_id=memory["id"],
                        value="用户希望邮件回复详细",
                        quote="邮件回复详细",
                    )
                )
        return proposals


class VerbatimProfileModel:
    def extract(self, memories):
        return [
            ProfileProposal(memory_id=m["id"], value=m["content"], quote=m["content"])
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
                    MemoryTask.task_type == "profile_rebuild",
                    MemoryTask.status.in_(["pending", "processing"]),
                )
                .limit(1)
            )
        if pending is None:
            return
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
            GovernancePending,
            MemoryEvidence,
            MemoryTask,
            ProfileProperty,
            MemoryItem,
            SourceRecord,
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
        def extract(self, memories):
            return [
                ProfileProposal(
                    memory_id=memories[0]["id"],
                    value="用户叫大钟",
                    quote="用户名叫大钟",
                ),
                ProfileProposal(
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
        assert all(p.property_key is None for p in profiles)
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
        def extract(self, memories):
            return [
                ProfileProposal(
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
        assert all(row.property_key is None and row.value_type == "string" for row in rows)
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
        assert rows[0].property_key is None
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
        task = enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")

    class ChangingModel(ExplicitProfileModel):
        def extract(self, memories):
            with factory.begin() as session:
                update_or_invalidate_source(
                    session, tenant_id=tenant_id, source_id=source.id, status="deleted"
                )
            return super().extract(memories)

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
        assert all(row.property_key is None for row in rows)


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
    _run_fact(factory, ScriptedModel({"business_domains": ["email"]}))
    _drain_profile(factory, tenant_id, VerbatimProfileModel())
    with factory() as session:
        rows = read_profiles(
            session, tenant_id=tenant_id, user_id="user_worker", business_domain="email"
        )
        assert len(rows) == 1
        first_id = rows[0].id
    _add(client, key="second", message_id="second", content=second)
    fields = {"business_domains": ["email"]}
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


def test_repository_creates_keyless_statements_independently(session):
    rows = [
        create_or_replace_profile(
            session,
            {
                "id": f"prf_{uuid4().hex}",
                "tenant_id": "tenant_keyless_repository",
                "user_id": "u1",
                "business_domains": ["email", "email"],
                "property_value": value,
                "value_type": "string",
                "effective_at": datetime.now(UTC),
            },
        )
        for value in ("用户希望邮件简洁", "用户希望邮件先给结论")
    ]
    assert all(row.status == "active" and row.property_key is None for row in rows)
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
        def extract(self, memories):
            Clock.current = initial_time + timedelta(hours=2)
            return super().extract(memories)

    monkeypatch.setattr(profile_module, "datetime", Clock)
    with factory.begin() as session:
        _fact(session, tenant_id, "用户希望邮件简洁", expired_at=initial_time + timedelta(hours=1))
        task = enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id="user_profile")
        task.available_at = initial_time - timedelta(hours=1)
    assert run_once(factory, worker_id="profile_test", model=ExpiringModel())
    with factory() as session:
        assert session.get(MemoryTask, task.id).status == "pending"
        assert "evidence changed" in session.get(MemoryTask, task.id).last_error
        assert (
            session.scalar(select(ProfileProperty.id).where(ProfileProperty.tenant_id == tenant_id))
            is None
        )
