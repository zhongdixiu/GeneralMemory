# Search 与 Profile 接口运行说明

首版提供当前租户中指定用户的事实、推断检索和独立画像读取。同一用户在不同 Agent 中产生的个人记忆默认共享；授权共享不表示所有候选都适用于当前任务。

## 临时鉴权与边界

沿用 `MEMORY_AUTH_CREDENTIALS_JSON` 中已有的静态 Agent 服务凭证，使用 `Authorization: Bearer <configured-token>`。租户及调用 Agent 由凭证确定，客户端不通过请求指定其他租户。

普通读取需要具有非空 `allowed_source_systems` 的 Agent 服务凭证。只有治理管理权限的凭证返回 403；管理身份不自动获得个人记忆读取权限。Agent 读取其他 Agent 来源的用户记忆时不受自己的来源写入列表限制，该列表继续限制 Add 的来源写入。

公司 Basic 用户凭证的验证暂未接入。`user_id` 由受信任后端提供，服务按指定用户过滤数据，但尚未核验实际登录用户与该 ID 的一致性。因此当前方式用于受信任后端接入及内部联调，不宣称具备登录用户级访问保护。后续授权改造见[实施方案](./用户身份授权与跨Agent共享实施方案.md)。

## Search

`POST /api/v1/memories:search`

```json
{
  "user_id": "user1",
  "query": "帮我给项目组写一封周报邮件，说明进展和风险",
  "limit": 10,
  "use_model": false
}
```

- user_id 必填，1～128 字符。
- query 必填，不能全部为空白，最多 8000 字符。任务描述应保留必要的项目、时间、对象等条件。
- limit 可选，默认 10，范围 1～50；表示最多返回多少条，不保证填满。
- use_model 可选，默认 false，只接受 JSON 布尔值。关闭时按合并候选的向量距离排序；
  开启时调用 Qwen 判断任务适用性并排序。两种模式均校验来源、证据、状态、生效时间及 TTL。
  关闭时不会判断正文中的项目、对象、业务时间或规则优先级，调用方仍需遵守记忆的完整适用条件。
  这是默认行为调整；需要原有模型筛选行为的调用方必须显式传入 true。
- agent_id 可选，1～128 字符，按接入事实来源的写入 Agent 过滤，不表示本次调用 Agent。
- source_system 可选，1～64 字符，按原始来源系统过滤。
- 两个过滤条件同时传入时按 AND 生效；不筛选时省略字段，不传 null、空串或全空白字符串。
- 本轮不增加 session_id。不要求业务标签；不接受额外的 tenant_id、授权主体等正文参数。

例如只检索 Agent A 从邮件系统接入的用户记忆：

```json
{
  "user_id": "user1",
  "query": "帮我写一封周报邮件",
  "agent_id": "agent_a",
  "source_system": "email",
  "limit": 10,
  "use_model": true
}
```

过滤只缩小既有可读范围，不授予额外权限。省略过滤条件时保持同用户跨 Agent 共享。

```json
{
  "results": [
    {
      "id": "mem_example",
      "memory": "用户要求周报先列结论，再提供数据依据。",
      "cognitive_type": "fact"
    },
    {
      "id": "inference_example",
      "memory": "用户在报告类任务中倾向先看结论。",
      "cognitive_type": "inference"
    }
  ]
}
```

仅返回同租户、指定用户的 fact 和 inference，不开放 Agent 自身经验或组织公共记忆。不返回画像、原始对话、调试信息或模型理由。memory 为完整陈述，保留正文中的适用条件；调用方应区分明确事实与推断。

检索复用场景和语义候选、向量匹配及可选的任务适用性判断。候选必须具有当前可用向量；结果受有限候选窗口以及开启时的模型判断影响，不保证全量召回。返回前检查状态、生效时间、TTL、依据版本和有效来源，推断递归检查派生依据；独立证据组按 OR、同组依据按 AND 验证。搜索期间内容、版本或依据发生变化时，跳过过时结果。

