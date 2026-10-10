from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import sessionmaker

from memory_cmic.api import create_app
from memory_cmic.auth import StaticCredentialStore
from memory_cmic.consolidation import (
    ConsolidationDecision,
    QwenConsolidationModel,
    RevertConflict,
    revert_operation,
    run_once,
)
from memory_cmic.governance_state import backfill_user, create_run, ensure_policy, schedule_due
from memory_cmic.lifecycle import update_or_invalidate_source
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
    SourceRecord,
)
from memory_cmic.repositories import add_evidence_group, create_memory, create_source
from memory_cmic.settings import Settings


class FakeModel:
    def __init__(self, decision: dict):
        self.decision = ConsolidationDecision.model_validate(decision)
        self.calls = 0

    def decide(self, payload: dict) -> ConsolidationDecision:
        self.calls += 1
        return self.decision


@pytest.mark.parametrize("domains", [None, ["report"]])
def test_qwen_accepts_inference_without_scope_known(monkeypatch, domains):
    model = QwenConsolidationModel(
        api_key="fake", base_url="https://example.invalid/v1", model="test", output_tokens=2048
    )
    raw = {
        "inferences": [{
            "content": "用户编写周报和复盘时倾向先给结论",
            "source_ids": ["first", "second"],
            "confidence": 0.8,
            "reason": "两次独立要求体现共同输出习惯",
            "business_domains": domains,
        }]
    }
    monkeypatch.setattr(model._model, "_json_completion", lambda *args: raw)
    model._model.last_usage = None
    decision = model.decide({})
    assert decision.inferences[0].business_domains == domains


@pytest.fixture
def context(engine: Engine):
    tenant = f"tenant_consolidation_{uuid4().hex}"
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
            MemoryItem,
            SourceRecord,
        ):
            conn.execute(delete(model).where(model.tenant_id == tenant))


def seed_fact(
    session,
    tenant: str,
    content: str,
    *,
    user: str = "user_1",
    project_context: str | None = None,
    session_id: str | None = None,
) -> MemoryItem:
    if session.get(BusinessScene, (tenant, "report")) is None:
        session.add(
            BusinessScene(
                tenant_id=tenant,
                name="report",
                definition="编写项目周报和复盘文档",
                aliases=[],
            )
        )
        session.flush()
    source_id = f"src_{uuid4().hex}"
    source = create_source(
        session,
        {
            "id": source_id,
            "tenant_id": tenant,
            "source_system": "test",
            "source_type": "chat",
            "user_id": user,
            "session_id": session_id or f"session_{uuid4().hex}",
            "message_id": f"message_{uuid4().hex}",
            "external_ref_id": source_id,
            "author_type": "user",
            "author_id": user,
            "raw_content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "occurred_at": datetime.now(UTC) - timedelta(days=1),
        },
    )
    memory = create_memory(
        session,
        {
            "id": f"mem_{uuid4().hex}",
            "tenant_id": tenant,
            "subject_type": "user",
            "subject_id": user,
            "cognitive_type": "fact",
            "business_domains": ["report"],
            "metadata": {"project_context": project_context} if project_context else None,
            "summary": content,
            "content": content,
            "effective_at": source.occurred_at,
            "created_by": "test",
        },
    )
    add_evidence_group(
        session,
        [
            {
                "id": f"evi_{uuid4().hex}",
                "tenant_id": tenant,
                "relationship_type": "supports",
                "evidence_group_id": f"evg_{uuid4().hex}",
                "upstream_source_id": source.id,
                "downstream_memory_id": memory.id,
                "upstream_version": source.version,
            }
        ],
    )
    return memory


