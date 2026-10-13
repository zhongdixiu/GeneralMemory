# Add 动物园计划上下文排查

排查日期：2026-10-13。数据库查询使用只读事务，未改动业务代码、任务或记忆。

## 结论

本例的必要历史已传给提取模型。直接失败点是提取模型将时间细化和计划取消判为 `no_durable_value`，导致没有候选事实进入治理，旧计划没有被替代。

另有独立的时间数据问题：后两条来源时间换算后比第一条早一小时。即使修正提取判断，使用这些来源时间进行 SUPERSEDE 仍会被现有迟到保护拒绝；本地已复现。

## 证据范围

- 会话范围：`cmic-yun / email_agent / email_agent / user_1001 / session_106`，顺序为 tenant、caller Agent、source system、user、session。
- 三次任务的 `prompt_version` 均为 `p02-v11-context-evidence`，每次只有一个切片，`diagnostics=[]`。
- 输入与处理结果来自 `memory_task.payload.execution` 保存的历史快照；来源、记忆、证据、审计也已交叉核对。
- [完整数据库证据](./add_zoo_context_diagnosis.json)保存三次任务、来源、相关记忆、证据和任务审计。
- [三次模型请求还原](./add_zoo_extraction_requests.json)包含 system prompt 和 user JSON。user 内容来自冻结切片，system prompt 来自当前同版本代码。历史 HTTP 请求、完整原始模型响应和当时的模型名称没有持久化，因此不将还原文件称为原始请求日志。

## 实际输入

数据库保存的第一条原文是“我周末准备带儿子去动物园玩一天”，与反馈中的措辞略有不同。

所有下列消息的 role 都是 `user`，`author_id=user_1001`，`timestamp_origin=source`；`reply_to_message_id` 和 `turn_id` 均为 null，`tool_calls` 和 `attachments` 为空。

| message_id | content | occurred_at |
| --- | --- | --- |
| msg_115 | 我不喜欢吃年糕 | 2026-10-10T10:00:00+08:00 |
| msg_119 | 我不喜欢吃年糕 | 2026-10-10T10:00:00+08:00 |
| msg_120 | 我喜欢波斯猫 | 2026-10-10T10:00:00+08:00 |
| msg_13 | 我最喜欢吃米粉了，是真的好吃 | 2026-10-10T10:00:00+08:00 |
| msg_124 | 我最近心情不好，连听我偶像小王的歌都没法开心 | 2026-10-10T10:00:00+08:00 |
| msg_125 | 我周末准备带儿子去动物园玩一天 | 2026-10-13T10:00:00+08:00 |
| msg_126 | 确切的说是这周六 | 2026-10-13T10:00:00+09:00 |
| msg_127 | 这周末还是不去了，要加班 | 2026-10-13T10:00:00+09:00 |

history 顺序与冻结切片完全一致：

| 次序 / batch_seq | task_id | targets | history（按传入顺序） |
| --- | --- | --- | --- |
| 1 / 11 | tsk_975432aa0cee4bb181e9283ac8ef840f | msg_125，原文字符 [0,15) | msg_115、msg_119、msg_120、msg_13、msg_124 |
| 2 / 12 | tsk_f7ba668037834bc5ab0cb4e0bd206673 | msg_126，原文字符 [0,8) | msg_119、msg_120、msg_13、msg_124、msg_125 |
| 3 / 13 | tsk_b3a6780d2cf649859713527bf769956d | msg_127，原文字符 [0,12) | msg_120、msg_13、msg_124、msg_125、msg_126 |

当前 provider 将 `{history: [...], targets: [...]}` 序列化为一个 user 消息，前面添加 system prompt；并非把每条历史分别转换为模型聊天回合。提取请求没有既有记忆列表、memory_id、已有记忆状态或先前提取结果。

当前代码按 UTF-8 字节保守估算的三次输入大小为 9852、9854、9870，输入限额为 `12000-2048=9952`。普通历史先取最多 30 条，再从最近向前按预算选入，所以本例每次最终只有 5 条历史。必要动物园上下文仍完整保留，这个预算限制不是本次失败的直接原因。

## 三次处理结果

### 第一次：新增

任务创建于北京时间 2026-10-13 09:43:42，`model_calls=3`，`changed=true`，结果为 `added`。

新增记忆 `mem_da8512395ead46fe81d60232e6c72edb`：

