from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_add_worker import FakeEmbedder

from memory_cmic.business_scenes import SceneDecision, normalize_fact_scenes
from memory_cmic.extraction_input import ExtractionBudget
from memory_cmic.models import BusinessScene, MemoryAuditLog, MemoryItem, ProfileProperty
from memory_cmic.providers import ExtractedFact, ProviderError
from memory_cmic.retrieval import search_task_memories
from memory_cmic.scene_migration import apply_report, build_report
from memory_cmic.smoke_data import SMOKE_MODEL_ID, SMOKE_TENANT_ID, prepare_smoke_data


class SceneModel:
    def normalize_scenes(self, *, labels, scenes):
        return [SceneDecision(label=label, name="方案编写", definition=None) for label in labels]


def test_concurrent_scene_creation_reuses_one_name(engine):
    tenant = f"tenant_scene_race_{uuid4().hex}"
    factory = sessionmaker(engine, expire_on_commit=False)
    barrier = Barrier(2)

    class SameSceneModel:
        def normalize_scenes(self, *, labels, scenes):
            if all(scene["name"] != "方案评审" for scene in scenes):
                barrier.wait(timeout=10)
            return [
                SceneDecision(label=label, name="方案评审", definition="审查项目方案")
                for label in labels
            ]

    def run(label):
        fact = ExtractedFact(memory=label, message_ids=["m1"], business_domains=[label])
        return normalize_fact_scenes(
            factory,
            tenant_id=tenant,
            fact=fact,
            model=SameSceneModel(),
            embedder=FakeEmbedder(),
            consume_call=lambda: None,
            budget=ExtractionBudget(),
        ).business_domains

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(run, ["项目方案审核", "项目方案评估"])) == [
                ["方案评审"],
                ["方案评审"],
            ]
        with Session(engine) as session:
            scene = session.get(BusinessScene, (tenant, "方案评审"))
            assert set(scene.aliases) == {"项目方案审核", "项目方案评估"}
    finally:
        with factory.begin() as session:
            for scene in session.scalars(
                select(BusinessScene).where(BusinessScene.tenant_id == tenant)
            ):
                session.delete(scene)


def test_tenant_scene_reuse_and_retry(engine):
    tenant = f"tenant_scene_{uuid4().hex}"
    other_tenant = f"tenant_scene_{uuid4().hex}"
    factory = sessionmaker(engine, expire_on_commit=False)
    calls = []
    try:
        first = ExtractedFact(
            memory="写方案时先列目标", message_ids=["m1"], business_domains=["方案编写"]
        )
        second = ExtractedFact(
            memory="写项目方案时先列范围", message_ids=["m2"], business_domains=["项目方案写作"]
        )
        result = normalize_fact_scenes(
            factory,
            tenant_id=tenant,
            fact=first,
            model=object(),
            embedder=FakeEmbedder(),
            consume_call=lambda: calls.append(1),
            budget=ExtractionBudget(),
        )
        assert result.business_domains == ["方案编写"]
        for _ in range(2):
            result = normalize_fact_scenes(
                factory,
                tenant_id=tenant,
                fact=second,
                model=SceneModel(),
                embedder=FakeEmbedder(),
                consume_call=lambda: calls.append(1),
                budget=ExtractionBudget(),
            )
            assert result.business_domains == ["方案编写"]
        with Session(engine) as session:
            scene = session.get(BusinessScene, (tenant, "方案编写"))
            assert scene.aliases == ["项目方案写作"]
            assert (
                len(
                    [
                        s
                        for s in session.scalars(
                            select(BusinessScene).where(BusinessScene.tenant_id == tenant)
                        )
                        if s.name == "方案编写"
                    ]
                )
                == 1
            )
        other = normalize_fact_scenes(
            factory,
            tenant_id=other_tenant,
            fact=second,
            model=object(),
            embedder=FakeEmbedder(),
            consume_call=lambda: None,
            budget=ExtractionBudget(),
        )
        assert other.business_domains == ["项目方案写作"]
        assert len(calls) == 2  # one embedding, one model normalization
    finally:
        with factory.begin() as session:
            for scene in session.scalars(
                select(BusinessScene).where(
                    BusinessScene.tenant_id.in_([tenant, other_tenant])
                )
            ):
                session.delete(scene)


class IncludeAll:
    def select_for_query(self, *, query, candidates):
        return [{"id": item["id"], "include": True, "reason": "relevant"} for item in candidates]


class DeterministicEmbedder:
    model_id = SMOKE_MODEL_ID

    def embed(self, texts):
        from memory_cmic.deterministic_embedding import deterministic_embedding

        return [deterministic_embedding(text, self.model_id) for text in texts]


