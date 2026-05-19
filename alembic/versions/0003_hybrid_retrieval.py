"""hybrid retrieval support

Revision ID: 0003_hybrid_retrieval
Revises: 0002_profiles_targets
Create Date: 2026-05-19
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision: str = "0003_hybrid_retrieval"
down_revision: str | None = "0002_profiles_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "target_elements",
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=1536), nullable=True),
    )
    op.create_table(
        "mapping_candidates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "target_element_id",
            sa.String(length=36),
            sa.ForeignKey("target_elements.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "profile_rule_id",
            sa.String(length=36),
            sa.ForeignKey("profile_rules.id"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_format_atoms_embedding_hnsw
        ON format_atoms USING hnsw (embedding vector_cosine_ops)
        WHERE embedding IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_target_elements_embedding_hnsw
        ON target_elements USING hnsw (embedding vector_cosine_ops)
        WHERE embedding IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_target_elements_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_format_atoms_embedding_hnsw")
    op.drop_table("mapping_candidates")
    op.drop_column("target_elements", "embedding")
