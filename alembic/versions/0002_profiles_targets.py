"""profiles and target mapping schema

Revision ID: 0002_profiles_targets
Revises: 0001_initial
Create Date: 2026-05-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_profiles_targets"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "format_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "profile_rules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "profile_id",
            sa.String(length=36),
            sa.ForeignKey("format_profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("rule_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("element_category", sa.String(length=128), nullable=False, index=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("selector", sa.JSON(), nullable=False),
        sa.Column("properties", sa.JSON(), nullable=False),
        sa.Column("source_atom_ids", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "target_elements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("source_atom_id", sa.String(length=36), sa.ForeignKey("format_atoms.id")),
        sa.Column("element_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("element_category", sa.String(length=128), nullable=True, index=True),
        sa.Column("part_name", sa.String(length=512), nullable=False, index=True),
        sa.Column("xml_path", sa.String(length=1024), nullable=True),
        sa.Column("text_summary", sa.Text(), nullable=True),
        sa.Column("style_id", sa.String(length=255), nullable=True, index=True),
        sa.Column("numbering_id", sa.String(length=255), nullable=True, index=True),
        sa.Column("normalized", sa.JSON(), nullable=False),
        sa.Column("classification", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "mapping_results",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "target_element_id",
            sa.String(length=36),
            sa.ForeignKey("target_elements.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("profile_rule_id", sa.String(length=36), sa.ForeignKey("profile_rules.id")),
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


def downgrade() -> None:
    op.drop_table("mapping_results")
    op.drop_table("target_elements")
    op.drop_table("profile_rules")
    op.drop_table("format_profiles")
