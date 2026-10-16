from __future__ import annotations

import argparse
import os
from pathlib import Path

from migration_tools.honcho.source import (
    build_profile,
    create_source_engine,
    readonly_connection,
    write_json,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile a Honcho database in read-only mode.")
    parser.add_argument("--output", type=Path, default=Path("reports/honcho_profile.json"))
    args = parser.parse_args()
    database_url = os.environ.get("HONCHO_DATABASE_URL")
    if not database_url:
        parser.error("HONCHO_DATABASE_URL must be set")

    engine = create_source_engine(database_url)
    try:
        with readonly_connection(engine) as connection:
            report = build_profile(connection)
        write_json(args.output, report)
        print(args.output)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
