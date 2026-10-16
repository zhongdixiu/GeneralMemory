"""Add tenant business scene directory.

Revision ID: 0004_business_scenes
Revises: 0003_governance_service
"""

from alembic import op

revision = "0004_business_scenes"
down_revision = "0003_governance_service"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE business_scene (
            tenant_id VARCHAR(64) NOT NULL,
            name VARCHAR(32) NOT NULL,
            definition VARCHAR(512) NOT NULL,
            aliases VARCHAR(32)[] NOT NULL,
            embedding vector(1024),
            model_id VARCHAR(128),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT pk_business_scene PRIMARY KEY (tenant_id, name)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_business_scene_embedding_hnsw "
        "ON business_scene USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )
    op.execute(
        """
        INSERT INTO business_scene (tenant_id, name, definition, aliases)
        SELECT tenant_id, name, definition, aliases
        FROM (SELECT DISTINCT tenant_id FROM memory_item
              UNION SELECT DISTINCT tenant_id FROM profile_property) AS tenants
        CROSS JOIN (VALUES
            ('email', '邮件的阅读、撰写、回复、处理和整理', ARRAY['邮件','电子邮件','邮件处理']::VARCHAR(32)[]),
            ('disk', '个人云盘文件的备份、存储、整理和共享，不含本地文件', ARRAY['云盘','个人云盘','cloud_drive']::VARCHAR(32)[]),
            ('communication', '非邮件通信，包括短信、电话和即时消息', ARRAY['通信','通讯','sms','短信','电话','即时消息']::VARCHAR(32)[])
        ) AS initial(name, definition, aliases)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE business_scene")
