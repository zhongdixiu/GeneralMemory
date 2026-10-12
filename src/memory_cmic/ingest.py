from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from memory_cmic.api_schemas import AddRequest, Message
from memory_cmic.audit import record_audit
from memory_cmic.auth import AuthContext
from memory_cmic.models import (
    ConversationSession,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)


class IngestError(ValueError):
    code = "INVALID_MESSAGE"


class IdempotencyConflict(IngestError):
    code = "IDEMPOTENCY_CONFLICT"


class MessageIdConflict(IngestError):
    code = "MESSAGE_ID_CONFLICT"


class SourceUnavailable(IngestError):
    code = "INVALID_MESSAGE"


@dataclass(frozen=True)
class IngestReceipt:
    task: MemoryTask
    replayed: bool


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def request_fingerprint(request: AddRequest) -> str:
    value = request.model_dump(
        mode="json", exclude={"wait_for_result", "wait_timeout_ms"}, exclude_none=True
    )
    return _sha256(_canonical_json(value))


def _resolved_author(message: Message, request: AddRequest, auth: AuthContext) -> tuple[str, str]:
    if message.role == "user":
        return "user", message.author_id or request.user_id
    if message.role == "assistant":
        if message.author_id is not None and message.author_id != auth.caller_agent_id:
            raise IngestError("assistant author_id is not allowed by this credential")
        return "agent", message.author_id or auth.caller_agent_id
    if message.role == "tool":
        return "system", message.author_id or message.metadata.tool_name or "tool"
    return "system", message.author_id or request.source_system


def _semantic_payload(
    message: Message, request: AddRequest, auth: AuthContext, author_id: str
) -> dict[str, Any]:
    metadata = message.metadata.model_dump(mode="json", exclude_none=True)
    metadata.pop("extensions", None)
    return {
        "role": message.role,
        "author_id": author_id,
        "content": message.content.replace("\r\n", "\n").replace("\r", "\n"),
        "occurred_at": message.occurred_at.astimezone(UTC).isoformat(),
        "turn_id": message.turn_id,
        "reply_to_message_id": message.reply_to_message_id,
        "metadata": metadata,
    }


def _source_values(
    message: Message, request: AddRequest, auth: AuthContext
) -> dict[str, Any]:
    author_type, author_id = _resolved_author(message, request, auth)
    logical_key = {
        "tenant_id": auth.tenant_id,
        "caller_agent_id": auth.caller_agent_id,
        "source_system": request.source_system,
        "user_id": request.user_id,
        "session_id": request.session_id,
        "message_id": message.message_id,
    }
    semantic_hash = _sha256(
        _canonical_json(_semantic_payload(message, request, auth, author_id))
    )
    metadata = message.metadata.model_dump(mode="json", exclude_none=True)
    metadata.update(
        {
            "role": message.role,
            "message_id": message.message_id,
            "turn_id": message.turn_id,
            "reply_to_message_id": message.reply_to_message_id,
            "original_occurred_at": message.occurred_at.isoformat(),
        }
    )
    return {
        "id": f"src_{uuid4().hex}",
        **logical_key,
        "source_type": "tool" if message.role == "tool" else "chat",
        "external_ref_id": _sha256(_canonical_json(logical_key)),
        "author_type": author_type,
        "author_id": author_id,
        "raw_content": message.content,
        "content_hash": _sha256(message.content),
        "semantic_hash": semantic_hash,
        "metadata_json": metadata,
        "occurred_at": message.occurred_at.astimezone(UTC),
    }


def _get_or_create_source(
    session: Session,
    *,
    message: Message,
    request: AddRequest,
    auth: AuthContext,
    correlation_id: str,
    task_id: str,
) -> tuple[SourceRecord, bool]:
    values = _source_values(message, request, auth)
    existing = session.scalars(
        select(SourceRecord).where(
            SourceRecord.tenant_id == auth.tenant_id,
            SourceRecord.caller_agent_id == auth.caller_agent_id,
            SourceRecord.source_system == request.source_system,
            SourceRecord.user_id == request.user_id,
            SourceRecord.session_id == request.session_id,
            SourceRecord.message_id == message.message_id,
        )
    ).one_or_none()
    if existing is not None:
        if existing.status != "active":
            raise SourceUnavailable(f"message {message.message_id} is no longer active")
        if existing.semantic_hash != values["semantic_hash"]:
            raise MessageIdConflict(
                f"message_id {message.message_id} is already bound to different content"
            )
        return existing, False

    source = SourceRecord(**values)
    session.add(source)
    session.flush()
    record_audit(
        session,
        tenant_id=auth.tenant_id,
        action="INSERT",
        target_type="source",
        target_id=source.id,
        operator_type="agent",
        operator_id=auth.caller_agent_id,
        reason_code="CONVERSATION_INGESTED",
        correlation_id=correlation_id,
        task_id=task_id,
        state_after={"message_id": message.message_id, "version": source.version,
                     "status": source.status},
    )
    return source, True


