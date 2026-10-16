from __future__ import annotations

import argparse
import os
from pathlib import Path

from memory_cmic.db import create_database_engine
from migration_tools.honcho.source import (
    MigrationBatch,
    MigrationConfig,
    create_source_engine,
    load_workspace_snapshot,
    readonly_connection,
    select_batch,
)

DEFAULT_CONFIG = Path("datas/honcho_migration_mapping.json")


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--workspace", default="hermes")
    parser.add_argument("--limit-sessions", type=int, default=10)
    parser.add_argument("--limit-messages", type=int, default=200)
    parser.add_argument("--limit-documents", type=int, default=50)


def require_url(variable: str) -> str:
    value = os.environ.get(variable)
    if not value:
        raise RuntimeError(f"{variable} must be set")
    return value


def load_batch(args: argparse.Namespace) -> tuple[MigrationBatch, object]:
    config = MigrationConfig.load(args.config)
    source_engine = create_source_engine(require_url("HONCHO_DATABASE_URL"))
    with readonly_connection(source_engine) as connection:
        snapshot = load_workspace_snapshot(connection, args.workspace)
        batch = select_batch(
            snapshot,
            config,
            workspace=args.workspace,
            limit_sessions=args.limit_sessions,
            limit_messages=args.limit_messages,
            limit_documents=args.limit_documents,
        )
    return batch, source_engine


def target_engine():
    return create_database_engine(require_url("MEMORY_DATABASE_URL"))