@pytest.mark.parametrize("with_ttl", [False, True])
def test_merge_keeps_sources_and_admin_can_revert(context, with_ttl):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "周报先写结论")
        second = seed_fact(session, tenant, "周报结论放在开头")
        expiry = first.effective_at + timedelta(days=180) if with_ttl else None
        first.expired_at = second.expired_at = expiry
        second_id = second.id
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    model = FakeModel({"merge_ids": [second_id], "inferences": [], "invalidations": []})
    assert run_once(factory, worker_id="test_consolidation", model=model, model_id="test-model")
    with factory() as session:
        operations = session.scalars(
            select(GovernanceOperation).where(
                GovernanceOperation.tenant_id == tenant, GovernanceOperation.kind == "merge"
            )
        ).all()
        assert len(operations) == 1
        operation = operations[0]
        survivor = session.get(MemoryItem, operation.details["winner_id"])
        loser = session.get(MemoryItem, operation.details["loser_id"])
        assert survivor.status == "active"
        assert loser.status == "invalidated"
        assert survivor.expired_at == loser.expired_at == expiry
        evidence = session.scalars(
            select(MemoryEvidence).where(
                MemoryEvidence.tenant_id == tenant,
                MemoryEvidence.downstream_memory_id == survivor.id,
                MemoryEvidence.status == "active",
            )
        ).all()
        assert len(evidence) == 2
        operation_id, expected_version = operation.id, loser.version

    settings = Settings(
        database_url="postgresql+psycopg://unused",
        credentials=StaticCredentialStore(
            [
                {
                    "token": "admin-token",
                    "tenant_id": tenant,
                    "caller_agent_id": "admin",
                    "can_manage_governance": True,
                },
                {
                    "token": "add-token",
                    "tenant_id": tenant,
                    "caller_agent_id": "agent",
                    "allowed_source_systems": ["test"],
                },
            ]
        ),
        dashscope_api_key=None,
        dashscope_base_url="",
        dashscope_model="",
        siliconflow_api_key=None,
        siliconflow_base_url="",
        siliconflow_embedding_model="test-model",
        duplicate_candidate_threshold=0.85,
    )
    with TestClient(create_app(settings=settings, engine=factory.kw["bind"])) as client:
        path = f"/api/v1/governance/operations/{operation_id}:revert"
        body = {"reason": "管理员确认两条要求不同", "expected_version": expected_version}
        assert (
            client.post(path, json=body, headers={"Authorization": "Bearer add-token"}).status_code
            == 403
        )
        response = client.post(
            path,
            json=body,
            headers={"Authorization": "Bearer admin-token", "Idempotency-Key": "revert-1"},
        )
        assert response.status_code == 200, response.text
        retry = client.post(
            path,
            json=body,
            headers={"Authorization": "Bearer admin-token", "Idempotency-Key": "revert-1"},
        )
        assert retry.json() == response.json()
    with factory() as session:
        assert session.get(MemoryItem, loser.id).status == "active"
        assert session.get(MemoryItem, loser.id).expired_at == expiry
        assert session.get(GovernanceOperation, operation_id).reverted_at is not None


@pytest.mark.parametrize("ttl_days", [None, 30])
@pytest.mark.parametrize("domains", [None, ["report"]])
def test_inference_needs_independent_user_sources(context, ttl_days, domains):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "周报写清楚结论")
        second = seed_fact(session, tenant, "周报要列出行动项")
        first.business_domains = second.business_domains = domains
        if ttl_days is not None:
            first.expired_at = first.effective_at + timedelta(days=ttl_days)
            second.expired_at = second.effective_at + timedelta(days=180)
        expiry = first.expired_at
        first_id, second_id = first.id, second.id
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    model = FakeModel(
        {
            "merge_ids": [],
            "inferences": [
                {
                    "content": "用户偏好周报给出清晰结论和行动项",
                    "source_ids": [first_id, second_id],
                    "confidence": 0.8,
                    "reason": "两次独立周报要求",
                    "business_domains": domains,
                }
            ],
            "invalidations": [],
        }
    )
    assert run_once(factory, worker_id="test_inference", model=model, model_id="test-model")
    with factory() as session:
        inferred = session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == tenant, MemoryItem.cognitive_type == "inference"
            )
        ).all()
        assert len(inferred) == 1
        assert inferred[0].status == "active"
        assert inferred[0].business_domains == domains
        assert inferred[0].expired_at == expiry
        event = session.scalars(select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == tenant,
            MemoryAuditLog.target_type == "memory", MemoryAuditLog.target_id == inferred[0].id,
            MemoryAuditLog.reason_code == "GOVERNANCE_INFERENCE",
        )).one()
        assert event.action == "INSERT" and event.task_id is not None
        assert event.reason == "两次独立周报要求"
        assert event.state_after["status"] == "active"
        assert "basis" not in event.state_after
        assert "operation_id" in event.context
        derives = session.scalars(
            select(MemoryEvidence).where(
                MemoryEvidence.tenant_id == tenant,
                MemoryEvidence.downstream_memory_id == inferred[0].id,
            )
        ).all()
        assert {edge.upstream_memory_id for edge in derives} == {first_id, second_id}
        assert len({edge.evidence_group_id for edge in derives}) == 1


