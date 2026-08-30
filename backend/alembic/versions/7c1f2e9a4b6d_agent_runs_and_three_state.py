"""persist aggregate agent runs and link KPI detections

Revision ID: 7c1f2e9a4b6d
Revises: 495cfc3af89a
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7c1f2e9a4b6d"
down_revision: str | None = "495cfc3af89a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing persisted four-state results remain readable under the final
    # contract; the former intermediate state is now an abnormal movement.
    op.execute("UPDATE detection_runs SET status = 'ABNORMAL' WHERE status = 'WATCH'")
    op.create_table(
        "agent_runs",
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("kpi_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("normal_count", sa.Integer(), nullable=False),
        sa.Column("abnormal_count", sa.Integer(), nullable=False),
        sa.Column("low_confidence_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("executed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["executed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.create_index("ix_agent_runs_company_id", ["company_id"], unique=False)
        batch_op.create_index("ix_agent_runs_target_date", ["target_date"], unique=False)
        batch_op.create_index("ix_agent_runs_company_target", ["company_id", "target_date"], unique=False)

    with op.batch_alter_table("detection_runs") as batch_op:
        batch_op.add_column(sa.Column("agent_run_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_detection_runs_agent_run_id", "agent_runs", ["agent_run_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.create_index("ix_detection_runs_agent_run_id", ["agent_run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("detection_runs") as batch_op:
        batch_op.drop_index("ix_detection_runs_agent_run_id")
        batch_op.drop_constraint("fk_detection_runs_agent_run_id", type_="foreignkey")
        batch_op.drop_column("agent_run_id")
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_index("ix_agent_runs_company_target")
        batch_op.drop_index("ix_agent_runs_target_date")
        batch_op.drop_index("ix_agent_runs_company_id")
    op.drop_table("agent_runs")
