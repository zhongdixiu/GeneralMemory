from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine


def get_database_url() -> str:
    database_url = os.environ.get("MEMORY_DATABASE_URL")
    if not database_url:
        raise RuntimeError("MEMORY_DATABASE_URL must be set")
    return database_url


def create_database_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_database_url(), pool_pre_ping=True)