def test_inference_cannot_expand_basis_business_scope(context):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "周报先写结论")
        second = seed_fact(session, tenant, "项目复盘先写结论")
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    model = FakeModel(
        {
            "inferences": [
                {
                    "content": "用户编写所有文档时都先写结论",
                    "source_ids": [first.id, second.id],
                    "confidence": 0.9,
                    "reason": "泛化到所有文档",
                    "business_domains": None,
                }
            ]
        }
    )
    assert run_once(factory, worker_id="test_scope_expansion", model=model, model_id="test-model")
    with factory() as session:
        assert not session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == tenant,
                MemoryItem.cognitive_type == "inference",
            )
        ).all()


def test_small_backlog_reaches_max_wait(context):
    factory, tenant = context
    with factory.begin() as session:
        seed_fact(session, tenant, "周报需要结论")
        policy = ensure_policy(session, tenant)
        policy.auto_enabled = True
        backfill_user(session, tenant, "user_1")
        pending = session.scalars(
            select(GovernancePending).where(GovernancePending.tenant_id == tenant)
        ).one()
        pending.first_change_at = datetime.now(UTC) - timedelta(hours=7)
        assert schedule_due(session) == 1
    with factory() as session:
        task = session.scalars(
            select(MemoryTask).where(
                MemoryTask.tenant_id == tenant, MemoryTask.task_type == "consolidate"
            )
        ).one()
        assert task.payload["automatic"] is True
        assert task.payload["trigger_reason"] == "max_wait"


def test_new_fact_during_model_call_retries_old_scope(context):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "周报先写结论")
        second = seed_fact(session, tenant, "周报结论放在开头")
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")

    class ConcurrentModel(FakeModel):
        def decide(self, payload: dict) -> ConsolidationDecision:
            with factory.begin() as session:
                seed_fact(session, tenant, "周报改成先列风险")
            return super().decide(payload)

    model = ConcurrentModel({"merge_ids": [second.id], "inferences": [], "invalidations": []})
    assert run_once(factory, worker_id="test_concurrent", model=model, model_id="test-model")
    with factory() as session:
        assert session.get(MemoryItem, first.id).status == "active"
        assert session.get(MemoryItem, second.id).status == "active"
        assert (
            session.scalars(
                select(GovernanceOperation).where(
                    GovernanceOperation.tenant_id == tenant,
                    GovernanceOperation.kind == "merge",
                )
            ).all()
            == []
        )
        task = session.scalars(
            select(MemoryTask).where(
                MemoryTask.tenant_id == tenant,
                MemoryTask.task_type == "consolidate",
            )
        ).one()
        assert task.status == "partial"
        assert any(item.get("reason") == "INPUT_CHANGED" for item in task.result_json)


