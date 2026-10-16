"""Evaluate Add confirmations, missing context and time provenance using synthetic dialogue."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from memory_cmic.providers import PROMPT_VERSION, QwenFactModel
from memory_cmic.settings import Settings


def message(mid, content, role="user", **extra):
    return {"message_id": mid, "role": role, "content": content,
            "occurred_at": "2026-09-24T09:00:00+08:00", "timestamp_origin": "source", **extra}


def main():
    settings = Settings.from_env()
    model = QwenFactModel(api_key=settings.dashscope_api_key,
                          base_url=settings.dashscope_base_url,
                          model=settings.dashscope_model, output_tokens=2048)
    cases = [
        ("confirmation_with_complete_support",
         [message("confirm", "可以，以后都这样。", reply_to_message_id="proposal")],
         [message("proposal", "以后周报采用三段式，可以吗？", "assistant")]),
        ("missing_referent", [message("missing", "记住这个。")], []),
        ("observed_relative_deadline",
         [message("observed", "本周我负责材料审核，下周恢复原安排。",
                  timestamp_origin="adapter_observed")], []),
        ("attachment_reference_without_content",
         [message("attachment", "", attachments=[{"file_id": "synthetic-file",
                                                   "mime_type": "application/pdf"}])], []),
    ]
    results = []
    for name, targets, history in cases:
        output = model.extract_with_outcomes(targets=targets, history=history)
        if name == "confirmation_with_complete_support":
            passed = any(set(f.message_ids) == {"proposal", "confirm"}
                         and "三段式" in f.memory for f in output.facts)
        elif name in {"missing_referent", "observed_relative_deadline"}:
            passed = not output.facts and any(a.outcome == "context_insufficient"
                                             for a in output.assessments)
        else:
            passed = not output.facts and bool(output.assessments)
        results.append({"case": name, "passed": passed, "targets": targets, "history": history,
                        "output": output.model_dump(mode="json")})
        print(name, "PASS" if passed else "FAIL", flush=True)
    path = Path("reports/add_context_evaluation.json")
    path.write_text(json.dumps({"prompt_version": PROMPT_VERSION,
                                "executed_at": datetime.now(UTC).isoformat(),
                                "model": settings.dashscope_model, "cases": results},
                               ensure_ascii=False, indent=2) + "\n")
    if not all(result["passed"] for result in results):
        raise SystemExit(
            "Add context evaluation failed; inspect reports/add_context_evaluation.json")


if __name__ == "__main__":
    main()
