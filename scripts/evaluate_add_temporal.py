"""Evaluate temporal extraction and sequential plan governance with synthetic user messages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from memory_cmic.providers import PROMPT_VERSION, QwenFactModel
from memory_cmic.settings import Settings
from memory_cmic.worker import TaskInput, _check_governance, _fact_time


def message(mid, text, when="2026-10-13T09:00:00+08:00", *, origin="source", position=0):
    return {
        "message_id": mid,
        "role": "user",
        "content": text,
        "occurred_at": when,
        "received_at": "2026-10-13T09:00:00+08:00",
        "timestamp_origin": origin,
        "batch_seq": position + 1,
        "position": 0,
    }


def task_input(targets, history):
    return TaskInput(
        task_id="synthetic",
        tenant_id="synthetic",
        caller_agent_id="synthetic",
        user_id="synthetic",
        source_system="synthetic",
        session_id="synthetic",
        batch_seq=targets[0]["batch_seq"],
        targets=targets,
        history=history,
        source_ids={},
        task_signature="synthetic",
        associations=(),
        source_states={},
    )


def main():
    settings = Settings.from_env()
    model = QwenFactModel(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
    )
    results = []
    history, candidate = [], None
    for index, text in enumerate(
        ["这周末我准备带儿子去动物园玩一天", "确切的说是这周六", "这周末还是不去了，要加班"]
    ):
        target = message(f"zoo-{index}", text, f"2026-10-13T09:0{index}:00+08:00", position=index)
        output = model.extract_with_outcomes(
            targets=[target], history=history, related_memories=[candidate] if candidate else []
        )
        fact = next((f for f in output.facts if "动物园" in f.memory), None)
        passed = fact is not None and fact.retention_kind == "temporary"
        decision = None
        if fact and candidate:
            decision = model.resolve(fact=fact, candidates=[candidate], targets=[target])
            _check_governance(decision, fact, [candidate], task_input([target], history))
            passed = passed and decision.action == "SUPERSEDE"
        if index == 1:
            passed = passed and "2026" in fact.memory and "17" in fact.memory
        if index == 2:
            passed = passed and any(word in fact.memory for word in ["取消", "不去", "放弃"])
            passed = passed and "加班" in fact.memory
        results.append(
            {
                "case": f"zoo_step_{index + 1}",
                "passed": bool(passed),
                "targets": [target],
                "history": list(history),
                "related_memories": [candidate] if candidate else [],
                "output": output.model_dump(mode="json"),
                "governance": decision.model_dump() if decision else None,
            }
        )
        print(results[-1]["case"], "PASS" if passed else "FAIL", flush=True)
        if fact:
            candidate = {
                "id": f"memory-{index}",
                "memory": fact.memory,
                "status": "active",
                "temporal_kind": fact.temporal_kind,
                "business_domains": fact.business_domains,
                "project_context": fact.project_context,
                "conflict_group_id": None,
                "explicit_expired_at": None,
                "expired_at": None,
                "effective_at": _fact_time(fact, task_input([target], history)).isoformat(),
                "source_occurred_at": target["occurred_at"],
                "timestamp_origin": "source",
                "source_message_ids": fact.message_ids,
                "statement_order": {
                    "caller_agent_id": "synthetic",
                    "source_system": "synthetic",
                    "session_id": "synthetic",
                    "batch_seq": index + 1,
                    "position": 0,
                },
            }
        history.append(target)
    cases = [
        (
            "past_residence_without_interval",
            [message("past", "我以前在北京住过")],
            [],
            lambda o: any(
                f.temporal_kind == "historical"
                and f.retention_kind == "regular"
                and f.expired_at is None
                and f.effective_at is None
                for f in o.facts
            ),
        ),
        (
            "past_year_import",
            [message("year", "我2024年在北京住过", "2024-07-01T09:00:00+08:00")],
            [],
            lambda o: any(
                "2024" in f.memory and f.temporal_kind == "historical" and f.expired_at is None
                for f in o.facts
            ),
        ),
        (
            "realtime_default_saturday",
            [message("live", "这周六去动物园", origin="server_received")],
            [],
            lambda o: any(
                "2026" in f.memory
                and "17" in f.memory
                and f.effective_at is None
                and f.expired_at is None
                for f in o.facts
            ),
        ),
        (
            "known_old_saturday",
            [message("old", "这周六去动物园", "2024-09-05T09:00:00+08:00")],
            [],
            lambda o: any(
                "2024" in f.memory
                and ("7日" in f.memory or "09-07" in f.memory)
                and f.expired_at is None
                for f in o.facts
            ),
        ),
        (
            "unknown_saturday",
            [message("unknown", "这周六去动物园", None, origin="adapter_observed")],
            [],
            lambda o: (
                not o.facts and any(a.reason_code == "MISSING_DATE_ANCHOR" for a in o.assessments)
            ),
        ),
        (
            "unknown_partial",
            [
                message(
                    "partial", "我以前在北京住过；这周六去动物园。", None, origin="adapter_observed"
                )
            ],
            [],
            lambda o: (
                any("北京" in f.memory for f in o.facts)
                and any(a.reason_code == "MISSING_DATE_ANCHOR" for a in o.assessments)
            ),
        ),
        (
            "future_rule",
            [message("rule", "下月1日起周报采用列表")],
            [],
            lambda o: any(
                f.effective_at is not None
                and f.effective_at == datetime(2026, 10, 31, 16, tzinfo=UTC)
                and f.expired_at is None
                for f in o.facts
            ),
        ),
        (
            "business_deadline_is_not_retention",
            [message("deadline", "本周我负责材料审核，下周恢复原安排")],
            [],
            lambda o: any(f.expired_at is None and "材料审核" in f.memory for f in o.facts),
        ),
    ]
    for name, targets, context, check in cases:
        output = model.extract_with_outcomes(targets=targets, history=context)
        passed = check(output)
        results.append(
            {
                "case": name,
                "passed": passed,
                "targets": targets,
                "history": context,
                "output": output.model_dump(mode="json"),
            }
        )
        print(name, "PASS" if passed else "FAIL", flush=True)
    path = Path("reports/add_temporal_evaluation.json")
    path.write_text(
        json.dumps(
            {
                "prompt_version": PROMPT_VERSION,
                "model": settings.dashscope_model,
                "executed_at": datetime.now(UTC).isoformat(),
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    if not all(result["passed"] for result in results):
        raise SystemExit(f"Temporal evaluation failed; inspect {path}")


if __name__ == "__main__":
    main()