def test_rejected_inference_paraphrase_waits_for_new_evidence(context):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "周报写清楚结论")
        second = seed_fact(session, tenant, "周报要列出行动项")
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    first_model = FakeModel(
        {
            "merge_ids": [],
            "inferences": [
                {
                    "content": "用户偏好周报给出清晰结论和行动项",
                    "source_ids": [first.id, second.id],
                    "confidence": 0.8,
                    "reason": "两次要求",
                    "business_domains": ["report"],
                }
            ],
            "invalidations": [],
        }
    )
    assert run_once(factory, worker_id="test_rejection", model=first_model, model_id="test-model")
    with factory.begin() as session:
        operation = session.scalars(
            select(GovernanceOperation).where(
                GovernanceOperation.tenant_id == tenant,
                GovernanceOperation.kind == "inference",
            )
        ).one()
        revert_operation(
            session,
            operation=operation,
            expected_version=1,
            operator_id="admin",
            reason="归纳不准确",
            model_id="test-model",
        )
        event = session.scalars(select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == tenant,
            MemoryAuditLog.target_id == operation.memory_id,
            MemoryAuditLog.reason_code == "GOVERNANCE_REVERTED",
        )).one()
        assert event.action == "INVALIDATE" and event.reason == "归纳不准确"
        assert event.operator_type == "admin" and event.operator_id == "admin"
        assert event.context["operation_id"] == operation.id
        assert event.correlation_id != operation.task_id
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    paraphrase = FakeModel(
        {
            "merge_ids": [],
            "inferences": [
                {
                    "content": "用户喜欢周报同时突出结论与后续动作",
                    "source_ids": [first.id, second.id],
                    "confidence": 0.9,
                    "reason": "两次要求",
                    "business_domains": ["report"],
                }
            ],
            "invalidations": [],
        }
    )
    assert run_once(
        factory, worker_id="test_rejection_again", model=paraphrase, model_id="test-model"
    )
    with factory() as session:
        active = session.scalars(
            select(MemoryItem).where(
                MemoryItem.tenant_id == tenant,
                MemoryItem.cognitive_type == "inference",
                MemoryItem.status == "active",
            )
        ).all()
        assert active == []


def test_policy_and_manual_run_are_tenant_admin_only(context):
    factory, tenant = context
    with factory.begin() as session:
        seed_fact(session, tenant, "周报先列结论")
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        credentials=StaticCredentialStore(
            [
                {
                    "token": "governance-admin",
                    "tenant_id": tenant,
                    "caller_agent_id": "admin",
                    "can_manage_governance": True,
                },
                {
                    "token": "normal-add",
                    "tenant_id": tenant,
                    "caller_agent_id": "agent",
                    "allowed_source_systems": ["test"],
                },
            ]
        ),
        dashscope_api_key=None,
        dashscope_base_url="",
        dashscope_model="",
        siliconflow_api_key=None,
        siliconflow_base_url="",
        siliconflow_embedding_model="test-model",
        duplicate_candidate_threshold=0.85,
    )
    with TestClient(create_app(settings=settings, engine=factory.kw["bind"])) as client:
        policy_path = "/api/v1/governance/policy"
        assert (
            client.get(policy_path, headers={"Authorization": "Bearer normal-add"}).status_code
            == 403
        )
        admin = {"Authorization": "Bearer governance-admin"}
        assert client.get(policy_path, headers=admin).json()["auto_enabled"] is False
        updated = client.patch(policy_path, json={"auto_enabled": True}, headers=admin)
        assert updated.status_code == 200
        assert updated.json()["version"] == 2
        run_headers = {**admin, "Idempotency-Key": "manual-once"}
        first = client.post(
            "/api/v1/governance/runs", json={"user_id": "user_1"}, headers=run_headers
        )
        replay = client.post(
            "/api/v1/governance/runs", json={"user_id": "user_1"}, headers=run_headers
        )
        assert first.status_code == 202
        assert first.json()["task_id"] == replay.json()["task_id"]
        assert first.json()["automatic"] is False
        assert first.json()["trigger_reason"] == "manual"
        assert (
            client.get(
                f"/api/v1/governance/runs/{first.json()['task_id']}", headers=admin
            ).status_code
            == 200
        )
    with factory() as session:
        assert (
            session.scalars(select(GovernancePending).where(GovernancePending.tenant_id == tenant))
            .one()
            .generation
            == 1
        )
        event = session.scalars(select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == tenant,
            MemoryAuditLog.reason_code == "GOVERNANCE_POLICY_CHANGED",
        )).one()
        assert event.target_type == "governance_policy" and event.target_id == tenant
        assert event.correlation_id is not None


