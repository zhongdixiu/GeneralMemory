# ADD 接口运行说明

Add 接入核心优化已实现，设计依据见 [Add 接入层优化方案](./Add接入层优化方案.md)。本文描述代码行为；启用本轮版本前须执行至 `0010_add_source_agent` 的迁移，并排空旧时间契约任务。本轮上下文与时间规则见 [优化方案](./Add上下文与时间优化方案.md)。MCP、框架插件和公司用户鉴权仍未交付。

## 范围

当前提供标准消息接入、来源持久化、事实抽取、范围判定、重复补证据、明确替代、争议处理、可恢复切片和任务查询。
不包含框架适配器、webhook、项目范围写入、定时记忆整理或治理队列执行器。

## 适用范围与治理

`source_system` 仅标识来源，与业务域没有映射。抽取按信息适用场景输出标签，以 `communication`（短信、电话、即时消息等非邮件通信）、`email`（邮件读写、处理和管理）、`disk`（个人云盘备份、存储、管理和分享）为初始标准标签，并允许其他场景，如“方案编写”。初始同义表达归一化，新场景标签校验非空及长度，不因不在初始词表中丢弃。通用个人事实不限定业务；确实无法判断的业务限制不能写成通用事实。

项目字段仅预留，`project_domains` 暂不赋值，不解析或猜测项目 ID。有效项目记忆正常生产，正文及内部 `metadata.project_context` 保留项目限定；未知或重名项目的自足陈述也保留，不推广成一般偏好。内部上下文是文本条件，不提供唯一项目身份或授权。

明确事实的抽取不再要求 `scope_known`，也不因业务分类不确定而跳过完整、有依据的事实。个人身份、职业、饮食偏好及习惯等未限定业务的事实使用 `business_domains=NULL`；原文明示的业务、项目、客户、场合和时间条件必须保留在正文，能确定的业务限制仍写入场景标签。`NULL` 不代表与每个任务都相关，任务检索仍结合正文判断相关性和条件；精确范围检索仅提供范围兼容候选。缺少关键指代或事实依据的内容仍不生成记忆。后台推断同样不要求 `scope_known`，仍校验至少两条跨会话独立用户证据和依据的业务／项目范围，禁止扩大受限信息的适用范围。画像仅从有效、当前、长期且非项目限定的明确事实生成，不把推断当成明确事实；画像生成和读取均保留原文条件及业务限制。画像重建任务需要单独运行 `memory-cmic-worker --role profile`。

事实、推断和画像正文使用原文语言，写成短句，主语、谓语、宾语或补语完整。关于当前用户的中文陈述使用“用户”作主语，不照搬“我”；依据目标消息和历史上下文消解人称、地点及对象指代，不猜测缺失信息。例如“我喜欢吃年糕”写成“用户喜欢吃年糕”；有杭州居住上下文时，“我住在那边”写成“用户住在杭州”，缺少上下文则不生成该事实。其他人物保留明确姓名或角色，电影名称等字面内容不替换。来源原文和证据引用保持原样，缩短正文不得丢失业务、项目、时间和其他条件。 可运行 `poetry run python scripts/evaluate_memory_wording.py`，用虚构样例验证完整短句、代词消解、上下文缺失、姓名和电影标题，以及推断和画像表述。

判重和治理针对同用户及等价的实际适用条件。不同项目上下文保守分开，已知不同初始场景不误合并；其他场景标签结合语义比较，不只按字符串判断。明确纠正或状态更新建立新事实和 `supersedes_id`，同步失效旧事实及受影响派生；无法裁决的矛盾双方进入同一争议组并暂停使用。争议只有新的明确用户澄清才能解决。historical 表示过去内容，可以保存缺少起止日期的过去经历，也可以明确更正同一经历。不同经历按正文日期和条件区分；迟到来源不能覆盖较新的声明，只能保留为标明原声明日期和后续声明边界的历史内容。

内部 `search_memories` 保留精确场景路径；没有任务上下文时，不返回尚未绑定项目 ID 的项目限定记忆。需要语义适用性判断时同时传入 `query` 和 `applicability_model`，在服务端授权主体及生命周期过滤后的候选中，根据正文及项目上下文筛选；场景标签不充当授权条件。公开 Search 接口通过 `search_task_memories` 检索，默认按向量距离排序，显式 use_model=true 时对有限候选窗口判断语义适用性并按模型顺序返回。

