"""agent run schema

Revision ID: 0007_agent_runs
Revises: 0006_render_snapshots
Create Date: 2026-05-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_agent_runs"
down_revision: str | None = "0006_render_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "target_document_version_id",
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
        sa.Column(
            "current_output_document_version_id",
            sa.String(length=36),
            sa.ForeignKey("document_versions.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.String(length=36),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default="0", index=True),
        sa.Column("step_type", sa.String(length=64), nullable=False, index=True),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column(
            "patch_plan_id",
            sa.String(length=36),
            sa.ForeignKey("patch_plans.id"),
            nullable=True,
        ),
        sa.Column(
            "patch_execution_id",
            sa.String(length=36),
            sa.ForeignKey("patch_executions.id"),
            nullable=True,
        ),
        sa.Column(
            "render_snapshot_id",
            sa.String(length=36),
            sa.ForeignKey("render_snapshots.id"),
            nullable=True,
        ),
        sa.Column("summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_steps")
    op.drop_table("agent_runs")
