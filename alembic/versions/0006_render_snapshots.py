"""render snapshot schema

Revision ID: 0006_render_snapshots
Revises: 0005_patch_execution
Create Date: 2026-05-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_render_snapshots"
down_revision: str | None = "0005_patch_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "render_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("renderer", sa.String(length=64), nullable=False, index=True),
        sa.Column("renderer_version", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("pdf_data", sa.LargeBinary(), nullable=True),
        sa.Column("pdf_size_bytes", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("render_snapshots")
