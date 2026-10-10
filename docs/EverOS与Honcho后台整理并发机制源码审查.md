# EverOS 与 Honcho 后台整理并发机制源码审查

日期：2026-10-10。性质：源码审查与方案修订建议，未实施业务代码变更。

## 1. 审查范围与结论

本次直接阅读本机两个干净 checkout 的代码，并核对官方 GitHub 仓库。结论限定于以下版本，不把它们称为上游最新版本：

- EverOS：`5076683ab88d714390573d8f88ff3c470e51129a`，提交日期 2026-09-08，仓库 `EverMind-AI/EverOS`。这不是另一个仓库 EverMemOS。
- Honcho：`2eb27b6cc0595d3f8deb693f0560e7241c2aeaff`，提交日期 2026-09-25，仓库 `plastic-labs/honcho`。

审查重点是后台触发、处理期间输入变化、写入校验、重试和画像更新。未运行两个项目的服务或并发测试，不能据此断言其生产环境不存在竞态。

结论：两个项目都采用任务/事件分批执行，并在不同层面串行化或重新检查写入对象。没有在审查路径中看到“用户任意数据变化就将整套记忆、推断、画像流程全部回滚重跑”的要求。它们也没有与 MemoryCMIC 完全相同的证据版本与 AND/OR 有效性契约。

对于 MemoryCMIC，应保留必要的提交前检查，但只延期受影响的模型处理批次。前一版方案新增全流程运行表、等待所有派生任务达到统一水位的设计，应暂缓；这不是解决 B 变化所必需的条件。

## 2. EverOS 的实际机制

### 2.1 画像由事件触发，并按用户串行

`extract_user_profile` 监听两类事件：有 embedding 时走 `ProfileClusterUpdated`；没有 embedding 时走 pipeline 产生的 `EpisodeExtracted`。两条路径通过条件判断避免重复触发。默认抽取间隔为每 1 个来源记忆单元，最少 1 个候选 memcell。

整个“读取旧画像 → 读取来源 → 调用模型合并 → 写回画像”放在按 `app_id/project_id/owner_id` 分组的 `asyncio.Lock` 内。相同用户的这一策略串行，不同用户并行。读取时使用旧画像时间戳选择增量，同时强制纳入当前触发事件。

这能防止两个画像任务同时读取旧画像并相互覆盖，不代表它锁住了该用户全部数据写入。锁按策略名区分，而且实现明确限定单进程；不能直接作为 MemoryCMIC 多进程 Worker 的分布式锁。

在该策略的模型返回与持久化之间，没有看到对全部输入内容/版本重新比对的步骤。因此不能声称它对外部修改、其他策略更新来源具有完整快照保证。

