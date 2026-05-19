"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-19
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False, index=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False, index=True),
        sa.Column("is_current_template", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_id", sa.String(length=36), sa.ForeignKey("documents.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False, index=True),
        sa.Column("raw_file", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "ooxml_parts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
        ),
        sa.Column("part_name", sa.String(length=512), nullable=False, index=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("is_xml", sa.Boolean(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("xml_text", sa.Text(), nullable=True),
        sa.Column("binary_data", sa.LargeBinary(), nullable=True),
        sa.Column("parsed_summary", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "relationships",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
        ),
        sa.Column("source_part", sa.String(length=512), nullable=False, index=True),
        sa.Column("relationship_id", sa.String(length=255), nullable=False),
        sa.Column("relationship_type", sa.String(length=512), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("target_mode", sa.String(length=64), nullable=True),
        sa.Column("resolved_target", sa.String(length=512), nullable=True, index=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
        ),
        sa.Column("part_name", sa.String(length=512), nullable=False, index=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "format_atoms",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=False,
        ),
        sa.Column("atom_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("part_name", sa.String(length=512), nullable=False, index=True),
        sa.Column("xml_path", sa.String(length=1024), nullable=True),
        sa.Column("raw_xml", sa.Text(), nullable=True),
        sa.Column("normalized", sa.JSON(), nullable=False),
        sa.Column("text_summary", sa.Text(), nullable=True),
        sa.Column("element_category", sa.String(length=128), nullable=True, index=True),
        sa.Column("page_context", sa.JSON(), nullable=True),
        sa.Column("style_id", sa.String(length=255), nullable=True, index=True),
        sa.Column("numbering_id", sa.String(length=255), nullable=True, index=True),
        sa.Column("relationship_id", sa.String(length=255), nullable=True),
        sa.Column("render_metrics", sa.JSON(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=1536), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "provider_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.String(length=128), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_calls")
    op.drop_table("format_atoms")
    op.drop_table("media_assets")
    op.drop_table("relationships")
    op.drop_table("ooxml_parts")
    op.drop_table("document_versions")
    op.drop_table("documents")
