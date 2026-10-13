from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


def _default_agent_id(context) -> str | None:
    return context.get_current_parameters().get("caller_agent_id")


class Base(DeclarativeBase):
    pass


class BusinessScene(Base):
    __tablename__ = "business_scene"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "name", name="pk_business_scene"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    definition: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    model_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "ix_business_scene_embedding_hnsw",
    BusinessScene.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_where=BusinessScene.embedding.is_not(None),
)


class SourceRecord(Base):
    __tablename__ = "source_record"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_type",
            "external_ref_id",
            name="uq_source_record_external_ref",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_source_record_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "user_id",
            "session_id",
            "message_id",
            "caller_agent_id",
            "agent_id",
            name="uq_source_record_message",
        ),
        CheckConstraint("version > 0", name="ck_source_record_version_positive"),
        CheckConstraint(
            "source_type IN ('chat', 'doc', 'tool', 'manual', 'legacy_import')",
            name="ck_source_record_source_type",
        ),
        CheckConstraint(
            "author_type IN ('user', 'agent', 'system', 'admin')",
            name="ck_source_record_author_type",
        ),
        CheckConstraint(
            "status IN ('active', 'invalidated', 'deleted')",
            name="ck_source_record_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128))
    session_id: Mapped[str | None] = mapped_column(String(128))
    message_id: Mapped[str | None] = mapped_column(String(256))
    caller_agent_id: Mapped[str | None] = mapped_column(String(128))
    agent_id: Mapped[str | None] = mapped_column(String(128), default=_default_agent_id)
    external_ref_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_type: Mapped[str] = mapped_column(String(32), nullable=False)
    author_id: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    processed_version: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "ix_source_record_author_history",
    SourceRecord.tenant_id,
    SourceRecord.author_type,
    SourceRecord.author_id,
    SourceRecord.occurred_at.desc(),
)
Index(
    "ix_source_record_conversation",
    SourceRecord.tenant_id,
    SourceRecord.source_system,
    SourceRecord.caller_agent_id,
    SourceRecord.agent_id,
    SourceRecord.user_id,
    SourceRecord.session_id,
)


class ConversationSession(Base):
    __tablename__ = "conversation_session"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "caller_agent_id", "agent_id", "source_system", "user_id", "session_id",
            name="pk_conversation_session"
        ),
        CheckConstraint("next_batch_seq > 0", name="ck_conversation_next_batch_seq_positive"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    caller_agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=_default_agent_id
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    next_batch_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class MemoryItem(Base):
    __tablename__ = "memory_item"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_memory_item_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "supersedes_id"],
            ["memory_item.tenant_id", "memory_item.id"],
            name="fk_memory_item_supersedes",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
        CheckConstraint("version > 0", name="ck_memory_version_positive"),
        CheckConstraint(
            "business_domains IS NULL OR cardinality(business_domains) > 0",
            name="ck_memory_business_domains_nonempty",
        ),
        CheckConstraint(
            "project_domains IS NULL OR cardinality(project_domains) > 0",
            name="ck_memory_project_domains_nonempty",
        ),
        CheckConstraint(
            "subject_type IN ('user', 'agent', 'company')", name="ck_memory_subject_type"
        ),
        CheckConstraint(
            "cognitive_type IN ('fact', 'inference', 'skill', 'rule')",
            name="ck_memory_cognitive_type",
        ),
        CheckConstraint(
            "status IN ('active', 'invalidated', 'disputed', 'expired')",
            name="ck_memory_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cognitive_type: Mapped[str] = mapped_column(String(32), nullable=False)
    business_domains: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)))
    project_domains: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)))
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default=text("1.000")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    supersedes_id: Mapped[str | None] = mapped_column(String(64))
    conflict_group_id: Mapped[str | None] = mapped_column(String(64))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "ix_memory_item_subject_status",
    MemoryItem.tenant_id,
    MemoryItem.subject_type,
    MemoryItem.subject_id,
    MemoryItem.status,
)
Index("ix_memory_item_business_domains_gin", MemoryItem.business_domains, postgresql_using="gin")
Index("ix_memory_item_project_domains_gin", MemoryItem.project_domains, postgresql_using="gin")
Index(
    "ix_memory_item_active_expired_at",
    MemoryItem.expired_at,
    postgresql_where=text("status = 'active'"),
)
Index("ix_memory_item_conflict_group", MemoryItem.tenant_id, MemoryItem.conflict_group_id)
Index(
    "ix_memory_item_fact_hash",
    MemoryItem.tenant_id,
    MemoryItem.subject_type,
    MemoryItem.subject_id,
    MemoryItem.semantic_hash,
    postgresql_where=text("status = 'active' AND semantic_hash IS NOT NULL"),
)


