# 统一记忆服务数据模型验证报告

> 状态：七批验证全部完成；最终结论 GO
> 模型版本：V1.0  
> 验证范围：工程环境、DDL、约束和索引、样例数据、领域函数、生命周期、PostgreSQL 任务队列、范围/向量检索、性能冒烟和 Honcho 小批量迁移

## 1. 环境和版本

| 组件 | 版本 | 状态 |
| --- | --- | --- |
| Python | 3.12.10 | 已确认 |
| Poetry | 2.4.1 | 已确认 |
| PostgreSQL | 15.17 | 已验证 |
| pgvector | 0.8.2 | 已验证 |
| Docker Compose | 2.32.4-desktop.1 | 已确认 |

## 2. 第一批验证项目

- [x] 独立 `memory-cmic-postgres` 容器运行在 Host 端口 5433；
- [x] 数据持久化到 `memory_cmic_pgdata`，容器重启后 migration 版本仍存在；
- [x] `memory_cmic` 数据库和用户可连接；
- [x] Alembic upgrade、downgrade、再次 upgrade 成功；
- [x] 七张运行期核心表和一张迁移辅助表存在；
- [x] 必要 CHECK、复合外键、唯一约束和索引存在；
- [x] 17 项数据库结构与约束测试通过；
- [x] SQLAlchemy metadata 与数据库无 migration 漂移；
- [x] `honcho-postgres` 未被修改或重启。

实际对象统计：

| 对象 | 数量 |
| --- | ---: |
| `public` 业务表 | 7 |
| `migration` 业务表 | 1 |
| 非主键索引 | 27 |
| 业务约束 | 54 |

执行结果：

```text
alembic upgrade head              通过
alembic downgrade base           通过
alembic upgrade head             通过
alembic check                    No new upgrade operations detected
pytest -q                        17 passed
ruff check .                     All checks passed
poetry check                     All set
```

## 3. 第二批验证项目

- [x] `datas/lifecycle_cases.json` 声明且仅包含虚构验证数据；
- [x] 两个租户、两个用户、一个 Agent、一个 company 主体均成功写入事务；
- [x] email、disk、communication 和 project_a、project_b、project_c 范围数据完整；
- [x] 相同文本与 `model_id` 产生相同 1536 维单位向量；
- [x] 业务域和项目域在写入前去重、排序，空数组继续由数据库拒绝；
- [x] evidence group 不能复用于其他下游对象；
- [x] 范围检索排除跨租户、非 active 和 TTL 已到期数据；
- [x] 任务按 `tenant_id + idempotency_key` 幂等；
- [x] 数据、任务、审计和 `correlation_id` 可在同一事务内保存；
- [x] 测试使用独立事务，互不依赖执行顺序。

执行结果：

```text
alembic current                  0001_memory_model_v1 (head)
pytest -q                        36 passed
alembic check                    No new upgrade operations detected
ruff check .                     All checks passed
poetry check                     All set
```

## 4. 第三批验证项目

- [x] 同组 AND、组间 OR 的 evidence group 充分性符合冻结语义；
- [x] 完整性判断包含全部历史边，并校验边状态、上游状态、版本和 TTL；
- [x] 来源失效后立即重检下游，仍有独立完整组时记忆保持 active；
- [x] 所有组均不完整时记忆立即停止检索并递归传播；
- [x] 推断失效触发 `dependency_recheck`，画像失效触发 `profile_rebuild`；
- [x] 记忆失效和 TTL 转换按模型生成幂等 `vector_delete`；
- [x] TTL 任务未运行时，在线检索仍实时排除已到期记忆；
- [x] 纠正、替代和 disputed 状态不泄漏到正常检索；
- [x] T01～T07、T09、T15、T16 均有显式自动化测试。

执行结果：

```text
pytest -q tests/test_lifecycle_cases.py tests/test_lifecycle_fixture.py
                                  31 passed
pytest -q                        48 passed
alembic check                    No new upgrade operations detected
ruff check .                     All checks passed
poetry check                     All set
```

## 5. 第四批验证项目

