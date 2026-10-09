"""Check memory wording with synthetic inputs against the configured live model."""

from __future__ import annotations

import json
import re

from memory_cmic.consolidation import QwenConsolidationModel
from memory_cmic.profile import QwenProfileModel
from memory_cmic.providers import PROMPT_VERSION, QwenFactModel
from memory_cmic.settings import Settings

CASES = [
    ("explicit_user_subject", "我喜欢吃年糕。", [], r"用户喜欢(?:吃)?年糕"),
    (
        "resolve_place_from_history",
        "我住在那边。",
        ["我最近搬到了杭州。"],
        r"用户(?:现在)?(?:居住|住)在杭州",
    ),
    ("missing_place_context", "我住在那边。", [], None),
    ("conditional_preference", "早餐时我喜欢吃年糕。", [], r"用户(?:在)?早餐时喜欢(?:吃)?年糕"),
    (
        "resolve_named_person",
        "以后邮件的审批都交给他负责。",
        ["同事李明负责邮件审批。"],
        r"(?:以后)?(?:邮件(?:的)?审批由李明负责|李明负责(?:以后)?(?:的)?邮件(?:的)?审批)",
    ),
    (
        "preserve_quoted_title",
        "我喜欢电影《我和我的祖国》。",
        [],
        r"用户喜欢(?:电影)?《我和我的祖国》",
    ),
]


def message(message_id: str, content: str) -> dict:
    return {
        "message_id": message_id,
        "role": "user",
        "content": content,
        "occurred_at": "2026-09-27T09:00:00+08:00",
    }


def main() -> None:
    settings = Settings.from_env()
    config = dict(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
        output_tokens=2048,
    )
    model = QwenFactModel(**config)
    results = []
    for name, content, history, expected in CASES:
        facts = model.extract(
            targets=[message("target", content)],
            history=[message(f"history_{i}", text) for i, text in enumerate(history)],
        )
        texts = [fact.memory for fact in facts]
        passed = (
            not texts
            if expected is None
            else (len(texts) == 1 and re.fullmatch(expected + r"[。.!！]?", texts[0]) is not None)
        )
        results.append({"case": name, "passed": passed, "memories": texts})

    decision = QwenConsolidationModel(**config).decide(
        {
            "anchor_id": "m1",
            "scenes": [],
            "rejected": [],
            "memories": [
                {
                    "id": f"m{i}",
                    "content": content,
                    "business_domains": None,
                    "project_domains": None,
                    "project_context": None,
                    "effective_at": "2026-09-01T00:00:00Z",
                    "expired_at": None,
                    "sources": [
                        {"id": f"s{i}", "text": content, "occurred_at": "2026-09-01T00:00:00Z"}
                    ],
                }
                for i, content in enumerate(
                    [
                        "做旅行计划时，我习惯先比较多个方案的优缺点再决定。",
                        "购买电脑前，我习惯对比几个型号的优缺点后再选择。",
                    ],
                    start=1,
                )
            ],
        }
    )
    results.append(
        {
            "case": "inference_has_explicit_subject",
            "passed": bool(decision.inferences)
            and all(
                "用户" in item.content
                and "我" not in item.content
                and item.business_domains is None
                and set(item.source_ids) == {"m1", "m2"}
                for item in decision.inferences
            ),
            "memories": [item.content for item in decision.inferences],
        }
    )
    properties = QwenProfileModel(**config).extract([{"id": "m1", "content": "我喜欢吃年糕。"}])
    results.append(
        {
            "case": "profile_resolves_legacy_first_person",
            "passed": len(properties) == 1
            and re.fullmatch(r"用户喜欢(?:吃)?年糕[。.!！]?", properties[0].value) is not None
            and properties[0].memory_id == "m1"
            and properties[0].quote in "我喜欢吃年糕。",
            "memories": [item.value for item in properties],
        }
    )
    print(
        json.dumps(
            {"prompt_version": PROMPT_VERSION, "cases": results}, ensure_ascii=False, indent=2
        )
    )
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