开启模型时，仍判断候选完整正文中的条件，输出只包含按相关性排序的入选 ID，不生成逐条解释；内部检索显式请求 trace 时保留完整决策理由。两种模式均按最终顺序复核，跳过失效项并继续补位，满足 limit 后停止复核。缺失场景向量与 query 合并为一次 embedding 调用（超过 32 条仍由 provider 分批），来源过滤范围为空时直接返回，不调用 embedding 或适用性模型。候选窗口仍为 max(30, limit * 3)，本轮不缩小召回范围。

### 来源过滤的判定

agent_id 沿事实的 supports 证据追溯到 source_record，再检查同租户、同用户、当前来源版本的 memory_task_source 与 fact_extract 任务，来源与任务的 agent_id 必须匹配。Add 的来源 agent_id 可由接入程序指定，省略时取凭证 caller_agent_id；可信调用身份仍单独保存在 caller_agent_id，来源过滤不授予权限。实际支持事实的同 Agent history 来源也可参与完整证据组过滤。仅补交 history 不会独立形成事实，形成正式事实仍需本次目标用户的新陈述或确认。

迁移前未绑定 Agent 的旧来源继续要求 kind=target 的可追溯提交关系，不使用 created_by、消息 author_id 或 source_system 猜测来源 Agent。升级后不同 Agent 使用相同外部消息 ID 会形成各自的来源，不能通过重复提交另一会话的 ID 为原事实授予新的来源标记；同用户未过滤的长期记忆仍共享。

统一更新接口创建的手工来源例外：source_type=manual、source_system=memory_management，按服务端记录的 caller_agent_id/agent_id 匹配编辑 Agent，无需伪造 fact_extract 提交。替代后的事实不沿用旧来源 Agent 或来源系统，具体契约见[记忆修改与删除接口运行说明](./记忆修改与删除接口运行说明.md)。

事实至少需要一组完整有效 supports 依据，其全部来源满足过滤条件。推断至少需要一组完整有效 derives 依据，其中每条事实都在筛选范围内得到完整来源支持。例如推断必须同时依赖 A、B 的事实时，只筛选 A 不返回它；如果推断另有一组完全来自 A 的独立有效依据，则可以返回。两个过滤条件必须由同一组依据中的来源同时满足，不能由不同来源分别凑齐。

来源过滤在向量候选截断之前执行，并在返回前重新核验。历史导入来源没有可追溯的目标提交关系时，不猜测 Agent：仍可参与未过滤或仅 source_system 过滤的查询，但不进入 agent_id 过滤结果。

来源过滤复用任务和证据关系；Add 核心升级新增来源 Agent 命名空间，需要执行 `0008_add_context` 迁移。过滤依赖任务和来源关联的保留，不能把这些溯源记录作为临时队列数据直接清除。带过滤条件的查询会遍历该用户符合状态、时间及项目条件的记忆依据，尚未做大规模数据下的性能验证。

没有可用结果返回 `{"results": []}`。此接口是记忆检索，最终任务输出由调用 Agent 生成。