- [x] 任务按 priority、available_at、created_at 顺序领取；
- [x] 两个独立连接通过 `FOR UPDATE SKIP LOCKED` 领取不同任务；
- [x] 未过期租约不可接管，过期后可由新 Worker 接管；
- [x] 续租、完成和失败操作校验 Worker 所有权及租约有效性；
- [x] 重试清空 Worker/租约并按 1、2、4 秒规则指数退避；
- [x] 达到 `max_attempts` 后任务进入 failed；
- [x] 旧 `input_version` 回写影响 0 行并将任务转为 cancelled；
- [x] 有效版本正常更新并完成任务；
- [x] 任务与审计继承根 `correlation_id`；
- [x] T10、T12、T17、T18、T19 均有显式自动化测试。

执行结果：

```text
pytest -q tests/test_task_queue.py
                                  9 passed
pytest -q                        57 passed
alembic check                    No new upgrade operations detected
ruff check .                     All checks passed
poetry check                     All set
```

PostgreSQL 是任务状态和调度事实源。本批未启动 MQ；未来 MQ 只允许传递 task ID 或唤醒
消费者，不独立维护任务状态、重试和优先级。

## 6. 第五批范围、向量检索与性能冒烟

- [x] 将 MVP 默认性能冒烟量级由固定1万条调整为3000条；
- [x] 保留 `--count 10000` 升级复核能力，但仅在3000条下无法稳定判断索引行为时使用；
- [x] 使用专用租户 `tenant_wp5_smoke`，批量数据与生命周期 fixture 及其他租户隔离；
- [x] 覆盖 user、agent、company、业务/项目范围、状态/TTL、stale hash、stale 向量和双模型；
- [x] 为来源反向依赖查询生成一对一来源和 supports 证据边；
- [x] 造数定向测试 2 passed；
- [x] 造数阶段全量测试 59 passed，`alembic check` 无 metadata drift，Ruff 和 Poetry 检查通过；
- [x] 实际装载3000条来源、3000条记忆、3000条证据边和3030条向量，耗时14.581秒；
- [x] `memory_embedding` 表（含索引）占49 MiB，HNSW 索引占24 MiB；
- [x] 预检查 `EXPLAIN` 选择 `ix_memory_embedding_active_hnsw`，默认量级已足以观察向量索引。
- [x] 用户、Agent、company 三路主体分别过滤后合并，T08 六组范围用例通过；
- [x] 指定 `model_id` 并按 cosine distance 排序，可区分同一记忆的两个模型向量；
- [x] 排除 stale 状态、`content_hash` 不一致、非 active 和 TTL 已到期记忆；
- [x] 检索定向测试 22 passed，全量测试 62 passed；
- [x] 在3000条记忆上完成五类查询各30次的 p50/p95 冒烟；
- [x] 完整计划已保存到 `reports/wp5_performance_smoke.json`。

量级调整依据：本批目标是结构、过滤、索引和明显退化的轻量冒烟，不是生产容量或 SLA 验证。
3000条向量的原始载荷约17.6 MiB，每个1%边界桶约30条，能保留低频反例并降低约70%的
生成与重建工作。当前量级下 HNSW 预检查和范围索引计划均可稳定解释，无需升至1万条。

性能结果：

| 查询 | p50 | p95 |
| --- | ---: | ---: |
| 单主体向量 | 12.521 ms | 18.093 ms |
| 业务/项目域过滤向量 | 9.741 ms | 10.502 ms |
| 三路主体向量 | 23.987 ms | 30.199 ms |
| TTL 扫描 | 0.942 ms | 1.455 ms |
| 来源反向依赖 | 0.753 ms | 1.412 ms |

HNSW 预检查、TTL 扫描和来源反查分别选择
`ix_memory_embedding_active_hnsw`、`ix_memory_item_active_expired_at` 和
`ix_evidence_upstream_source`。完整范围过滤向量查询对3030条向量使用顺序扫描，
扫描本身约0.7 ms、完整计划约9.1 ms，属当前小数据量与 join/范围过滤成本下的 planner 选择，
未出现明显退化。不强制 planner 或新增推测性索引；扩容后先复核 pgvector iterative scan。

最终验收：

```text
pytest -q tests/test_retrieval.py tests/test_lifecycle_fixture.py
                                  22 passed
pytest -q                        62 passed
alembic check                    No new upgrade operations detected
ruff check .                     All checks passed
poetry check                     All set
```

## 7. 第六批 Honcho Profile 与迁移验证

