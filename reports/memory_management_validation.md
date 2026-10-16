# 记忆修改与删除验证记录

日期：2026-10-16。

## 交付范围

- 统一 delete_memory 和 DELETE 接口，memory_item 增加 deleted，原行保留并通过现有审计追溯。
- 统一 update_memory 和 PUT 接口，只编辑当前有效个人 fact 正文；新建手工来源、证据和替代事实，返回新 ID。
- 新增独立 can_manage_memories 权限；自然语言修改仍走 Add，自然语言删除由业务 Agent 定位后调用 DELETE。
- 既有来源、适用范围与期限不因管理操作被隐式改写；派生失效及向量/画像维护复用现有链路。

## 验证环境与结果

使用本地 PostgreSQL 的临时独立数据库，从空库执行所有 Alembic 迁移至 0011_memory_deleted。测试结束自动删除临时数据库。未迁移或改写现有开发库，未修改真实凭证，未调用真实模型。

- 新增专项 31 项通过，包含权限与归属、各状态删除及重复请求、AND/OR 依赖、画像即时过滤、替代来源及审计、来源过滤、过时向量任务、事务回滚和迁移降级保护。
- 相关回归：**275 passed，2 deselected**。覆盖 test_memory_management、test_read_api、test_lifecycle_cases、test_governance_worker、test_profile、test_consolidation、test_audit、test_add_api、test_fact_governance、test_schema_constraints。
- `ruff check src tests migrations/versions/0011_memory_deleted.py` 通过，`git diff --check` 通过。
- 依赖仅报告既有 Starlette TestClient/httpx 弃用告警。

## 排除项及初轮失败处理

下面两项在本轮扩大回归失败，并在未修改 HEAD `9535993` 的独立源码副本与空库中复现相同断言失败，最终相关回归明确排除，未修改或标记 xfail 原测试：

- `test_terminal_partial_preserves_committed_changes[True]`：预期仅补证据、不返回 ADD，实际 result_json 包含一条 ADD。
- `test_removed_execution_payload_after_evidence_commit_stays_partial`：预期 partial 的结果为空，实际包含一条 ADD。

因此不将整个原有测试集称为全绿。这两项属于已有 Add 分片/补证据问题，未并入本次修改删除范围。

初轮另有 10 项测试因继承 SOCKS 代理且环境缺少 socksio 而无法初始化替身客户端；最终测试仅在子进程清除代理后通过。一次画像 DISPUTE 集成用例初轮失败，单独复查及最终完整相关回归通过；未据此改写无关画像逻辑。

专项初轮的向量清理断言由测试外层长事务引起：任务 available_at 使用实际时间，而 CURRENT_TIMESTAMP 固定为事务开始时间。测试将待执行维护任务设为当前事务可领取时间后验证了完整清理，生产任务逻辑未为此改动。

## 部署及能力边界

部署仍需执行 0011_memory_deleted，并按需配置 can_manage_memories。接口成功代表记忆变更与任务入队事务完成，向量和画像异步收敛；更新后的新向量完成前可能出现短暂检索空档。deleted 不可通过普通更新/治理恢复，但不承诺永久屏蔽相似新信息，不提供原文或审计副本物理清除。

接口细节见[运行说明](../docs/记忆修改与删除接口运行说明.md)。

## 过渡权限收敛补充

2026-10-16 按用户确认，实际 .env 仅保留三个原有业务 Agent 凭证，两个独立 governance_admin 凭证移除；业务 token、租户绑定和来源列表保留，三个现有能力开关均开启。不新增角色、授权表或权限控制面。

GET /api/v1/audit-logs 允许 can_read_audit_content 独立授予审计读取能力，can_manage_governance 仍可只读审计元信息；其他治理接口继续要求治理开关。新增回归验证审计开关不授予治理/修改/删除权限且保持租户隔离。`tests/test_audit.py` 16 passed，`ruff check src tests` 及配置加载验证通过。本次不改变数据库治理策略的 auto_enabled。
