"""patch plan schema

Revision ID: 0004_patch_plans
Revises: 0003_hybrid_retrieval
Create Date: 2026-05-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_patch_plans"
down_revision: str | None = "0003_hybrid_retrieval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "patch_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "template_document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "patch_operations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "patch_plan_id",
            sa.String(length=36),
            sa.ForeignKey("patch_plans.id"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "target_element_id",
            sa.String(length=36),
            sa.ForeignKey("target_elements.id"),
            nullable=False,
        ),
        sa.Column("mapping_result_id", sa.String(length=36), sa.ForeignKey("mapping_results.id")),
        sa.Column("profile_rule_id", sa.String(length=36), sa.ForeignKey("profile_rules.id")),
        sa.Column("operation_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("part_name", sa.String(length=512), nullable=False, index=True),
        sa.Column("xml_path", sa.String(length=1024), nullable=True),
        sa.Column("selector", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False, index=True),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("rationale", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("patch_operations")
    op.drop_table("patch_plans")
