"""review report schema

Revision ID: 0007_review_reports
Revises: 0006_render_snapshots
Create Date: 2026-05-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_review_reports"
down_revision: str | None = "0006_render_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_reports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("patch_plan_id", sa.String(length=36), sa.ForeignKey("patch_plans.id")),
        sa.Column(
            "render_snapshot_id",
            sa.String(length=36),
            sa.ForeignKey("render_snapshots.id"),
        ),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "review_findings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "review_report_id",
            sa.String(length=36),
            sa.ForeignKey("review_reports.id"),
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
            "patch_operation_id",
            sa.String(length=36),
            sa.ForeignKey("patch_operations.id"),
        ),
        sa.Column("severity", sa.String(length=16), nullable=False, index=True),
        sa.Column("category", sa.String(length=128), nullable=False, index=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("review_findings")
    op.drop_table("review_reports")