def _allocate_batch_seq(
    session: Session, *, tenant_id: str, caller_agent_id: str, source_system: str,
    session_id: str, user_id: str
) -> int:
    session.execute(
        pg_insert(ConversationSession)
        .values(
            tenant_id=tenant_id,
            caller_agent_id=caller_agent_id,
            source_system=source_system,
            session_id=session_id,
            user_id=user_id,
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "caller_agent_id", "source_system", "user_id",
                            "session_id"]
        )
    )
    conversation = session.scalars(
        select(ConversationSession)
        .where(
            ConversationSession.tenant_id == tenant_id,
            ConversationSession.caller_agent_id == caller_agent_id,
            ConversationSession.user_id == user_id,
            ConversationSession.source_system == source_system,
            ConversationSession.session_id == session_id,
        )
        .with_for_update()
    ).one()
    batch_seq = conversation.next_batch_seq
    conversation.next_batch_seq += 1
    return batch_seq


def ingest_and_enqueue(
    session: Session,
    *,
    auth: AuthContext,
    request: AddRequest,
    idempotency_key: str,
    request_id: str,
) -> IngestReceipt:
    fingerprint = request_fingerprint(request)
    lock_scope = _canonical_json(
        [auth.tenant_id, auth.caller_agent_id, request.user_id, idempotency_key]
    )
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": lock_scope},
    )
    existing_task = session.scalars(
        select(MemoryTask).where(
            MemoryTask.tenant_id == auth.tenant_id,
            MemoryTask.caller_agent_id == auth.caller_agent_id,
            MemoryTask.user_id == request.user_id,
            MemoryTask.idempotency_key == idempotency_key,
        )
    ).one_or_none()
    if existing_task is not None:
        if existing_task.request_fingerprint != fingerprint:
            raise IdempotencyConflict("Idempotency-Key is already bound to another request")
        return IngestReceipt(task=existing_task, replayed=True)

    batch_seq = _allocate_batch_seq(
        session,
        tenant_id=auth.tenant_id,
        caller_agent_id=auth.caller_agent_id,
        source_system=request.source_system,
        session_id=request.session_id,
        user_id=request.user_id,
    )
    task_id = f"tsk_{uuid4().hex}"
    correlation_id = uuid4().hex
    task = MemoryTask(
        id=task_id,
        tenant_id=auth.tenant_id,
        task_type="fact_extract",
        target_type="batch",
        target_id=f"batch_{uuid4().hex}",
        caller_agent_id=auth.caller_agent_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        correlation_id=correlation_id,
        source_system=request.source_system,
        user_id=request.user_id,
        session_id=request.session_id,
        batch_seq=batch_seq,
        payload={"config_version": 2, "request_id": request_id},
        max_attempts=5,
    )
    session.add(task)
    session.flush()

    for kind, messages in (("history", request.history_messages), ("target", request.messages)):
        for position, message in enumerate(messages):
            source, _ = _get_or_create_source(
                session,
                message=message,
                request=request,
                auth=auth,
                correlation_id=correlation_id,
                task_id=task.id,
            )
            session.add(
                MemoryTaskSource(
                    tenant_id=auth.tenant_id,
                    task_id=task.id,
                    source_id=source.id,
                    source_version=source.version,
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
        operator_type="agent",
        operator_id=auth.caller_agent_id,
        reason_code="FACT_EXTRACTION_ENQUEUED",
        correlation_id=correlation_id,
        task=task,
        state_after={"status": "pending", "batch_seq": batch_seq},
    )
    session.flush()
    return IngestReceipt(task=task, replayed=False)
