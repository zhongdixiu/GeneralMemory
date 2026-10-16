# GeneralMemory MCP Server

将 GeneralMemory 记忆引擎通过标准 MCP 协议暴露给各类智能体（Hermes、AgentScope、Claude Desktop、Cursor 等）。


## 架构

```
┌────────────────────┐
│ Agent (MCP Client) │
└────────┬───────────┘
         │ stdio 或 HTTP
         ▼
┌────────────────────┐
│   MCP Server       │  ← 本目录
│   app.py / app_http│
└────────┬───────────┘
         │ SQLAlchemy 直连
         ▼
┌────────────────────┐     ┌──────────────────┐
│ PostgreSQL+pgvector│     │ DashScope +       │
│ (Docker :5433)    │     │ SiliconFlow (API) │
└────────────────────┘     └──────────────────┘
```

MCP Server 直连数据库，不经 HTTP 中间层，与 memory-platform 的 Gateway 模式不同。

---



## 文件存放位置

将mcp-server文件夹放在GeneralMemory-2.5.0/src/memory_cmic/路径下，并更新pyproject.toml文件（里面新增了mcp库）

## 前置条件

1. **PostgreSQL 已启动**（`docker compose up -d`）
2. **数据库已迁移**（`poetry run alembic upgrade head`）
3. **环境变量已配置**

```bash
cd /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0
cp .env.example .env   # 编辑后加载
set -a && source .env && set +a
```

必填环境变量：

| 变量                             | 说明                                |
| -------------------------------- | ----------------------------------- |
| `MEMORY_DATABASE_URL`          | PostgreSQL 连接串                   |
| `MEMORY_AUTH_CREDENTIALS_JSON` | 凭证 JSON 数组，至少一个            |
| `DASHSCOPE_API_KEY`            | 阿里云百炼 API Key（提取/搜索需要） |
| `SILICONFLOW_API_KEY`          | 硅基流动 API Key（向量嵌入需要）    |
| `PYTHONPATH`                   | 必须指向`src/` 目录               |

.env配置示例：

数据库

MEMORY_DATABASE_URL=postgresql+psycopg://memory_cmic:your-password@localhost:5433/memory_cmic

鉴权（按你的示例）

MEMORY_AUTH_CREDENTIALS_JSON='[{"token":"test-token","tenant_id":"dev","caller_agent_id":"test_agent","allowed_source_systems":["test","mcp-server","hermes","mcp-http"]}]'

LLM（阿里云百炼）

DASHSCOPE_API_KEY=sk-xxx
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen3.8-max

嵌入（硅基流动）

SILICONFLOW_API_KEY=sk-xxx
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_EMBEDDING_MODEL=BAAI/bge-m3

端口（默认 8001）

MCP_HTTP_PORT=8001

---

## 两种接入模式

提供两种传输方式，按场景选择：

| 维度                    | stdio 模式 (`app.py`)                       | HTTP 模式 (`app_http.py`)                        |
| ----------------------- | --------------------------------------------- | -------------------------------------------------- |
| **传输协议**      | stdio（标准输入输出）                         | Streamable HTTP                                    |
| **部署方式**      | Agent 客户端 fork 子进程                      | 独立常驻 HTTP 服务                                 |
| **网络要求**      | 本地，无需端口                                | 需要可访问的端口                                   |
| **鉴权方式**      | 环境变量自动选凭证                            | 请求头`Authorization: Bearer <token>`            |
| **多 Agent 共享** | 各启各的进程，互不影响                        | 共用一个服务端                                     |
| **适用场景**      | 个人本地 Agent、开发调试                      | 团队共享、远程 Agent、容器化部署                   |
| **启动命令**      | `python3 src/memory_cmic/mcp-server/app.py` | `python3 src/memory_cmic/mcp-server/app_http.py` |

两者完全独立，可以只启动一个，也可以同时启动。

### 选择指南

```
你有多个 Agent 要共享？ ──是──→ 用 app_http.py (HTTP)
         │
        否
         │
Agent 和 DB 在同一台机器？ ──是──→ 用 app.py (stdio)
         │
        否
         │
         └──────────────────────→ 用 app_http.py (HTTP)
```

---

## 工具列表

两种模式下工具完全一致，共 6 个：

| 工具              | 功能                       | 关键参数                                       |
| ----------------- | -------------------------- | ---------------------------------------------- |
| `search_memory` | 语义检索用户记忆           | `user_id`, `query`, `limit`              |
| `save_memory`   | 保存单条消息，异步提取事实 | `user_id`, `content`, `role`             |
| `save_dialogue` | 保存一轮对话，蒸馏提取     | `user_id`, `user_says`, `assistant_says` |
| `get_profile`   | 读取用户画像               | `user_id`, `include_inferred`              |
| `list_memories` | 按时间列出记忆             | `user_id`, `limit`                         |
| `list_tenants`  | 列出可用租户和 Agent       | 无                                             |

### 工具详解

**search_memory** — 语义检索

```
输入：user_id="u1", query="用户喜欢的代码风格", limit=10
输出：[{id, memory: "用户偏好简洁的函数命名", cognitive_type: "fact"}, ...]
```

Agent 在回答涉及用户背景、偏好、历史决策前优先调用。
需要 DashScope + SiliconFlow API Key。

**save_memory** — 单条消息写入

```
输入：user_id="u1", content="我习惯用 Rust 写后端", role="user"
输出：{task_id: "tsk_...", status: "pending"}
```

消息进入异步提取管线，由 Worker 完成事实抽取、去重、向量化。
`status: "pending"` 仅表示已接收，不代表已记住。

**save_dialogue** — 轮对话蒸馏
将 `user_says` 和 `assistant_says` 同时写入，提取质量优于单条消息。

**get_profile** — 用户画像