`results` 只列出本次形成且提交时可用的记忆，保持 `event=ADD`。替代后的旧值、争议事实、尚未生效或已过期的记忆不进入该列表。新增 `processing_results` 按消息／片段记录处理结果，因此不能仅凭 `succeeded` 或 `results=[]` 判定是否已记住。

| `outcome` | 含义 |
| --- | --- |
| `added` | 新记忆已提交 |
| `already_exists` | 事实与支持组均已存在，本次未重复写入 |
| `evidence_added` | 已有事实，本次补充依据；同一支持组不会重复写入 |
| `superseded` | 明确纠正或更新已提交 |
| `disputed` | 形成争议，不能作为已确认事实使用 |
| `historical` | 仅兼容旧任务的已保存但已过期结果；新任务不产生 |
| `no_durable_value` | 已评估，没有持久保存价值；辅助消息也可返回此结果 |
| `context_insufficient` | 缺少必要依据，或必要依据超出输入预算 |
| `evaluated_no_fact` | 空提取未提供可靠的原因分类，不猜测具体原因 |
| `already_processed` | 此来源版本已处理，本次仅可辅助理解 |
| `time_unresolved` | 缺少必要日期锚点、声明顺序未知、迟到更正或非法保存区间；具体见 reason_code |

每项包含 `message_id`、`start_char`、`end_char`、`outcome`、`memory_ids` 和可选原因 `reason`、稳定的 `reason_code`；涉及记忆时附加 `temporal_kind` 和提交时 `memory_status`。字符区间采用原文的左闭右开索引。一个消息可包含多个结果，例如部分陈述已保存、另一部分上下文不足。`processing_results` 表示已提交的处理动作；同批后续纠正可能使先前新增记忆失效，可用记忆以 `results` 为准。处理中两种结果均为 `null`，终态失败／部分完成只公开已提交片段的结果；迁移前任务未记录此信息时为 `null`，不事后推测。

`context_insufficient`、`time_unresolved` 和未分类空结果不会把来源标为提取完整。相同幂等键仍重放原任务；后续补充上下文并以新幂等键再次提交未完成来源时，新任务可重新评估，原任务输入和结果保留。已完成来源不会因补交历史自动重提取；管理员可使用下述受控重处理命令创建新任务。生产提取模型必须为每个目标用户返回 assessment；漏项、矛盾和 evaluated 空事实是协议失败，不作为已完成空结果。

## 长输入与重试

默认输入预算为 12000，输出预留 2048；仍按 UTF-8 字节保守估算，不声称是精确 token 数。Add 规划和 Provider 共用模型载荷构造，按实际紧凑 JSON、提示词及 512 固定预留检查，默认输入阈值为 9952。未知时间转换、timezone 和空 history 省略都计入该检查。预算配置见 `.env.example`。

当前用户正文完整抽取，长正文按原文位置分片，保留至多 256 字符衔接。纯 assistant/system/tool 片完整保存并直接完成来源处理，不调用抽取模型，不生成用户事实；后续用户仍可引用它们。每任务 20 次总调用上限仍包含抽取、embedding、治理及附加调用，因此任意 100 条消息并不保证在默认额度内全部完成。

历史先尝试原文；候选历史整体放不下时，各角色正文统一保留前 300 字符并标记 truncated，再按剩余预算优先选择关联历史和较新的普通历史。继续使用最近 30 条普通历史窗口，结构关联可超出窗口。无有效历史时省略 history，不阻塞当前用户消息。related_memories 仅在完整原文支持组可用时附加。截断视图保留准确字符位置，数据库原文不变；引用被截断的其他历史消息的事实由确定性校验拦下，返回 context_insufficient / HISTORY_TRUNCATED，独立当前事实继续保存，相关来源不标记提取完整。

治理候选使用短编号和公共字段默认值压缩，Provider 将返回编号还原后进入原有校验。候选正文、状态、来源时间、明确截止时间、范围和完整争议组都保留；模型不接收系统 TTL 和原始来源消息 ID 列表，未知时间候选仅接收来源重合标记。治理只携带该事实引用的当前用户陈述及必要时间/引文字段，服务端仍使用完整候选和原文校验。压缩后仍超限的事实返回 context_insufficient / GOVERNANCE_BUDGET_EXCEEDED，同片其他事实继续，不把超限降级为 ADD。目标元数据本身超限时报告 context_insufficient / TARGET_METADATA_BUDGET_EXCEEDED，其他消息继续。具体设计与验证见 [本轮优化方案](./Add输入预算与长对话批量处理优化方案.md)。