def test_task_search_has_two_candidate_channels_and_project_filter(session):
    generated = datetime(2026, 9, 21, 8, tzinfo=UTC)
    prepare_smoke_data(session, memory_count=60, generated_at=generated)
    session.add(
        BusinessScene(
            tenant_id=SMOKE_TENANT_ID,
            name="email",
            definition="邮件处理",
            aliases=["邮件"],
        )
    )
    session.flush()
    trace = []
    memories = search_task_memories(
        session,
        tenant_id=SMOKE_TENANT_ID,
        user_id="user_wp5",
        query="虚构 disk 记忆 00000026\n用于第五批范围和向量检索冒烟的虚构内容，序号 00000026。",
        authorized_agent_ids=["agent_wp5"],
        company_ids=["company_wp5"],
        project_id="project_a",
        embedder=DeterministicEmbedder(),
        applicability_model=IncludeAll(),
        now=generated,
        trace=trace,
    )
    assert memories
    assert any(item["sources"] == ["semantic"] for item in trace)
    assert any("scene" in item["sources"] for item in trace)
    assert all(
        memory.project_domains is None or "project_a" in memory.project_domains
        for memory in memories
    )
    user_only = search_task_memories(
        session,
        tenant_id=SMOKE_TENANT_ID,
        user_id="user_wp5",
        query="整理云盘文件",
        authorized_agent_ids=[],
        company_ids=[],
        project_id="project_a",
        embedder=DeterministicEmbedder(),
        applicability_model=IncludeAll(),
        now=generated,
    )
    assert all(memory.subject_type == "user" for memory in user_only)

    class InvalidSelector:
        def select_for_query(self, *, query, candidates):
            return [{"id": "outside", "include": True, "reason": "invented"}]

    with pytest.raises(ProviderError, match="unknown or missing"):
        search_task_memories(
            session,
            tenant_id=SMOKE_TENANT_ID,
            user_id="user_wp5",
            query="整理云盘文件",
            authorized_agent_ids=[],
            company_ids=[],
            embedder=DeterministicEmbedder(),
            applicability_model=InvalidSelector(),
            now=generated,
        )


def test_reviewed_migration_is_idempotent_and_audited(session):
    tenant = f"tenant_migrate_{uuid4().hex}"
    memory = MemoryItem(
        id=f"mem_{uuid4().hex}",
        tenant_id=tenant,
        subject_type="user",
        subject_id="u1",
        cognitive_type="fact",
        business_domains=["邮件"],
        summary="邮件偏好",
        content="处理邮件时先写结论",
        status="active",
        effective_at=datetime.now(UTC),
        created_by="test",
    )
    session.add(memory)
    session.flush()
    report = next(
        item for item in build_report(session, tenant_id=tenant) if item["id"] == memory.id
    )
    assert report["proposed"] == ["email"]
    assert apply_report(session, [report])[0]["status"] == "APPLIED"
    assert apply_report(session, [report])[0]["status"] == "UNCHANGED"
    assert memory.business_domains == ["email"]
    assert session.scalars(
        select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == tenant,
            MemoryAuditLog.reason_code == "BUSINESS_SCENE_NORMALIZED",
        )
    ).all()


def test_profile_collision_and_changed_input_are_reported(session):
    tenant = f"tenant_profile_scene_{uuid4().hex}"
    rows = [
        ProfileProperty(
            id=f"profile_{uuid4().hex}",
            tenant_id=tenant,
            user_id="u1",
            business_domains=scope,
            property_key="pref.reply_style",
            property_value="concise",
            value_type="string",
            status="active",
            effective_at=datetime.now(UTC),
        )
        for scope in (["邮件"], ["email"])
    ]
    session.add_all(rows)
    session.flush()
    item = next(
        item for item in build_report(session, tenant_id=tenant) if item["id"] == rows[0].id
    )
    assert apply_report(session, [item])[0]["status"] == "PROFILE_COLLISION"
    rows[0].version += 1
    assert apply_report(session, [item])[0]["status"] == "INPUT_CHANGED"


def test_reviewed_custom_alias_is_registered(session):
    tenant = f"tenant_custom_migrate_{uuid4().hex}"
    memory = MemoryItem(
        id=f"mem_{uuid4().hex}",
        tenant_id=tenant,
        subject_type="user",
        subject_id="u1",
        cognitive_type="fact",
        business_domains=["项目方案写作"],
        summary="方案偏好",
        content="写项目方案时先列目标",
        status="active",
        effective_at=datetime.now(UTC),
        created_by="test",
    )
    session.add(memory)
    session.flush()
    item = build_report(session, tenant_id=tenant)[0]
    assert item["unresolved"] == "UNKNOWN_SCENE"
    item["proposed"] = ["方案编写"]
    item["definitions"] = {"方案编写": "编写项目方案文稿"}
    item["register_aliases"] = ["项目方案写作"]
    item["reason"] = "正文限定为项目方案编写"
    assert apply_report(session, [item])[0]["status"] == "APPLIED"
    scene = session.get(BusinessScene, (tenant, "方案编写"))
    assert scene.aliases == ["项目方案写作"]


def test_ambiguous_old_name_can_split_without_becoming_alias(session):
    tenant = f"tenant_ambiguous_scene_{uuid4().hex}"
    memories = [
        MemoryItem(
            id=f"mem_{uuid4().hex}",
            tenant_id=tenant,
            subject_type="user",
            subject_id="u1",
            cognitive_type="fact",
            business_domains=["方案编写"],
            summary=content,
            content=content,
            status="active",
            effective_at=datetime.now(UTC),
            created_by="test",
        )
        for content in ("写周报先列结论", "整理项目复盘先列原因")
    ]
    session.add_all(memories)
    session.flush()
    report = build_report(session, tenant_id=tenant)
    names = ["周报编写", "项目复盘"]
    for item, name in zip(report, names, strict=True):
        item["proposed"] = [name]
        item["definitions"] = {name: name}
        item["reason"] = "按正文的任务活动归类"
    report[0]["register_aliases"] = ["方案编写"]
    savepoint = session.begin_nested()
    assert apply_report(session, report)[0]["status"] == "ALIAS_CONFLICT"
    savepoint.rollback()
    report[0]["register_aliases"] = []
    assert [outcome["status"] for outcome in apply_report(session, report)] == [
        "APPLIED",
        "APPLIED",
    ]
    assert all(session.get(BusinessScene, (tenant, name)).aliases == [] for name in names)
