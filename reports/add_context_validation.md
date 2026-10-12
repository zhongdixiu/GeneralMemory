# Add 核心优化验证记录

日期：2026-10-12。范围为本轮确认的 HTTP Add 核心链路；终态任务管理恢复、已完成来源的受控重处理、MCP／插件和公司鉴权暂缓。

## 通过的检查

- 最终相关回归：184 passed。覆盖 Add API、Worker、上下文切片、证据与治理、查询来源过滤、模型协议、迁移和任务队列。
- 失败终态 `failed/partial/cancelled` 不传播给后续任务；重试耗尽且租约过期的前序任务由 Add Worker 收尾，后续任务成功执行并可读取有效原文。
- 真实模型固定虚构样例：4/4 passed，见 [模型评估报告](./add_context_evaluation.json)。覆盖多轮确认完整证据、缺少指代、观测时间不能定位相对日期、附件引用不能代替文件内容。这四例不代表全面事实质量验收。
- `ruff check src tests scripts/evaluate_add_context.py migrations/versions/0008_add_context.py`、`poetry check`、`git diff --check` 通过。
- 在新建隔离数据库执行升级；迁移回归验证来源／证据保留、模糊 Agent 归属拒绝、降级命名空间冲突回滚。

最终回归命令（需使用隔离测试数据库并提供测试配置）：

```sh
pytest -q tests/test_add_context.py tests/test_add_worker.py tests/test_extraction_input.py tests/test_add_api.py tests/test_fact_governance.py tests/test_read_api.py tests/test_providers.py tests/test_add_migration.py tests/test_task_queue.py --tb=short
```

## 未通过或不稳定的全量检查

补充最后两项过期租约测试之前，全量回归为 362 passed / 2 failed。失败项为：

- `test_consolidation.py::test_merge_keeps_sources_and_admin_can_revert[True]`：撤销返回 `REVERT_CONFLICT`，提示范围中出现新事实。
- `test_governance_optimization.py::test_oversized_original_evidence_keeps_work_without_provider_retry[True]`：`consolidate_once` 未取得任务。

两项用例的所有参数变体与任务队列单独复跑合计 16 passed。验证期间观察到数据库时间／租约时间倒退，失败表现存在时间敏感性；不据此宣称全量回归已稳定通过，也未修改无关的撤销／合并逻辑。

`alembic check` 仍报告两项既有结构差异：`memory_embedding.embedding` 可空性与模型不一致，以及数据库仍有 `embedding_legacy` 列。分别以原库／原模型、隔离库／新模型核对，两组差异相同，本轮没有新增 Add 结构差异。全量迁移目录 Ruff 的三项既有 E501 位于 `0004_business_scenes.py:42–44`；本轮新增迁移静态检查通过。

## 部署状态

原数据库未迁移，未停止服务，也未部署新代码。启用前须按 [ADD 接口运行说明](../docs/ADD接口运行说明.md)暂停写入、备份并执行 `0008_add_context`；不能直接用新模型连接旧结构运行。