- [x] 使用 `transaction_read_only=on` 的强制只读会话完成源库 Profile；
- [x] 确认 1 个 workspace、4 个 peer、26 个 session、142 条 message、3 个 collection、
  268 条 document、138 条 message embedding 和 805 条 queue 记录；
- [x] 确认 document level 为 explicit 205、deductive 39、inductive 24，软删除 0；
- [x] 确认 `internal_metadata.message_ids` 的 216 次引用全部匹配 message，迁移为 supports；
- [x] 确认 `source_ids` 的 184 次引用中 168 次匹配 document，迁移为 derives；
- [x] 16 次无法匹配的 document 引用涉及 12 个缺失 ID 和 12 条下游 document；
- [x] dry-run 选择 10 个 session、116 条 message 和 50 条 document，覆盖三种 level、
  全部 16 次缺失引用及 6 条无来源 document；
- [x] 实际写入 134 条 source、50 条 memory、84 条 evidence、166 条 mapping 和
  50 个 `priority=0` 的 `vector_upsert` 任务；
- [x] 18 条 `legacy_import` 明确保留缺失 ID 或无来源原因，未伪造 message/document 关系；
- [x] 第二次迁移 source、memory、evidence 和 task 新增均为 0；
- [x] 对账期望与实际一致，悬空 evidence 为 0；
- [x] 50 个 `vector_upsert` task 与 268 条迁移 audit 的 `correlation_id` 一致；
- [x] 迁移前后 Honcho 八张相关表的行数及完整行内容指纹一致；
- [x] T13、T14 自动化回归测试通过。

迁移对账：

| 项目 | 期望 | 实际 | 第二次新增 |
| --- | ---: | ---: | ---: |
| `source_record` | 134 | 134 | 0 |
| `memory_item` | 50 | 50 | 0 |
| `memory_evidence` | 84 | 84 | 0 |
| `external_entity_mapping` | 166 | 166 | 0 |
| `memory_task` | 50 | 50 | 0 |
| `memory_audit_log` | 268 | 268 | 0 |
| 悬空 evidence | 0 | 0 | — |

执行结果：

```text
pytest -q tests/test_honcho_migration.py
                                  3 passed
pytest -q                        65 passed
alembic check                    No new upgrade operations detected
ruff check .                     All checks passed
poetry check                     All set
```

详细输出保存在 `reports/honcho_profile.json`、`reports/honcho_migration_dry_run.json`、
`reports/honcho_migration_first.json`、`reports/honcho_migration_second.json`、
`reports/honcho_migration_validation.json` 和 `reports/honcho_migration_report.md`。

## 8. 第七批综合验收

统一入口 `scripts/run_validation.sh` 已落地，默认只连接目标验证库；只有显式传入
`--with-honcho` 才会执行 Honcho 只读 Profile、dry-run 和迁移结果复核。2026-09-21 的默认综合
验收结果：

```text
alembic upgrade head              通过
pytest -q                         65 passed
alembic check                     No new upgrade operations detected
ruff check .                      All checks passed
poetry check                      All set
validation_results.json           已生成
```

另在随机命名的一次性数据库完成 `upgrade -> downgrade base -> upgrade -> current --check-heads`，
最终版本为 `0001_memory_model_v1 (head)`；临时数据库随后已删除，现有验证数据未被回滚。

### 8.1 T01～T19 结果

| 用例 | 结果 | 主要自动化证据 |
| --- | --- | --- |
| T01～T07 | 通过 | `tests/test_lifecycle_cases.py` |
| T08 | 通过 | `tests/test_lifecycle_fixture.py`、`tests/test_retrieval.py` |
| T09 | 通过 | `tests/test_lifecycle_cases.py` |
| T10 | 通过 | `tests/test_task_queue.py` |
| T11 | 通过 | `tests/test_schema_constraints.py`、`tests/test_lifecycle_fixture.py` |
| T12 | 通过 | `tests/test_task_queue.py` |
| T13～T14 | 通过 | `tests/test_honcho_migration.py` |
| T15～T16 | 通过 | `tests/test_lifecycle_cases.py` |
| T17～T19 | 通过 | `tests/test_task_queue.py` |

