from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from memory_cmic.audit import record_audit
from memory_cmic.models import TenantProfileConfig
from memory_cmic.task_queue import enqueue_task

DEFAULT_DIMENSIONS = [
    {
        "key": "identity",
        "name": "基本身份",
        "description": "用户本人的姓名、称呼、明确自述的性别、出生日期与年龄。区分姓名和偏好称呼；"
        "年龄必须保留可靠的时间条件，不猜测出生日期或性别。",
        "enabled": True,
    },
    {
        "key": "residence",
        "name": "居住与生活背景",
        "description": "用户的常住地区、长期居住方式及共同生活安排。区分当前常住地、籍贯、工作地和"
        "过去居住经历，不把旅行或出差地当作常住地。",
        "enabled": True,
    },
    {
        "key": "education",
        "name": "教育与学习背景",
        "description": "用户已获得的学历、专业、学校、当前在读阶段及长期学习方向。"
        "学生身份归入本维度；"
        "明确区分已毕业与在读，不将过去就读经历改写为当前在读。",
        "enabled": True,
    },
    {
        "key": "occupation",
        "name": "职业与当前角色",
        "description": "用户当前职业、岗位、行业及持续影响生活的角色，"
        "例如求职者、退休人员和家庭照护者。"
        "允许兼任，保留阶段条件；排除临时项目职责，学生身份归入教育维度。",
        "enabled": True,
    },
    {
        "key": "relationships",
        "name": "家庭与重要社会关系",
        "description": "用户明确提及的亲属、伴侣、好友等重要关系及关系人的长期背景、偏好和限制。"
        "每条保留关系人的明确主语；不得归为用户本人的属性，不猜测人物对应关系。",
        "enabled": True,
    },
    {
        "key": "social_habits",
        "name": "社交习惯",
        "description": "用户本人的长期社交方式、频率与互动偏好。排除关系人的偏好及一次性活动安排。",
        "enabled": True,
    },
    {
        "key": "diet",
        "name": "饮食偏好与习惯",
        "description": "用户本人的长期口味、饮食选择与用餐习惯。不包含关系人的饮食信息；"
        "过敏和明确禁忌归入饮食限制。",
        "enabled": True,
    },
    {
        "key": "dietary_restrictions",
        "name": "饮食限制",
        "description": "用户本人明确表达的食物过敏、禁忌及其他饮食限制。不把不喜欢改写成不能吃，"
        "不包含关系人的限制。",
        "enabled": True,
    },
    {
        "key": "preferences",
        "name": "兴趣与其他个人偏好",
        "description": "用户本人的长期兴趣爱好及未被专门维度覆盖的选择偏好，例如阅读、娱乐和出行。"
        "不重复归入饮食、社交、沟通等专门维度，不包含关系人的偏好。",
        "enabled": True,
    },
    {
        "key": "communication",
        "name": "沟通与协作偏好",
        "description": "用户对交流、解释、反馈和工作协作方式的长期要求。保留文档类型、业务场景、"
        "否定及例外条件，不把场景限定要求扩大成通用偏好。",
        "enabled": True,
    },
    {
        "key": "other",
        "name": "其他长期背景",
        "description": "有明确依据和持续背景价值、但不属于其他启用维度的信息。"
        "仅在没有更具体维度时使用，"
        "排除临时任务、一次性事件和未经支持的推断。",
        "enabled": True,
    },
]


class ProfileDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2048)
    enabled: bool = Field(default=True, strict=True)

    @field_validator("name", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dimension name and description must not be blank")
        return value.strip()


class ProfileConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    dimensions: list[ProfileDimension] = Field(max_length=32)

    @model_validator(mode="after")
    def unique_dimensions(self):
        keys = [d.key for d in self.dimensions if d.key is not None]
        names = [d.name for d in self.dimensions]
        if len(keys) != len(set(keys)) or len(names) != len(set(names)):
            raise ValueError("dimension keys and names must be unique")
        return self


class ProfileConfigResult(BaseModel):
    version: int
    dimensions: list[ProfileDimension]


class ProfileConfigConflict(ValueError):
    pass


def ensure_profile_config(session: Session, tenant_id: str) -> TenantProfileConfig:
    existing = session.get(TenantProfileConfig, tenant_id)
    if existing is None:
        session.execute(
            pg_insert(TenantProfileConfig)
            .values(tenant_id=tenant_id, dimensions=deepcopy(DEFAULT_DIMENSIONS))
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        existing = session.get(TenantProfileConfig, tenant_id)
    return existing


def enabled_dimensions(config: TenantProfileConfig) -> list[dict]:
    return [d for d in config.dimensions if d["enabled"]]


def enqueue_config_apply(session: Session, config: TenantProfileConfig):
    return enqueue_task(
        session,
        {
            "id": f"tsk_{uuid4().hex}",
            "tenant_id": config.tenant_id,
            "task_type": "profile_config_apply",
            "target_type": "batch",
            "target_id": config.tenant_id,
            "idempotency_key": f"profile_config_apply:{config.version}",
            "payload": {"config_version": config.version},
        },
    )


def update_profile_config(
    session: Session,
    *,
    tenant_id: str,
    body: ProfileConfigUpdate,
    operator_id: str,
) -> TenantProfileConfig:
    ensure_profile_config(session, tenant_id)
    config = session.scalars(
        select(TenantProfileConfig)
        .where(TenantProfileConfig.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if config.version != body.expected_version:
        raise ProfileConfigConflict("profile configuration version changed; reload before saving")
    known_keys = {d["key"] for d in config.dimensions}
    dimensions = []
    for dimension in body.dimensions:
        if dimension.key is not None and dimension.key not in known_keys:
            raise ValueError("new dimensions must omit key; existing keys cannot be changed")
        dimensions.append({**dimension.model_dump(), "key": dimension.key or f"dim_{uuid4().hex}"})
    from memory_cmic.extraction_input import input_size
    from memory_cmic.profile import PROFILE_INPUT_LIMIT, PROFILE_PROMPT

    if (
        input_size(
            PROFILE_PROMPT,
            {
                "memories": [],
                "dimensions": [d for d in dimensions if d["enabled"]],
            },
        )
        > PROFILE_INPUT_LIMIT - 2048
    ):
        raise ValueError("dimension configuration exceeds the profile model input budget")
    if dimensions == config.dimensions:
        return config
    before = {"version": config.version, "dimensions": config.dimensions}
    config.dimensions = dimensions
    config.version += 1
    config.updated_at = datetime.now(UTC)
    session.flush()
    enqueue_config_apply(session, config)
    record_audit(
        session,
        tenant_id=tenant_id,
        action="UPDATE",
        target_type="profile_config",
        target_id=tenant_id,
        operator_type="admin",
        operator_id=operator_id,
        reason_code="PROFILE_CONFIG_CHANGED",
        state_before=before,
        state_after={"version": config.version, "dimensions": dimensions},
    )
    return config
