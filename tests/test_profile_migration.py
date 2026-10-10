from __future__ import annotations

from pathlib import Path
from runpy import run_path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_schema_constraints import insert_memory, insert_profile, insert_source

from memory_cmic.models import Base


def test_profile_schema_matches_metadata(connection):
    context = MigrationContext.configure(
        connection,
        opts={
            "include_schemas": True,
            "include_name": lambda name, kind, parent: (
                kind != "schema" or name in {None, connection.dialect.default_schema_name}
            ),
            "include_object": lambda obj, name, kind, reflected, compare_to: (
                kind != "table" or name == "profile_property"
            ),
        },
    )
    assert compare_metadata(context, Base.metadata) == []


def test_profile_governance_migration_coalesces_legacy_jobs_and_guards_downgrade(connection):
    migration = run_path(
        Path(__file__).parents[1] / "migrations" / "versions" / "0007_profile_governance.py"
    )
    with Operations.context(MigrationContext.configure(connection)):
        migration["downgrade"]()
        insert_profile(connection, profile_id="prf_governance_migration")
        for status in ("pending", "processing"):
            for index in range(2):
                task_id = f"tsk_migration_{status}_{index}"
                connection.execute(text("""
                    INSERT INTO memory_task (
                        id, tenant_id, task_type, target_type, target_id, idempotency_key, status
                    ) VALUES (
                        :id, 'tenant_a', 'profile_rebuild', 'profile',
                        'prf_governance_migration', :id, :status
                    )
                """), {"id": task_id, "status": status})
        connection.execute(text("""
            INSERT INTO memory_task (
                id, tenant_id, task_type, target_type, target_id, idempotency_key
            ) VALUES (
                'tsk_migration_subject', 'tenant_a', 'profile_rebuild', 'subject',
                'user_2', 'tsk_migration_subject'
            )
        """))
        connection.execute(text("INSERT INTO governance_policy (tenant_id) VALUES ('tenant_a')"))
        migration["upgrade"]()
        assert connection.execute(text("""
            SELECT user_id, status, count(*) FROM memory_task
            WHERE tenant_id = 'tenant_a' GROUP BY user_id, status ORDER BY user_id, status
        """)).all() == [
            ("user_1", "cancelled", 2), ("user_1", "pending", 1),
            ("user_1", "processing", 1), ("user_2", "pending", 1),
        ]
        assert connection.execute(text("""
            SELECT generate_inferences FROM governance_policy WHERE tenant_id = 'tenant_a'
        """)).scalar_one() is True
        assert connection.execute(text("""
            SELECT assertion_kind FROM profile_property WHERE id = 'prf_governance_migration'
        """)).scalar_one() == "explicit"
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text("""
                INSERT INTO memory_task (id, tenant_id, task_type, target_type,
                                         target_id, user_id, idempotency_key)
                VALUES ('tsk_migration_duplicate', 'tenant_a', 'profile_rebuild', 'subject',
                        'user_1', 'user_1', 'tsk_migration_duplicate')
            """))
        connection.execute(text("""
            UPDATE profile_property SET assertion_kind = 'inferred'
            WHERE id = 'prf_governance_migration'
        """))
        with pytest.raises(DBAPIError, match="inferred profiles"), connection.begin_nested():
            migration["downgrade"]()


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
