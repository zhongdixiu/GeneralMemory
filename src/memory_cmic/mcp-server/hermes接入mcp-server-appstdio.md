# GeneralMemory MCP Server 接入示例

## Hermes Agent 接入步骤

### 环境信息

| 项 | 值 |
|---|---|
| Hermes 版本 | v0.10.0 |
| Hermes 目录 | `~/.hermes/` |
| Python（运行 MCP） | `/home/lyy/miniforge3/bin/python3` (3.13) |
| Python（Hermes venv） | `~/.hermes/hermes-agent/venv/bin/python3` (3.11) |
| MCP Server 路径 | `src/memory_cmic/mcp-server/app.py` |

### 步骤 1：确保凭证包含 MCP 所需的 source_system

编辑 `.env`，`allowed_source_systems` 中至少包含 `"mcp-server"`：

```bash
MEMORY_AUTH_CREDENTIALS_JSON='[{
  "token":"test-token",
  "tenant_id":"dev",
  "caller_agent_id":"test_agent",
  "allowed_source_systems":["test","mcp-server","hermes"]
}]'
```

### 步骤 2：加载环境变量并注册 MCP 服务器

```bash
# 进入项目目录
cd /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0

# 加载 .env
set -a && source .env && set +a

# 注册到 Hermes
hermes mcp add general-memory \
  --command /home/lyy/miniforge3/bin/python3 \
  --env "PYTHONPATH=/home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0/src" \
        "MEMORY_DATABASE_URL=${MEMORY_DATABASE_URL}" \
        "MEMORY_AUTH_CREDENTIALS_JSON=${MEMORY_AUTH_CREDENTIALS_JSON}" \
        "DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY}" \
        "DASHSCOPE_BASE_URL=${DASHSCOPE_BASE_URL}" \
        "DASHSCOPE_MODEL=${DASHSCOPE_MODEL}" \
        "SILICONFLOW_API_KEY=${SILICONFLOW_API_KEY}" \
        "SILICONFLOW_BASE_URL=${SILICONFLOW_BASE_URL}" \
        "SILICONFLOW_EMBEDDING_MODEL=${SILICONFLOW_EMBEDDING_MODEL}" \
  --args /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0/src/memory_cmic/mcp-server/app.py
```

> **注意**：`--command` 使用系统 Python3（已安装 `pgvector`、`mcp` 等依赖），而非 Hermes 自带的 venv Python（缺少依赖）。`PYTHONPATH` 指向 `src/` 目录使 `memory_cmic` 模块可导入。

### 步骤 3：验证

```bash
# 查看 MCP 服务器列表
hermes mcp list
# 应显示：general-memory  (stdio)

# 测试连接，列出 6 个工具
hermes mcp test general-memory
```

预期输出 6 个工具：`search_memory`、`save_memory`、`save_dialogue`、`get_profile`、`list_memories`、`list_tenants`。

### 步骤 4：重启 Hermes

完全退出 Hermes（`⌘Q` 或 `kill` 进程），重新打开。

### 步骤 5：在对话中使用

重启后 Hermes agent 可以在对话中调用以下工具：

| 场景 | 工具调用 |
|------|----------|
| 了解用户背景 | `search_memory(user_id="user_001", query="代码风格偏好")` |
| 保存事实 | `save_memory(user_id="user_001", content="用户决定用 Rust 重构后端")` |
| 蒸馏对话 | `save_dialogue(user_id="user_001", user_says="我习惯用 Rust", assistant_says="了解")` |
| 查看画像 | `get_profile(user_id="user_001")` |
| 浏览记忆 | `list_memories(user_id="user_001", limit=20)` |
| 查看租户 | `list_tenants()` |

---

## AgentScope 接入

在 AgentScope MCP 配置中添加：