任务首次处理保存来源/历史输入、相关记忆状态、首次 received_at、切片计划、预算、北京时间和 prompt 版本。相关状态在提交前校验，变化时重新提取未提交片段，原始输入及已提交片段保留。每片的记忆、证据、审计、结果与进度在短事务内提交；模型调用不持有事务。重试从未提交片继续，消息全部片完成才设置来源处理版本。续租不会缩短已有租约截止时间。输出截断最多连续拆分两轮，模型调用有默认 20 次的每任务总预算；每个抽取、治理请求及 embedding 批次都计数，失败调用也不退还额度。

服务内部重试期间仍返回 `pending`，不暴露已提交的中间结果。最终失败时，有任何已提交记忆或证据变更则返回 `partial`，否则 `failed`；只补证据的 `partial` 允许结果为空。公共接口未增加重试入口，终态不通过普通 Add 重放来强制重处理。

同会话前序 `pending/processing` 保持有序等待；进入 `partial/failed/cancelled` 终态后不再阻断后续任务。Add Worker 会将重试耗尽且租约已过期的 `processing` 任务转为失败／部分完成，无需依赖其他角色的 Worker 才能解除阻塞。上下文读取与提取状态分开，前序失败任务保存的有效原文仍可使用。向量清理及派生重建任务仍待 P03/P04 消费；同步失效保证旧值立即退出使用。

## 会话、来源与上下文

会话范围为 `(tenant_id, caller_agent_id, agent_id, source_system, user_id, session_id)`，来源唯一性在该范围内再加 `message_id`。同一凭证下，不同来源 Agent 的外部会话／消息 ID 可以相同，原始历史、批次和处理状态仍独立；跨凭证保持原隔离，用户长期记忆的共享和治理范围保持用户级。

请求幂等范围为 `(tenant_id, caller_agent_id, agent_id, user_id, Idempotency-Key)`。同键同内容返回原任务，不同内容返回冲突；不同用户、不同来源 Agent 可使用相同键。来源系统、会话和正文属于请求内容，同一调用身份、用户和来源 Agent 不能以同一键投递不同来源／会话。后台任务的幂等范围保留。来源 `agent_id` 可由请求指定，省略时沿用凭证的 `caller_agent_id`；凭证身份和来源身份分别保存。用户仍由受信任后端指定，尚无公司登录用户身份验证。

请求 `agent_id` 为可选非空字符串，最长 128 字符，保持大小写与原值；不能传 null 或纯空白。身份来自接入程序，不应由模型补造。assistant 消息省略 `author_id` 时取来源 `agent_id`，显式作者须匹配它。`source_system`、用户消息 `author_id` 的含义不变。

`agent_id` 仅标记来源，不授予权限：租户、允许写入的来源系统和管理权限仍来自 Bearer 凭证。任务回执仍按凭证中的 `caller_agent_id` 授权，无需在 GET 中补传来源 Agent；同一凭证可查询自己提交的各来源 Agent 任务，其他调用身份不能凭自报同名 Agent 读取这些回执。Search 的可选 `agent_id` 过滤现改为匹配来源 Agent，不把来源过滤当成授权。

每个任务只使用已接收、在自身批次边界内且版本有效的来源。首次执行冻结实际使用的来源和切片，重试复用；提交前重新校验来源和用户级治理状态。必要历史可与目标用户的新陈述／确认组成同一证据支持组，任一必要来源撤回时，该组失效，已有依赖治理负责检查记忆。

`history_messages` 本次可供理解，不独立触发事实提取。具备真实来源时间和目标时间锚点的补充历史可供后续任务自动选择，合并时保持历史位置；只有观测时间或无法确定相对顺序的历史只用于当前显式请求，后续需明确回复／回合／工具关联才能选用。普通已处理消息在去重后仍可作为上下文。

`content` 仍必填，可为空字符串，但必须有有效 `metadata.tool_calls` 或 `metadata.attachments`；纯空白且没有结构化内容会被拒绝。工具调用只允许在 assistant 消息上；tool 消息仍需真实的 `tool_name`、`tool_call_id`，缺少 `tool_status` 时默认 `unknown`。附件引用会保存和传递，不下载或解析附件正文。