class ProfileProperty(Base):
    __tablename__ = "profile_property"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_profile_property_tenant_id_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_profile_confidence"),
        CheckConstraint("version > 0", name="ck_profile_version_positive"),
        CheckConstraint("assertion_kind IN ('explicit', 'inferred')", name="ck_profile_assertion"),
        CheckConstraint(
            "business_domains IS NULL OR cardinality(business_domains) > 0",
            name="ck_profile_business_domains_nonempty",
        ),
        CheckConstraint(
            "value_type IN ('string', 'number', 'boolean', 'list', 'object')",
            name="ck_profile_value_type",
        ),
        CheckConstraint(
            "property_key IS NOT NULL OR (value_type = 'string' "
            "AND jsonb_typeof(property_value) = 'string' "
            "AND length(btrim(property_value #>> '{}')) > 0)",
            name="ck_profile_statement_value",
        ),
        CheckConstraint(
            "status IN ('active', 'invalidated', 'disputed', 'expired')",
            name="ck_profile_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assertion_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'explicit'")
    )
    business_domains: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)))
    property_key: Mapped[str | None] = mapped_column(String(128))
    property_value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool] = mapped_column(
        JSONB, nullable=False
    )
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default=text("1.000")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "uq_profile_property_active_key",
    ProfileProperty.tenant_id,
    ProfileProperty.user_id,
    ProfileProperty.business_domains,
    ProfileProperty.property_key,
    unique=True,
    postgresql_nulls_not_distinct=True,
    postgresql_where=text("status = 'active' AND property_key IS NOT NULL"),
)
Index(
    "ix_profile_property_active_user",
    ProfileProperty.tenant_id,
    ProfileProperty.user_id,
    postgresql_where=text("status = 'active'"),
)


class MemoryEvidence(Base):
    __tablename__ = "memory_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "upstream_source_id"],
            ["source_record.tenant_id", "source_record.id"],
            name="fk_evidence_upstream_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "upstream_memory_id"],
            ["memory_item.tenant_id", "memory_item.id"],
            name="fk_evidence_upstream_memory",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "downstream_memory_id"],
            ["memory_item.tenant_id", "memory_item.id"],
            name="fk_evidence_downstream_memory",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "downstream_profile_id"],
            ["profile_property.tenant_id", "profile_property.id"],
            name="fk_evidence_downstream_profile",
        ),
        CheckConstraint(
            "num_nonnulls(upstream_source_id, upstream_memory_id) = 1",
            name="ck_evidence_one_upstream",
        ),
        CheckConstraint(
            "num_nonnulls(downstream_memory_id, downstream_profile_id) = 1",
            name="ck_evidence_one_downstream",
        ),
        CheckConstraint(
            "(relationship_type = 'supports' AND upstream_source_id IS NOT NULL "
            "AND downstream_memory_id IS NOT NULL) OR "
            "(relationship_type = 'derives' AND upstream_memory_id IS NOT NULL "
            "AND downstream_memory_id IS NOT NULL) OR "
            "(relationship_type = 'profile_basis' AND upstream_memory_id IS NOT NULL "
            "AND downstream_profile_id IS NOT NULL)",
            name="ck_evidence_relationship_shape",
        ),
        CheckConstraint("upstream_version > 0", name="ck_evidence_version_positive"),
        CheckConstraint(
            "upstream_memory_id IS NULL OR downstream_memory_id IS NULL "
            "OR upstream_memory_id <> downstream_memory_id",
            name="ck_evidence_no_self_loop",
        ),
        CheckConstraint("status IN ('active', 'invalidated')", name="ck_evidence_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    upstream_source_id: Mapped[str | None] = mapped_column(String(64))
    upstream_memory_id: Mapped[str | None] = mapped_column(String(64))
    downstream_memory_id: Mapped[str | None] = mapped_column(String(64))
    downstream_profile_id: Mapped[str | None] = mapped_column(String(64))
    upstream_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_snippet: Mapped[str | None] = mapped_column(Text)
    evidence_locator: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_evidence_upstream_source", MemoryEvidence.tenant_id, MemoryEvidence.upstream_source_id)
Index("ix_evidence_upstream_memory", MemoryEvidence.tenant_id, MemoryEvidence.upstream_memory_id)
Index(
    "ix_evidence_downstream_memory", MemoryEvidence.tenant_id, MemoryEvidence.downstream_memory_id
)
Index(
    "ix_evidence_downstream_profile", MemoryEvidence.tenant_id, MemoryEvidence.downstream_profile_id
)
Index(
    "uq_evidence_active_supports",
    MemoryEvidence.tenant_id,
    MemoryEvidence.evidence_group_id,
    MemoryEvidence.upstream_source_id,
    MemoryEvidence.downstream_memory_id,
    unique=True,
    postgresql_where=text("status = 'active' AND relationship_type = 'supports'"),
)
Index(
    "uq_evidence_active_derives",
    MemoryEvidence.tenant_id,
    MemoryEvidence.evidence_group_id,
    MemoryEvidence.upstream_memory_id,
    MemoryEvidence.downstream_memory_id,
    unique=True,
    postgresql_where=text("status = 'active' AND relationship_type = 'derives'"),
)
Index(
    "uq_evidence_active_profile_basis",
    MemoryEvidence.tenant_id,
    MemoryEvidence.evidence_group_id,
    MemoryEvidence.upstream_memory_id,
    MemoryEvidence.downstream_profile_id,
    unique=True,
    postgresql_where=text("status = 'active' AND relationship_type = 'profile_basis'"),
)


class MemoryEmbedding(Base):
    __tablename__ = "memory_embedding"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "memory_id", "model_id", name="pk_memory_embedding"),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memory_item.tenant_id", "memory_item.id"],
            name="fk_embedding_memory",
        ),
        CheckConstraint("status IN ('active', 'stale')", name="ck_embedding_status"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "ix_memory_embedding_active_hnsw",
    MemoryEmbedding.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_where=text("status = 'active'"),
)


