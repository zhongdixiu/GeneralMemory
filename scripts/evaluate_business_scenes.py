"""Run fictional scene normalization and task applicability cases with the configured model."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from memory_cmic.providers import PROMPT_VERSION, QwenFactModel, SiliconFlowEmbedder
from memory_cmic.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    if not settings.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required")
    model = QwenFactModel(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
        output_tokens=settings.extraction_budget.output_tokens,
        input_tokens=settings.extraction_budget.input_tokens,
    )
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "prompt_version": PROMPT_VERSION,
        "model": settings.dashscope_model,
        "data": "fictional",
        "quality_status": "pending_manual_review",
        "cases": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }

    def record(case: dict) -> None:
        report["cases"].append(case)
        usage = model.last_usage or {}
        for key in report["usage"]:
            report["usage"][key] += usage.get(key, 0)

    scenes = [
        {"name": "email", "definition": "邮件的阅读、撰写、回复、处理和整理", "aliases": ["邮件"]},
        {"name": "方案编写", "definition": "编写项目方案文稿", "aliases": []},
    ]
    facts = model.extract(
        targets=[
            {
                "message_id": "source_global",
                "role": "user",
                "content": "我的称呼是林舟。",
                "occurred_at": "2026-10-01T09:00:00+08:00",
            },
            {
                "message_id": "source_proposal",
                "role": "user",
                "content": "写项目方案时，先列目标。",
                "occurred_at": "2026-10-01T09:01:00+08:00",
            },
        ],
        history=[],
    )
    record(
        {
            "id": "source_independent_assignment",
            "source_system": "email_agent",
            "facts": [fact.model_dump(mode="json") for fact in facts],
            "expected": "称呼业务范围为 null；项目方案偏好属于编写场景",
        }
    )
    decisions = model.normalize_scenes(labels=["项目方案写作", "方案评审"], scenes=scenes)
    record(
        {
            "id": "scene_reuse_and_distinction",
            "decisions": [decision.__dict__ for decision in decisions],
            "expected": "项目方案写作复用方案编写；方案评审另建场景",
        }
    )
    candidates = [
        {
            "id": "general",
            "memory": "写邮件先列要点，并使用中文。",
            "business_domains": ["email"],
            "project_context": None,
        },
        {
            "id": "overseas",
            "memory": "给海外客户写邮件时使用英文。",
            "business_domains": ["email"],
            "project_context": None,
        },
        {
            "id": "disk",
            "memory": "整理云盘文件时按日期排序。",
            "business_domains": ["disk"],
            "project_context": None,
        },
    ]
    if settings.siliconflow_api_key:
        embedder = SiliconFlowEmbedder(
            api_key=settings.siliconflow_api_key,
            base_url=settings.siliconflow_base_url,
            model_id=settings.siliconflow_embedding_model,
        )
        texts = ["给海外客户写邮件", *[scene["definition"] for scene in scenes]]
        vectors = embedder.embed(texts)
        query_vector = vectors[0]
        ranks = sorted(
            (
                {
                    "name": scene["name"],
                    "similarity": sum(a * b for a, b in zip(query_vector, vector, strict=True)),
                }
                for scene, vector in zip(scenes, vectors[1:], strict=True)
            ),
            key=lambda item: -item["similarity"],
        )
        report["embedding_scene_ranking"] = ranks
        report["embedding_calls"] = 1
    for case_id, query, expected in (
        ("overseas_email", "给海外客户写一封邮件，先列要点。", ["general", "overseas"]),
        ("unknown_recipient", "帮我写一封邮件，先列要点。", ["general", "overseas"]),
        ("unrelated_disk", "整理云盘文件", ["disk"]),
        (
            "multiple_scenes",
            "帮我写一封邮件并整理云盘文件，邮件先列要点。",
            ["general", "overseas", "disk"],
        ),
    ):
        result = model.select_for_query(query=query, candidates=candidates)
        record(
            {
                "id": case_id,
                "query": query,
                "decisions": result,
                "expected_included": expected,
            }
        )
    output = Path("reports/business_scene_real_model_evaluation.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
