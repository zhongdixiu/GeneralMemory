from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from memory_cmic.models import MemoryItem

RetentionKind = Literal["temporary", "regular", "long_term"]


def calculate_expired_at(
    *,
    retention_kind: RetentionKind,
    effective_at: datetime,
    explicit_expired_at: datetime | None,
) -> datetime | None:
    if explicit_expired_at is not None:
        return explicit_expired_at
    if retention_kind == "long_term":
        return None
    days = 30 if retention_kind == "temporary" else 180
    return effective_at + timedelta(days=days)


def explicit_expired_at(memory: MemoryItem) -> str | None:
    """Keep a system TTL out of duplicate comparisons; preserve legacy time boundaries."""
    metadata = memory.metadata_json or {}
    if "retention_kind" in metadata:
        return metadata["explicit_expired_at"]
    return memory.expired_at.isoformat() if memory.expired_at else None
