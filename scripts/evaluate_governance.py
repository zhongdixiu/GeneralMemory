"""Small, synthetic live-model check for consolidation decisions.

Run after loading .env. No repository conversation data is sent to the provider.
"""

from __future__ import annotations

import json
import os

from memory_cmic.consolidation import QwenConsolidationModel
from memory_cmic.profile import QwenProfileModel


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
        "name": "general_decision_habit",
        "payload": {
            "anchor_id": "m1",
            "memories": [
                {
                    **memory(
                        "m1",
                        "做旅行计划时，我习惯先比较多个方案的优缺点再决定。",
                        source("s1", "做旅行计划时，我习惯先比较多个方案的优缺点再决定。", EARLIER),
                    ),
                    "business_domains": None,
                },
                {
                    **memory(
                        "m2",
                        "购买电脑前，我习惯对比几个型号的优缺点后再选择。",
                        source("s2", "购买电脑前，我习惯对比几个型号的优缺点后再选择。", LATER),
                    ),
                    "business_domains": None,
                },
            ],
            "rejected": [],
        },
        "merge": [],
        "invalidation": [],
        "inference": True,
        "domains": None,
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
            "phase": "facts",
            "scenes": [{"name": "report", "definition": "编写项目周报和复盘文档", "aliases": []}],
        }
        decision = model.decide(payload)
        actual = decision.model_dump()
        if not actual["merge_ids"] and not actual["invalidations"]:
            inferred = model.decide(
                {
                    **payload,
                    "phase": "inference",
                    "existing_inferences": [],
                    "generate_inferences": True,
                }
            )
            actual["inferences"] = [item.model_dump() for item in inferred.inferences]
        passed = (
            set(actual["merge_ids"]) == set(case["merge"])
            and {item["memory_id"] for item in actual["invalidations"]} == set(case["invalidation"])
            and bool(actual["inferences"]) == case["inference"]
            and all(
                item["business_domains"] == case.get("domains", ["report"])
                and len(set(item["source_ids"])) >= 2
                and set(item["source_ids"]) <= {m["id"] for m in payload["memories"]}
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
    members = [
        {
            **memory("m1", "邮件默认范围7天", source("s1", "邮件默认范围7天", EARLIER)),
            "status": "disputed",
            "conflict_group_id": "cg1",
        },
        {
            **memory("m2", "邮件默认范围3天", source("s2", "邮件默认范围3天", LATER)),
            "status": "disputed",
            "conflict_group_id": "cg1",
        },
    ]
    for clarified in (False, True):
        clarification = memory(
            "m3",
            "以后邮件统一按3天整理",
            source(
                "s3",
                "澄清：以后邮件统一按3天整理，之前7天的要求作废。",
                "2026-10-01T10:00:00+08:00",
            ),
        )
        decision = model.decide(
            {
                "phase": "conflicts",
                "anchor_id": "m1",
                "conflict_group_id": "cg1",
                "scenes": [],
                "rejected": [],
                "memories": [*members, clarification] if clarified else members,
            }
        )
        review = decision.conflict_review
        passed = bool(review and review.outcome == ("resolved" if clarified else "unresolved"))
        if clarified and review:
            passed = (
                passed
                and bool(review.facts)
                and all(
                    fact.source_id == "s3" and fact.quote in clarification["sources"][0]["text"]
                    for fact in review.facts
                )
            )
        results.append(
            {
                "case": "clarified_conflict" if clarified else "unresolved_conflict",
                "passed": passed,
                "decision": decision.model_dump(),
            }
        )

    profile_model = QwenProfileModel(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=os.environ.get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        model=os.environ.get("DASHSCOPE_MODEL", "qwen3.8-max"),
        output_tokens=2048,
    )
    for name, inputs, expected in [
        (
            "stable_inferred_profile",
            [
                {
                    "id": "i1",
                    "cognitive_type": "inference",
                    "content": "根据多次独立报告任务，用户倾向于先给结论再展开说明。",
                }
            ],
            {"i1"},
        ),
        (
            "temporary_request_not_profile",
            [
                {
                    "id": "f1",
                    "cognitive_type": "fact",
                    "content": "用户要求这次赶时间的邮件写短一些。",
                }
            ],
            set(),
        ),
        (
            "explicit_fact_overrides_inferred_pattern",
            [
                {"id": "i1", "cognitive_type": "inference", "content": "用户倾向于简洁邮件。"},
                {
                    "id": "f1",
                    "cognitive_type": "fact",
                    "content": "用户明确要求以后邮件都详细说明。",
                },
            ],
            {"f1"},
        ),
    ]:
        proposals = profile_model.extract(inputs)
        by_id = {item["id"]: item["content"] for item in inputs}
        results.append(
            {
                "case": name,
                "passed": {p.memory_id for p in proposals} == expected
                and all(p.quote in by_id[p.memory_id] for p in proposals),
                "properties": [p.model_dump() for p in proposals],
            }
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
