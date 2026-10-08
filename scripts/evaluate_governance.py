"""Small, synthetic live-model check for consolidation decisions.

Run after loading .env. No repository conversation data is sent to the provider.
"""

from __future__ import annotations

import json
import os

from memory_cmic.consolidation import QwenConsolidationModel


def source(source_id: str, text: str, occurred_at: str) -> dict:
    return {"id": source_id, "text": text, "occurred_at": occurred_at}


def memory(memory_id: str, content: str, item: dict) -> dict:
    return {
        "id": memory_id,
        "content": content,
        "effective_at": item["occurred_at"],
        "expired_at": None,
        "business_domains": ["report"],
        "project_domains": None,
        "project_context": None,
        "sources": [item],
    }


EARLIER = "2026-09-01T10:00:00+08:00"
LATER = "2026-09-27T10:00:00+08:00"

CASES = [
    {
        "name": "semantic_duplicate",
        "payload": {
            "anchor_id": "m1",
            "memories": [
                memory("m1", "以后周报先给结论", source("s1", "以后周报先给结论", EARLIER)),
                memory("m2", "周报把结论放在最前面", source("s2", "周报把结论放在最前面", LATER)),
            ],
            "rejected": [],
        },
        "merge": ["m2"],
        "invalidation": [],
        "inference": False,
    },
    {
        "name": "complementary_requirements",
        "payload": {
            "anchor_id": "m1",
            "memories": [
                memory("m1", "周报先给结论", source("s1", "周报先给结论", EARLIER)),
                memory("m2", "周报附上数据依据", source("s2", "周报附上数据依据", LATER)),
            ],
            "rejected": [],
        },
        "merge": [],
        "invalidation": [],
        "inference": False,
    },
    {
        "name": "explicit_correction",
        "payload": {
            "anchor_id": "m1",
            "memories": [
                memory("m1", "以后周报先给结论", source("s1", "以后周报先给结论", EARLIER)),
                memory("m2", "以后周报先列风险", source("s2", "以后周报改成先列风险", LATER)),
            ],
            "rejected": [],
        },
        "merge": [],
        "invalidation": ["m1"],
        "inference": False,
    },
    {
        "name": "scoped_work_habit",
        "payload": {
            "anchor_id": "m1",
            "memories": [
                memory(
                    "m1",
                    "项目复盘先写结论再列依据",
                    source("s1", "这次项目复盘请先写结论再列依据", EARLIER),
                ),
                memory(
                    "m2",
                    "周报先写结论再展开细节",
                    source("s2", "这次周报也请先写结论再展开细节", LATER),
                ),
            ],
            "rejected": [],
        },
        "merge": [],
        "invalidation": [],
        "inference": True,
    },
    {
        "name": "age_alone_is_not_invalidity",
        "payload": {
            "anchor_id": "m1",
            "memories": [
                memory("m1", "周报先列结论", source("s1", "周报先列结论", EARLIER)),
                memory("m2", "最近半年没有提交周报", source("s2", "最近半年没有提交周报", LATER)),
            ],
            "rejected": [],
        },
        "merge": [],
        "invalidation": [],
        "inference": False,
    },
    {
        "name": "single_incidental_action",
        "payload": {
            "anchor_id": "m1",
            "memories": [
                memory(
                    "m1", "今天赶时间，周报简单些", source("s1", "今天赶时间，周报简单些", EARLIER)
                ),
                memory("m2", "周报附数据来源", source("s2", "这次周报附上数据来源", LATER)),
            ],
            "rejected": [],
        },
        "merge": [],
        "invalidation": [],
        "inference": False,
    },
]


def main() -> None:
    model = QwenConsolidationModel(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=os.environ.get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        model=os.environ.get("DASHSCOPE_MODEL", "qwen3.8-max"),
        output_tokens=2048,
    )
    results = []
    for case in CASES:
        payload = {
            **case["payload"],
            "scenes": [
                {"name": "report", "definition": "编写项目周报和复盘文档", "aliases": []}
            ],
        }
        decision = model.decide(payload)
        actual = decision.model_dump()
        passed = (
            set(actual["merge_ids"]) == set(case["merge"])
            and {item["memory_id"] for item in actual["invalidations"]} == set(case["invalidation"])
            and bool(actual["inferences"]) == case["inference"]
            and all(
                item["scope_known"] and item["business_domains"] == ["report"]
                for item in actual["inferences"]
            )
        )
        results.append(
            {
                "case": case["name"],
                "passed": passed,
                "decision": actual,
                "usage": getattr(model, "last_usage", None),
            }
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
