"""Audited, administrator-only recovery of selected Add sources."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.api_schemas import AddRequest, Message
from memory_cmic.audit import record_audit
from memory_cmic.auth import AuthContext, AuthorizationError
from memory_cmic.db import create_database_engine
from memory_cmic.ingest import _allocate_batch_seq, _canonical_json, _semantic_payload, _sha256
from memory_cmic.lifecycle import update_or_invalidate_source
from memory_cmic.models import MemoryTask, MemoryTaskSource, SourceRecord
from memory_cmic.settings import Settings


def reprocess_add(
    session: Session,
    *,
    auth: AuthContext,
    task_id: str,
    reason: str,
    history_source_ids: list[str] | None = None,
    source_times: dict[str, datetime] | None = None,
) -> MemoryTask:
    if not auth.can_manage_governance:
        raise AuthorizationError("governance management permission required")
    if not reason.strip():
        raise ValueError("a recovery reason is required")
    previous = session.get(MemoryTask, task_id)
    if (
        previous is None
        or previous.tenant_id != auth.tenant_id
        or previous.task_type != "fact_extract"
    ):
        raise LookupError("Add task does not exist in tenant")
    if previous.status in {"pending", "processing"}:
        raise ValueError("finish or drain the original task before reprocessing")
    associations = session.scalars(
        select(MemoryTaskSource)
        .where(MemoryTaskSource.tenant_id == auth.tenant_id, MemoryTaskSource.task_id == task_id)
        .order_by(MemoryTaskSource.kind, MemoryTaskSource.position)
    ).all()
    targets = [a.source_id for a in associations if a.kind == "target"]
    history = list(
        dict.fromkeys(
            [a.source_id for a in associations if a.kind == "history"] + (history_source_ids or [])
        )
    )
    source_ids = set(targets + history)
    if not targets or not set(source_times or {}).issubset(targets):
        raise ValueError("time revisions must identify target sources of the selected task")
    sources = session.scalars(
        select(SourceRecord)
        .where(SourceRecord.id.in_(source_ids))
        .order_by(SourceRecord.id)
        .with_for_update()
    ).all()
    identity = (
        auth.tenant_id,
        previous.caller_agent_id,
        previous.agent_id,
        previous.source_system,
        previous.user_id,
        previous.session_id,
    )
    if len(sources) != len(source_ids) or any(
        (s.tenant_id, s.caller_agent_id, s.agent_id, s.source_system, s.user_id, s.session_id)
        != identity
        or s.status != "active"
        for s in sources
    ):
        raise ValueError("recovery sources must be active and in the original conversation")
    if session.scalar(
        select(MemoryTask.id)
        .join(MemoryTaskSource, MemoryTaskSource.task_id == MemoryTask.id)
        .where(
            MemoryTaskSource.source_id.in_(source_ids),
            MemoryTask.status.in_(["pending", "processing"]),
        )
        .limit(1)
    ):
        raise ValueError("source is still associated with an unfinished task; drain it first")
    correlation_id = uuid4().hex
    owner = AuthContext(
        auth.tenant_id, previous.caller_agent_id, frozenset([previous.source_system])
    )
    for source in sources:
        if source.id not in (source_times or {}):
            continue
        value = source_times[source.id]
        if value.tzinfo is None:
            raise ValueError("source revision time must include a timezone")
        before = {
            "occurred_at": source.occurred_at.isoformat() if source.occurred_at else None,
            "version": source.version,
            "metadata": source.metadata_json,
            "raw_content": source.raw_content,
        }
        update_or_invalidate_source(
            session,
            tenant_id=auth.tenant_id,
            source_id=source.id,
            status="active",
            correlation_id=correlation_id,
        )
        metadata = dict(source.metadata_json or {})
        fields = {
            key: metadata[key]
            for key in (
                "channel",
                "tool_name",
                "tool_call_id",
                "tool_status",
                "tool_calls",
                "attachments",
                "extensions",
            )
            if key in metadata
        }
        message = Message(
            message_id=source.message_id,
            role=metadata["role"],
            author_id=source.author_id,
            content=source.raw_content,
            occurred_at=value,
            metadata={**fields, "timestamp_origin": "source"},
            **{
                key: metadata[key]
                for key in ("turn_id", "reply_to_message_id")
                if metadata.get(key) is not None
            },
        )
        request = AddRequest(
            agent_id=previous.agent_id,
            source_system=previous.source_system,
            user_id=previous.user_id,
            session_id=previous.session_id,
            messages=[message],
        )
        payload = _semantic_payload(message, request, owner, source.author_id)
        source.occurred_at = value.astimezone(UTC)
        source.semantic_hash = _sha256(_canonical_json(payload))
        source.metadata_json = {
            **metadata,
            "original_occurred_at": value.isoformat(),
            "original_occurred_at_input": value.isoformat(),
            "timestamp_origin": "source",
            "time_semantics_version": 3,
            "source_input": payload,
        }
        source.processed_version = None
        record_audit(
            session,
            tenant_id=auth.tenant_id,
            action="UPDATE",
            target_type="source",
            target_id=source.id,
            operator_type="admin",
            operator_id=auth.caller_agent_id,
            reason_code="SOURCE_TIME_REVISED",
            reason=reason,
            correlation_id=correlation_id,
            state_before=before,
            state_after={"occurred_at": value.isoformat(), "version": source.version},
        )
    batch_seq = _allocate_batch_seq(
        session,
        tenant_id=auth.tenant_id,
        caller_agent_id=previous.caller_agent_id,
        agent_id=previous.agent_id,
        source_system=previous.source_system,
        user_id=previous.user_id,
        session_id=previous.session_id,
    )
    task = MemoryTask(
        id=f"tsk_{uuid4().hex}",
        tenant_id=auth.tenant_id,
        task_type="fact_extract",
        target_type="batch",
        target_id=f"batch_{uuid4().hex}",
        caller_agent_id=previous.caller_agent_id,
        agent_id=previous.agent_id,
        user_id=previous.user_id,
        source_system=previous.source_system,
        session_id=previous.session_id,
        batch_seq=batch_seq,
        correlation_id=correlation_id,
        idempotency_key=f"admin_reprocess:{uuid4().hex}",
        payload={
            "config_version": 3,
            "reprocess": {
                "previous_task_id": previous.id,
                "reason": reason,
                "operator_id": auth.caller_agent_id,
            },
        },
        max_attempts=5,
    )
    session.add(task)
    session.flush()
    by_id = {source.id: source for source in sources}
    for kind, ids in (("history", history), ("target", targets)):
        for position, source_id in enumerate(ids):
            session.add(
                MemoryTaskSource(
                    tenant_id=auth.tenant_id,
                    task_id=task.id,
                    source_id=source_id,
                    source_version=by_id[source_id].version,
                    kind=kind,
                    position=position,
                )
            )
    record_audit(
        session,
        tenant_id=auth.tenant_id,
        action="INSERT",
        target_type="task",
        target_id=task.id,
        operator_type="admin",
        operator_id=auth.caller_agent_id,
        reason_code="ADD_REPROCESS_ENQUEUED",
        reason=reason,
        task=task,
        context={
            "previous_task_id": previous.id,
            "target_source_ids": targets,
            "revised_source_ids": list(source_times or {}),
        },
    )
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--history-source-id", action="append", default=[])
    parser.add_argument("--source-time", action="append", default=[], metavar="SOURCE_ID=ISO8601")
    args = parser.parse_args()
    settings = Settings.from_env()
    auth = settings.credentials.authenticate("Bearer " + os.environ["MEMORY_MANAGEMENT_TOKEN"])
    times = {}
    for item in args.source_time:
        source_id, value = item.split("=", 1)
        parsed = Message(message_id="revision", role="user", content="revision", occurred_at=value)
        times[source_id] = parsed.occurred_at
    with Session(create_database_engine(settings.database_url)) as session, session.begin():
        task = reprocess_add(
            session,
            auth=auth,
            task_id=args.task_id,
            reason=args.reason,
            history_source_ids=args.history_source_id,
            source_times=times,
        )
        print(task.id)


if __name__ == "__main__":
    main()
