from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

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
from memory_cmic.repositories import add_evidence_group, create_memory, create_source
from memory_cmic.task_queue import enqueue_task


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
                        key="邮件回复偏好",
                        value="用户希望邮件回复简洁",
                        quote="邮件回复简洁",
                    )
                )
            if "邮件回复详细" in memory["content"]:
                proposals.append(
                    ProfileProposal(
                        memory_id=memory["id"],
                        key="邮件回复偏好",
                        value="用户希望邮件回复详细",
                        quote="邮件回复详细",
                    )
                )
        return proposals


def _drain_profile(factory, tenant_id, model):
    deadline = monotonic() + 5
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
    pytest.fail("profile task queue did not drain")


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
                    key="姓名",
                    value="用户叫大钟",
                    quote="用户名叫大钟",
                ),
                ProfileProposal(
                    memory_id=memories[0]["id"],
                    key="职位",
                    value="用户是一名算法研发",
                    quote="是一名算法研发",
                ),
            ]

    with factory.begin() as session:
        _, memory = _fact(
            session, tenant_id, "用户名叫大钟，是一名算法研发", business_domains=None
        )
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
        assert {p.property_key: p.property_value for p in profiles} == {
            "姓名": "用户叫大钟",
            "职位": "用户是算法研发",
        }
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


def test_multiple_output_requirements_share_all_basis(profile_context):
    factory, tenant_id = profile_context

    class RequirementsModel:
        def extract(self, memories):
            return [
                ProfileProposal(
                    memory_id=memory["id"],
                    key="输出要求",
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
        assert len(rows) == 1
        assert rows[0].property_value == [
            "用户写项目周报时先写结论",
            "用户写项目复盘时先写结论",
        ]
        assert rows[0].value_type == "list"
        edges = session.scalars(
            select(MemoryEvidence).where(MemoryEvidence.downstream_profile_id == rows[0].id)
        ).all()
        assert len(edges) == 2
        assert len({edge.evidence_group_id for edge in edges}) == 1
    with factory.begin() as session:
        update_or_invalidate_source(
            session, tenant_id=tenant_id, source_id=source.id, status="deleted"
        )
        assert read_profiles(
            session, tenant_id=tenant_id, user_id="user_profile", business_domain="方案编写"
        ) == []


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
        assert rows[0].property_key == "邮件回复偏好"
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


def test_source_loss_and_conflict_remove_old_value(profile_context):
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
        _fact(session, tenant_id, "以后邮件回复简洁")
        _fact(session, tenant_id, "以后邮件回复详细")
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
        assert rows[0].version > 1
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
