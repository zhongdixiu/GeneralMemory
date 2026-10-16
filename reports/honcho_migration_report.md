# Honcho 小批量迁移验证报告

> 结果：通过

## 对账

| 项目 | 期望 | 实际 |
| --- | ---: | ---: |
| `source_records` | 134 | 134 |
| `memory_items` | 50 | 50 |
| `evidence_edges` | 84 | 84 |
| `external_mappings` | 166 | 166 |
| `vector_tasks` | 50 | 50 |
| `audit_logs` | 268 | 268 |
| `orphan_evidence` | 0 | 0 |

## 幂等与源库保护

- 第二次迁移新增 source：0；
- 第二次迁移新增 memory：0；
- 第二次迁移新增 evidence：0；
- Honcho 源库指纹不变：是；
- Honcho 会话强制 `transaction_read_only=on`；
- 旧 embedding、queue 和锁状态未迁移。

## 已确认的实际语义偏差

- `documents.internal_metadata.message_ids` 指向 `messages.id`，迁移为 `supports`；
- `documents.source_ids` 主要指向其他 `documents.id`，迁移为 `derives`；
- 无法匹配的历史 document 引用由带缺失 ID 的 `legacy_import` 来源保守承接；
- 源库只有两个 session 含 document，无法形成 5～10 个 document session，报告保留此偏差。
