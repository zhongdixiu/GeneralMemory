from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from test_add_api import _settings
from test_consolidation import seed_fact

from memory_cmic.api import create_app
from memory_cmic.audit import audit_body, list_audits, record_audit
from memory_cmic.auth import StaticCredentialStore
from memory_cmic.consolidation import InvalidationProposal, _invalidate, revert_operation
from memory_cmic.models import Base, GovernanceOperation, MemoryAuditLog, MemoryEvidence, MemoryTask
from memory_cmic.profile import enqueue_profile_rebuild


def _event(session, **kwargs):
    values = {
        "tenant_id": "tenant_audit",
        "action": "INSERT",
        "target_type": "memory",
        "target_id": "memory_audit",
        "reason_code": "FACT_EXTRACTED",
    }
    values.update(kwargs)
    row = record_audit(session, **values)
    session.flush()
    return row


@pytest.fixture
def audit_client(connection):
    credentials = StaticCredentialStore(
        [
            {
                "token": "manager",
                "tenant_id": "tenant_audit",
                "caller_agent_id": "admin",
                "can_manage_governance": True,
            },
            {
                "token": "reader",
                "tenant_id": "tenant_audit",
                "caller_agent_id": "reader",
                "can_manage_governance": True,
                "can_read_audit_content": True,
            },
            {
                "token": "writer",
                "tenant_id": "tenant_audit",
                "caller_agent_id": "agent",
                "allowed_source_systems": ["test"],
            },
        ]
    )
    settings = replace(_settings("tenant_audit"), credentials=credentials)
    with TestClient(create_app(settings, engine=connection)) as client:
        yield client


def test_writer_uses_sql_null_and_explicit_task_link(session):
    task = MemoryTask(
        id="task_audit",
        tenant_id="tenant_audit",
        task_type="fact_extract",
        target_type="batch",
        target_id="batch",
        caller_agent_id="agent",
        idempotency_key="audit",
        payload={},
    )
    session.add(task)
    session.flush()
    row = _event(session, task=task)
    assert row.task_id == task.id
    assert row.correlation_id == task.correlation_id
    assert row.correlation_id is not None
    assert row.context == {"initiator_id": "agent"}
    assert row.reason == "创建事实记忆"
    assert session.execute(
        text("SELECT state_before IS NULL, state_after IS NULL FROM memory_audit_log WHERE id=:id"),
        {"id": row.id},
    ).one() == (True, True)
    with pytest.raises(ValueError, match="same tenant"):
        _event(session, task=task, tenant_id="tenant_other_audit")


def test_legacy_inference_is_in_object_history_without_rewriting(session):
    task = MemoryTask(
        id="task_old_inference",
        tenant_id="tenant_audit",
        task_type="consolidate",
        target_type="subject",
        target_id="user",
        idempotency_key="old",
        correlation_id="old",
    )
    operation = GovernanceOperation(
        id="operation_old",
        tenant_id="tenant_audit",
        user_id="user",
        task_id=task.id,
        kind="inference",
        memory_id="inference_old",
        details={},
    )
    session.add_all([task, operation])
    session.flush()
    row = _event(
        session,
        action="UPDATE",
        target_type="task",
        target_id=task.id,
        reason_code="GOVERNANCE_INFERENCE",
        correlation_id="old",
        state_after={"operation_id": operation.id, "content": "私密推断", "reason": "私密依据"},
    )
    # A different tenant cannot borrow the operation's memory association.
    other = _event(
        session,
        tenant_id="tenant_other_audit",
        target_type="task",
        reason_code="GOVERNANCE_INFERENCE",
        state_after={"operation_id": operation.id},
    )
    results = list_audits(
        session, tenant_id="tenant_audit", target_type="memory", target_id="inference_old"
    )["events"]
    assert len(results) == 1
    event = results[0]
    assert event["id"] == row.id
    assert event["target_type"] == "memory" and event["target_id"] == "inference_old"
    assert event["action"] == "INSERT" and event["task_id"] == task.id
    assert event["reason"] is None
    assert row.target_type == "task" and row.action == "UPDATE"
    assert not list_audits(
        session, tenant_id=other.tenant_id, target_type="memory", target_id="inference_old"
    )["events"]