Search 使用既有 SiliconFlow 配置：`SILICONFLOW_API_KEY`、`SILICONFLOW_BASE_URL`、`SILICONFLOW_EMBEDDING_MODEL`。仅在 use_model=true 时要求 Qwen 配置：`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`DASHSCOPE_MODEL`；关闭时不创建 Qwen 客户端，也不校验适用性模型输入预算。两种模式都需要 embedding，读取可能为既有场景补齐当前模型的向量索引，不新增场景。

## Profile

`GET /api/v1/profiles?user_id=user1`

```json
{
  "profiles": [
    {
      "id": "profile_example",
      "assertion_kind": "explicit",
      "property_key": "communication",
      "property_value": "用户希望邮件回复简洁，但风险部分详细说明。",
      "value_type": "string",
      "business_domains": ["email"]
    }
  ],
  "config_version": 1,
  "applied_config_version": 1,
  "status": "ready"
}
```

user_id 必填，1～128 字符。新增 assertion_kind=explicit/inferred；默认 include_inferred=false，仅返回明确画像，传入 include_inferred=true 才纳入有效推断画像。默认返回该用户全部业务范围的当前有效明确画像，不按来源 Agent、业务范围或任务 query 筛选，也不分页或按 Search 的 limit 截断。business_domains=null 表示通用，否则保留具体适用场景；调用方使用时必须遵守陈述和业务范围中的条件。

`0012_profile_dimensions` 起，property_key 为租户配置中的稳定维度 key，property_value 为非空字符串陈述。
同维度允许多条，调用方应分组为数组，不能转换为 key 到单条记录的字典。新增 config_version、
applied_config_version 和 status=ready/pending/failed；配置尚未应用时不输出旧配置画像。
配置 API、默认维度和升级步骤见[租户画像维度配置运行说明](./租户画像维度配置运行说明.md)。

读取递归检查画像状态、有效时间、依据类型、证据版本和原始来源；推断画像还核对准入指纹及已知冲突，相关依据变化后旧批准不继续生效。无画像时 profiles 为空数组，保留配置版本和更新状态。此接口直接读取已有画像，不触发重建，不调用真实模型，也不要求 Search 的模型凭证已配置。

内部 `read_profiles()` 省略 business_domain 时读取全部；显式传入业务场景仍保留既有筛选行为。

## 事实、推断和画像的生成边界

- 明确事实按租户、用户归属，允许不同 Agent 的独立来源支持同一事实；合并后继续保留来源证据。
- 后台推断使用同租户、同用户、业务和项目范围一致的有效明确事实，允许跨 Agent、跨会话，不以写入 Agent 隔离。至少两条依据事实、两个不同的用户原始文本 content_hash，以及两个不同的 `(source_system, session_id)` 才能通过证据门槛；推断范围不得扩大，且不以已有推断继续生成推断。
- 相同原文转发后即使消息元数据或接入 Agent 不同，也不增加独立文本数量。不同文本及不同会话只是必要条件，不证明语义上独立；改写转述等情况仍需模型和业务质量评估。
- 画像跨 Agent 汇总有效、当前、长期且非项目限定的明确事实；一条明确陈述即可成为候选。开启 MEMORY_PROFILE_ALLOW_INFERRED 后，合格推断可作为 inferred 依据，不能转为明确事实或硬性指令，也不扩大适用范围。Profile 不增加来源过滤。
- 候选先按代码规则筛选，模型蒸馏再判断长期价值；不新增三次/七天的硬门槛。首次与后续相关变化均经过 2 分钟静默或 30 分钟最长等待，空结果也记录已评估输入，无变化不周期性调用模型。
- 候选删除、过期、争议或证据失效立即阻断相应画像读取；新画像可以等待触发条件。输入变化延期不计服务失败重试，同用户画像任务合并且不并发执行。
- POST /api/v1/profiles/rebuild 接收 {user_id}，需 can_manage_governance，显式重建绕过等待。GET /profiles 仍只读。存量回填及运行步骤见 [治理后台运行说明](./记忆治理后台运行说明.md)。

独立文本判定的收紧用于后续推断生成，本轮没有批量重算或作废历史推断。

## 错误与验证

| HTTP 状态 | 错误码 | 含义 |
| --- | --- | --- |
| 401 | UNAUTHENTICATED | 静态凭证缺失或无效 |
| 403 | FORBIDDEN | 凭证只有治理管理能力，未授予普通 Agent 接入能力 |
| 422 | INVALID_MESSAGE | 参数非法，沿用已有校验错误格式 |
| 503 | SEARCH_UNAVAILABLE | Search 所需模型凭证未配置 |
| 502 | SEARCH_FAILED | 向量或适用性模型请求失败、输出非法、输入超过适用性模型预算 |

失败不会伪装成空结果；错误响应不返回上游原始异常内容。其他接口行为继续沿用现有契约。

回归入口为 `tests/test_read_api.py`，与画像、检索、场景目录和原有 Add/审计测试一起执行。接口回归使用确定性模型替身，不调用真实模型；实际模型的召回质量、耗时和成本仍需业务联调验证。
