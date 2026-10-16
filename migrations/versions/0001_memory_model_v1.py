"""Create the frozen V1.0 memory model.

Revision ID: 0001_memory_model_v1
Revises: None
Create Date: 2026-09-20
"""

from __future__ import annotations

from alembic import op

revision = "0001_memory_model_v1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS migration")

    op.execute(
        """
        CREATE TABLE source_record (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            source_system VARCHAR(64) NOT NULL,
            source_type VARCHAR(32) NOT NULL,
            session_id VARCHAR(128),
            external_ref_id VARCHAR(128) NOT NULL,
            author_type VARCHAR(32) NOT NULL,
            author_id VARCHAR(64) NOT NULL,
            raw_content TEXT NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            metadata JSONB,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            occurred_at TIMESTAMPTZ NOT NULL,
            invalidated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_source_record_external_ref UNIQUE
                (tenant_id, source_system, source_type, external_ref_id),
            CONSTRAINT uq_source_record_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_source_record_version_positive CHECK (version > 0),
            CONSTRAINT ck_source_record_source_type CHECK
                (source_type IN ('chat', 'doc', 'tool', 'manual', 'legacy_import')),
            CONSTRAINT ck_source_record_author_type CHECK
                (author_type IN ('user', 'agent', 'system', 'admin')),
            CONSTRAINT ck_source_record_status CHECK
                (status IN ('active', 'invalidated', 'deleted'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_source_record_author_history
        ON source_record (tenant_id, author_type, author_id, occurred_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE memory_item (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            subject_type VARCHAR(32) NOT NULL,
            subject_id VARCHAR(64) NOT NULL,
            cognitive_type VARCHAR(32) NOT NULL,
            business_domains VARCHAR(32)[],
            project_domains VARCHAR(64)[],
            summary VARCHAR(512) NOT NULL,
            content TEXT NOT NULL,
            metadata JSONB,
            confidence NUMERIC(4,3) NOT NULL DEFAULT 1.000,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            supersedes_id VARCHAR(64),
            conflict_group_id VARCHAR(64),
            effective_at TIMESTAMPTZ NOT NULL,
            expired_at TIMESTAMPTZ,
            invalidated_at TIMESTAMPTZ,
            created_by VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_memory_item_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT fk_memory_item_supersedes FOREIGN KEY (tenant_id, supersedes_id)
                REFERENCES memory_item (tenant_id, id),
            CONSTRAINT ck_memory_confidence CHECK (confidence >= 0 AND confidence <= 1),
            CONSTRAINT ck_memory_version_positive CHECK (version > 0),
            CONSTRAINT ck_memory_business_domains_nonempty CHECK
                (business_domains IS NULL OR cardinality(business_domains) > 0),
            CONSTRAINT ck_memory_project_domains_nonempty CHECK
                (project_domains IS NULL OR cardinality(project_domains) > 0),
            CONSTRAINT ck_memory_subject_type CHECK
                (subject_type IN ('user', 'agent', 'company')),
            CONSTRAINT ck_memory_cognitive_type CHECK
                (cognitive_type IN ('fact', 'inference', 'skill', 'rule')),
            CONSTRAINT ck_memory_status CHECK
                (status IN ('active', 'invalidated', 'disputed', 'expired'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_memory_item_subject_status
        ON memory_item (tenant_id, subject_type, subject_id, status)
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_item_business_domains_gin "
        "ON memory_item USING gin (business_domains)"
    )
    op.execute(
        "CREATE INDEX ix_memory_item_project_domains_gin "
        "ON memory_item USING gin (project_domains)"
    )
    op.execute(
        """
        CREATE INDEX ix_memory_item_active_expired_at
        ON memory_item (expired_at) WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_memory_item_conflict_group
        ON memory_item (tenant_id, conflict_group_id)
        """
    )

    op.execute(
        """
        CREATE TABLE profile_property (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            business_domains VARCHAR(32)[],
            property_key VARCHAR(128) NOT NULL,
            property_value JSONB NOT NULL,
            value_type VARCHAR(32) NOT NULL,
            confidence NUMERIC(4,3) NOT NULL DEFAULT 1.000,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            effective_at TIMESTAMPTZ NOT NULL,
            expired_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_profile_property_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_profile_confidence CHECK (confidence >= 0 AND confidence <= 1),
            CONSTRAINT ck_profile_version_positive CHECK (version > 0),
            CONSTRAINT ck_profile_business_domains_nonempty CHECK
                (business_domains IS NULL OR cardinality(business_domains) > 0),
            CONSTRAINT ck_profile_value_type CHECK
                (value_type IN ('string', 'number', 'boolean', 'list', 'object')),
            CONSTRAINT ck_profile_status CHECK
                (status IN ('active', 'invalidated', 'disputed', 'expired'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_profile_property_active_key
        ON profile_property (tenant_id, user_id, business_domains, property_key)
        NULLS NOT DISTINCT
        WHERE status = 'active'
        """
    )

    op.execute(
        """
        CREATE TABLE memory_evidence (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            relationship_type VARCHAR(32) NOT NULL,
            evidence_group_id VARCHAR(64) NOT NULL,
            upstream_source_id VARCHAR(64),
            upstream_memory_id VARCHAR(64),
            downstream_memory_id VARCHAR(64),
            downstream_profile_id VARCHAR(64),
            upstream_version INTEGER NOT NULL,
            evidence_snippet TEXT,
            evidence_locator JSONB,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            invalidated_at TIMESTAMPTZ,
            CONSTRAINT fk_evidence_upstream_source FOREIGN KEY
                (tenant_id, upstream_source_id)
                REFERENCES source_record (tenant_id, id),
            CONSTRAINT fk_evidence_upstream_memory FOREIGN KEY
                (tenant_id, upstream_memory_id)
                REFERENCES memory_item (tenant_id, id),
            CONSTRAINT fk_evidence_downstream_memory FOREIGN KEY
                (tenant_id, downstream_memory_id)
                REFERENCES memory_item (tenant_id, id),
            CONSTRAINT fk_evidence_downstream_profile FOREIGN KEY
                (tenant_id, downstream_profile_id)
                REFERENCES profile_property (tenant_id, id),
            CONSTRAINT ck_evidence_one_upstream CHECK
                (num_nonnulls(upstream_source_id, upstream_memory_id) = 1),
            CONSTRAINT ck_evidence_one_downstream CHECK
                (num_nonnulls(downstream_memory_id, downstream_profile_id) = 1),
            CONSTRAINT ck_evidence_relationship_shape CHECK (
                (relationship_type = 'supports'
                    AND upstream_source_id IS NOT NULL
                    AND downstream_memory_id IS NOT NULL)
                OR (relationship_type = 'derives'
                    AND upstream_memory_id IS NOT NULL
                    AND downstream_memory_id IS NOT NULL)
                OR (relationship_type = 'profile_basis'
                    AND upstream_memory_id IS NOT NULL
                    AND downstream_profile_id IS NOT NULL)
            ),
            CONSTRAINT ck_evidence_version_positive CHECK (upstream_version > 0),
            CONSTRAINT ck_evidence_no_self_loop CHECK (
                upstream_memory_id IS NULL
                OR downstream_memory_id IS NULL
                OR upstream_memory_id <> downstream_memory_id
            ),
            CONSTRAINT ck_evidence_status CHECK (status IN ('active', 'invalidated'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_evidence_upstream_source
        ON memory_evidence (tenant_id, upstream_source_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_evidence_upstream_memory
        ON memory_evidence (tenant_id, upstream_memory_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_evidence_downstream_memory
        ON memory_evidence (tenant_id, downstream_memory_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_evidence_downstream_profile
        ON memory_evidence (tenant_id, downstream_profile_id)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_evidence_active_supports
        ON memory_evidence
            (tenant_id, evidence_group_id, upstream_source_id, downstream_memory_id)
        WHERE status = 'active' AND relationship_type = 'supports'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_evidence_active_derives
        ON memory_evidence
            (tenant_id, evidence_group_id, upstream_memory_id, downstream_memory_id)
        WHERE status = 'active' AND relationship_type = 'derives'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_evidence_active_profile_basis
        ON memory_evidence
            (tenant_id, evidence_group_id, upstream_memory_id, downstream_profile_id)
        WHERE status = 'active' AND relationship_type = 'profile_basis'
        """
    )

    op.execute(
        """
        CREATE TABLE memory_embedding (
            tenant_id VARCHAR(64) NOT NULL,
            memory_id VARCHAR(64) NOT NULL,
            model_id VARCHAR(128) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            embedding VECTOR(1536) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT pk_memory_embedding PRIMARY KEY (tenant_id, memory_id, model_id),
            CONSTRAINT fk_embedding_memory FOREIGN KEY (tenant_id, memory_id)
                REFERENCES memory_item (tenant_id, id),
            CONSTRAINT ck_embedding_status CHECK (status IN ('active', 'stale'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_memory_embedding_active_hnsw
        ON memory_embedding USING hnsw (embedding vector_cosine_ops)
        WHERE status = 'active'
        """
    )

    op.execute(
        """
        CREATE TABLE memory_task (
            id VARCHAR(64) PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            task_type VARCHAR(32) NOT NULL,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(128) NOT NULL,
            input_version INTEGER,
            idempotency_key VARCHAR(256) NOT NULL,
            correlation_id VARCHAR(64),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            priority SMALLINT NOT NULL DEFAULT 50,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            locked_until TIMESTAMPTZ,
            worker_id VARCHAR(128),
            last_error TEXT,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_memory_task_idempotency UNIQUE (tenant_id, idempotency_key),
            CONSTRAINT ck_task_attempt_count CHECK (attempt_count >= 0),
            CONSTRAINT ck_task_max_attempts CHECK (max_attempts > 0),
            CONSTRAINT ck_task_priority CHECK (priority IN (0, 50, 100)),
            CONSTRAINT ck_task_type CHECK (
                task_type IN (
                    'fact_extract', 'inference_derive', 'profile_rebuild',
                    'dependency_recheck', 'vector_upsert', 'vector_delete', 'ttl_expire'
                )
            ),
            CONSTRAINT ck_task_target_type CHECK
                (target_type IN ('source', 'memory', 'profile', 'subject', 'batch')),
            CONSTRAINT ck_task_status CHECK
                (status IN ('pending', 'processing', 'succeeded', 'failed', 'cancelled')),
            CONSTRAINT ck_task_pending_has_no_lease CHECK (
                status <> 'pending' OR (locked_until IS NULL AND worker_id IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_memory_task_pending_claim
        ON memory_task (tenant_id, priority DESC, available_at ASC, created_at ASC)
        WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_memory_task_correlation
        ON memory_task (tenant_id, correlation_id, created_at)
        """
    )

    op.execute(
        """
        CREATE TABLE memory_audit_log (
            id BIGSERIAL PRIMARY KEY,
            tenant_id VARCHAR(64) NOT NULL,
            action VARCHAR(32) NOT NULL,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(128) NOT NULL,
            operator_type VARCHAR(32) NOT NULL,
            operator_id VARCHAR(64) NOT NULL,
            reason_code VARCHAR(64) NOT NULL,
            reason VARCHAR(512),
            correlation_id VARCHAR(64),
            state_before JSONB,
            state_after JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_audit_action CHECK
                (action IN ('INSERT', 'UPDATE', 'INVALIDATE', 'EXPIRE', 'DELETE', 'MIGRATE')),
            CONSTRAINT ck_audit_target_type CHECK
                (target_type IN ('source', 'memory', 'evidence', 'profile', 'embedding', 'task')),
            CONSTRAINT ck_audit_operator_type CHECK
                (operator_type IN ('user', 'admin', 'agent', 'system'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_audit_target_history
        ON memory_audit_log (tenant_id, target_type, target_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_audit_correlation
        ON memory_audit_log (tenant_id, correlation_id, created_at)
        """
    )

    op.execute(
        """
        CREATE TABLE migration.external_entity_mapping (
            tenant_id VARCHAR(64) NOT NULL,
            source_system VARCHAR(32) NOT NULL,
            source_namespace VARCHAR(128) NOT NULL,
            entity_type VARCHAR(32) NOT NULL,
            external_id VARCHAR(128) NOT NULL,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(128) NOT NULL,
            migration_batch_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT pk_external_entity_mapping PRIMARY KEY
                (tenant_id, source_system, source_namespace, entity_type, external_id),
            CONSTRAINT ck_external_mapping_entity_type CHECK (
                entity_type IN ('workspace', 'peer', 'session', 'message', 'collection', 'document')
            ),
            CONSTRAINT ck_external_mapping_target_type CHECK
                (target_type IN ('source', 'memory'))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE migration.external_entity_mapping")
    op.execute("DROP TABLE memory_audit_log")
    op.execute("DROP TABLE memory_task")
    op.execute("DROP TABLE memory_embedding")
    op.execute("DROP TABLE memory_evidence")
    op.execute("DROP TABLE profile_property")
    op.execute("DROP TABLE memory_item")
    op.execute("DROP TABLE source_record")
    op.execute("DROP SCHEMA migration")
