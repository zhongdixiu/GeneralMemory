from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from migration_tools.honcho.cli import add_source_arguments, load_batch, target_engine
from migration_tools.honcho.migration import apply_migration
from migration_tools.honcho.source import batch_plan_report, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run or migrate a small Honcho batch.")
    add_source_arguments(parser)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--migration-batch-id")
    parser.add_argument(
        "--output", type=Path, default=Path("reports/honcho_migration_dry_run.json")
    )
    args = parser.parse_args()

    batch, source_engine = load_batch(args)
    try:
        if args.dry_run:
            report = {
                "mode": "dry-run",
                "generated_at": datetime.now(UTC),
                "target_written": False,
                "plan": batch_plan_report(batch),
            }
        else:
            migration_batch_id = args.migration_batch_id or datetime.now(UTC).strftime(
                "honcho_%Y%m%dT%H%M%SZ"
            )
            engine = target_engine()
            try:
                with Session(engine) as session, session.begin():
                    report = apply_migration(
                        session,
                        batch,
                        migration_batch_id=migration_batch_id,
                        embedding_model_id=os.environ.get(
                            "SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"
                        ),
                    )
            finally:
                engine.dispose()
            report["mode"] = "migrate"
            report["target_written"] = True
        write_json(args.output, report)
        print(args.output)
    finally:
        source_engine.dispose()


if __name__ == "__main__":
    main()
