"""Store independent profile statements and rebuild legacy generated properties.

Downgrade requires converting keyless statements first; it never deletes them.
"""

from alembic import op

revision = "0005_atomic_profiles"
down_revision = "0004_business_scenes"
branch_labels = None
depends_on = None

LEGACY_KEYS_SQL = (
    "('姓名', '职位', '邮件回复偏好', '输出要求', 'identity.preferred_name', "
    "'pref.email.reply_style', 'pref.output.requirements')"
)


def upgrade() -> None:
    op.execute("ALTER TABLE profile_property ALTER COLUMN property_key DROP NOT NULL")
    op.execute("DROP INDEX uq_profile_property_active_key")
    op.execute(
        "CREATE UNIQUE INDEX uq_profile_property_active_key "
        "ON profile_property (tenant_id, user_id, business_domains, property_key) "
        "NULLS NOT DISTINCT WHERE status = 'active' AND property_key IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE profile_property ADD CONSTRAINT ck_profile_statement_value CHECK "
        "(property_key IS NOT NULL OR (value_type = 'string' "
        "AND jsonb_typeof(property_value) = 'string' "
        "AND length(btrim(property_value #>> '{}')) > 0))"
    )
    op.execute(
        "CREATE INDEX ix_profile_property_active_user "
        "ON profile_property (tenant_id, user_id) WHERE status = 'active'"
    )
    op.execute(
        f"""
        INSERT INTO memory_task (
            id, tenant_id, task_type, target_type, target_id, user_id,
            idempotency_key, payload
        )
        SELECT 'tsk_' || md5(tenant_id || ':atomic_profiles:' || user_id),
               tenant_id, 'profile_rebuild', 'subject', user_id, user_id,
               'profile_rebuild:v3:migration0005:' || user_id,
               '{{"reason":"ATOMIC_PROFILE_MIGRATION"}}'::jsonb
        FROM profile_property
        WHERE status = 'active' AND property_key IN {LEGACY_KEYS_SQL}
        GROUP BY tenant_id, user_id
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO memory_audit_log (
            tenant_id, action, target_type, target_id, operator_type, operator_id,
            reason_code, state_before, state_after
        )
        SELECT tenant_id, 'INVALIDATE', 'profile', id, 'system', 'memory_cmic',
               'ATOMIC_PROFILE_MIGRATION',
               jsonb_build_object('status', status, 'version', version),
               jsonb_build_object('status', 'invalidated', 'version', version + 1)
        FROM profile_property
        WHERE status = 'active' AND property_key IN {LEGACY_KEYS_SQL}
        """
    )
    op.execute(
        f"""
        UPDATE memory_evidence AS evidence
        SET status = 'invalidated', invalidated_at = CURRENT_TIMESTAMP
        FROM profile_property AS profile
        WHERE evidence.tenant_id = profile.tenant_id
          AND evidence.downstream_profile_id = profile.id
          AND evidence.status = 'active' AND profile.status = 'active'
          AND profile.property_key IN {LEGACY_KEYS_SQL}
        """
    )
    op.execute(
        f"""
        UPDATE profile_property
        SET status = 'invalidated', version = version + 1, updated_at = CURRENT_TIMESTAMP
        WHERE status = 'active' AND property_key IN {LEGACY_KEYS_SQL}
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE profile_property ALTER COLUMN property_key SET NOT NULL")
    op.execute("ALTER TABLE profile_property DROP CONSTRAINT ck_profile_statement_value")
    op.execute("DROP INDEX ix_profile_property_active_user")
    op.execute("DROP INDEX uq_profile_property_active_key")
    op.execute(
        "CREATE UNIQUE INDEX uq_profile_property_active_key "
        "ON profile_property (tenant_id, user_id, business_domains, property_key) "
        "NULLS NOT DISTINCT WHERE status = 'active'"
    )
