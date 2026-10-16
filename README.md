# MemoryCMIC

MemoryCMIC 是基于 PostgreSQL、pgvector、FastAPI 和后台 Worker 的统一记忆服务。它接收对话来源，抽取带证据的用户事实，提供任务检索与画像读取，并处理记忆治理、到期和审计。

## 本地启动

以下命令均在项目根目录执行，适用于本地开发环境。项目需要：

| 依赖 | 用途 |
| --- | --- |
| Python `>=3.11,<4.0`、Poetry | 安装依赖，运行 API、Worker 和数据库迁移 |
| Docker Compose | 启动本地 PostgreSQL；镜像为 `pgvector/pgvector:pg15`，包含向量扩展 |
| DashScope API Key | `add`、`consolidation`、`profile` Worker，以及 `use_model=true` 的 Search 调用模型 |
| SiliconFlow API Key | `add`、`maintenance` Worker 生成向量；Search 接口也需要它 |

`docker-compose.yml` **只启动数据库**。API 和 Worker 是独立进程；只启动 API 时，Add、治理和画像等后台任务不会自动处理。

### 1. 配置 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，至少替换以下占位值：

| 配置项 | 如何填写 |
| --- | --- |
| `MEMORY_POSTGRES_PASSWORD` | 本地数据库密码；Compose 启动时必需 |
| `MEMORY_DATABASE_URL` | 应与上面的数据库用户名、密码、端口、库名一致。默认连接本机 `localhost:5433/memory_cmic`；修改密码时也要同步修改 URL |
| `MEMORY_AUTH_CREDENTIALS_JSON` | 服务端 Bearer 凭证数组。把 `token` 换成自己的测试 token，设置 `tenant_id`、`caller_agent_id`，并让 `allowed_source_systems` 包含后续 Add 请求使用的 `source_system` |
| `DASHSCOPE_API_KEY` | 要运行 `add`、`consolidation`、`profile` Worker 或使用 Search 的 `use_model=true` 时填写有效 Key |
| `SILICONFLOW_API_KEY` | 要运行 `add`、`maintenance` Worker 或调用 Search 时填写有效 Key |

`.env.example` 已给出凭证 JSON 格式及模型地址、模型名、输入预算等默认配置。`can_manage_memories`、`can_manage_governance`、`can_read_audit_content` 三个能力开关默认关闭；需要测试修改/删除、治理、审计正文时，按需在凭证项中设为 `true`。`HONCHO_DATABASE_URL` 仅历史数据迁移时需要。不要把含真实密码、token 或 API Key 的 `.env` 提交到 Git。

例如，使用默认数据库名和端口时，这三项应保持一致（示例值仅用于本地演示）：

```dotenv
MEMORY_POSTGRES_PASSWORD=local-dev-password
MEMORY_DATABASE_URL=postgresql+psycopg://memory_cmic:local-dev-password@localhost:5433/memory_cmic
MEMORY_AUTH_CREDENTIALS_JSON='[{"token":"local-dev-token","tenant_id":"tenant_demo","caller_agent_id":"email_agent","allowed_source_systems":["email_agent"],"can_manage_memories":false,"can_manage_governance":false,"can_read_audit_content":false}]'
```

### 2. 启动数据库并执行迁移

```bash
docker compose up -d postgres
docker compose ps
```

确认 `postgres` 显示为 `healthy` 后，在同一终端加载环境变量、安装依赖并将数据库迁移到当前版本：

```bash
set -a
source .env
set +a
poetry install
poetry run alembic upgrade head
```

Compose 默认把容器的 PostgreSQL `5432` 映射到本机 `5433`。如果修改了 `MEMORY_POSTGRES_PORT`、数据库名或用户名，也要同步调整 `MEMORY_DATABASE_URL`。迁移命令会修改目标数据库结构，执行前确认 URL 指向本地开发库。

### 3. 启动 API 和 Worker

每条命令在**独立终端**持续运行。每个新终端都先进入项目根目录并加载 `.env`：

```bash
set -a
source .env
set +a
```

然后分别运行：

| 进程 | 启动命令 | 负责的任务 |
| --- | --- | --- |
| API | `poetry run memory-cmic-api` | 接收和查询 HTTP 请求，默认监听 `0.0.0.0:8000` |
| Add Worker | `poetry run memory-cmic-worker --role add` | 抽取新消息中的事实、写证据及新事实向量 |
| Maintenance Worker | `poetry run memory-cmic-worker --role maintenance` | 后续向量更新/删除、TTL 到期、依赖复查 |
| Consolidation Worker | `poetry run memory-cmic-worker --role consolidation` | 合并、争议复核与推断等治理任务 |
| Profile Worker | `poetry run memory-cmic-worker --role profile` | 画像重建和画像维度配置应用 |

本地验证 Add 至少启动 API 与 Add Worker；完整链路再启动其他三类 Worker。直接运行 `memory-cmic-worker` 时默认是 `add` 角色，建议显式传 `--role`，避免启动了错误的消费进程。

### 4. 验证服务

打开 `http://localhost:8000/docs` 可查看当前运行版本的 OpenAPI 页面。要验证写入链路，先在终端设置与 `.env` 凭证一致的 token：

```bash
export API='http://localhost:8000'
export TOKEN='替换为 MEMORY_AUTH_CREDENTIALS_JSON 中配置的 token'
```

使用与该凭证 `allowed_source_systems` 一致的 `source_system` 提交消息：

```bash
curl -X POST "$API/api/v1/memories:add" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Idempotency-Key: readme-demo-001' \
  -H 'Content-Type: application/json' \
  -d '{"source_system":"email_agent","user_id":"readme_user","session_id":"readme_session","messages":[{"message_id":"readme_message_001","role":"user","content":"以后周报请先写结论。"}]}'
```

响应中的 `task_id` 用于查询处理结果：

```bash
curl "$API/api/v1/tasks/<替换为返回的task_id>" \
  -H "Authorization: Bearer $TOKEN"
```

返回 `pending` 表示仍在等待或处理；`succeeded`、`partial`、`failed` 是终态。`results` 为空不一定表示没有处理，需同时查看 `processing_results`。示例消息的具体记忆结果取决于模型判断。Search、Profile 和治理的调用方式见[完整接口说明](docs/完整接口说明.md)。

## 继续阅读

新同事可从[交接文档索引](docs/README.md)了解架构、模块边界和现有设计资料。更细的本地联调与测试选择见[开发与验证](docs/开发与验证.md)；所有 HTTP 接口的字段和响应见[完整接口说明](docs/完整接口说明.md)。
