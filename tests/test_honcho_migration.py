from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.models import MemoryEvidence, MemoryItem, SourceRecord
from migration_tools.honcho.migration import apply_migration, validate_migration
from migration_tools.honcho.source import MigrationBatch, load_sanitized_batch

SAMPLE_PATH = Path(__file__).parents[1] / "datas" / "honcho_sanitized_sample.json"


@pytest.fixture
def honcho_batch() -> MigrationBatch:
    return load_sanitized_batch(SAMPLE_PATH)


def test_t13_repeated_honcho_migration_adds_no_records(
    session: Session, honcho_batch: MigrationBatch
) -> None:
    first = apply_migration(session, honcho_batch, migration_batch_id="batch_honcho_test_01")
    second = apply_migration(session, honcho_batch, migration_batch_id="batch_honcho_test_02")

    assert first["inserted"] == {
        "source_records": 3,
        "memory_items": 2,
        "evidence_edges": 3,
        "legacy_import_sources": 1,
        "tasks": 2,
    }
    assert second["inserted"] == {
        "source_records": 0,
        "memory_items": 0,
        "evidence_edges": 0,
        "legacy_import_sources": 0,
        "tasks": 0,
    }
    validation = validate_migration(session, honcho_batch)
    assert validation["passed"] is True
    assert validation["actual"]["orphan_evidence"] == 0
    assert validation["actual"]["vector_tasks"] == 2
    assert validation["actual"]["audit_logs"] == 8


def test_t14_missing_honcho_source_becomes_marked_legacy_import(
    session: Session, honcho_batch: MigrationBatch
) -> None:
    apply_migration(session, honcho_batch, migration_batch_id="batch_honcho_test_03")

    legacy_source = session.scalars(
        select(SourceRecord).where(
            SourceRecord.tenant_id == honcho_batch.tenant_id,
            SourceRecord.source_type == "legacy_import",
        )
    ).one()
    assert legacy_source.metadata_json["missing_honcho_document_ids"] == ["missing_document_001"]
    assert legacy_source.metadata_json["synthetic"] is True

    incomplete_memory = session.scalars(
        select(MemoryItem).where(
            MemoryItem.tenant_id == honcho_batch.tenant_id,
            MemoryItem.metadata_json["honcho_document_id"].astext == "sample_document_002",
        )
    ).one()
    assert incomplete_memory.metadata_json["evidence_quality"] == "legacy_incomplete"

    edges = session.scalars(
        select(MemoryEvidence).where(
            MemoryEvidence.tenant_id == honcho_batch.tenant_id,
            MemoryEvidence.downstream_memory_id == incomplete_memory.id,
        )
    ).all()
    assert {edge.relationship_type for edge in edges} == {"supports", "derives"}
    assert {edge.evidence_group_id for edge in edges} == {edges[0].evidence_group_id}


def test_unknown_subject_type_stops_migration(
    session: Session, honcho_batch: MigrationBatch
) -> None:
    invalid_batch = MigrationBatch(
        workspace=honcho_batch.workspace,
        tenant_id=honcho_batch.tenant_id,
        source_namespace=honcho_batch.source_namespace,
        sessions=honcho_batch.sessions,
        messages=honcho_batch.messages,
        documents=honcho_batch.documents,
        all_document_ids=honcho_batch.all_document_ids,
        peer_types={"sample_user": "user"},
    )

    with pytest.raises(KeyError, match="sample_agent"):
        apply_migration(session, invalid_batch, migration_batch_id="batch_honcho_test_04")
