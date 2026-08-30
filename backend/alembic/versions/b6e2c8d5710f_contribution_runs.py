"""persist contribution analyses alongside detection runs

Revision ID: b6e2c8d5710f
Revises: a1d4f7b2c903

An investigation is a read of the company's own business data, broken down the way
one person chose. That makes it an auditable event, not a transient view, so its
result is stored the way a detection run is -- linked to the run whose movement it
split, with the ranked parts kept so an old investigation can be re-displayed
without re-querying a source whose rows have since moved on.

There is deliberately no per-contributor status column. A contribution is
arithmetic on measured values; the only verdict in this table is ``kpi_status``,
carried over from the detection run it belongs to.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b6e2c8d5710f"
down_revision: str | None = "a1d4f7b2c903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contribution_runs",
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("detection_run_id", sa.String(length=36), nullable=True),
        sa.Column("kpi_definition_id", sa.String(length=36), nullable=False),
        sa.Column("kpi_version_id", sa.String(length=36), nullable=False),
        sa.Column("kpi_key", sa.String(length=80), nullable=False),
        sa.Column("kpi_name", sa.String(length=200), nullable=False),
        sa.Column("kpi_version", sa.Integer(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("dimension", sa.String(length=120), nullable=False),
        sa.Column("path", sa.JSON(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("entry_point", sa.String(length=20), nullable=False),
        sa.Column("kpi_actual", sa.Float(), nullable=True),
        sa.Column("kpi_expected", sa.Float(), nullable=True),
        sa.Column("kpi_movement", sa.Float(), nullable=True),
        sa.Column("kpi_status", sa.String(length=20), nullable=True),
        sa.Column("contributors", sa.JSON(), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("ranked_count", sa.Integer(), nullable=False),
        sa.Column("explained_pct", sa.Float(), nullable=True),
        sa.Column("unexplained_pct", sa.Float(), nullable=True),
        sa.Column("leader_entity", sa.String(length=200), nullable=True),
        sa.Column("leader_share_pct", sa.Float(), nullable=True),
        sa.Column("leader_is_sufficient", sa.Boolean(), nullable=False),
        sa.Column("additive", sa.Boolean(), nullable=False),
        sa.Column("shares_available", sa.Boolean(), nullable=False),
        sa.Column("reference_dates", sa.JSON(), nullable=False),
        sa.Column("withheld_count", sa.Integer(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("queries", sa.JSON(), nullable=False),
        sa.Column("query_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("executed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        # The movement outlives its breakdown rather than the other way round: if a
        # detection run is removed the investigation is kept and simply stops
        # pointing at one, because the audit question "who looked at this" survives
        # the deletion of the number they looked at.
        sa.ForeignKeyConstraint(
            ["detection_run_id"], ["detection_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["kpi_definition_id"], ["kpi_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["kpi_version_id"], ["kpi_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["executed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("contribution_runs") as batch_op:
        batch_op.create_index("ix_contribution_runs_company_id", ["company_id"], unique=False)
        batch_op.create_index(
            "ix_contribution_runs_detection_run_id", ["detection_run_id"], unique=False
        )
        batch_op.create_index(
            "ix_contribution_runs_kpi_definition_id", ["kpi_definition_id"], unique=False
        )
        batch_op.create_index(
            "ix_contribution_runs_kpi_version_id", ["kpi_version_id"], unique=False
        )
        batch_op.create_index("ix_contribution_runs_target_date", ["target_date"], unique=False)
        batch_op.create_index("ix_contribution_runs_executed_at", ["executed_at"], unique=False)
        batch_op.create_index(
            "ix_contribution_runs_lookup",
            ["company_id", "kpi_version_id", "target_date"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("contribution_runs") as batch_op:
        batch_op.drop_index("ix_contribution_runs_lookup")
        batch_op.drop_index("ix_contribution_runs_executed_at")
        batch_op.drop_index("ix_contribution_runs_target_date")
        batch_op.drop_index("ix_contribution_runs_kpi_version_id")
        batch_op.drop_index("ix_contribution_runs_kpi_definition_id")
        batch_op.drop_index("ix_contribution_runs_detection_run_id")
        batch_op.drop_index("ix_contribution_runs_company_id")
    op.drop_table("contribution_runs")
