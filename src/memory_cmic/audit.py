from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from memory_cmic.models import GovernanceOperation, MemoryAuditLog, MemoryTask

# Event names describe what happened; reason carries the specific decision when available.
EVENT_LABELS = {
    "CONVERSATION_INGESTED": "接入来源消息",
    "SOURCE_INGESTED": "接入来源",
    "SOURCE_STATUS_CHANGED": "变更来源状态",
    "SOURCE_CORRECTED": "纠正来源",
    "FACT_EXTRACTION_ENQUEUED": "提交事实抽取任务",
    "FACT_EXTRACTED": "创建事实记忆",
    "MEMORY_CREATED": "创建记忆",
    "MEMORY_DELETED": "删除记忆",
    "MEMORY_UPDATED": "修改并替代记忆",
    "MANUAL_MEMORY_SOURCE": "记录记忆修改来源",
    "FACT_EVIDENCE_ADDED": "补充事实证据",
    "FACT_DISPUTED": "标记记忆存在争议",
    "FACT_SUPERSEDED": "替代原事实记忆",
    "SUPERSEDED_BY_CORRECTION": "纠正并替代原记忆",
    "EVIDENCE_INCOMPLETE": "因证据不足使对象失效",
    "TTL_EXPIRED": "记忆到期",
    "TTL_CLASSIFIED_LONG_TERM": "将记忆归为长期保留",
    "PROFILE_REBUILT": "重建用户画像",
    "ATOMIC_PROFILE_MIGRATION": "迁移旧画像属性",
    "GOVERNANCE_POLICY_CHANGED": "变更治理策略",
    "GOVERNANCE_MERGED": "合并并停用重复记忆",
    "GOVERNANCE_MERGE": "合并记忆证据",
    "GOVERNANCE_INFERENCE": "创建推断记忆",
    "GOVERNANCE_INFERENCE_REVIEW": "复核推断记忆",
    "INFERENCE_CONTRADICTED": "因明确反证使推断失效",
    "GOVERNANCE_CONFLICT_REVIEW": "复核记忆争议",
    "CONFLICT_RESOLVED": "依据澄清替代争议记忆",
    "PROFILE_GOVERNANCE_BACKFILL": "复核并失效存量画像",
    "GOVERNANCE_INVALIDATE": "治理否定记忆",
    "GOVERNANCE_SUSPECTED_STALE": "记录疑似过时的治理判断",
    "GOVERNANCE_REVERTED": "撤销治理操作",
    "VECTOR_UPSERT": "更新检索向量",
    "VECTOR_DELETE": "删除检索向量",
    "VECTOR_TASK_ENQUEUED": "提交向量任务",
    "DEPENDENCY_RECHECK_ENQUEUED": "提交依赖复查任务",
    "SCOPE_UNRESOLVED": "跳过适用范围不明确的候选",
    "TIME_UNRESOLVED": "跳过时间不明确的候选",
    "HONCHO_MIGRATION": "导入 Honcho 数据",
    "BUSINESS_SCENE_NORMALIZED": "规范业务场景",
}
TECHNICAL_EVENTS = {
    "FACT_EXTRACTION_ENQUEUED",
    "VECTOR_UPSERT",
    "VECTOR_DELETE",
    "VECTOR_TASK_ENQUEUED",
    "DEPENDENCY_RECHECK_ENQUEUED",
    "SCOPE_UNRESOLVED",
    "TIME_UNRESOLVED",
}
SAFE_STATE_FIELDS = {
    "status",
    "version",
    "effective_at",
    "expired_at",
    "confidence",
    "conflict_group_id",
    "evidence_count",
}