class MemoryTask(Base):
    __tablename__ = "memory_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_memory_task_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "caller_agent_id",
            "agent_id",
            "idempotency_key",
            "user_id",
            name="uq_memory_task_idempotency",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_task_attempt_count"),
        CheckConstraint("max_attempts > 0", name="ck_task_max_attempts"),
        CheckConstraint("priority IN (0, 50, 100)", name="ck_task_priority"),
        CheckConstraint(
            "task_type IN ('fact_extract', 'inference_derive', 'profile_rebuild', "
            "'dependency_recheck', 'vector_upsert', 'vector_delete', 'ttl_expire', "
            "'consolidate')",
            name="ck_task_type",
        ),
        CheckConstraint(
            "target_type IN ('source', 'memory', 'profile', 'subject', 'batch')",
            name="ck_task_target_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'partial', 'failed', 'cancelled')",
            name="ck_task_status",
        ),
        CheckConstraint(
            "status <> 'pending' OR (locked_until IS NULL AND worker_id IS NULL)",
            name="ck_task_pending_has_no_lease",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_version: Mapped[int | None] = mapped_column(Integer)
    caller_agent_id: Mapped[str | None] = mapped_column(String(128))
    agent_id: Mapped[str | None] = mapped_column(String(128), default=_default_agent_id)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    source_system: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[str | None] = mapped_column(String(128))
    session_id: Mapped[str | None] = mapped_column(String(128))
    batch_seq: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    result_json: Mapped[list[dict[str, Any]] | None] = mapped_column("result", JSONB)
    error_json: Mapped[dict[str, Any] | None] = mapped_column("error", JSONB)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("50"))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "ix_memory_task_pending_claim",
    MemoryTask.tenant_id,
    MemoryTask.priority.desc(),
    MemoryTask.available_at,
    MemoryTask.created_at,
    postgresql_where=text("status = 'pending'"),
)
Index(
    "ix_memory_task_conversation",
    MemoryTask.tenant_id,
    MemoryTask.source_system,
    MemoryTask.caller_agent_id,
    MemoryTask.agent_id,
    MemoryTask.user_id,
    MemoryTask.session_id,
    MemoryTask.batch_seq,
)
Index(
    "uq_governance_active_run",
    MemoryTask.tenant_id,
    MemoryTask.user_id,
    unique=True,
    postgresql_where=text("task_type = 'consolidate' AND status IN ('pending', 'processing')"),
)
Index(
    "uq_profile_pending_user", MemoryTask.tenant_id, MemoryTask.user_id, unique=True,
    postgresql_where=text("task_type = 'profile_rebuild' AND status = 'pending'"),
)
Index(
    "uq_profile_processing_user", MemoryTask.tenant_id, MemoryTask.user_id, unique=True,
    postgresql_where=text("task_type = 'profile_rebuild' AND status = 'processing'"),
)
Index(
    "uq_system_task_key",
    MemoryTask.tenant_id,
    MemoryTask.idempotency_key,
    unique=True,
    postgresql_where=text("caller_agent_id IS NULL"),
)
Index(
    "uq_service_task_key",
    MemoryTask.tenant_id,
    MemoryTask.caller_agent_id,
    MemoryTask.idempotency_key,
    unique=True,
    postgresql_where=text("caller_agent_id IS NOT NULL AND user_id IS NULL"),
)


