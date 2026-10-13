from datetime import UTC, datetime, timedelta

import pytest

from memory_cmic.models import MemoryItem, SourceRecord
from memory_cmic.ttl import calculate_expired_at, explicit_expired_at
from memory_cmic.worker import _source_message


@pytest.mark.parametrize("kind,days", [("temporary", 30), ("regular", 180), ("long_term", None)])
def test_default_expiry_uses_effective_time(kind, days):
    effective_at = datetime(2026, 9, 24, tzinfo=UTC)
    assert calculate_expired_at(
        retention_kind=kind, effective_at=effective_at, explicit_expired_at=None
    ) == (effective_at + timedelta(days=days) if days is not None else None)


@pytest.mark.parametrize("kind", ["temporary", "regular", "long_term"])
@pytest.mark.parametrize("days", [7, 365])
def test_explicit_deadline_overrides_default(kind, days):
    effective_at = datetime(2026, 9, 24, tzinfo=UTC)
    deadline = effective_at + timedelta(days=days)
    assert calculate_expired_at(
        retention_kind=kind, effective_at=effective_at, explicit_expired_at=deadline
    ) == deadline


def test_managed_ttl_is_not_an_explicit_deadline_and_legacy_expiry_is_preserved():
    expiry = datetime(2027, 1, 1, tzinfo=UTC)
    memory = MemoryItem(expired_at=expiry)
    assert explicit_expired_at(memory) == expiry.isoformat()
    memory.metadata_json = {"retention_kind": "regular", "explicit_expired_at": None}
    assert explicit_expired_at(memory) is None


def test_extraction_uses_beijing_and_keeps_stored_instant_for_legacy():
    source = SourceRecord(
        occurred_at=datetime(2026, 10, 4, 16, 30, tzinfo=UTC),
        received_at=datetime(2026, 10, 13, tzinfo=UTC),
        metadata_json={"original_occurred_at": "2026-10-05T00:30:00+08:00"},
    )
    assert _source_message(source)["occurred_at"] == "2026-10-05T00:30:00+08:00"
    source.metadata_json = None
    assert _source_message(source)["occurred_at"] == "2026-10-05T00:30:00+08:00"