def test_old_policy_dispute_and_json_null_are_readable(session):
    row = _event(
        session,
        target_type="task",
        target_id="tenant_audit",
        action="UPDATE",
        reason_code="GOVERNANCE_POLICY_CHANGED",
    )
    event = list_audits(session, tenant_id="tenant_audit", target_type="governance_policy")[
        "events"
    ][0]
    assert event["target_type"] == "governance_policy" and event["task_id"] is None
    session.execute(
        text("UPDATE memory_audit_log SET state_before='null'::jsonb WHERE id=:id"), {"id": row.id}
    )
    session.refresh(row)
    assert audit_body(row)["changes"] == []
    dispute = _event(
        session,
        action="INVALIDATE",
        reason_code="FACT_DISPUTED",
        state_before={"status": "active"},
        state_after={"status": "disputed"},
    )
    assert audit_body(dispute)["action"] == "UPDATE"
    assert dispute.action == "INVALIDATE"


def test_api_permissions_redact_free_text_and_nested_content(session, audit_client):
    _event(
        session,
        reason="原因包含私密原文",
        state_before={"status": "active", "content": "旧原文"},
        state_after={"status": "disputed", "content": "新原文", "basis": [{"content": "依据"}]},
        context={"quote": "关联原文"},
    )
    _event(session, tenant_id="tenant_other_audit", reason="其他租户原文")
    path = "/api/v1/audit-logs"
    assert audit_client.get(path).status_code == 401
    assert audit_client.get(path, headers={"Authorization": "Bearer writer"}).status_code == 403
    manager = {"Authorization": "Bearer manager"}
    response = audit_client.get(path, headers=manager)
    assert response.status_code == 200
    event = response.json()["events"][0]
    assert len(response.json()["events"]) == 1
    assert event["reason"] is None and "state_after" not in event and "context" not in event
    assert event["changes"] == [{"field": "status", "before": "active", "after": "disputed"}]
    assert "原文" not in response.text and "依据" not in response.text
    assert (
        audit_client.get(path, headers=manager, params={"include_details": True}).status_code == 403
    )
    full = audit_client.get(
        path, headers={"Authorization": "Bearer reader"}, params={"include_details": True}
    ).json()["events"][0]
    assert full["reason"] == "原因包含私密原文"
    assert full["state_after"]["basis"] == [{"content": "依据"}]
    assert audit_client.get(path, headers=manager, params={"target_id": "x"}).status_code == 422
    assert (
        audit_client.get(path, headers=manager, params={"target_type": "invalid"}).status_code
        == 422
    )
    assert audit_client.get(path, headers=manager, params={"limit": 101}).status_code == 422


def test_business_filter_and_stable_pagination(session):
    timestamp = datetime(2026, 10, 10, tzinfo=UTC)
    first = _event(session, created_at=timestamp)
    second = _event(session, created_at=timestamp, target_id="second")
    technical = _event(session, reason_code="VECTOR_UPSERT", target_type="embedding")
    page = list_audits(session, tenant_id="tenant_audit", limit=1)
    assert page["has_more"] and page["events"][0]["id"] == second.id
    next_page = list_audits(session, tenant_id="tenant_audit", limit=1, offset=1)
    assert not next_page["has_more"] and next_page["events"][0]["id"] == first.id
    assert technical.id in {
        e["id"]
        for e in list_audits(session, tenant_id="tenant_audit", include_technical=True)["events"]
    }


def test_coalesced_profile_task_preserves_both_trigger_chains(session):
    task = enqueue_profile_rebuild(
        session, tenant_id="tenant_audit", user_id="user", correlation_id="first"
    )
    reused = enqueue_profile_rebuild(
        session, tenant_id="tenant_audit", user_id="user", correlation_id="second"
    )
    assert reused.id == task.id and reused.correlation_id == "first"
    assert reused.payload["trigger_correlation_ids"] == ["first", "second"]
    row = _event(session, target_type="profile", task=task, reason_code="PROFILE_REBUILT")
    for chain in ("first", "second"):
        assert [
            e["id"]
            for e in list_audits(session, tenant_id="tenant_audit", correlation_id=chain)["events"]
        ] == [row.id]


def test_revert_can_be_found_from_original_chain(session):
    task = MemoryTask(
        id="task_original",
        tenant_id="tenant_audit",
        task_type="consolidate",
        target_type="subject",
        target_id="user",
        idempotency_key="original",
        correlation_id="original_chain",
    )
    session.add(task)
    session.flush()
    old = _event(session, reason_code="GOVERNANCE_REVERTED", correlation_id=task.id)
    new = _event(
        session,
        reason_code="GOVERNANCE_REVERTED",
        correlation_id="new_revert_chain",
        task=task,
        context={"original_correlation_id": task.correlation_id},
    )
    assert [
        e["id"]
        for e in list_audits(session, tenant_id="tenant_audit", correlation_id=task.correlation_id)[
            "events"
        ]
    ] == [old.id, new.id]