class GovernancePolicy(Base):
    __tablename__ = "governance_policy"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    auto_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    generate_inferences: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    change_threshold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    idle_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    cooldown_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )
    max_wait_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("360")
    )
    max_memories: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class GovernanceSubjectState(Base):
    __tablename__ = "governance_subject_state"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "user_id"),)

    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(128))
    last_add_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_input: Mapped[str | None] = mapped_column(String(64))
    profile_evaluated_input: Mapped[str | None] = mapped_column(String(64))
    profile_first_dirty_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class GovernancePending(Base):
    __tablename__ = "governance_pending"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "user_id", "memory_id"),)

    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(128))
    memory_id: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    first_change_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class GovernanceOperation(Base):
    __tablename__ = "governance_operation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_governance_operation_task", GovernanceOperation.tenant_id, GovernanceOperation.task_id)
Index("ix_governance_pending_age", GovernancePending.tenant_id, GovernancePending.first_change_at)


class MemoryTaskSource(Base):
    __tablename__ = "memory_task_source"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "task_id", "source_id", name="pk_memory_task_source"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["memory_task.tenant_id", "memory_task.id"],
            name="fk_memory_task_source_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["source_record.tenant_id", "source_record.id"],
            name="fk_memory_task_source_source",
        ),
        CheckConstraint("source_version > 0", name="ck_task_source_version_positive"),
        CheckConstraint("kind IN ('target', 'history')", name="ck_task_source_kind"),
        CheckConstraint("position >= 0", name="ck_task_source_position_nonnegative"),
        CheckConstraint(
            "status IN ('pending', 'processed', 'skipped')", name="ck_task_source_status"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "ix_memory_task_source_source",
    MemoryTaskSource.tenant_id,
    MemoryTaskSource.source_id,
    MemoryTaskSource.kind,
)
Index(
    "ix_memory_task_correlation",
    MemoryTask.tenant_id,
    MemoryTask.correlation_id,
    MemoryTask.created_at,
)


class MemoryAuditLog(Base):
    __tablename__ = "memory_audit_log"
    __table_args__ = (
        CheckConstraint(
            "action IN ('INSERT', 'UPDATE', 'INVALIDATE', 'EXPIRE', 'DELETE', 'MIGRATE')",
            name="ck_audit_action",
        ),
        CheckConstraint(
            "target_type IN ('source', 'memory', 'evidence', 'profile', 'embedding', 'task', "
            "'governance_policy')",
            name="ck_audit_target_type",
        ),
        CheckConstraint(
            "operator_type IN ('user', 'admin', 'agent', 'system')",
            name="ck_audit_operator_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(512))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    state_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    state_after: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


Index(
    "ix_audit_target_history",
    MemoryAuditLog.tenant_id,
    MemoryAuditLog.target_type,
    MemoryAuditLog.target_id,
    MemoryAuditLog.created_at.desc(),
)
Index(
    "ix_audit_correlation",
    MemoryAuditLog.tenant_id,
    MemoryAuditLog.correlation_id,
    MemoryAuditLog.created_at,
)
Index("ix_audit_task", MemoryAuditLog.tenant_id, MemoryAuditLog.task_id, MemoryAuditLog.id)
Index(
    "ix_audit_timeline", MemoryAuditLog.tenant_id,
    MemoryAuditLog.created_at.desc(), MemoryAuditLog.id.desc(),
)


@event.listens_for(MemoryAuditLog, "before_update")
@event.listens_for(MemoryAuditLog, "before_delete")
def _prevent_audit_rewrite(mapper, connection, target) -> None:
    raise ValueError("audit logs are append-only")


class ExternalEntityMapping(Base):
    __tablename__ = "external_entity_mapping"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "source_system",
            "source_namespace",
            "entity_type",
            "external_id",
            name="pk_external_entity_mapping",
        ),
        CheckConstraint(
            "entity_type IN ('workspace', 'peer', 'session', 'message', 'collection', 'document')",
            name="ck_external_mapping_entity_type",
        ),
        CheckConstraint(
            "target_type IN ('source', 'memory')", name="ck_external_mapping_target_type"
        ),
        {"schema": "migration"},
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    source_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    migration_batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
