from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from migration_tools.honcho.cli import add_source_arguments, load_batch, target_engine
from migration_tools.honcho.migration import validate_migration
from migration_tools.honcho.source import (
    readonly_connection,
    source_fingerprint,
    write_json,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    validation = report["migration_validation"]
    second = report["second_run_inserted"]
    source_unchanged = report["source_unchanged"]
    status = "通过" if report["passed"] else "失败"
    lines = [
        "# Honcho 小批量迁移验证报告",
        "",
        f"> 结果：{status}",
        "",
        "## 对账",
        "",
        "| 项目 | 期望 | 实际 |",
        "| --- | ---: | ---: |",
    ]
    for key in (
        "source_records",
        "memory_items",
        "evidence_edges",
        "external_mappings",
        "vector_tasks",
        "audit_logs",
    ):
        lines.append(f"| `{key}` | {validation['expected'][key]} | {validation['actual'][key]} |")
    lines.extend(
        [
            f"| `orphan_evidence` | 0 | {validation['actual']['orphan_evidence']} |",
            "",
            "## 幂等与源库保护",
            "",
            f"- 第二次迁移新增 source：{second['source_records']}；",
            f"- 第二次迁移新增 memory：{second['memory_items']}；",
            f"- 第二次迁移新增 evidence：{second['evidence_edges']}；",
            f"- Honcho 源库指纹不变：{'是' if source_unchanged else '否'}；",
            "- Honcho 会话强制 `transaction_read_only=on`；",
            "- 旧 embedding、queue 和锁状态未迁移。",
            "",
            "## 已确认的实际语义偏差",
            "",
            "- `documents.internal_metadata.message_ids` 指向 `messages.id`，迁移为 `supports`；",
            "- `documents.source_ids` 主要指向其他 `documents.id`，迁移为 `derives`；",
            "- 无法匹配的历史 document 引用由带缺失 ID 的 `legacy_import` 来源保守承接；",
            "- 源库只有两个 session 含 document，无法形成 5～10 个 document session，"
            "报告保留此偏差。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a migrated Honcho small batch.")
    add_source_arguments(parser)
    parser.add_argument("--profile", type=Path, default=Path("reports/honcho_profile.json"))
    parser.add_argument(
        "--second-run-report",
        type=Path,
        default=Path("reports/honcho_migration_second.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/honcho_migration_validation.json")
    )
    parser.add_argument("--markdown", type=Path, default=Path("reports/honcho_migration_report.md"))
    args = parser.parse_args()

    profile = _load_json(args.profile)
    second_run = _load_json(args.second_run_report)
    batch, source_engine = load_batch(args)
    try:
        with readonly_connection(source_engine) as connection:
            current_fingerprint = source_fingerprint(connection)
        engine = target_engine()
        try:
            with Session(engine) as session:
                validation = validate_migration(session, batch)
        finally:
            engine.dispose()

        second_inserted = second_run["inserted"]
        idempotent = all(
            second_inserted[key] == 0
            for key in ("source_records", "memory_items", "evidence_edges", "tasks")
        )
        source_unchanged = current_fingerprint == profile["source_fingerprint"]
        report = {
            "migration_validation": validation,
            "second_run_inserted": second_inserted,
            "idempotent": idempotent,
            "source_unchanged": source_unchanged,
            "passed": validation["passed"] and idempotent and source_unchanged,
        }
        write_json(args.output, report)
        _write_markdown(args.markdown, report)
        print(args.output)
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        source_engine.dispose()


if __name__ == "__main__":
    main()
