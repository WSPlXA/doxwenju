"""patch execution schema

Revision ID: 0005_patch_execution
Revises: 0004_patch_plans
Create Date: 2026-05-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_patch_execution"
down_revision: str | None = "0004_patch_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patch_plans",
        sa.Column(
            "output_document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=True,
        ),
    )
    op.create_table(
        "patch_executions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "patch_plan_id",
            sa.String(length=36),
            sa.ForeignKey("patch_plans.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "output_document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("patch_executions")
    op.drop_column("patch_plans", "output_document_version_id")