源码：[画像策略](https://github.com/EverMind-AI/EverOS/blob/5076683ab88d714390573d8f88ff3c470e51129a/src/everos/memory/strategies/extract_user_profile.py)、[策略分区锁](https://github.com/EverMind-AI/EverOS/blob/5076683ab88d714390573d8f88ff3c470e51129a/src/everos/memory/_partition_locks.py)。

### 2.2 Reflection 是定时的局部整理

`reflect_episodes` 默认 disabled；其 Cron 表达式为 `0 2 * * 1`，即调度器时区的每周一 02:00，并非文件注释所称的每日夜间。执行还要求 embedding 可用。

每个用户范围内按 cluster 处理：选取候选 → 模型合并 → 写新 episode → 等待事实重抽取 → 废弃原 episode。每轮最多 10 个候选 cluster；单个 cluster 失败可跳过，继续其他 cluster。

废弃旧成员前重新查询当前成员，目标取“开始时成员 ∩ 当前成员”。运行期间新增的成员不会被本轮错误地废弃，已移除的成员也被排除。这属于对具体操作目标的重新核对，不能等价为对成员正文和全部来源版本的校验；合并内容也不会因这个交集计算自动重算。

源码：[定时入口](https://github.com/EverMind-AI/EverOS/blob/5076683ab88d714390573d8f88ff3c470e51129a/src/everos/memory/strategies/reflect_episodes.py)、[Reflection 编排及 `_resolve_deprecation_targets`](https://github.com/EverMind-AI/EverOS/blob/5076683ab88d714390573d8f88ff3c470e51129a/src/everos/memory/reflection/orchestrator.py)。

### 2.3 重试针对策略任务

OME 对单个策略事件执行退避重试和 dead letter；进程重启时恢复中断的任务。策略必须能够安全重放。这不意味着整个用户的全部记忆流程需要重跑。

源码：[任务执行器](https://github.com/EverMind-AI/EverOS/blob/5076683ab88d714390573d8f88ff3c470e51129a/src/everos/infra/ome/_dispatch/runner.py)。

## 3. Honcho 的实际机制

### 3.1 Dream 采用变化量、空闲和冷却触发

本地版本默认配置：Dream 开启；新增 explicit observation 阈值 50；空闲等待 60 分钟；距上次 Dream 至少 8 小时。推断产物不计入 explicit 增量，避免后台自己生成内容后不断触发自身。

定时调度路径会在新消息入队时取消尚未触发的内存计时器，再按变化条件重新调度。队列侧用 pending Dream 唯一索引去重。另一个 due 查询路径检查 explicit 增量、空闲、冷却、上次尝试及未处理 representation 任务；这些是不同调度路径，不能混写成每次执行都有的全部门槛。

取消等待计时器不等于撤销已经执行中的 Dream。

源码：[默认配置](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/config.py)、[Dream 调度](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/dreamer/dream_scheduler.py)、[新消息入队](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/deriver/enqueue.py)、[due 条件](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/dreamer/dream_due.py)。

### 3.2 按 work unit 排他领取，工具写入局部串行

`ActiveQueueSession.work_unit_key` 唯一，领取时通过插入和冲突忽略确保一个 work unit 只有一个领取者。representation 和 dream 的 key 不同；这不是把同一用户所有类型的任务排成一条全局串行队列。

工具执行器另外共享按 `workspace/observer/observed` 分组的进程内锁，保护创建、删除观察及写 peer card 等操作。它同样不是跨进程、覆盖整个 LLM 思考过程的用户锁。

模型执行使用短数据库会话，不在整个模型调用期间保持数据库事务。

源码：[领取与执行](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/deriver/queue_manager.py)、[work unit 构造](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/utils/work_unit.py)、[共享工具锁](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/utils/agent_tools.py)。

### 3.3 重新检查具体来源或目标，不做全库版本屏障

创建派生 observation 前，`_filter_ungrounded_source_ids` 查询引用 ID 是否仍对应可用 observation。无法解析的来源被移除，剩余来源不足时拒绝该 observation，并向模型返回单条失败信息。

文档去重/强化的提交路径 `_apply_document_row_updates` 重新锁定、读取目标行。如果目标已经不存在或被软删除，就跳过该操作或采用预先准备的新增候选；它不要求重新运行整个 Dream。

这些检查不能直接替代 MemoryCMIC 的契约：本项目一组 AND 依据少了 B 时，不允许简单删除 B 后保留原推断结论。需要完整依据或重新推断。

peer card 写入路径采用完整列表替换对应 JSONB 字段；审查的 `set_peer_card` 路径没有 expected_version/CAS 比较。不能把 Honcho 的局部写入锁解释成完整来源版本一致性保证。

源码：[派生来源校验](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/utils/agent_tools.py)、[目标行复核](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/crud/document.py)、[peer card 持久化](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/crud/peer_card.py)。

### 3.4 任务处理成功不能直接解释为完整治理成功

普通队列的临时错误可留待重试，并明确采用至少一次处理；源码承认重新模型生成可能增加重复计数。Dream 自身的 `process_dream` 又捕获异常而不向队列重抛，以便任务被标为 processed。

`run_dream` 返回中含 deduction/induction 的各自 success 标记；`process_dream` 只要收到非空 result 就写入冷却和数量基线。不能仅凭注释中“成功后推进”认定两个 specialist 都成功才推进。

因此，应借鉴分任务执行和结果明细，不照搬“processed 等于全部成功”的完成语义。

源码：[Dream 执行与结果](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/dreamer/orchestrator.py)。

### 3.5 画像与推断并无统一开源惯例

EverOS 的画像包含 `explicit_info` 和 `implicit_traits`，由画像抽取策略持久化。Honcho 这个版本的 peer card 则明确排除行为倾向和推断偏好，InductionSpecialist 没有写 peer card 的权限；相关内容保留在 observation 中。CardRefresh 的 rebuild 模式不读取旧卡片，而是从现存 observation 重建。

这说明“推断是否进入画像”取决于画像产品契约，不能用开源项目存在推理功能来证明其画像会无条件接纳推理。MemoryCMIC 仍可采用已讨论的 explicit/inferred 分型，但应保留独立准入与调用方识别。

源码：[Honcho specialist 与 peer card 规则](https://github.com/plastic-labs/honcho/blob/2eb27b6cc0595d3f8deb693f0560e7241c2aeaff/src/dreamer/specialists.py)。

## 4. MemoryCMIC 已经如何处理 B 变化

当前 consolidation 的 `_process_anchor` 已经实现：

1. 读取当前候选范围，生成包含记忆和证据状态的指纹。
2. 调用模型。
3. 提交事务中再次读取；anchor 的待处理 generation 或候选指纹变化时，跳过这次模型结果。
4. 保留待处理记录，推进本轮游标，其他处理继续。
5. 本轮仍有待处理内容时，任务返回 partial；没有回滚其他已经提交的结果。

因此，需要重新计算的是这次模型调用对应的候选批次，不是整个用户所有记忆，也不是重新跑 maintenance、推断、画像全部步骤。当前指纹范围较保守，同范围新增事实也可能使这次结果失效；后续如确有成本问题，再缩小到经过验证的决策范围，不能仅检查结果引用的 ID 而忽略新出现的反证。

当前 automatic 关闭时，保留的 pending 数据不会自行创建下一次 consolidation，需要再次手动触发；开启时按现有阈值、冷却或最长等待调度。不要把“保留待处理”说成已经自动重算完毕。

Profile 的行为不同：全用户画像快照变化时抛出 ProviderError，进入普通失败重试。这是需要统一的地方。输入变化属于正常并发情况，建议改为合并一次后续画像重建，不消耗模型故障重试预算；实现时需专门区分正常延期与真正异常，不能只改错误文案。

依据：`src/memory_cmic/consolidation.py::_process_anchor/process_run`、`profile.py::run_once`；已有测试 `test_new_fact_during_model_call_retries_old_scope`、`test_fact_expiring_during_model_call_cannot_commit_profile` 覆盖相应保护路径，本次只阅读，未重跑。

## 5. 建议收缩后的优化方案

保留三条运行链：

```text
来源/事实变化或 TTL 到期 → maintenance 完成证据与向量维护
手动或自动条件满足      → consolidation 整理事实、复核冲突、更新推断
画像候选及其依据变化    → profile 合并等待，满足时间条件后蒸馏
```

后续讨论已将画像规则纳入 [统一优化方案第 4、6 节](./记忆治理与推断画像统一优化方案.md)：确定性规则筛选候选，蒸馏判断长期价值；首次一条明确事实即可进入等待，后续相关变化使用相同静默窗口与最长等待。空结果记录已评估输入，无变化不周期性重跑；失效立即影响读取。实施范围以更新后的统一方案为准。

首期不新增 GovernanceRun 表和跨 Worker 的完成屏障。现有治理任务报告自身结果、未决冲突、因输入变化延期的批次，以及它触发的画像/向量任务 ID 和状态。整理完成不暗示所有下游已完成；调用方需要时分别查询关联状态。

并发规则限定为：

- 同一用户的同类后台任务避免并发；跨进程保护复用数据库任务唯一性/租约，不能照搬 asyncio.Lock。
- 任务取一批输入，提交前用短事务校验该批候选和依据。
- B 的内容、状态或必要证据变化：丢弃涉及该批次的模型输出，保留待处理标记。
- 其他已验证批次可以提交；不全局回滚、不在原循环里无限立即重算。
- 新写入留待后续任务；正常变化延期与服务错误退避重试分别计数。
- 删除、撤回与到期必须立即影响读取；不能为减少冲突把用户修改锁在漫长模型调用之后。

“输入水位”在本项目文档中应改成“本次处理的记忆、证据版本和变化标记”。它是内部校验信息，不是新的用户概念，也不是一套需要单独建设的全局快照系统。

优先实施顺序建议：补冲突复核与待澄清结果 → 统一输入变化的延期行为及同类任务去重 → 推断复核及推断画像 → 补充关联任务的可见状态。统一证据有效性校验仍作为相关改动的必要基础。

前一版方案中的完整业务职责和推断画像契约可以保留，但固定六阶段强编排、专用运行表和所有派生任务达到统一水位的要求暂缓。只有明确出现“一次请求必须等待全部派生结果一致完成”的业务需要时，再评估新增编排层。
