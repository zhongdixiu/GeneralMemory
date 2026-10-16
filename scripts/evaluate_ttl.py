"""Check TTL extraction on fictional statements using the configured model."""

from __future__ import annotations

import json
from datetime import datetime

from memory_cmic.providers import PROMPT_VERSION, QwenFactModel, candidate_skip_reason
from memory_cmic.settings import Settings

CASES = [
    ("identity", "我叫林舟，职业是审计师。", {"long_term"}, 2, None),
    ("lasting_preference", "以后邮件回复都简洁一些。", {"long_term"}, 1, None),
    ("phase_responsibility", "本阶段由我负责星河项目材料审核。", {"temporary"}, 1, None),
    ("regular_fact", "邮件归档用的共享目录是“季度归档”。", {"regular"}, 1, None),
    ("phase_preference", "本阶段邮件回复都用详细格式。", {"temporary"}, 1, None),
    (
        "weekly_deadline", "本周邮件回复都用详细格式。", {"temporary"}, 1,
        datetime.fromisoformat("2026-10-12T00:00:00+08:00"),
    ),
    ("execution_progress", "文件正在生成，邮件已经发送完成。", set(), 0, None),
    (
        "mixed_lifetimes", "以后邮件都先给结论；但本阶段邮件回复详细一些。",
        {"long_term", "temporary"}, 2, None,
    ),
]


def main() -> None:
    settings = Settings.from_env()
    if not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY must be set")
    model = QwenFactModel(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
        output_tokens=settings.extraction_budget.output_tokens,
    )
    print(f"prompt={PROMPT_VERSION}, data=fictional", flush=True)
    failed = 0
    for name, content, kinds, count, deadline in CASES:
        facts = model.extract(targets=[{
            "message_id": name,
            "role": "user",
            "content": content,
            "occurred_at": "2026-10-05T00:30:00+08:00",
        }], history=[])
        passed = (
            len(facts) == count
            and {fact.retention_kind for fact in facts} == kinds
            and all(fact.expired_at == deadline for fact in facts)
            and all(candidate_skip_reason(fact) is None for fact in facts)
        )
        failed += not passed
        print(json.dumps({
            "case": name,
            "passed": passed,
            "facts": [fact.model_dump(mode="json") for fact in facts],
        }, ensure_ascii=False), flush=True)
    print(f"{len(CASES) - failed}/{len(CASES)} passed", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