def record_audit(
    session: Session,
    *,
    tenant_id: str,
    action: str,
    target_type: str,
    target_id: str,
    reason_code: str,
    operator_type: str = "system",
    operator_id: str = "memory_cmic",
    reason: str | None = None,
    correlation_id: str | None = None,
    task: MemoryTask | None = None,
    task_id: str | None = None,
    state_before: dict[str, Any] | None = None,
    state_after: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> MemoryAuditLog:
    if task is not None:
        if task.tenant_id != tenant_id:
            raise ValueError("audit task must belong to the same tenant")
        task_id = task.id
        task.correlation_id = task.correlation_id or uuid4().hex
        correlation_id = correlation_id or task.correlation_id
        context = dict(context or {})
        if task.caller_agent_id:
            context["initiator_id"] = task.caller_agent_id
        if task.agent_id and task.agent_id != task.caller_agent_id:
            context["agent_id"] = task.agent_id
        triggers = (task.payload or {}).get("trigger_correlation_ids")
        if triggers:
            context["trigger_correlation_ids"] = triggers
    row = MemoryAuditLog(
        tenant_id=tenant_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        operator_type=operator_type,
        operator_id=operator_id,
        reason_code=reason_code,
        reason=(reason[:512] if reason else EVENT_LABELS.get(reason_code)),
        correlation_id=correlation_id,
        task_id=task_id,
        state_before=state_before,
        state_after=state_after,
        context=context or None,
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    return row


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def audit_body(
    row: MemoryAuditLog,
    *,
    memory_id: str | None = None,
    legacy_task_id: str | None = None,
    can_read_content: bool = False,
    include_details: bool = False,
) -> dict:
    before, after = _object(row.state_before), _object(row.state_after)
    context = _object(row.context)
    target_type, target_id = row.target_type, row.target_id
    action = row.action
    if row.reason_code == "GOVERNANCE_POLICY_CHANGED":
        target_type = "governance_policy"
    elif row.target_type == "task" and memory_id is not None:
        target_type, target_id = "memory", memory_id
        if row.reason_code == "GOVERNANCE_INFERENCE":
            action = "INSERT"
    if row.reason_code == "FACT_DISPUTED":
        action = "UPDATE"
    keys = before.keys() | after.keys()
    if not (can_read_content and include_details):
        keys &= SAFE_STATE_FIELDS
    changes = [
        {"field": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(keys)
        if (key in before) != (key in after) or before.get(key) != after.get(key)
    ]
    label = EVENT_LABELS.get(row.reason_code, row.reason_code)
    if row.reason_code == "PROFILE_REBUILT":
        label = "创建画像陈述" if action == "INSERT" else "停用旧画像陈述"
    body = {
        "id": row.id,
        "created_at": row.created_at,
        "event": row.reason_code,
        "event_name": label,
        "category": "technical" if row.reason_code in TECHNICAL_EVENTS else "business",
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "operator_type": row.operator_type,
        "operator_id": row.operator_id,
        "reason": (row.reason or after.get("reason")) if can_read_content else None,
        "summary": None,
        "correlation_id": row.correlation_id,
        "task_id": row.task_id
        or legacy_task_id
        or after.get("task_id")
        or (
            row.target_id
            if row.target_type == "task" and target_type != "governance_policy"
            else None
        ),
        "changes": changes,
    }
    summary = after.get("content", after.get("value"))
    if can_read_content and isinstance(summary, str):
        body["summary"] = summary[:160]
    if include_details and can_read_content:
        body["state_before"] = row.state_before
        body["state_after"] = row.state_after
        body["context"] = context
    return body


def list_audits(
    session: Session,
    *,
    tenant_id: str,
    target_type: str | None = None,
    target_id: str | None = None,
    correlation_id: str | None = None,
    task_id: str | None = None,
    reason_code: str | None = None,
    include_technical: bool = False,
    include_details: bool = False,
    can_read_content: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    audit, operation = MemoryAuditLog, GovernanceOperation
    operation_id = func.coalesce(
        audit.context["operation_id"].astext, audit.state_after["operation_id"].astext
    )
    query = (
        select(audit, operation.memory_id, operation.task_id)
        .outerjoin(
            operation, and_(operation.tenant_id == audit.tenant_id, operation.id == operation_id)
        )
        .where(audit.tenant_id == tenant_id)
    )
    if target_type == "memory":
        targets = [audit.target_type == "memory"]
        if target_id is not None:
            targets = [and_(audit.target_type == "memory", audit.target_id == target_id)]
        legacy = and_(audit.target_type == "task", operation.memory_id.is_not(None))
        if target_id is not None:
            legacy = and_(legacy, operation.memory_id == target_id)
            # Legacy merge operations also changed the surviving memory's evidence.
            legacy = or_(
                legacy,
                and_(
                    audit.reason_code == "GOVERNANCE_MERGE",
                    audit.state_after["winner_id"].astext == target_id,
                ),
            )
            if include_technical:
                targets.append(and_(audit.target_type == "embedding", audit.target_id == target_id))
        query = query.where(or_(*targets, legacy))
    elif target_type == "governance_policy":
        query = query.where(
            or_(audit.target_type == target_type, audit.reason_code == "GOVERNANCE_POLICY_CHANGED")
        )
        if target_id is not None:
            query = query.where(audit.target_id == target_id)
    elif target_type is not None:
        query = query.where(audit.target_type == target_type)
        if target_id is not None:
            query = query.where(audit.target_id == target_id)
    if correlation_id is not None:
        query = query.where(
            or_(
                audit.correlation_id == correlation_id,
                audit.context["trigger_correlation_ids"].contains([correlation_id]),
                audit.context["original_correlation_id"].astext == correlation_id,
                # Old reverts used the original task ID as their correlation.
                and_(
                    audit.reason_code == "GOVERNANCE_REVERTED",
                    audit.correlation_id.in_(
                        select(MemoryTask.id).where(
                            MemoryTask.tenant_id == tenant_id,
                            MemoryTask.correlation_id == correlation_id,
                        )
                    ),
                ),
            )
        )
    if task_id is not None:
        query = query.where(
            or_(
                audit.task_id == task_id,
                operation.task_id == task_id,
                audit.state_after["task_id"].astext == task_id,
                and_(audit.target_type == "task", audit.target_id == task_id),
            )
        )
    if reason_code is not None:
        query = query.where(audit.reason_code == reason_code)
    if not include_technical:
        query = query.where(audit.reason_code.not_in(TECHNICAL_EVENTS))
    if target_id is not None or correlation_id is not None or task_id is not None:
        query = query.order_by(audit.created_at, audit.id)
    else:
        query = query.order_by(audit.created_at.desc(), audit.id.desc())
    rows = session.execute(query.offset(offset).limit(limit + 1)).all()
    return {
        "events": [
            audit_body(
                row,
                memory_id=memory_id,
                legacy_task_id=legacy_task_id,
                can_read_content=can_read_content,
                include_details=include_details,
            )
            for row, memory_id, legacy_task_id in rows[:limit]
        ],
        "has_more": len(rows) > limit,
    }
