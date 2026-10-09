from __future__ import annotations

from pathlib import Path
from runpy import run_path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_schema_constraints import insert_memory, insert_profile, insert_source

from memory_cmic.models import Base


def test_profile_schema_matches_metadata(connection):
    context = MigrationContext.configure(
        connection,
        opts={
            "include_schemas": True,
            "include_object": lambda obj, name, kind, reflected, compare_to: (
                kind != "table" or name == "profile_property"
            ),
        },
    )
    assert compare_metadata(context, Base.metadata) == []


def test_atomic_profile_migration_invalidates_and_enqueues_rebuild(connection):
    migration = run_path(
        Path(__file__).parents[1] / "migrations" / "versions" / "0005_atomic_profiles.py"
    )
    with Operations.context(MigrationContext.configure(connection)):
        migration["downgrade"]()
        insert_source(connection, source_id="src_profile_migration")
        insert_memory(connection, memory_id="mem_profile_migration")
        for profile_id, key in (
            ("prf_legacy", "输出要求"),
            ("prf_english", "pref.email.reply_style"),
            ("prf_fixed", "residence"),
        ):
            insert_profile(connection, profile_id=profile_id, property_key=key)
        connection.execute(
            text("""
            INSERT INTO memory_evidence (
                id, tenant_id, relationship_type, evidence_group_id,
                upstream_memory_id, downstream_profile_id, upstream_version
            ) VALUES (
                'evi_profile_migration', 'tenant_a', 'profile_basis', 'evg_profile_migration',
                'mem_profile_migration', 'prf_legacy', 1
            )
        """)
        )
        migration["upgrade"]()
        profiles = connection.execute(
            text(
                "SELECT id, status, version FROM profile_property "
                "WHERE tenant_id = 'tenant_a' ORDER BY id"
            )
        ).all()
        assert profiles == [
            ("prf_english", "invalidated", 2),
            ("prf_fixed", "active", 1),
            ("prf_legacy", "invalidated", 2),
        ]
        assert (
            connection.execute(
                text("SELECT status FROM memory_evidence WHERE id = 'evi_profile_migration'")
            ).scalar_one()
            == "invalidated"
        )
        assert connection.execute(
            text("""
            SELECT target_type, target_id, user_id, status FROM memory_task
            WHERE task_type = 'profile_rebuild' AND tenant_id = 'tenant_a'
        """)
        ).all() == [("subject", "user_1", "user_1", "pending")]
        assert (
            connection.execute(
                text("""
            SELECT count(*) FROM memory_audit_log
            WHERE reason_code = 'ATOMIC_PROFILE_MIGRATION' AND tenant_id = 'tenant_a'
        """)
            ).scalar_one()
            == 2
        )
        insert_profile(connection, profile_id="prf_atomic_1", property_key=None)
        insert_profile(connection, profile_id="prf_atomic_2", property_key=None)
        with pytest.raises(IntegrityError), connection.begin_nested():
            migration["downgrade"]()
        assert (
            connection.execute(
                text("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'profile_property'
              AND column_name = 'property_key'
        """)
            ).scalar_one()
            == "YES"
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM profile_property "
                    "WHERE property_key IS NULL AND tenant_id = 'tenant_a'"
                )
            ).scalar_one()
            == 2
        )