`occurred_at` 是用户发出消息的时间，不是正文中的事件日期，可省略。业务 Agent 知道真实交互时间时应原样传入。完整无时区时间按北京时间解析；带时区时间保留真实时刻并转换到北京时间供模型解析；支持 ISO8601、空格日期时间和既有数字 Unix 秒/毫秒（Pydantic 按绝对值 2e10 分界），不支持日期单值或自然语言时间字段。正文中的“本周六”按来源时间所在的北京时间日历周解析。可选字段仍按原契约省略而不传 null。

服务保存不可由调用方填写的首次 `received_at`。普通 targets 省略 occurred_at 时按实时接入补为 received_at，服务记录 `server_received`；同键重放、换键重投、Worker 重试都复用首次值。history_messages 省略时间保留未知，不默认今天。

`metadata.timestamp_origin` 默认为 `source`；导入缺少原始发送时间时可显式使用既有 `adapter_observed`。其 occurred_at 可省略，旧调用传入的观测值仅保留审计，新来源真实 occurred_at 存 NULL。未知来源的发生时间和接收时间均不提供给模型作为日期锚点；仍可保存无日期依赖的事实。相同声明时间使用来源首次出现的批次、history/target 位置和序号裁决，历史来源后来改为目标不会因重投获得较新的声明次序。已知当前用户引用原文明确更正未知日期的旧断言时可裁决；未知旧声明不能凭导入时间覆盖较新的当前声明。时间倒序、未知先后和保存区间拒绝用 reason_code 报告，不按网络故障盲重试。

## 升级与迁移

运行环境升级前暂停 API／Worker 写入并备份数据库，再执行 `alembic upgrade head`。`0008_add_context` 在事务内锁定相关表，从既有任务关系映射来源 Agent，保留来源、证据、任务 ID、原执行进度及审计，批次序号不会倒退。迁移不会自动重提取已完成任务。

同一来源关联多个 Agent、关联任务缺少 Agent，或会话没有可验证的 Agent 映射时，迁移直接报错并回滚。需先核对 `memory_task_source` 与 `memory_task` 的真实归属，不能删除来源或猜测 Agent 以绕过检查。

`0009_add_time_semantics` 新增 received_at 并允许未知 occurred_at；旧来源用 created_at 回填收到时间，metadata.received_at_origin=legacy_created_at 标明近似值。旧 adapter_observed 的原值、记忆 TTL、expired 状态及任务结果均不改写。含 NULL 来源时间时不允许降级，因为无法恢复真实日期。

`0010_add_source_agent` 分别在来源、会话和任务中新增 agent_id，旧记录回填为原 caller_agent_id，保留来源 ID、原文、任务进度和历史哈希。新命名空间在原凭证范围内增加来源 Agent，不覆盖已认证调用身份。旧调用省略与显式填写原 caller_agent_id 使用同一幂等身份；外部引用哈希只用于新来源，存量不重写。已使用不同于 caller_agent_id 的来源 Agent 时，降级会拒绝丢失来源身份。

Agent 参数实现及回归结果见 [验证记录](../reports/add_agent_validation.md)。

新任务 config_version=3，prompt 为 p02-v14-lean-governance。升级前先用原 Worker 排空 config_version<3 及旧 prompt 的任务；新 Worker 拒绝混用旧冻结语义，保留任务记录并给出明确失败。管理员随后可选择终态任务重处理。旧幂等键及来源 semantic_hash 按原版本验证，不因新字段产生冲突。

新命名空间投入使用后，回退可能出现旧来源唯一性、会话或幂等键冲突。`alembic downgrade 0007_profile_governance` 遇到冲突会在事务内失败并回滚；应保留新版本并处理冲突，或按上线前备份恢复，不能删除证据强行降级。

新增确定性测试见 `tests/test_add_context.py` 和 `tests/test_add_migration.py`；真实提取评估可运行 `poetry run python scripts/evaluate_add_context.py`，报告写入 `reports/add_context_evaluation.json`。四组固定虚构样例仅验证本次多轮证据、缺指代、观测相对时间与附件引用边界，不代表全面事实质量验收。实际检查结果及既有失败见 [本轮验证记录](../reports/add_context_validation.md)。

## 配置与启动

