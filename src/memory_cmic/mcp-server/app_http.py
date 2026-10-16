"""GeneralMemory MCP Server · Streamable HTTP 版 —— 远程共享接入。

基于 app.py（stdio 版）改造：传输层从 stdio 换成 Streamable HTTP，
工具逻辑完全复用（SQLAlchemy 直连 + memory_cmic 库）。

启动：PYTHONPATH=./src python3 src/memory_cmic/mcp-server/app_http.py
默认监听 0.0.0.0:8000，MCP 端点 /mcp，REST API 通过 --port 8001 启动
的 memory-cmic-api 提供。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from memory_cmic.api_schemas import AddRequest, Message, MessageMetadata
from memory_cmic.auth import AuthContext, AuthenticationError
from memory_cmic.db import create_database_engine
from memory_cmic.ingest import ingest_and_enqueue
from memory_cmic.models import MemoryItem
from memory_cmic.profile import read_profiles
from memory_cmic.providers import ProviderError, QwenFactModel, SiliconFlowEmbedder
from memory_cmic.retrieval import search_task_memories
from memory_cmic.settings import Settings
from memory_cmic.task_results import to_task_response
from sqlalchemy import select

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── 配置加载（启动时一次性）──────────────────────────────────

_settings: Settings = Settings.from_env()
_engine = create_database_engine(_settings.database_url)
_session_factory = sessionmaker(_engine, expire_on_commit=False)

if not _settings.credentials._credentials:
    print("错误：MEMORY_AUTH_CREDENTIALS_JSON 至少需要一个凭证", file=sys.stderr)
    sys.exit(1)

# 凭证索引：原始 token → AuthContext（从原始 JSON 中提取 token 映射）
_TOKEN_MAP: dict[str, AuthContext] = {}
_raw_credentials: list = json.loads(os.environ.get("MEMORY_AUTH_CREDENTIALS_JSON", "[]"))
for item in _raw_credentials:
    token = item.get("token")
    if not token:
        continue
    # 通过 token_sha256 查找对应的 AuthContext
    for digest, ctx in _settings.credentials._credentials.items():
        if digest == hashlib.sha256(token.encode()).hexdigest():
            _TOKEN_MAP[token] = ctx
            break


# ── 请求级上下文 ────────────────────────────────────────────

@dataclass(frozen=True)
class _RequestContext:
    """每个 HTTP 请求的上下文。"""
    auth: AuthContext
    request_id: str


_request_ctx: ContextVar[_RequestContext | None] = ContextVar("gm_http_request", default=None)


def _get_request_ctx() -> _RequestContext:
    ctx = _request_ctx.get()
    if ctx is None:
        raise RuntimeError("request context not set")
    return ctx


# ── 运维 ────────────────────────────────────────────────────

def _get_embedder():
    if not _settings.siliconflow_api_key:
        raise _ToolError("SILICONFLOW_API_KEY 未配置")
    return SiliconFlowEmbedder(
        api_key=_settings.siliconflow_api_key,
        base_url=_settings.siliconflow_base_url,
        model_id=_settings.siliconflow_embedding_model,
    )


def _get_applicability_model():
    if not _settings.dashscope_api_key:
        raise _ToolError("DASHSCOPE_API_KEY 未配置")
    return QwenFactModel(
        api_key=_settings.dashscope_api_key,
        base_url=_settings.dashscope_base_url,
        model=_settings.dashscope_model,
        input_tokens=_settings.extraction_budget.input_tokens,
        output_tokens=_settings.extraction_budget.output_tokens,
    )


class _ToolError(Exception):
    pass


# ── MCP 服务器 ──────────────────────────────────────────────

mcp = FastMCP(
    "general-memory",
    instructions=(
        "GeneralMemory 记忆工具（远程版）：按用户维度保存对话消息、语义检索长期记忆、查看用户画像。"
        "save_memory 在对话产生值得记住的事实后调用；"
        "recall_memory 在需要用户背景/偏好/历史时先检索。"
    ),
    stateless_http=True,     # ← key change: Streamable HTTP
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ── 工具（逻辑与 app.py 一致）────────────────────────────────

@mcp.tool()
def search_memory(
    user_id: str,
    query: str,
    limit: int = 10,
    agent_id: str = "",
    source_system: str = "",
) -> list[dict[str, Any]]:
    if limit < 1:
        limit = 10
    limit = min(limit, 50)

    ctx = _get_request_ctx()
    try:
        embedder = _get_embedder()
        with embedder._client:
            model = _get_applicability_model()
            with model._client, _session_factory() as session:
                memories = search_task_memories(
                    session,
                    tenant_id=ctx.auth.tenant_id,
                    user_id=user_id,
                    query=query,
                    authorized_agent_ids=[],
                    company_ids=[],
                    limit=limit,
                    embedder=embedder,
                    applicability_model=model,
                    validate_user_evidence=True,
                    agent_id=agent_id or None,
                    source_system=source_system or None,
                )
                return [
                    {"id": m.id, "memory": m.content, "cognitive_type": m.cognitive_type}
                    for m in memories
                ]
    except ProviderError:
        raise _ToolError("记忆检索失败：LLM 或嵌入服务返回异常")
    except _ToolError:
        raise
    except Exception as exc:
        raise _ToolError(f"检索异常：{exc}")


@mcp.tool()
def save_memory(
    user_id: str,
    content: str,
    role: str = "user",
    source_system: str = "mcp-http",
    session_id: str = "",
) -> dict[str, Any]:
    if role not in ("user", "assistant"):
        raise _ToolError("role 必须为 'user' 或 'assistant'")

    ctx = _get_request_ctx()
    sid = session_id or f"mcp_{uuid4().hex[:12]}"
    now = datetime.now(UTC)

    message = Message(
        message_id=f"msg_{uuid4().hex}",
        role=role,
        content=content,
        occurred_at=now,
        author_id=user_id if role == "user" else ctx.auth.caller_agent_id,
        metadata=MessageMetadata(),
    )

    request = AddRequest(
        source_system=source_system,
        user_id=user_id,
        session_id=sid,
        messages=[message],
    )

    try:
        with _session_factory.begin() as session:
            receipt = ingest_and_enqueue(
                session, auth=ctx.auth, request=request,
                idempotency_key=f"mcp_{uuid4().hex}",
                request_id=ctx.request_id,
            )
        task = receipt.task
        response = to_task_response(task)
        return {
            "task_id": response.task_id,
            "status": response.status.value,
            "results": [r.model_dump() for r in (response.results or [])],
            "error": response.error.model_dump() if response.error else None,
        }
    except Exception as exc:
        raise _ToolError(f"保存失败：{exc}")


@mcp.tool()
def save_dialogue(
    user_id: str,
    user_says: str,
    assistant_says: str,
    source_system: str = "mcp-http",
    session_id: str = "",
) -> dict[str, Any]:
    ctx = _get_request_ctx()
    sid = session_id or f"mcp_{uuid4().hex[:12]}"
    now = datetime.now(UTC)

    messages = [
        Message(message_id=f"msg_{uuid4().hex}", role="user",
                content=user_says, occurred_at=now, author_id=user_id,
                metadata=MessageMetadata()),
        Message(message_id=f"msg_{uuid4().hex}", role="assistant",
                content=assistant_says, occurred_at=now,
                author_id=ctx.auth.caller_agent_id,
                metadata=MessageMetadata()),
    ]

    request = AddRequest(source_system=source_system, user_id=user_id,
                         session_id=sid, messages=messages)

    try:
        with _session_factory.begin() as session:
            receipt = ingest_and_enqueue(
                session, auth=ctx.auth, request=request,
                idempotency_key=f"mcp_{uuid4().hex}",
                request_id=ctx.request_id,
            )
        task = receipt.task
        response = to_task_response(task)
        return {
            "task_id": response.task_id,
            "status": response.status.value,
            "results": [r.model_dump() for r in (response.results or [])],
            "error": response.error.model_dump() if response.error else None,
        }
    except Exception as exc:
        raise _ToolError(f"保存失败：{exc}")


@mcp.tool()
def get_profile(user_id: str, include_inferred: bool = False) -> list[dict[str, Any]]:
    ctx = _get_request_ctx()
    try:
        with _session_factory() as session:
            profiles = read_profiles(
                session, tenant_id=ctx.auth.tenant_id,
                user_id=user_id, include_inferred=include_inferred,
            )
            return [
                {
                    "id": p.id, "assertion_kind": p.assertion_kind,
                    "property_value": p.property_value,
                    "value_type": p.value_type,
                    "business_domains": p.business_domains,
                }
                for p in profiles
            ]
    except Exception as exc:
        raise _ToolError(f"画像查询失败：{exc}")


@mcp.tool()
def list_memories(
    user_id: str, limit: int = 20,
    agent_id: str = "", source_system: str = "",
) -> list[dict[str, Any]]:
    if limit < 1:
        limit = 20
    limit = min(limit, 200)

    ctx = _get_request_ctx()
    try:
        with _session_factory() as session:
            stmt = (
                select(MemoryItem)
                .where(
                    MemoryItem.tenant_id == ctx.auth.tenant_id,
                    MemoryItem.subject_type == "user",
                    MemoryItem.subject_id == user_id,
                    MemoryItem.status == "active",
                )
                .order_by(MemoryItem.created_at.desc())
                .limit(limit)
            )
            if agent_id:
                stmt = stmt.where(MemoryItem.created_by == agent_id)
            memories = session.scalars(stmt).all()
            return [
                {
                    "id": m.id, "summary": m.summary,
                    "cognitive_type": m.cognitive_type,
                    "status": m.status,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in memories
            ]
    except Exception as exc:
        raise _ToolError(f"列举失败：{exc}")


@mcp.tool()
def list_tenants() -> dict[str, Any]:
    tenants = []
    for ctx in _credential_contexts.values():
        tenants.append({
            "tenant_id": ctx.tenant_id,
            "agent_id": ctx.caller_agent_id,
            "source_systems": sorted(ctx.allowed_source_systems),
        })
    return {"tenants": tenants, "token_count": len(tenants)}


# ── Bearer 鉴权中间件 ────────────────────────────────────────

class _BearerGate:
    """从 Authorization: Bearer <token> 解析 AuthContext，注入请求上下文。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get("x-request-id") or f"req_{uuid4().hex}"
        auth_header = headers.get("authorization", "")

        if not auth_header.startswith("Bearer "):
            await _json_error(send, 401, "UNAUTHENTICATED",
                              "缺少 Authorization: Bearer <token>", request_id)
            return

        token = auth_header[7:]
        auth = _TOKEN_MAP.get(token)
        if auth is None:
            await _json_error(send, 401, "UNAUTHENTICATED",
                              "无效的 Bearer Token", request_id)
            return

        token_ref = _request_ctx.set(_RequestContext(auth=auth, request_id=request_id))
        try:
            await self.app(scope, receive, send)
        finally:
            _request_ctx.reset(token_ref)


async def _json_error(send: Send, status: int, code: str, message: str, request_id: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send({
        "type": "http.response.start", "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"x-request-id", request_id.encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def create_http_app() -> _BearerGate:
    mcp_app = mcp.streamable_http_app()
    return _BearerGate(mcp_app)


# ── 入口 ─────────────────────────────────────────────────────

def main():
    import uvicorn
    port = int(os.environ.get("MCP_HTTP_PORT", "8001"))
    logger.info("GeneralMemory MCP (Streamable HTTP) 启动 → http://0.0.0.0:%d/mcp", port)
    uvicorn.run("memory_cmic.mcp-server.app_http:create_http_app",
                factory=True, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()