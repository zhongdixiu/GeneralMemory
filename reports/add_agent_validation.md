# Add 来源 Agent 参数验证

日期：2026-10-13。已完成工作区修改；未迁移业务数据库、未重启服务。

## 接口及身份

Add 增加可选 `agent_id`，省略时取凭证的 `caller_agent_id`。不接受 null、空白或超过 128 字符的值。来源 Agent 与凭证调用身份分别保存，不把自报 Agent 当成权限。

来源、会话、任务的命名空间在原调用身份边界内增加来源 Agent。共享凭证下不同来源 Agent 的相同用户、会话、消息 ID 和幂等键互不冲突；同来源 Agent 的任务仍按批次有序处理。不同调用身份继续隔离原始上下文和任务回执。用户记忆共享规则保持不变。

assistant 消息省略 author_id 时使用来源 Agent；显式作者应匹配来源 Agent。Search 的 agent_id 过滤按来源及提交任务的来源 Agent 判断，并保留检索后的有效性复核。审计记录真实凭证调用身份，来源 Agent 不同时另记 agent_id。管理重处理保留两种身份。

## 迁移

新增 `0010_add_source_agent`，在 source_record、conversation_session、memory_task 增加 agent_id，旧值从 caller_agent_id 回填。调整来源唯一性、会话主键、任务幂等唯一性及相关索引；不重写来源 ID、外部引用、历史哈希和任务进度。

迁移及模型适配保持旧调用和旧 ORM 创建路径兼容；省略 agent_id 与显式填入原 caller_agent_id 使用同一幂等身份。已保存不同来源 Agent 时，降级拒绝丢失该身份。

启用前停写并备份，执行 `alembic upgrade head`，再启动新版 API 与 Worker；时间优化的旧任务排空要求仍见 [ADD 运行说明](../docs/ADD接口运行说明.md)。

## 验证

- 独立数据库 `memory_cmic_agent_test` 完成从空库到迁移 0010 的升级。迁移回归验证旧来源/任务/进度保留、升级降级，以及拒绝丢失显式来源身份。
- 首轮接口、上下文、时间兼容及队列相关回归：**77 passed**。
- 全量检查覆盖 **420 项**：**418 passed、2 failed**。发现审计对旧格式增加冗余字段，以及 Search 测试仍通过修改 caller_agent_id 模拟来源变更。
- 将审计调整为只在来源 Agent 不同时补充字段；Search 并发来源变更测试改为修改独立 agent_id。修正后完整复验 `test_audit.py`、`test_read_api.py`、`test_add_agent.py`：**88 passed**。全量检查发现的两项失败均已通过复验，未重新运行剩余不受这两处修正影响的用例。
- Ruff 检查 `src`、`tests`、新增迁移及时间评估脚本通过，`git diff --check` 通过。
- 隔离库 `alembic check` 仍仅报告既有 embedding 可空性与 embedding_legacy 列差异，未发现新增 Agent schema 差异。该检查并非完全通过，本轮不修改 embedding 结构。

保留既有 Starlette TestClient 弃用警告。新增 API 行为通过确定性回归验证，本轮没有额外调用真实模型。
