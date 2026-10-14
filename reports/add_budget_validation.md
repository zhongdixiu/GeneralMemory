# Add 输入预算优化验证记录

日期：2026-10-14。本文保留 `p02-v13` 的验证基线，并记录后续 `p02-v14` 治理载荷精简。公共 Add 工具参数位置未改变，无数据库 schema 迁移。

## 自动化回归

`p02-v13` 回归命令覆盖：

```text
tests/test_add_context.py
tests/test_add_worker.py
tests/test_fact_governance.py
tests/test_add_time_semantics.py
tests/test_add_api.py
tests/test_add_agent.py
tests/test_task_queue.py
tests/test_extraction_input.py
tests/test_model_input.py
tests/test_providers.py
```

**199 passed，耗时 122.61 秒。** 其中 140 项数据库/接入回归，59 项模型输入/Provider 离线回归。测试移除环境代理，数据库测试在沙箱外连接已启动的本地 PostgreSQL。Starlette/httpx 的既有 deprecation warning 不影响测试结果。

Ruff 检查 `src`、`tests`、`scripts/evaluate_add_budget.py` 通过，`git diff --check` 通过。

关键覆盖：

- 紧凑候选保持完整比较信息，默认值可还原；短编号输出还原及未知编号拒绝。
- 历史超限时统一前 300 字符，原文不变；没有有效历史时省略 history。
- 纯辅助来源不调用抽取模型，完成后仍能作为后续用户上下文。
- 被截断的其他历史不能支持落库事实；独立事实仍保存，未完成来源不误标完整。
- 长当前用户消息及输出截断恢复不会因共享消息 ID 的衔接历史被错误阻断。
- 治理输入超限时不调用模型、不自动 ADD，其他事实正常提交。
- 目标元数据本身超限时逐消息报告，其他独立消息仍可处理。
- 来源失效、候选变化、租约、幂等、时间规则和切片重试维持原有校验。

## 真实模型与容量检查

使用 qwen3.8-max、5 组虚构样本：独立用户事实＋长历史、确认未见完整条件的方案、DUPLICATE、SUPERSEDE、DISPUTE，最终全部通过。

首次真实检查发现模型会忽略 truncated 标记并提取整套方案偏好，修复采用确定性的引用校验；通过表示整体处理链路拦截了不完整证据，不表示模型自行保证了语义完整性。

合成 10 候选请求的估算大小从 11688 降为 6163，减少约 47%。1/10/50 轮、辅助回复 500/2000/10000 字的 9 组数据都保留来源原文；抽取次数等于用户轮数，不随辅助回复长度增加，实际发送请求均符合预算。50 轮规划在本机约 15–49 毫秒，不包含数据库和模型端到端耗时。

详细输入、用量和测量见 [add_budget_evaluation.json](./add_budget_evaluation.json)。可使用 `poetry run python scripts/evaluate_add_budget.py --live` 复现；真实检查产生模型费用。

## 原故障任务只读验证

任务 `tsk_8c8288f581d84aeca1f898d8710cb226` 的当前最近 10 候选及争议组，旧表示估算为 10236，新表示为 **8052**，低于默认输入阈值 9952。

当时的实际候选和相似检索补充候选未冻结，此测量是当前数据库的只读重建。未修改原任务状态、回执、来源或记忆，也未发起任务重处理；原任务仍为 failed。

## 实际限制

- 仍按保守 UTF-8 字节估算，不是精确 tokenizer 计数。
- 每任务 20 次外部调用总额度保留；大批量可能按既有规则 partial/failed。
- 历史截断会丢失内容；引用被截断旧历史的事实暂缓，不承诺任意长上下文都能形成记忆。
- `p02-v13` 的 prompt 为 p02-v13-compact-bounded-history；部署前排空旧冻结版本，或对终态任务按现有受控入口重处理。

## p02-v14 治理载荷精简

`p02-v14-lean-governance` 仅改变治理模型可见载荷：候选 `source_message_ids` 改为未知时间下的 `shared_source` 布尔值，系统 TTL 不发送；事实省略可推导的 `project_limited` 和存储策略 `retention_kind`；当前用户消息只保留该事实引用的原文及时间/顺序字段。数据库和服务端校验仍使用完整对象。

**200 项测试通过**：140 项数据库/接入回归、60 项离线模型输入/Provider 回归。新增回归验证了治理实际发送内容与预算计算一致、未引用的长用户消息不进入该事实的治理请求，以及未知时间来源重合标记。Ruff 和 `git diff --check` 均通过。

合成 10 候选载荷从上一版 6163 降至 **5651** 字节；本版提示词下旧非紧凑表示为 11905。使用 qwen3.8-max 的 6 组虚构样本全部通过，包括新增的未知时间且来源重合的明确纠正。通过表示样本中的模型决策正确，不保证所有未知时间表达都能自动裁决。

原故障任务按当前数据库只读重建最近 10 候选及争议组，本版估算为 **7491**，默认阈值 9952。原任务仍为 failed；历史实际候选和相似检索补充候选未冻结，本次未重处理或调用模型。合成样本和模型用量见 [add_budget_evaluation_v14.json](./add_budget_evaluation_v14.json)。