```json
{
  "mcpServers": {
    "general-memory": {
      "command": "python3",
      "args": ["/path/to/src/memory_cmic/mcp-server/app.py"],
      "env": {
        "PYTHONPATH": "/path/to/GeneralMemory-2.5.0/src",
        "MEMORY_DATABASE_URL": "postgresql+psycopg://user:pass@localhost:5433/memory_cmic",
        "MEMORY_AUTH_CREDENTIALS_JSON": "[{\"token\":\"...\",\"tenant_id\":\"dev\",\"caller_agent_id\":\"agent\",\"allowed_source_systems\":[\"mcp-server\"]}]",
        "DASHSCOPE_API_KEY": "sk-xxx",
        "SILICONFLOW_API_KEY": "sk-xxx"
      }
    }
  }
}
```

---

## Claude Code / 桌面端 接入

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

或命令行注册：

```bash
claude mcp add general-memory -s user \
  -e PYTHONPATH=/path/to/GeneralMemory-2.5.0/src \
  -e MEMORY_DATABASE_URL=postgresql+psycopg://... \
  -e 'MEMORY_AUTH_CREDENTIALS_JSON=[{...}]' \
  -e DASHSCOPE_API_KEY=sk-xxx \
  -e SILICONFLOW_API_KEY=sk-xxx \
  -- python3 /path/to/src/memory_cmic/mcp-server/app.py
```

---

## 直接运行调试

```bash
cd /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0
set -a && source .env && set +a

# 直接启动（通过 stdio 交互）
PYTHONPATH=./src python3 src/memory_cmic/mcp-server/app.py

# 用 MCP Inspector 调试
PYTHONPATH=./src npx @anthropic-ai/mcp-inspector python3 src/memory_cmic/mcp-server/app.py
```

---

## 故障排查

| 现象 | 检查 |
|------|------|
| `hermes mcp list` 不显示 | 确认 `hermes mcp add` 命令成功执行 |
| 连接失败 | 直接运行 `python3 src/memory_cmic/mcp-server/app.py` 看报错 |
| `search_memory` 报 LLM 错误 | 确认 DASHSCOPE_API_KEY / SILICONFLOW_API_KEY 正确 |
| `save_memory` 返回 403 | 确认 `source_system` 在 `allowed_source_systems` 中 |
| 工具不出现 | `hermes mcp test general-memory` 看具体错误 |
| `ModuleNotFoundError: memory_cmic` | 确认 PYTHONPATH 指向 `src/` 目录 |
| `ModuleNotFoundError: pgvector` | 使用了 Hermes venv Python，改用系统 Python3 |
| `MEMORY_DATABASE_URL must be set` | 确认 MCP env 中包含所有必要的环境变量 |

## 写入 Hermes 的 config.yaml 示例

执行 `hermes mcp add` 后，`~/.hermes/config.yaml` 会新增：

```yaml
mcp_servers:
  general-memory:
    command: /home/lyy/miniforge3/bin/python3
    args:
      - /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0/src/memory_cmic/mcp-server/app.py
    env:
      PYTHONPATH: /home/lyy/data-22/cloud/memory/cm_memory/GeneralMemory-2.5.0/GeneralMemory-2.5.0/src
      MEMORY_DATABASE_URL: postgresql+psycopg://memory_cmic:mypass123@localhost:5433/memory_cmic
      MEMORY_AUTH_CREDENTIALS_JSON: '[{"token":"test-token","tenant_id":"dev","caller_agent_id":"test_agent","allowed_source_systems":["test","mcp-server","hermes"]}]'
      DASHSCOPE_API_KEY: sk-xxx
      DASHSCOPE_BASE_URL: https://dashscope.aliyuncs.com/compatible-mode/v1
      DASHSCOPE_MODEL: qwen3.8-max
      SILICONFLOW_API_KEY: sk-xxx
      SILICONFLOW_BASE_URL: https://api.siliconflow.cn/v1
      SILICONFLOW_EMBEDDING_MODEL: BAAI/bge-m3
```

也可以通过 `hermes config` 命令修改：

```bash
hermes config set mcp_servers.general-memory.env.MEMORY_DATABASE_URL "新URL" --force
```

移除 MCP 服务器：

```bash
hermes mcp remove general-memory
```