复制 `.env.example` 中新增的鉴权及模型配置到本地 `.env`，不要提交真实 token 或 API Key。

```bash
set -a
source .env
set +a
poetry install
poetry run alembic upgrade head
poetry run memory-cmic-api
```

另开进程启动 Worker：

```bash
set -a
source .env
set +a
poetry run memory-cmic-worker
```

## 提交消息

```bash
curl -X POST http://localhost:8000/api/v1/memories:add \
  -H 'Authorization: Bearer <configured-token>' \
  -H 'Idempotency-Key: example-request-1' \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "email_business_agent",
    "source_system": "email_agent",
    "user_id": "user_1001",
    "session_id": "session_101",
    "messages": [{
      "message_id": "msg_101",
      "role": "user",
      "content": "以后周报都使用项目符号。",
      "occurred_at": "2026-09-24T09:00:00+08:00"
    }]
  }'
```

默认返回 `202` 和 pending 任务。使用相同 `Idempotency-Key` 重试同一请求会返回原 task_id。

## 查询任务

```bash
curl http://localhost:8000/api/v1/tasks/<task_id> \
  -H 'Authorization: Bearer <configured-token>'
```

重复事实不会生成新的结果项；Worker 会把新来源补充为已有记忆的证据，任务返回
`succeeded` 和空 `results`。

## 记忆有效期

Add Worker 使用 `p02-v14-lean-governance`，默认保存期限为 temporary 30 天、regular 180 天、long_term 无默认到期日。

新增事实 effective_at 默认为所引用目标用户消息的首次 received_at 最大值；仅明确未来才开始适用的规则使用 max(received_at, 未来起点)。未来计划的事件日期写进正文，计划现在即可检索和取消。明确未来才适用的变更先作为带条件的新规则保存，不立即失效当前规则；开始后检索按正文条件选择适用规则。默认 TTL 从此 effective_at 起算；“我以前在北京住过”作为 historical + regular + active 保存 180 天，没有起止日期也保存。今天导入过去事实不会因为事件过去而立即到期。

expired_at 只接受用户明确的记忆保存期限，例如“这条记忆只保存到12月1日”。业务结束、事件日期或过去经历区间保留正文，不能作为系统忘记时间。Search 的适用性判断明确收到北京时间当前时刻，检验正文条件；过去经历可用于历史问题，不能变成当前事实，过去计划不能推定为已执行。Profile 仍只采纳适合的长期当前信息。

重复陈述增加证据，已知的更新确认时间用于判断后续消息的新旧，不续期。检索和后台整理也不续期。到期记忆不会进入结果或检索；记录仍保留用于审计，maintenance Worker 负责状态转换和向量清理。

临时计划及对已有记忆的确认、细化、纠正和取消正常提取；单次工具进度和完成通知不独立形成记忆，不提供任务结束联动或候选确认流程。新分类的画像仅使用长期事实，仍受显式期限、用户纠正和证据失效约束。分类由模型判断，需要保留独立陈述及其时效条件。

已有未分类记忆不自动改写。存量待分类清单的导出方法见 [记忆治理后台运行说明](记忆治理后台运行说明.md#存量记忆的-ttl-复核)。

## 受控重处理

管理命令使用环境变量 MEMORY_MANAGEMENT_TOKEN 指定既有管理凭证，要求 can_manage_governance=true；仅处理指定租户内已终态任务的原会话来源，不能处理仍绑定未完成任务的来源。命令创建新任务并记录原因、操作者和旧任务 ID，保留旧任务及已提交证据；已有相同事实经过正常判重，不刷新首次收到时间或 TTL。

```bash
poetry run python -m memory_cmic.add_management \
  --task-id <terminal-task-id> --reason '规则升级后复核提取'
```

可选 --history-source-id 补入同会话原文；可选 --source-time '<source-id>=2026-10-13T10:00:00+08:00' 修订已核实的目标来源日期。时间修订增加 source.version，记录原值/原文/metadata 审计，失效旧版支持组并触发依赖处理，再创建新提取任务。不得直接更新表或删除来源绕过冲突。多条相关任务须按原对话顺序操作；动物园原测试的 +08/+09 倒序须核实后才能修订。

本轮真实模型评估命令：poetry run python scripts/evaluate_add_temporal.py，报告见 reports/add_temporal_evaluation.json。固定虚构样例验证分类和日期理解，不代表全部自然语言场景均可靠。
