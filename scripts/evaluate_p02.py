"""Run the authorized fictional P02 cases; record model outputs for manual review."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from memory_cmic.providers import (
    PROMPT_VERSION,
    QwenFactModel,
    SiliconFlowEmbedder,
    candidate_skip_reason,
)
from memory_cmic.settings import Settings
from memory_cmic.worker import _check_governance, _compatible_business


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "datas/p02_fact_evaluation_cases.json").read_text())
    settings = Settings.from_env()
    if not settings.dashscope_api_key or not settings.siliconflow_api_key:
        raise RuntimeError("evaluation requires configured model and embedding credentials")
    model = QwenFactModel(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
        output_tokens=settings.extraction_budget.output_tokens,
    )
    embedder = SiliconFlowEmbedder(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model_id=settings.siliconflow_embedding_model,
    )
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "prompt_version": PROMPT_VERSION,
        "dataset_version": cases["version"],
        "model": settings.dashscope_model,
        "embedding_model": embedder.model_id,
        "data": "fictional",
        "gate": {
            "misattribution": 0,
            "scope_errors": 0,
            "incorrect_supersession": 0,
            "other_check_pass_rate_min": 0.90,
        },
        "quality_status": "pending_manual_review",
        "cases": [],
    }
    output = root / "reports/p02_real_model_evaluation.json"
    for case in cases["cases"]:
        record = {"id": case["id"], "expected_checks": case["expected_checks"]}
        try:
            facts = model.extract(targets=case["targets"], history=case["history"])
            record["facts"] = [f.model_dump(mode="json") for f in facts]
            record["skipped"] = [
                {"memory": f.memory, "reason": candidate_skip_reason(f)}
                for f in facts
                if candidate_skip_reason(f)
            ]
            accepted = [f for f in facts if not candidate_skip_reason(f)]
            vectors = embedder.embed([f.memory for f in accepted])
            record["embedding_shapes"] = [len(v) for v in vectors]
            record["governance"] = []
            for fact in accepted:
                candidates = [
                    {
                        "id": m["id"],
                        "memory": m["content"],
                        "business_domains": m["business_domains"],
                        "project_context": m.get("project_context"),
                        "status": m["status"],
                        "effective_at": m.get("effective_at", "2026-09-01T00:00:00+00:00"),
                        "expired_at": m.get("expired_at"),
                        "conflict_group_id": None,
                        "temporal_kind": m.get("temporal_kind", "current"),
                        "source_occurred_at": m.get("source_occurred_at"),
                    }
                    for m in case["existing_facts"]
                    if _compatible_business(fact, m)
                    and m.get("project_context") == fact.project_context
                    and m["project_domains"] is None
                ]
                if candidates:
                    decision = model.resolve(
                        fact=fact, candidates=candidates, targets=case["targets"]
                    )
                    _check_governance(
                        decision, fact, candidates, SimpleNamespace(targets=case["targets"])
                    )
                    record["governance"].append({"memory": fact.memory, **decision.model_dump()})
            record["applicability"] = []
            for check in case.get("applicability_checks", []):
                candidates = [
                    {"id": f"candidate_{i}", **fact.model_dump(mode="json")}
                    for i, fact in enumerate(accepted)
                ]
                selected = model.select_applicable(
                    query=check["query"],
                    business_domain=check["business_domain"],
                    candidates=candidates,
                )
                expected = [f"candidate_{i}" for i in check["expected_candidate_indexes"]]
                record["applicability"].append(
                    {
                        **check,
                        "selected_ids": selected,
                        "expected_ids": expected,
                        "passed": set(selected) == set(expected),
                    }
                )
            record["execution_status"] = "ok"
        except Exception as exc:
            record["execution_status"] = "failed"
            record["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        report["cases"].append(record)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"{case['id']}: {record['execution_status']}", flush=True)


if __name__ == "__main__":
    main()
