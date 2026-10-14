"""Measure synthetic Add payloads and optionally check their meaning with the real model."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter

from memory_cmic.extraction_input import ExtractionBudget, build_slices, input_size
from memory_cmic.model_input import extraction_size, governance_payload, request_size
from memory_cmic.providers import (
    EXTRACTION_PROMPT,
    GOVERNANCE_PROMPT,
    PROMPT_VERSION,
    ExtractedFact,
    QwenFactModel,
)
from memory_cmic.settings import Settings


def message(mid, content, role="user", **extra):
    return {"message_id": mid, "role": role, "content": content,
            "occurred_at": "2026-10-14T09:00:00+08:00", **extra}


def candidates():
    return [{
        "id": f"mem_{i:032x}", "memory": f"用户要求第{i}类邮件使用简洁语句。",
        "business_domains": None, "project_context": None,
        "status": "active", "effective_at": "2026-10-13T08:00:00+00:00",
        "expired_at": None, "explicit_expired_at": None, "conflict_group_id": None,
        "temporal_kind": "current", "source_occurred_at": "2026-10-13T08:00:00+00:00",
        "timestamp_origin": "source",
        "statement_order": {"caller_agent_id": "agent_" + "a" * 32,
                            "agent_id": "agent_" + "a" * 32, "source_system": "email_agent",
                            "session_id": "session_" + "a" * 64,
                            "batch_seq": i, "phase": 1, "position": 0},
        "source_message_ids": [f"message_{i:032x}"],
    } for i in range(10)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Run synthetic model checks (paid calls)")
    args = parser.parse_args()
    budget = ExtractionBudget()
    rows = []
    for rounds in (1, 10, 50):
        for length in (500, 2000, 10000):
            targets = [m for i in range(rounds) for m in (
                message(f"u{i}", "我喜欢简洁回复。"), message(f"a{i}", "甲" * length, "assistant"))]
            times = []
            for _ in range(5):
                start = perf_counter()
                slices = build_slices(targets, [], system=EXTRACTION_PROMPT, budget=budget)
                times.append((perf_counter() - start) * 1000)
            requests = [s for s in slices if not s.get("context_only")]
            sizes = [extraction_size(EXTRACTION_PROMPT, s["targets"], s["history"],
                                     s.get("related_memories")) for s in requests]
            assert max(sizes) <= budget.input_tokens - budget.output_tokens
            assert all("".join(m["content"] for s in slices for m in s["targets"]
                               if m["message_id"] == t["message_id"]) == t["content"]
                       for t in targets)
            rows.append({"rounds": rounds, "assistant_chars": length,
                         "extraction_requests": len(requests), "max_request_bytes": max(sizes),
                         "planning_median_ms": round(median(times), 3),
                         "within_call_limit_before_embedding_governance": len(requests) <= 20})
    memories = candidates()
    fact = ExtractedFact(memory="用户要求邮件简洁。", message_ids=["user"])
    targets = [message("user", "以后邮件请简洁些。")]
    old_payload = {"fact": fact.model_dump(mode="json"), "candidates": memories, "targets": targets}
    compressed = governance_payload(fact.model_dump(mode="json"), memories, targets)
    report = {"executed_at": datetime.now(UTC).isoformat(), "prompt_version": PROMPT_VERSION,
              "measurement": "synthetic conservative UTF-8 bytes, not token counts",
              "governance": {"before_bytes": input_size(GOVERNANCE_PROMPT, old_payload),
                             "after_bytes": request_size(GOVERNANCE_PROMPT, compressed)},
              "planning": rows, "live": []}
    if args.live:
        settings = Settings.from_env()
        model = QwenFactModel(api_key=settings.dashscope_api_key,
                              base_url=settings.dashscope_base_url, model=settings.dashscope_model)
        report["model"] = settings.dashscope_model
        proposal = message("proposal", "Use bullet points for all future weekly reports. "
                           + "Detailed explanation. " * 100 + "Only for temporary tests.",
                           "assistant")
        for name, target in [
            ("independent_with_truncated_history", message("independent", "我喜欢吃年糕。")),
            ("confirmation_of_unseen_conditions", message("confirm", "可以，以后就按这个方案。",
                                                         reply_to_message_id="proposal")),
        ]:
            current = build_slices([target], [proposal], system=EXTRACTION_PROMPT, budget=budget)[0]
            output = model.extract_with_outcomes(targets=current["targets"],
                                                  history=current["history"])
            passed = (any("年糕" in f.memory for f in output.facts)
                      if name.startswith("independent")
                      else not output.facts and any(a.outcome == "context_insufficient"
                                                   for a in output.assessments))
            report["live"].append({"case": name, "passed": passed, "usage": model.last_usage,
                                   "history": current["history"],
                                   "output": output.model_dump(mode="json")})
            print(name, "PASS" if passed else "FAIL", flush=True)
        for action, old_text, new_text, change in [
            ("DUPLICATE", "用户喜欢吃年糕。", "用户喜欢吃年糕。", "none"),
            ("SUPERSEDE", "用户住在上海。", "用户现在搬到了杭州。", "update"),
            ("DISPUTE", "用户生日是5月1日。", "用户生日是6月1日。", "none"),
        ]:
            memories = candidates()
            memories[0]["memory"] = old_text
            fact = ExtractedFact(memory=new_text, message_ids=["user"], change_kind=change)
            decision = model.resolve(fact=fact, candidates=memories,
                                     targets=[message("user", new_text)])
            passed = decision.action == action and decision.memory_ids == [memories[0]["id"]]
            report["live"].append({"case": action, "passed": passed, "usage": model.last_usage,
                                   "output": decision.model_dump(mode="json")})
            print(action, "PASS" if passed else "FAIL", flush=True)
        memories = candidates()
        memories[0].update(memory="用户现在住上海。", source_occurred_at=None,
                           timestamp_origin="adapter_observed", statement_order=None,
                           source_message_ids=["old"])
        correction = message("user", "我现在住杭州，不是上海。")
        fact = ExtractedFact(memory="用户现在住杭州。", message_ids=["old", "user"],
                             change_kind="correction")
        decision = model.resolve(fact=fact, candidates=memories, targets=[correction])
        passed = (decision.action == "SUPERSEDE"
                  and decision.memory_ids == [memories[0]["id"]]
                  and decision.justification_message_id == "user"
                  and bool(decision.justification_quote
                           and decision.justification_quote in correction["content"]))
        report["live"].append({"case": "UNKNOWN_TIME_SHARED_SOURCE", "passed": passed,
                               "usage": model.last_usage,
                               "output": decision.model_dump(mode="json")})
        print("UNKNOWN_TIME_SHARED_SOURCE", "PASS" if passed else "FAIL", flush=True)
    path = Path("reports/add_budget_evaluation_v14.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(path)
    if any(not result["passed"] for result in report["live"]):
        raise SystemExit("Synthetic live checks failed; inspect the report")


if __name__ == "__main__":
    main()