```
输入：user_id="u1", include_inferred=false
输出：[{id, assertion_kind: "explicit", property_value: "用户偏好 Rust 语言"}, ...]
```

默认只返回明确陈述的画像；`include_inferred=true` 同时返回推断画像。

**list_memories** — 时间排序列表

```
输入：user_id="u1", limit=20
输出：[{id, summary, cognitive_type, status, created_at}, ...]
```

非语义检索，按创建时间倒序。

**list_tenants** — 租户信息

```
输出：{tenants: [{tenant_id, agent_id, source_systems}], token_count: 3}
```

---

## 接入示例

### 方式一：stdio 模式 (`app.py`)

#### Hermes Agent

```bash
cd /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0
set -a && source .env && set +a

hermes mcp add general-memory \
  --command /home/lyy/miniforge3/bin/python3 \
  --env "PYTHONPATH=$(pwd)/src" \
        "MEMORY_DATABASE_URL=${MEMORY_DATABASE_URL}" \
        "MEMORY_AUTH_CREDENTIALS_JSON=${MEMORY_AUTH_CREDENTIALS_JSON}" \
        "DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY}" \
        "DASHSCOPE_BASE_URL=${DASHSCOPE_BASE_URL}" \
        "DASHSCOPE_MODEL=${DASHSCOPE_MODEL}" \
        "SILICONFLOW_API_KEY=${SILICONFLOW_API_KEY}" \
        "SILICONFLOW_BASE_URL=${SILICONFLOW_BASE_URL}" \
        "SILICONFLOW_EMBEDDING_MODEL=${SILICONFLOW_EMBEDDING_MODEL}" \
  --args "$(pwd)/src/memory_cmic/mcp-server/app.py"

# 验证
hermes mcp list
hermes mcp test general-memory
# 重启 Hermes 后生效
```

#### AgentScope

```json
{
  "mcpServers": {
    "general-memory": {
      "command": "python3",
      "args": ["-m", "memory_cmic.mcp-server.app"],
      "env": {
        "PYTHONPATH": "/path/to/GeneralMemory-2.5.0/src",
        "MEMORY_DATABASE_URL": "postgresql+psycopg://...",
        "MEMORY_AUTH_CREDENTIALS_JSON": "[{\"token\":\"t1\",\"tenant_id\":\"dev\",...}]",
        "DASHSCOPE_API_KEY": "sk-xxx",
        "SILICONFLOW_API_KEY": "sk-xxx"
      }
    }
  }
}
```

#### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "general-memory": {
      "command": "python3",
      "args": ["/path/to/src/memory_cmic/mcp-server/app.py"],
      "env": {
        "PYTHONPATH": "/path/to/GeneralMemory-2.5.0/src",
        "MEMORY_DATABASE_URL": "postgresql+psycopg://...",
        "MEMORY_AUTH_CREDENTIALS_JSON": "[...]",
        "DASHSCOPE_API_KEY": "...",
        "SILICONFLOW_API_KEY": "..."
      }
    }
  }
}
```

---

### 方式二：HTTP 模式 (`app_http.py`)

#### 启动服务

```bash
# 停旧进程
pkill -f app_http.py

# 重新加载 .env 并启动
set -a && source .env && set +a
MCP_HTTP_PORT=8001 PYTHONPATH=./src python3 src/memory_cmic/mcp-server/app_http.py

# 另开终端测试
curl -s http://localhost:8001/mcp \
  -H "Authorization: Bearer test-token" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 监听 0.0.0.0:8001，MCP 端点 /mcp
# 端口可通过 MCP_HTTP_PORT 环境变量修改
```

#### Hermes（远程连接）

```bash
hermes mcp add general-memory-http \
  --url http://localhost:8001/mcp \
  --header "Authorization: Bearer test-token"
```

#### Claude Desktop / Cursor / 任意 MCP 客户端

```json
{
  "mcpServers": {
    "general-memory": {
      "url": "http://localhost:8001/mcp",
      "headers": {
        "Authorization": "Bearer test-token"
      }
    }
  }
}
```

#### curl 验证

```bash
TOKEN="test-token"
curl -s http://localhost:8001/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool
```

---

## 调试

### MCP Inspector

两种模式都支持用 MCP Inspector 调试：

```bash
# stdio 模式
PYTHONPATH=./src npx @anthropic-ai/mcp-inspector python3 src/memory_cmic/mcp-server/app.py

# HTTP 模式（另开终端启动 app_http.py 后）
npx @anthropic-ai/mcp-inspector http://localhost:8000/mcp
```

### 直接命令行测试（仅 stdio 模式）

```bash
cd /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0
set -a && source .env && set +a

printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | \
  PYTHONPATH=./src python3 src/memory_cmic/mcp-server/app.py
```

---

## 故障排查

| 现象                                 | 检查                                                                                      |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| `MEMORY_DATABASE_URL must be set`  | 确认`.env` 已 source，或 MCP env 中包含此变量                                           |
| `ModuleNotFoundError: memory_cmic` | 确认`PYTHONPATH` 指向 `src/` 目录                                                     |
| `ModuleNotFoundError: pgvector`    | 使用系统 Python 而非 Hermes venv                                                          |
| `search_memory` 报 402 / 401       | 检查`SILICONFLOW_API_KEY` 是否有效、余额是否充足                                        |
| `save_memory` 返回后搜不到         | Worker 未启动（`poetry run memory-cmic-worker --role add`）                             |
| HTTP 模式 401`UNAUTHENTICATED`     | 确认`Authorization: Bearer <token>` 与 `MEMORY_AUTH_CREDENTIALS_JSON` 中的 token 一致 |
| Hermes 工具不显示                    | `hermes mcp test general-memory` 查看错误，重启 Hermes 后重试                           |
