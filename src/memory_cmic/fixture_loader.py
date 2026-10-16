from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from memory_cmic.deterministic_embedding import deterministic_embedding
from memory_cmic.models import (
    ExternalEntityMapping,
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.repositories import normalize_scope


def load_fixture(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as fixture_file:
        data = json.load(fixture_file)
    if data.get("synthetic_data_only") is not True:
        raise ValueError("lifecycle fixture must declare synthetic_data_only=true")
    return data


def _datetime_values(values: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(values)
    for key, value in result.items():
        if key.endswith("_at") and isinstance(value, str):
            result[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result


def _metadata_values(values: Mapping[str, Any]) -> dict[str, Any]:
    result = _datetime_values(values)
    if "metadata" in result:
        result["metadata_json"] = result.pop("metadata")
    return result


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _directive_text(directive: str, memory_text: str) -> str:
    if "$memory_text" in directive:
        return memory_text
    match = re.search(r"\('([^']*)'", directive)
    if match is None:
        raise ValueError(f"unsupported fixture directive: {directive}")
    return match.group(1)


def apply_lifecycle_fixture(session: Session, fixture: Mapping[str, Any]) -> None:
    memories_by_id = {record["id"]: record for record in fixture["memory_item"]}

    sources = []
    for record in fixture["source_record"]:
        values = _metadata_values(record)
        if values["content_hash"] == "$sha256(raw_content)":
            values["content_hash"] = _sha256(values["raw_content"])
        sources.append(SourceRecord(**values))
    session.add_all(sources)
    session.flush()

    memories = []
    for record in fixture["memory_item"]:
        values = _metadata_values(record)
        values["business_domains"] = normalize_scope(values.get("business_domains"))
        values["project_domains"] = normalize_scope(values.get("project_domains"))
        memories.append(MemoryItem(**values))
    session.add_all(memories)
    session.flush()

    profiles = []
    for record in fixture["profile_property"]:
        values = _datetime_values(record)
        values["business_domains"] = normalize_scope(values.get("business_domains"))
        profiles.append(ProfileProperty(**values))
    session.add_all(profiles)
    session.flush()

    session.add_all(
        MemoryEvidence(**_datetime_values(record)) for record in fixture["memory_evidence"]
    )
    session.flush()

    embeddings = []
    for record in fixture["memory_embedding"]:
        values = _datetime_values(record)
        memory = memories_by_id[values["memory_id"]]
        memory_text = f"{memory['summary']}\n{memory['content']}"
        text = _directive_text(values["embedding"], memory_text)
        values["embedding"] = deterministic_embedding(text, values["model_id"])
        values["content_hash"] = _sha256(_directive_text(values["content_hash"], memory_text))
        embeddings.append(MemoryEmbedding(**values))
    session.add_all(embeddings)
    session.flush()

    session.add_all(MemoryTask(**_datetime_values(record)) for record in fixture["memory_task"])
    session.add_all(
        MemoryAuditLog(**_datetime_values(record)) for record in fixture["memory_audit_log"]
    )
    session.add_all(
        ExternalEntityMapping(**_datetime_values(record))
        for record in fixture["external_entity_mapping"]
    )
    session.flush()
