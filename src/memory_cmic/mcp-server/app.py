"""GeneralMemory MCP Server —— 单机版记忆服务，支持 AgentScope 接入。

通过 stdio 协议提供 MCP 工具，AgentScope agent 可直接调用。
直接连接数据库，使用 memory_cmic 库的全部能力，无需额外部署 API 服务。

配置（全部通过环境变量）：
  MEMORY_DATABASE_URL          数据库连接串（必填）
  MEMORY_AUTH_CREDENTIALS_JSON 凭证 JSON 数组，至少一个（必填）
  DASHSCOPE_API_KEY            DashScope LLM API Key（搜索/提取需要）
  SILICONFLOW_API_KEY          SiliconFlow 嵌入 API Key（搜索需要）
  MEMORY_DUPLICATE_CANDIDATE_THRESHOLD  去重阈值，默认 0.85

AgentScope 接入示例：
  {
    "mcpServers": {
      "general-memory": {
        "command": "python3",
        "args": ["-m", "memory_cmic.mcp-server.app"],
        "env": {
          "MEMORY_DATABASE_URL": "postgresql+psycopg://...",
          "MEMORY_AUTH_CREDENTIALS_JSON": "[{\"token\":\"...\",\"tenant_id\":\"...\",...}]",
          "DASHSCOPE_API_KEY": "...",
          "SILICONFLOW_API_KEY": "..."
        }
      }
    }
  }
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from memory_cmic.api_schemas import AddRequest, Message, MessageMetadata
from memory_cmic.auth import AuthContext
from memory_cmic.db import create_database_engine
from memory_cmic.ingest import ingest_and_enqueue
from memory_cmic.profile import read_profiles
from memory_cmic.providers import ProviderError, QwenFactModel, SiliconFlowEmbedder
from memory_cmic.retrieval import search_task_memories
from memory_cmic.settings import Settings
from memory_cmic.task_results import to_task_response

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── 配置加载 ──────────────────────────────────────────────

_settings: Settings = Settings.from_env()
_engine = create_database_engine(_settings.database_url)
_session_factory = sessionmaker(_engine, expire_on_commit=False)

# 从凭证存储中提取所有可用身份
_credential_contexts: dict[str, AuthContext] = dict(_settings.credentials._credentials)
if not _credential_contexts:
    print("错误：MEMORY_AUTH_CREDENTIALS_JSON 至少需要一个凭证", file=sys.stderr)
    sys.exit(1)

# 选择默认凭证：优先选带 source_system 授权的，其次选第一个
_default_digest: str = ""
for digest, ctx in _credential_contexts.items():
    if ctx.allowed_source_systems:
        _default_digest = digest
        break
if not _default_digest:
    _default_digest = next(iter(_credential_contexts))

logger.info("MCP server 已加载 %d 个凭证，默认 tenant=%s agent=%s",
            len(_credential_contexts),
            _credential_contexts[_default_digest].tenant_id,
            _credential_contexts[_default_digest].caller_agent_id)


def _resolve_auth(source_system: str = "mcp-server") -> tuple[AuthContext, str]:
    """解析认证上下文和有效的 source_system。

    遍历凭证列表，选择第一个允许该 source_system 的凭证。
    无匹配时回退到默认凭证，source_system 也切换为该凭证允许的第一个值。
    返回 (AuthContext, 实际可用的 source_system)。
    """
    for ctx in _credential_contexts.values():
        if source_system in ctx.allowed_source_systems:
            return ctx, source_system
    # 回退：取默认凭证及其第一个允许的 source_system
    default_ctx = _credential_contexts[_default_digest]
    fallback_source = next(iter(default_ctx.allowed_source_systems), "mcp-server")
    return default_ctx, fallback_source


def _get_embedder():
    """创建嵌入器，检查 API Key 是否已配置。"""
    if not _settings.siliconflow_api_key:
        raise _ToolError(
            "SILICONFLOW_API_KEY 未配置，无法生成向量嵌入。请设置环境变量后重试。"
        )
    return SiliconFlowEmbedder(
        api_key=_settings.siliconflow_api_key,
        base_url=_settings.siliconflow_base_url,
        model_id=_settings.siliconflow_embedding_model,
    )


def _get_applicability_model():
    """创建 LLM 适用性模型，检查 API Key 是否已配置。"""
    if not _settings.dashscope_api_key:
        raise _ToolError(
            "DASHSCOPE_API_KEY 未配置，无法调用 LLM。请设置环境变量后重试。"
        )
    return QwenFactModel(
        api_key=_settings.dashscope_api_key,
        base_url=_settings.dashscope_base_url,
        model=_settings.dashscope_model,
        input_tokens=_settings.extraction_budget.input_tokens,
        output_tokens=_settings.extraction_budget.output_tokens,
    )


class _ToolError(Exception):
    """工具层业务错误，消息会直接展示给调用方。"""
    pass


# ── MCP 服务器 ────────────────────────────────────────────

mcp = FastMCP(
    "general-memory",
    instructions=(
        "GeneralMemory 记忆工具：按用户维度保存对话消息、语义检索长期记忆、查看用户画像。"
        "save_memory 在对话产生值得记住的事实后调用，平台自动提取和去重；"
        "recall_memory 在需要用户背景/偏好/历史时先检索，结果注入上下文。"
    ),
)


@mcp.tool()
def search_memory(
    user_id: str,
    query: str,
    limit: int = 10,
    agent_id: str = "",
    source_system: str = "",
) -> list[dict[str, Any]]:
    """语义检索用户记忆。在需要了解用户背景、偏好、历史决策时调用。

    参数：
    - user_id: 用户标识（必填）
    - query: 自然语言查询（必填），支持换一种说法的语义匹配
    - limit: 返回条数上限，默认 10，最大 50
    - agent_id: 按写入 agent 过滤（可选）
    - source_system: 按来源系统过滤（可选）

    返回：[{id, memory, cognitive_type}, ...] 按语义相关性排序。
    """
    if limit < 1:
        limit = 10
    limit = min(limit, 50)

    auth, _ = _resolve_auth()
    try:
        embedder = _get_embedder()
        with embedder._client:
            model = _get_applicability_model()
            with model._client, _session_factory() as session:
                memories = search_task_memories(
                    session,
                    tenant_id=auth.tenant_id,
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
                    {
                        "id": m.id,
                        "memory": m.content,
                        "cognitive_type": m.cognitive_type,
                    }
                    for m in memories
                ]
    except ProviderError:
        raise _ToolError("记忆检索失败：LLM 或嵌入服务返回异常，请检查 API Key 和网络。")
    except _ToolError:
        raise
    except Exception as exc:
        raise _ToolError(f"检索异常：{exc}")


@mcp.tool()
def save_memory(
    user_id: str,
    content: str,
    role: str = "user",
    source_system: str = "mcp-server",
    session_id: str = "",
) -> dict[str, Any]:
    """保存一条对话消息到记忆系统，平台自动提取事实并去重。

    在对话中产生值得长期记住的信息后调用（如用户自述偏好、决策、项目背景）。
    单条消息也会进入异步提取管线，由 Worker 完成事实抽取。

    参数：
    - user_id: 用户标识（必填）
    - content: 消息内容（必填），用户原话或助手回答
    - role: 消息角色，"user" 或 "assistant"，默认 "user"
    - source_system: 来源系统标识，默认 "mcp-server"
    - session_id: 会话标识（可选），留空自动生成

    返回：{task_id, status, results, error}
    """
    if role not in ("user", "assistant"):
        raise _ToolError("role 必须为 'user' 或 'assistant'")

    auth, resolved_source = _resolve_auth(source_system)
    sid = session_id or f"mcp_{uuid4().hex[:12]}"
    now = datetime.now(UTC)

    message = Message(
        message_id=f"msg_{uuid4().hex}",
        role=role,
        content=content,
        occurred_at=now,
        author_id=user_id if role == "user" else auth.caller_agent_id,
        metadata=MessageMetadata(),
    )

    request = AddRequest(
        source_system=resolved_source,
        user_id=user_id,
        session_id=sid,
        messages=[message],
    )

    idempotency_key = f"mcp_{uuid4().hex}"

    try:
        with _session_factory.begin() as session:
            receipt = ingest_and_enqueue(
                session,
                auth=auth,
                request=request,
                idempotency_key=idempotency_key,
                request_id=f"req_{uuid4().hex}",
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
    source_system: str = "mcp-server",
    session_id: str = "",
) -> dict[str, Any]:
    """保存一轮完整对话（用户消息 + 助手回答），平台自动提取值得记住的事实。

    比 save_memory 更适合需要完整上下文的事实提取场景。

    参数：
    - user_id: 用户标识（必填）
    - user_says: 用户原话（必填）
    - assistant_says: 助手回答（必填）
    - source_system: 来源系统标识，默认 "mcp-server"
    - session_id: 会话标识（可选），留空自动生成

    返回：{task_id, status, results, error}
    """
    auth, resolved_source = _resolve_auth(source_system)
    sid = session_id or f"mcp_{uuid4().hex[:12]}"
    now = datetime.now(UTC)

    messages = [
        Message(
            message_id=f"msg_{uuid4().hex}",
            role="user",
            content=user_says,
            occurred_at=now,
            author_id=user_id,
            metadata=MessageMetadata(),
        ),
        Message(
            message_id=f"msg_{uuid4().hex}",
            role="assistant",
            content=assistant_says,
            occurred_at=now,
            author_id=auth.caller_agent_id,
            metadata=MessageMetadata(),
        ),
    ]

    request = AddRequest(
        source_system=resolved_source,
        user_id=user_id,
        session_id=sid,
        messages=messages,
    )

    idempotency_key = f"mcp_{uuid4().hex}"

    try:
        with _session_factory.begin() as session:
            receipt = ingest_and_enqueue(
                session,
                auth=auth,
                request=request,
                idempotency_key=idempotency_key,
                request_id=f"req_{uuid4().hex}",
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
    """获取用户画像。返回已提取的稳定用户属性（身份、偏好、习惯等）。

    参数：
    - user_id: 用户标识（必填）
    - include_inferred: 是否包含推断属性，默认 false（仅返回显式属性）

    返回：[{id, assertion_kind, property_value, value_type, business_domains}, ...]
    """
    auth, _ = _resolve_auth()
    try:
        with _session_factory() as session:
            profiles = read_profiles(
                session,
                tenant_id=auth.tenant_id,
                user_id=user_id,
                include_inferred=include_inferred,
            )
            return [
                {
                    "id": p.id,
                    "assertion_kind": p.assertion_kind,
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
    user_id: str,
    limit: int = 20,
    agent_id: str = "",
    source_system: str = "",
) -> list[dict[str, Any]]:
    """列出用户最近记忆（非语义检索，按时间排序）。

    参数：
    - user_id: 用户标识（必填）
    - limit: 返回条数上限，默认 20，最大 200
    - agent_id: 按写入 agent 过滤（可选）
    - source_system: 按来源系统过滤（可选）

    返回：[{id, summary, cognitive_type, status, created_at}, ...]
    """
    if limit < 1:
        limit = 20
    limit = min(limit, 200)

    auth, _ = _resolve_auth()

    from memory_cmic.models import MemoryItem

    try:
        with _session_factory() as session:
            stmt = (
                select(MemoryItem)
                .where(
                    MemoryItem.tenant_id == auth.tenant_id,
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
                    "id": m.id,
                    "summary": m.summary,
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
    """列出已配置的租户和 Agent 信息（不返回密钥明文）。

    返回：{tenants: [{tenant_id, agent_id, source_systems}], default_tenant, default_agent}
    """
    tenants = []
    for ctx in _credential_contexts.values():
        tenants.append({
            "tenant_id": ctx.tenant_id,
            "agent_id": ctx.caller_agent_id,
            "source_systems": sorted(ctx.allowed_source_systems),
        })
    default = _credential_contexts[_default_digest]
    return {
        "tenants": tenants,
        "default_tenant": default.tenant_id,
        "default_agent": default.caller_agent_id,
    }


def main():
    """MCP 服务器入口。通过 stdio 运行。"""
    mcp.run()


if __name__ == "__main__":
    main()