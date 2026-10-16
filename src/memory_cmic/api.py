from __future__ import annotations

import time
from collections import Counter
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session, object_session, sessionmaker

from memory_cmic.api_schemas import (
    AddRequest,
    ErrorBody,
    ErrorInfo,
    GovernancePolicyPatch,
    GovernanceRevertRequest,
    GovernanceRunRequest,
    ProfileResponse,
    ProfileResult,
    SearchRequest,
    SearchResponse,
    SearchResult,
    TaskResponse,
)
from memory_cmic.audit import list_audits, record_audit
from memory_cmic.auth import (
    AuthContext,
    AuthenticationError,
    AuthorizationError,
)
from memory_cmic.consolidation import RevertConflict, revert_operation
from memory_cmic.db import create_database_engine
from memory_cmic.governance_state import backfill_history, backfill_user, create_run, ensure_policy
from memory_cmic.ingest import IngestError, ingest_and_enqueue
from memory_cmic.models import GovernanceOperation, GovernancePolicy, MemoryItem, MemoryTask
from memory_cmic.profile import enqueue_profile_rebuild, read_profiles
from memory_cmic.providers import ProviderError, QwenFactModel, SiliconFlowEmbedder
from memory_cmic.retrieval import search_task_memories
from memory_cmic.settings import Settings
from memory_cmic.task_results import to_task_response


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error_response(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    body = ErrorBody(error=ErrorInfo(code=code, message=message))
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


def _load_task(
    session_factory: sessionmaker[Session], auth: AuthContext, task_id: str
) -> MemoryTask | None:
    with session_factory() as session:
        return session.scalars(
            select(MemoryTask).where(
                MemoryTask.id == task_id,
                MemoryTask.tenant_id == auth.tenant_id,
                MemoryTask.caller_agent_id == auth.caller_agent_id,
            )
        ).one_or_none()


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_engine = engine or create_database_engine(resolved_settings.database_url)
    session_factory = sessionmaker(resolved_engine, expire_on_commit=False)
    app = FastAPI(title="MemoryCMIC", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.engine = resolved_engine
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
        if len(request_id) > 128:
            request_id = f"req_{uuid4().hex}"
        request.state.request_id = request_id
        if request.method == "POST" and len(await request.body()) > 2 * 1024 * 1024:
            return _error_response(
                413, "PAYLOAD_TOO_LARGE", "request body exceeds 2 MiB", request_id
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return _error_response(
            exc.status_code, exc.code, exc.message, request.state.request_id
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {"msg": "invalid request"}
        if first.get("type") == "json_invalid":
            return _error_response(
                400, "INVALID_JSON", "request body is not valid JSON", request.state.request_id
            )
        return _error_response(
            422,
            "INVALID_MESSAGE",
            str(first.get("msg", "invalid request")),
            request.state.request_id,
        )

    def authenticate(authorization: str | None = Header(default=None)) -> AuthContext:
        try:
            return resolved_settings.credentials.authenticate(authorization)
        except AuthenticationError as exc:
            raise ApiError(401, "UNAUTHENTICATED", str(exc)) from exc

    def manage_governance(auth: AuthContext = Depends(authenticate)) -> AuthContext:
        if not auth.can_manage_governance:
            raise ApiError(403, "FORBIDDEN", "governance management permission is required")
        return auth

    def read_user_memory(auth: AuthContext = Depends(authenticate)) -> AuthContext:
        if not auth.allowed_source_systems:
            raise ApiError(403, "FORBIDDEN", "an Agent service credential is required")
        return auth

    @app.post("/api/v1/memories:search", response_model=SearchResponse)
    def search_user_memories(
        body: SearchRequest, auth: AuthContext = Depends(read_user_memory),
    ) -> SearchResponse:
        if not resolved_settings.dashscope_api_key or not resolved_settings.siliconflow_api_key:
            raise ApiError(503, "SEARCH_UNAVAILABLE", "search providers are not configured")
        try:
            embedder = SiliconFlowEmbedder(
                api_key=resolved_settings.siliconflow_api_key,
                base_url=resolved_settings.siliconflow_base_url,
                model_id=resolved_settings.siliconflow_embedding_model,
            )
            with embedder._client:
                model = QwenFactModel(
                    api_key=resolved_settings.dashscope_api_key,
                    base_url=resolved_settings.dashscope_base_url,
                    model=resolved_settings.dashscope_model,
                    input_tokens=resolved_settings.extraction_budget.input_tokens,
                    output_tokens=resolved_settings.extraction_budget.output_tokens,
                )
                with model._client, session_factory.begin() as session:
                    memories = search_task_memories(
                        session, tenant_id=auth.tenant_id, user_id=body.user_id, query=body.query,
                        authorized_agent_ids=[], company_ids=[], limit=body.limit,
                        embedder=embedder, applicability_model=model, validate_user_evidence=True,
                        agent_id=body.agent_id, source_system=body.source_system,
                    )
                    return SearchResponse(results=[
                        SearchResult(id=m.id, memory=m.content, cognitive_type=m.cognitive_type)
                        for m in memories
                    ])
        except ProviderError as exc:
            raise ApiError(
                502, "SEARCH_FAILED", "search provider request or output failed"
            ) from exc

    @app.get("/api/v1/profiles", response_model=ProfileResponse)
    def get_profiles(
        user_id: str = Query(min_length=1, max_length=128),
        include_inferred: bool = False,
        auth: AuthContext = Depends(read_user_memory),
    ) -> ProfileResponse:
        with session_factory() as session:
            return ProfileResponse(profiles=[
                ProfileResult(
                    id=p.id, property_key=p.property_key, property_value=p.property_value,
                    value_type=p.value_type, business_domains=p.business_domains,
                    assertion_kind=p.assertion_kind,
                )
                for p in read_profiles(session, tenant_id=auth.tenant_id, user_id=user_id,
                                       include_inferred=include_inferred)
            ])

    @app.post("/api/v1/profiles/rebuild", status_code=202)
    def rebuild_profiles(
        body: GovernanceRunRequest, auth: AuthContext = Depends(manage_governance),
    ) -> dict:
        with session_factory.begin() as session:
            task = enqueue_profile_rebuild(
                session, tenant_id=auth.tenant_id, user_id=body.user_id, force=True,
                allow_inferred=resolved_settings.profile_allow_inferred,
            )
            return {"task_id": task.id, "status": task.status, "available_at": task.available_at}

    @app.get("/api/v1/audit-logs")
    def get_audit_logs(
        target_type: str | None = None, target_id: str | None = None,
        correlation_id: str | None = None, task_id: str | None = None,
        reason_code: str | None = None, include_technical: bool = False,
        include_details: bool = False, limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        auth: AuthContext = Depends(manage_governance),
    ) -> dict:
        if target_id is not None and target_type is None:
            raise ApiError(422, "INVALID_AUDIT_FILTER", "target_id requires target_type")
        if target_type is not None and target_type not in {
            "source", "memory", "evidence", "profile", "embedding", "task", "governance_policy"
        }:
            raise ApiError(422, "INVALID_AUDIT_FILTER", "invalid target_type")
        if include_details and not auth.can_read_audit_content:
            raise ApiError(403, "FORBIDDEN", "audit content permission is required")
        with session_factory() as session:
            return list_audits(
                session, tenant_id=auth.tenant_id, target_type=target_type,
                target_id=target_id, correlation_id=correlation_id, task_id=task_id,
                reason_code=reason_code, include_technical=include_technical,
                include_details=include_details, can_read_content=auth.can_read_audit_content,
                limit=limit, offset=offset,
            )

    def policy_body(policy: GovernancePolicy) -> dict:
        return {
            "auto_enabled": policy.auto_enabled,
            "generate_inferences": policy.generate_inferences,
            "change_threshold": policy.change_threshold,
            "idle_minutes": policy.idle_minutes,
            "cooldown_minutes": policy.cooldown_minutes,
            "max_wait_minutes": policy.max_wait_minutes,
            "max_memories": policy.max_memories,
            "max_model_calls": policy.max_model_calls,
            "version": policy.version,
        }

    def run_body(task: MemoryTask, can_read_content: bool = False) -> dict:
        execution = task.payload or {}
        session = object_session(task)
        associated = session.scalars(select(MemoryTask).where(
            MemoryTask.tenant_id == task.tenant_id,
            MemoryTask.task_type.in_(["profile_rebuild", "vector_upsert", "vector_delete",
                                     "dependency_recheck", "ttl_expire"]),
            or_(MemoryTask.correlation_id == task.correlation_id,
                MemoryTask.payload["trigger_correlation_ids"].contains([task.correlation_id])),
        ).order_by(MemoryTask.created_at, MemoryTask.id)).all() if task.correlation_id else []
        unresolved = session.scalars(select(MemoryItem.conflict_group_id).where(
            MemoryItem.tenant_id == task.tenant_id, MemoryItem.subject_type == "user",
            MemoryItem.subject_id == task.user_id, MemoryItem.status == "disputed",
            MemoryItem.conflict_group_id.is_not(None),
        ).distinct()).all()
        results = [({**item, "reason": None, "question": None}
                    if item.get("kind") == "conflict_review" and not can_read_content else item)
                   for item in task.result_json or []]
        return {
            "task_id": task.id, "user_id": task.user_id,
            "status": task.status, "automatic": execution.get("automatic", False),
            "trigger_reason": execution.get("trigger_reason"),
            "policy_version": execution.get("policy_version"),
            "processed": execution.get("cursor", 0),
            "selected": len(execution.get("selection", [])),
            "input_memory_ids": [row[0] for row in execution.get("selection", [])],
            "remaining": execution.get("remaining"),
            "model_calls": execution.get("model_calls", 0),
            "token_usage": execution.get("token_usage"),
            "results": results, "error": task.error_json,
            "result_counts": dict(Counter(item["kind"] for item in task.result_json or [])),
            "unresolved_conflicts": len(unresolved),
            "deferred_batches": [item for item in task.result_json or []
                                 if item.get("reason") == "INPUT_CHANGED"],
            "associated_tasks": [{"task_id": linked.id, "task_type": linked.task_type,
                                  "status": linked.status, "error": linked.error_json,
                                  "results": linked.result_json
                                  if linked.status in {"succeeded", "partial"} else None,
                                  "available_at": linked.available_at}
                                 for linked in associated],
            "created_at": task.created_at, "started_at": task.started_at,
            "completed_at": task.completed_at,
        }

    def conflict_body(
        session: Session, tenant_id: str, group_id: str, can_read_content: bool,
    ) -> dict:
        members = session.scalars(select(MemoryItem).where(
            MemoryItem.tenant_id == tenant_id, MemoryItem.conflict_group_id == group_id,
        ).order_by(MemoryItem.id)).all()
        if not members:
            raise ApiError(404, "CONFLICT_NOT_FOUND", "conflict group does not exist")
        review = session.scalars(select(GovernanceOperation).where(
            GovernanceOperation.tenant_id == tenant_id,
            GovernanceOperation.kind == "conflict_review",
            GovernanceOperation.details["conflict_group_id"].as_string() == group_id,
        ).order_by(GovernanceOperation.created_at.desc()).limit(1)).first()
        return {"conflict_group_id": group_id, "user_id": members[0].subject_id,
                "unresolved": any(m.status == "disputed" for m in members),
                "members": [{"id": m.id, "content": m.content if can_read_content else None,
                             "status": m.status,
                             "version": m.version, "business_domains": m.business_domains,
                             "effective_at": m.effective_at, "expired_at": m.expired_at}
                            for m in members],
                "last_review": {"operation_id": review.id,
                                "outcome": review.details.get("outcome"),
                                "reason": (review.details.get("reason")
                                           if can_read_content else None),
                                "question": (review.details.get("question")
                                             if can_read_content else None),
                                "reviewed_at": review.created_at} if review else None}

    @app.get("/api/v1/governance/conflicts")
    def list_conflicts(
        user_id: str | None = None, limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0), auth: AuthContext = Depends(manage_governance),
    ) -> dict:
        with session_factory() as session:
            query = select(MemoryItem.conflict_group_id).where(
                MemoryItem.tenant_id == auth.tenant_id, MemoryItem.subject_type == "user",
                MemoryItem.status == "disputed", MemoryItem.conflict_group_id.is_not(None),
            )
            if user_id is not None:
                query = query.where(MemoryItem.subject_id == user_id)
            groups = session.scalars(query.distinct().order_by(MemoryItem.conflict_group_id)
                                     .offset(offset).limit(limit)).all()
            return {"conflicts": [conflict_body(session, auth.tenant_id, group,
                                               auth.can_read_audit_content)
                                  for group in groups]}

    @app.get("/api/v1/governance/conflicts/{group_id}")
    def get_conflict(group_id: str, auth: AuthContext = Depends(manage_governance)) -> dict:
        with session_factory() as session:
            return conflict_body(session, auth.tenant_id, group_id, auth.can_read_audit_content)

    @app.get("/api/v1/governance/policy")
    def get_governance_policy(auth: AuthContext = Depends(manage_governance)) -> dict:
        with session_factory.begin() as session:
            return policy_body(ensure_policy(session, auth.tenant_id))

    @app.patch("/api/v1/governance/policy")
    def patch_governance_policy(
        body: GovernancePolicyPatch, auth: AuthContext = Depends(manage_governance)
    ) -> dict:
        changes = body.model_dump(exclude_unset=True)
        with session_factory.begin() as session:
            policy = ensure_policy(session, auth.tenant_id)
            session.refresh(policy, with_for_update=True)
            before = policy_body(policy)
            if changes:
                for field, value in changes.items():
                    setattr(policy, field, value)
                policy.version += 1
                if changes.get("auto_enabled") and not before["auto_enabled"]:
                    backfill_history(session, auth.tenant_id)
                record_audit(
                    session,
                    tenant_id=auth.tenant_id,
                    action="UPDATE",
                    target_type="governance_policy",
                    target_id=auth.tenant_id,
                    operator_type="admin",
                    operator_id=auth.caller_agent_id,
                    reason_code="GOVERNANCE_POLICY_CHANGED",
                    correlation_id=uuid4().hex,
                    state_before=before,
                    state_after=policy_body(policy),
                )
            return policy_body(policy)

    @app.post("/api/v1/governance/runs", status_code=202)
    def trigger_governance_run(
        body: GovernanceRunRequest, auth: AuthContext = Depends(manage_governance),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        if idempotency_key is not None and (
            len(idempotency_key) > 128 or not idempotency_key.isascii()
        ):
            raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "invalid Idempotency-Key")
        with session_factory.begin() as session:
            task_key = (
                f"consolidate:{body.user_id}:{idempotency_key}" if idempotency_key else None
            )
            if task_key is not None:
                previous = session.scalars(select(MemoryTask).where(
                    MemoryTask.tenant_id == auth.tenant_id,
                    MemoryTask.caller_agent_id == auth.caller_agent_id,
                    MemoryTask.idempotency_key == task_key,
                )).one_or_none()
                if previous is not None:
                    return run_body(previous, auth.can_read_audit_content)
            backfill_user(session, auth.tenant_id, body.user_id)
            task = create_run(
                session, tenant_id=auth.tenant_id, user_id=body.user_id,
                operator_id=auth.caller_agent_id,
                idempotency_key=task_key,
            )
            return run_body(task, auth.can_read_audit_content)

    @app.get("/api/v1/governance/runs")
    def list_governance_runs(
        user_id: str | None = None, status: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        auth: AuthContext = Depends(manage_governance),
    ) -> dict:
        with session_factory() as session:
            query = select(MemoryTask).where(
                MemoryTask.tenant_id == auth.tenant_id, MemoryTask.task_type == "consolidate"
            )
            if user_id is not None:
                query = query.where(MemoryTask.user_id == user_id)
            if status is not None:
                query = query.where(MemoryTask.status == status)
            tasks = session.scalars(
                query.order_by(MemoryTask.created_at.desc(), MemoryTask.id.desc())
                .offset(offset).limit(limit)
            ).all()
            return {"runs": [run_body(task, auth.can_read_audit_content) for task in tasks]}

    def get_managed_run(session: Session, tenant_id: str, run_id: str) -> MemoryTask:
        task = session.get(MemoryTask, run_id)
        if task is None or task.tenant_id != tenant_id or task.task_type != "consolidate":
            raise ApiError(404, "TASK_NOT_FOUND", "governance run does not exist")
        return task

    @app.get("/api/v1/governance/runs/{run_id}")
    def get_governance_run(
        run_id: str, auth: AuthContext = Depends(manage_governance)
    ) -> dict:
        with session_factory() as session:
            return run_body(get_managed_run(session, auth.tenant_id, run_id),
                            auth.can_read_audit_content)

    @app.get("/api/v1/governance/runs/{run_id}/operations")
    def list_governance_operations(
        run_id: str, limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        auth: AuthContext = Depends(manage_governance),
    ) -> dict:
        with session_factory() as session:
            get_managed_run(session, auth.tenant_id, run_id)
            operations = session.scalars(select(GovernanceOperation).where(
                GovernanceOperation.tenant_id == auth.tenant_id,
                GovernanceOperation.task_id == run_id,
            ).order_by(GovernanceOperation.created_at, GovernanceOperation.id)
            .offset(offset).limit(limit)).all()
            return {"operations": [
                {"id": op.id, "kind": op.kind, "memory_id": op.memory_id,
                 "details": op.details if auth.can_read_audit_content else None,
                 "created_at": op.created_at,
                 "reverted_at": op.reverted_at}
                for op in operations
            ]}

    @app.post("/api/v1/governance/operations/{operation_id}:revert")
    def revert_governance_operation(
        operation_id: str, body: GovernanceRevertRequest,
        auth: AuthContext = Depends(manage_governance),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        if idempotency_key is not None and (
            len(idempotency_key) > 128 or not idempotency_key.isascii()
        ):
            raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "invalid Idempotency-Key")
        with session_factory.begin() as session:
            operation = session.scalars(select(GovernanceOperation).where(
                GovernanceOperation.tenant_id == auth.tenant_id,
                GovernanceOperation.id == operation_id,
            ).with_for_update()).one_or_none()
            if operation is None:
                raise ApiError(404, "OPERATION_NOT_FOUND", "governance operation does not exist")
            if operation.reverted_at is not None and idempotency_key:
                previous = session.scalars(select(GovernanceOperation).where(
                    GovernanceOperation.tenant_id == auth.tenant_id,
                    GovernanceOperation.kind == "rejection",
                    GovernanceOperation.details["reverted_operation_id"].as_string()
                    == operation_id,
                    GovernanceOperation.details["idempotency_key"].as_string() == idempotency_key,
                )).first()
                if previous is not None:
                    return {"operation_id": previous.id, "reverted_operation_id": operation.id}
            try:
                feedback = revert_operation(
                    session, operation=operation, expected_version=body.expected_version,
                    operator_id=auth.caller_agent_id, reason=body.reason,
                    model_id=resolved_settings.siliconflow_embedding_model,
                    idempotency_key=idempotency_key,
                )
            except RevertConflict as exc:
                raise ApiError(409, "REVERT_CONFLICT", str(exc)) from exc
            return {"operation_id": feedback.id, "reverted_operation_id": operation.id}

    @app.post(
        "/api/v1/memories:add",
        response_model=TaskResponse,
        responses={400: {"model": ErrorBody}, 409: {"model": ErrorBody}},
    )
    def add_memories(
        body: AddRequest,
        request: Request,
        response: Response,
        auth: AuthContext = Depends(authenticate),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TaskResponse:
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or not idempotency_key.isascii()
        ):
            raise ApiError(
                422,
                "INVALID_MESSAGE",
                "Idempotency-Key must contain 1-128 ASCII characters",
            )
        try:
            resolved_settings.credentials.authorize_source(auth, body.source_system)
        except AuthorizationError as exc:
            raise ApiError(403, "FORBIDDEN", str(exc)) from exc

        try:
            with session_factory.begin() as session:
                receipt = ingest_and_enqueue(
                    session,
                    auth=auth,
                    request=body,
                    idempotency_key=idempotency_key,
                    request_id=request.state.request_id,
                )
        except IngestError as exc:
            status_code = 409 if exc.code.endswith("CONFLICT") else 422
            raise ApiError(status_code, exc.code, str(exc)) from exc

        task = receipt.task
        if body.wait_for_result and task.status in {"pending", "processing"}:
            deadline = time.monotonic() + (body.wait_timeout_ms or 10_000) / 1000
            while time.monotonic() < deadline:
                time.sleep(0.1)
                refreshed = _load_task(session_factory, auth, task.id)
                if refreshed is not None:
                    task = refreshed
                if task.status not in {"pending", "processing"}:
                    break

        task_response = to_task_response(task)
        response.status_code = 202 if task_response.status == "pending" else 200
        return task_response

    @app.get(
        "/api/v1/tasks/{task_id}",
        response_model=TaskResponse,
        responses={404: {"model": ErrorBody}},
    )
    def get_task(task_id: str, auth: AuthContext = Depends(authenticate)) -> TaskResponse:
        if not 1 <= len(task_id) <= 128:
            raise ApiError(404, "TASK_NOT_FOUND", "task does not exist")
        task = _load_task(session_factory, auth, task_id)
        if task is None:
            raise ApiError(404, "TASK_NOT_FOUND", "task does not exist")
        return to_task_response(task)

    return app
