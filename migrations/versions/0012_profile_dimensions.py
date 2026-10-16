"""Tenant-owned profile dimensions and versioned rebuilds.

Defaults are frozen here so later template edits do not change historical migrations.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0012_profile_dimensions"
down_revision = "0011_memory_deleted"
branch_labels = None
depends_on = None

DEFAULT_DIMENSIONS = [
    {
        "key": "identity",
        "name": "基本身份",
        "description": (
            "用户本人的姓名、称呼、明确自述的性别、出生日期与年龄。区分姓名和偏好称"
            "呼；年龄必须保留可靠的时间条件，不猜测出生日期或性别。"
        ),
        "enabled": True,
    },
    {
        "key": "residence",
        "name": "居住与生活背景",
        "description": (
            "用户的常住地区、长期居住方式及共同生活安排。区分当前常住地、籍贯、工作"
            "地和过去居住经历，不把旅行或出差地当作常住地。"
        ),
        "enabled": True,
    },
    {
        "key": "education",
        "name": "教育与学习背景",
        "description": (
            "用户已获得的学历、专业、学校、当前在读阶段及长期学习方向。学生身份归入"
            "本维度；明确区分已毕业与在读，不将过去就读经历改写为当前在读。"
        ),
        "enabled": True,
    },
    {
        "key": "occupation",
        "name": "职业与当前角色",
        "description": (
            "用户当前职业、岗位、行业及持续影响生活的角色，例如求职者、退休人员和家"
            "庭照护者。允许兼任，保留阶段条件；排除临时项目职责，学生身份归入教育维"
            "度。"
        ),
        "enabled": True,
    },
    {
        "key": "relationships",
        "name": "家庭与重要社会关系",
        "description": (
            "用户明确提及的亲属、伴侣、好友等重要关系及关系人的长期背景、偏好和限制"
            "。每条保留关系人的明确主语；不得归为用户本人的属性，不猜测人物对应关系"
            "。"
        ),
        "enabled": True,
    },
    {
        "key": "social_habits",
        "name": "社交习惯",
        "description": (
            "用户本人的长期社交方式、频率与互动偏好。排除关系人的偏好及一次性活动安排。"
        ),
        "enabled": True,
    },
    {
        "key": "diet",
        "name": "饮食偏好与习惯",
        "description": (
            "用户本人的长期口味、饮食选择与用餐习惯。不包含关系人的饮食信息；过敏和"
            "明确禁忌归入饮食限制。"
        ),
        "enabled": True,
    },
    {
        "key": "dietary_restrictions",
        "name": "饮食限制",
        "description": (
            "用户本人明确表达的食物过敏、禁忌及其他饮食限制。不把不喜欢改写成不能吃"
            "，不包含关系人的限制。"
        ),
        "enabled": True,
    },
    {
        "key": "preferences",
        "name": "兴趣与其他个人偏好",
        "description": (
            "用户本人的长期兴趣爱好及未被专门维度覆盖的选择偏好，例如阅读、娱乐和出"
            "行。不重复归入饮食、社交、沟通等专门维度，不包含关系人的偏好。"
        ),
        "enabled": True,
    },
    {
        "key": "communication",
        "name": "沟通与协作偏好",
        "description": (
            "用户对交流、解释、反馈和工作协作方式的长期要求。保留文档类型、业务场景"
            "、否定及例外条件，不把场景限定要求扩大成通用偏好。"
        ),
        "enabled": True,
    },
    {
        "key": "other",
        "name": "其他长期背景",
        "description": (
            "有明确依据和持续背景价值、但不属于其他启用维度的信息。仅在没有更具体维"
            "度时使用，排除临时任务、一次性事件和未经支持的推断。"
        ),
        "enabled": True,
    },
]


def _task_types(include_config: bool) -> str:
    values = "'fact_extract', 'inference_derive', 'profile_rebuild', 'dependency_recheck', "
    values += "'vector_upsert', 'vector_delete', 'ttl_expire', 'consolidate'"
    return "task_type IN (" + values + (", 'profile_config_apply'" if include_config else "") + ")"


def _audit_types(include_config: bool) -> str:
    values = "'source', 'memory', 'evidence', 'profile', 'embedding', 'task', 'governance_policy'"
    return "target_type IN (" + values + (", 'profile_config'" if include_config else "") + ")"


def upgrade() -> None:
    op.create_table(
        "tenant_profile_config",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("dimensions", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("version > 0", name="ck_profile_config_version"),
        sa.CheckConstraint(
            "jsonb_typeof(dimensions) = 'array'", name="ck_profile_config_dimensions"
        ),
    )
    op.add_column("governance_subject_state", sa.Column("profile_config_version", sa.Integer()))
    op.drop_index("uq_profile_property_active_key", table_name="profile_property")
    op.drop_constraint("ck_task_type", "memory_task", type_="check")
    op.create_check_constraint("ck_task_type", "memory_task", _task_types(True))
    op.drop_constraint("ck_audit_target_type", "memory_audit_log", type_="check")
    op.create_check_constraint("ck_audit_target_type", "memory_audit_log", _audit_types(True))
    defaults = json.dumps(DEFAULT_DIMENSIONS, ensure_ascii=False).replace("'", "''")
    op.execute(f"""
        INSERT INTO tenant_profile_config (tenant_id, dimensions)
        SELECT tenant_id, '{defaults}'::jsonb FROM (
            SELECT tenant_id FROM source_record UNION SELECT tenant_id FROM memory_item
            UNION SELECT tenant_id FROM profile_property
            UNION SELECT tenant_id FROM governance_subject_state
            UNION SELECT tenant_id FROM governance_policy UNION SELECT tenant_id FROM memory_task
        ) tenants
    """)
    op.execute("""
        INSERT INTO memory_audit_log (
            tenant_id, action, target_type, target_id, operator_type, operator_id,
            reason_code, state_before, state_after
        )
        SELECT tenant_id, 'INVALIDATE', 'profile', id, 'system', 'memory_cmic',
               'PROFILE_DIMENSION_MIGRATION',
               jsonb_build_object('status', status, 'version', version, 'key', property_key),
               jsonb_build_object('status', 'invalidated', 'version', version + 1)
        FROM profile_property WHERE status = 'active'
    """)
    op.execute("""
        UPDATE memory_evidence AS evidence
        SET status = 'invalidated', invalidated_at = CURRENT_TIMESTAMP
        FROM profile_property AS profile
        WHERE evidence.tenant_id = profile.tenant_id
          AND evidence.downstream_profile_id = profile.id
          AND evidence.status = 'active' AND profile.status = 'active'
    """)
    op.execute("""
        UPDATE profile_property SET status = 'invalidated', version = version + 1,
               updated_at = CURRENT_TIMESTAMP WHERE status = 'active'
    """)
    op.create_check_constraint(
        "ck_profile_active_dimension",
        "profile_property",
        "status <> 'active' OR (property_key IS NOT NULL "
        "AND length(btrim(property_key)) > 0 AND value_type = 'string' "
        "AND jsonb_typeof(property_value) = 'string' "
        "AND length(btrim(property_value #>> '{}')) > 0)",
    )
    op.execute("""
        UPDATE governance_subject_state
        SET profile_input = NULL, profile_evaluated_input = NULL
    """)
    op.execute("""
        INSERT INTO memory_task (
            id, tenant_id, task_type, target_type, target_id, idempotency_key, payload
        )
        SELECT 'tsk_' || md5(tenant_id || ':profile_dimensions:1'), tenant_id,
               'profile_config_apply', 'batch', tenant_id, 'profile_config_apply:1',
               jsonb_build_object('config_version', 1)
        FROM tenant_profile_config
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM memory_task WHERE task_type = 'profile_config_apply')
               OR EXISTS (SELECT 1 FROM memory_audit_log WHERE target_type = 'profile_config')
               OR EXISTS (SELECT 1 FROM tenant_profile_config WHERE version > 1) THEN
                RAISE EXCEPTION 'profile dimension configuration and task history require review';
            END IF;
        END $$
    """)
    # Multiple statements in one dimension deliberately prevent restoring single-value uniqueness.
    op.execute("""
        CREATE UNIQUE INDEX uq_profile_property_active_key
        ON profile_property (tenant_id, user_id, business_domains, property_key)
        NULLS NOT DISTINCT WHERE status = 'active' AND property_key IS NOT NULL
    """)
    op.drop_constraint("ck_profile_active_dimension", "profile_property", type_="check")
    op.drop_constraint("ck_task_type", "memory_task", type_="check")
    op.create_check_constraint("ck_task_type", "memory_task", _task_types(False))
    op.drop_constraint("ck_audit_target_type", "memory_audit_log", type_="check")
    op.create_check_constraint("ck_audit_target_type", "memory_audit_log", _audit_types(False))
    op.drop_column("governance_subject_state", "profile_config_version")
    op.drop_table("tenant_profile_config")