补充数据库约束测试全部通过，包括 tenant 必填、source 幂等、confidence 范围、空范围数组、
relationship shape、同组重复 active 边、self-loop、profile active 唯一性、embedding 主键、任务
priority/attempt、external mapping 幂等和 supersedes 跨租户拒绝。

### 8.2 验收门槛复核

| 门槛 | 结论 | 证据摘要 |
| --- | --- | --- |
| 结构可行性 | 通过 | 空库 migration 循环通过；跨租户和非法关系由数据库拒绝；无 metadata drift |
| 行为可行性 | 通过 | T01～T19 全部通过；AND/OR、TTL、失效、任务租约/版本/correlation 行为稳定 |
| 检索与性能 | 通过 | 3000条轻量冒烟完成；关键索引可观察；未发现明显退化 |
| 迁移可行性 | 通过 | Honcho Profile、dry-run、实迁和对账完成；第二次新增 0；悬空 evidence 0；源指纹不变 |
| 结构性重构 | 不需要 | 验证中未发现冻结核心字段必须重构的情形 |

七项 No-Go 条件均未触发。

## 9. 设计偏差与处理结论

- 经确认，Python 包由实施方案示例中的 `memory_validation` 统一命名为
  `memory_cmic`。该调整仅影响工程命名，不改变冻结数据模型。
- 当前 Honcho 的 `documents.source_ids` 实际指向 document，而不是冻结文档假设的 message；
  本批依据真实数据改用 `internal_metadata.message_ids -> supports` 和
  `source_ids -> derives`，未修改目标表结构。
- 源容器没有专用只读登录角色，实际连接账号为 `postgres` superuser；工具强制验证只读事务，
  且源库完整行指纹前后相同，但凭据权限仍高于生产迁移要求。正式迁移必须改用专用只读账号。
- 只有两个 session 含 document，无法满足计划的 5～10 个 document session；样本仍覆盖
  10 个 message session、三种 level、所有缺失引用和无来源场景。

以上偏差均可由命名、来源适配或运行条件处理，不要求修改冻结核心字段，因此不阻止数据模型 GO。

## 10. 剩余风险与 deferred 项

- “一个 evidence group 只能对应一个下游对象”已由最小写入函数校验，未新增 evidence group 表；
  直接绕过写入函数执行 SQL 时，数据库不能单独保证该规则。核心服务必须收口写路径。
- 业务域和项目域数组由写入层去重、排序；直接 SQL 只保证非空。核心服务和迁移适配器必须复用
  规范化写入路径。
- 当前 Honcho 凭据为 superuser，即使工具强制 `transaction_read_only=on` 且源指纹不变，正式迁移
  仍必须改用专用只读账号；这是生产迁移门槛，不是数据模型 No-Go。
- 画像属性归一化、冲突处理及 `profile_rebuild -> profile_property + profile_basis` 端到端验收按冻结
  设计 deferred 到核心画像 Worker 完成后；当前只验证失效和任务契约。
- 性能结果是3000条规模的结构与退化冒烟，不代表生产容量或 SLA。数据规模增长后需复核过滤向量
  查询、pgvector iterative scan、分区或分域索引。
- Honcho 结论来自当前单一实例和代表性小批次；其他版本或新来源必须重新执行 Source Profile、
  dry-run、实际迁移和幂等对账。

## 11. Go / No-Go 结论

第一批工程与数据库基线、第二批样例数据与最小领域函数、第三批证据组与生命周期、
第四批 PostgreSQL 异步任务队列、第五批范围/向量检索与性能冒烟、第六批 Honcho Profile
与代表性小批量迁移、第七批综合验收均通过。冻结模型未发现需要结构性重构的问题。

```text
GO：数据模型 V1.0 可进入统一记忆服务核心功能开发。
```

该 GO 仅针对数据模型工程基线，不等同于生产上线或全量迁移批准。正式 Honcho 迁移仍受专用只读
账号约束，画像质量和生产容量仍按上节 deferred 项补验。

## 12. 下一阶段建议

按冻结设计顺序进入核心服务：先实现来源写入、结构化直存和读取 API，再实现事实抽取/查重/证据
追加与向量生成，随后接入分路检索、生命周期 Worker 和画像 Worker。每个阶段继续以 PostgreSQL
任务状态为事实源，并保留 tenant、版本、evidence 和 `correlation_id` 约束。