def test_different_projects_never_enter_same_model_window(context):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "甲项目周报先写结论", project_context="甲项目")
        second = seed_fact(session, tenant, "乙项目周报先写结论", project_context="乙项目")
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    model = FakeModel({"merge_ids": [second.id], "inferences": [], "invalidations": []})
    assert run_once(factory, worker_id="test_projects", model=model, model_id="test-model")
    assert model.calls == 0
    with factory() as session:
        assert session.get(MemoryItem, first.id).status == "active"
        assert session.get(MemoryItem, second.id).status == "active"


def test_deleting_a_basis_stops_using_inference(context):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "周报结论要清楚")
        second = seed_fact(session, tenant, "周报要列行动项")
        first_source = session.scalars(
            select(MemoryEvidence.upstream_source_id).where(
                MemoryEvidence.tenant_id == tenant,
                MemoryEvidence.downstream_memory_id == first.id,
            )
        ).one()
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    model = FakeModel(
        {
            "merge_ids": [],
            "inferences": [
                {
                    "content": "用户希望周报结论清楚且有行动项",
                    "source_ids": [first.id, second.id],
                    "confidence": 0.8,
                    "reason": "两条独立要求",
                    "business_domains": ["report"],
                }
            ],
            "invalidations": [],
        }
    )
    assert run_once(factory, worker_id="test_source_delete", model=model, model_id="test-model")
    with factory.begin() as session:
        inference_id = session.scalars(
            select(MemoryItem.id).where(
                MemoryItem.tenant_id == tenant,
                MemoryItem.cognitive_type == "inference",
            )
        ).one()
        update_or_invalidate_source(
            session, tenant_id=tenant, source_id=first_source, status="deleted"
        )
        assert session.get(MemoryItem, inference_id).status == "invalidated"


def test_same_session_restatements_do_not_support_inference(context):
    factory, tenant = context
    with factory.begin() as session:
        first = seed_fact(session, tenant, "周报结论清楚", session_id="one_session")
        second = seed_fact(session, tenant, "周报要有行动项", session_id="one_session")
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    model = FakeModel(
        {
            "merge_ids": [],
            "inferences": [
                {
                    "content": "用户偏好结论和行动项明确的周报",
                    "source_ids": [first.id, second.id],
                    "confidence": 0.9,
                    "reason": "同会话的两条说法",
                    "business_domains": ["report"],
                }
            ],
            "invalidations": [],
        }
    )
    assert run_once(factory, worker_id="test_same_session", model=model, model_id="test-model")
    with factory() as session:
        assert (
            session.scalars(
                select(MemoryItem).where(
                    MemoryItem.tenant_id == tenant,
                    MemoryItem.cognitive_type == "inference",
                )
            ).all()
            == []
        )


def test_revert_rejects_new_fact_even_if_its_created_at_is_old(context):
    factory, tenant = context
    with factory.begin() as session:
        seed_fact(session, tenant, "周报先写结论")
        second = seed_fact(session, tenant, "周报结论放在前面")
        backfill_user(session, tenant, "user_1")
        create_run(session, tenant_id=tenant, user_id="user_1", operator_id="admin")
    model = FakeModel({"merge_ids": [second.id], "inferences": [], "invalidations": []})
    assert run_once(factory, worker_id="test_revert_conflict", model=model, model_id="test-model")
    with factory.begin() as session:
        operation = session.scalars(
            select(GovernanceOperation).where(
                GovernanceOperation.tenant_id == tenant,
                GovernanceOperation.kind == "merge",
            )
        ).one()
        new_fact = seed_fact(session, tenant, "周报改成先列风险")
        new_fact.created_at = operation.created_at - timedelta(days=10)
        with pytest.raises(RevertConflict, match="facts or evidence changed"):
            revert_operation(
                session,
                operation=operation,
                expected_version=operation.details["loser_version"],
                operator_id="admin",
                reason="复核旧合并",
                model_id="test-model",
            )
