from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session

from memory_cmic.fixture_loader import apply_lifecycle_fixture, load_fixture


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    database_url = os.environ.get("MEMORY_DATABASE_URL")
    if not database_url:
        pytest.exit("MEMORY_DATABASE_URL must be set for database tests")

    value = create_engine(database_url, pool_pre_ping=True)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as value:
        transaction = value.begin()
        try:
            yield value
        finally:
            transaction.rollback()


@pytest.fixture(scope="session")
def lifecycle_fixture_data() -> dict[str, Any]:
    return load_fixture(Path(__file__).parents[1] / "datas" / "lifecycle_cases.json")


@pytest.fixture
def session(connection: Connection) -> Iterator[Session]:
    with Session(bind=connection, expire_on_commit=False) as value:
        yield value


@pytest.fixture
def lifecycle_session(
    session: Session, lifecycle_fixture_data: dict[str, Any]
) -> Session:
    apply_lifecycle_fixture(session, lifecycle_fixture_data)
    return session