def test_governance_details_require_content_permission(session, audit_client):
    task = MemoryTask(
        id="task_details",
        tenant_id="tenant_audit",
        task_type="consolidate",
        target_type="subject",
        target_id="user",
        idempotency_key="details",
    )
    session.add(task)
    session.add(
        GovernanceOperation(
            id="operation_details",
            tenant_id="tenant_audit",
            user_id="user",
            task_id=task.id,
            kind="inference",
            memory_id="memory",
            details={"basis": [{"content": "私密"}]},
        )
    )
    session.flush()
    path = f"/api/v1/governance/runs/{task.id}/operations"
    hidden = audit_client.get(path, headers={"Authorization": "Bearer manager"}).json()
    assert hidden["operations"][0]["details"] is None
    visible = audit_client.get(path, headers={"Authorization": "Bearer reader"}).json()
    assert visible["operations"][0]["details"]["basis"] == [{"content": "私密"}]


def test_audit_schema_matches_metadata(connection):
    context = MigrationContext.configure(
        connection,
        opts={
            "include_schemas": True,
            "include_object": lambda obj, name, kind, reflected, compare_to: (
                kind != "table" or name == "memory_audit_log"
            ),
        },
    )
    assert compare_metadata(context, Base.metadata) == []


def test_governance_invalidation_and_revert_keep_real_reasons(session):
    old = seed_fact(session, "tenant_audit", "用户希望周报使用完整段落")
    current = seed_fact(session, "tenant_audit", "之前要求取消，周报改用项目符号")
    source_id = session.scalar(
        select(MemoryEvidence.upstream_source_id).where(
            MemoryEvidence.downstream_memory_id == current.id
        )
    )
    task = MemoryTask(
        id="task_reason",
        tenant_id="tenant_audit",
        task_type="consolidate",
        target_type="subject",
        target_id="user_1",
        user_id="user_1",
        idempotency_key="reason",
        correlation_id="chain_reason",
        payload={},
    )
    session.add(task)
    session.flush()
    result = _invalidate(
        session,
        task,
        InvalidationProposal(
            memory_id=old.id,
            source_id=source_id,
            quote="之前要求取消",
            reason="用户已明确取消原来的段落要求",
        ),
        {old.id: old, current.id: current},
    )
    assert result is not None
    session.flush()
    event = session.scalars(
        select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == "tenant_audit",
            MemoryAuditLog.target_id == old.id,
        )
    ).one()
    assert event.reason_code == "GOVERNANCE_INVALIDATE" and event.action == "INVALIDATE"
    assert event.reason == "用户已明确取消原来的段落要求"
    assert event.context["operation_id"] == result["operation_id"]
    assert event.task_id == task.id and event.state_before["status"] == "active"
    operation = session.get(GovernanceOperation, result["operation_id"])
    revert_operation(
        session,
        operation=operation,
        expected_version=old.version,
        operator_id="admin",
        reason="管理员复核后恢复原要求",
        model_id="test-model",
    )
    session.flush()
    reverted = session.scalars(
        select(MemoryAuditLog).where(
            MemoryAuditLog.tenant_id == "tenant_audit",
            MemoryAuditLog.reason_code == "GOVERNANCE_REVERTED",
        )
    ).one()
    assert reverted.operator_type == "admin" and reverted.operator_id == "admin"
    assert reverted.reason == "管理员复核后恢复原要求"
    assert reverted.state_after["status"] == "active"
    assert reverted.correlation_id != task.correlation_id
    assert reverted.context["original_correlation_id"] == task.correlation_id


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_orm_cannot_rewrite_audit_history(session, operation):
    row = _event(session)
    with pytest.raises(ValueError, match="append-only"):
        with session.begin_nested():
            if operation == "update":
                row.reason = "改写历史"
            else:
                session.delete(row)
            session.flush()
    session.refresh(row)
    assert row.reason == "创建事实记忆"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_content_permission_requires_boolean(value):
    with pytest.raises(ValueError, match="can_read_audit_content"):
        StaticCredentialStore(
            [
                {
                    "token": "bad",
                    "tenant_id": "tenant_audit",
                    "caller_agent_id": "admin",
                    "can_manage_governance": True,
                    "can_read_audit_content": value,
                }
            ]
        )