> 用户周末准备带儿子去动物园玩一天。

`retention_kind=temporary`、`temporal_kind=current`、`business_domains=null`、`governance_action=ADD`。生效时间为 2026-10-13 10:00 +08:00，到期时间为 2026-10-19 00:00 +08:00。查询时仍为 `active`、`version=1`、`supersedes_id=null`，只关联 msg_125 的一条有效证据。

### 第二次：过滤时间细化

任务创建于北京时间 2026-10-13 09:46:02，`model_calls=1`、`changed=false`、`results=[]`。

持久化的模型 assessment reason：

> Clarifies the timing of a one-time weekend plan to an exact day, which is single-task execution detail without durable value.

模型已经理解其是在细化前文计划的日期，却因“一次性任务细节”拒绝生成事实。失败原因不是缺少 msg_125，也不是没有消解指代。

### 第三次：过滤取消计划

任务创建于北京时间 2026-10-13 09:47:32，`model_calls=1`、`changed=false`、`results=[]`。

持久化的模型 assessment reason：

> Cancelling a one-time weekend plan due to overtime is single-task progress with no durable value.

模型理解为取消周末计划，但再次被持久价值门槛过滤。

后两次都没有记忆新增、替代、争议或补证据审计；这与旧计划仍为 version 1、只有第一条证据相符。三个任务的 `succeeded` 仅说明处理完成，不说明记忆已更新。

## 失败机制

1. `providers.py:25` 要求事实具有超出单次任务的价值，`:85` 排除单次执行进度。提示词没有明确规定“已经保存的临时计划，其时间细化、纠正和取消必须维护已有状态”。第一条被保存为临时计划，后续维护又被排除，表现为保存策略不一致。
2. 提取只接收 targets 和原始 history，无法知道动物园计划已经保存为有效记忆。既有记忆候选只在提取出事实之后由 `_plan_decisions` 查询。
3. 后两次空提取使 accepted facts 为空，embedding 和逐事实治理均没有触发，无法执行 `SUPERSEDE`。`_slice_processing_results` 直接保存模型的 `no_durable_value`，不是 API 将治理错误翻译成该结果。
4. 三条 source 的 `processed_version=version=1`。`no_durable_value` 被当作完整处理，因此相同幂等键重放返回原任务；换新幂等键提交相同已处理来源也会跳过提取。修复后不会自动重评估这两个终态任务。

## 独立时间问题

本次落库并非三个不同日期：三条 occurred_at 都是 2026-10-13，API 接收时间也相隔几分钟。

- msg_125：10:00 +08:00 = 02:00 UTC。
- msg_126、msg_127：10:00 +09:00 = 01:00 UTC。

因此按真实来源时间，后两条比旧计划早一小时。`worker.py:643` 对 SUPERSEDE 检查 incoming < source_occurred_at，会抛出 `late input cannot supersede a newer fact`。

这不是这两次实际的 `no_durable_value` 原因：它们尚未进入治理。它是修复提取后仍会遇到的下一处阻碍，应核实并正确提供真实来源日期、时间与时区，不应直接取消迟到保护。

## 本地验证与修复方向

验证没有请求真实模型、没有写数据库：

- 使用三份冻结切片调用 provider 的提取入口，并替换底层 completion 为本地捕获，3/3 确认 user payload 完整传递。
- 使用实际旧记忆和后两条来源时间，构造明确 correction + SUPERSEDE，2/2 复现迟到校验拒绝。
- 仅在本地将目标时间改为合成的 10:01 +08:00，两个治理校验均通过。此检查只证明时间保护条件，不证明真实模型会生成正确更新。

修复应明确将计划自身的临时保存价值与更新既有记忆的价值分开：已有计划的日期细化和取消不应因为不能形成长期偏好而被丢弃。提取需产出自足的更正事实并引用必要历史，后续治理替代同一计划，而非增加互相冲突的独立计划。可结合相关已保存记忆给提取提供状态依据；跨会话时尤其需要该依据，因为当前自动原始历史限于同一会话命名空间。

本例期望行为是：保存临时计划 → 细化为 2026-10-17 周六 → 因加班取消该动物园计划，旧计划失效。到期边界应保持该次事件的范围，不能将“要加班”推广为长期工作习惯。应补充覆盖完整三次 Add 的回归和真实模型评估，并单独覆盖时间倒序保护。以上为修复方向，本次未实施修复或重放历史任务。
