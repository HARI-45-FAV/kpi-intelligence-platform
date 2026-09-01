"""investigation findings: the human conclusion beside the measurement

Revision ID: d3b8c1f60a72
Revises: b6e2c8d5710f

A contribution run records what the platform measured when someone split a
movement. This table records what that person concluded — anchored to the KPI,
the date and, when the note was written against one part of the business, the
dimension, drill path and entity it belongs to.

Two deliberate absences:

* **No verdict column.** ``status`` is the state of the *investigation*
  (OPEN / IN PROGRESS / RESOLVED), never of the KPI. The detection verdict lives
  in ``detection_runs.status`` and nothing here can change it.
* **No synthesised history.** ``created_at``, ``updated_at`` and ``resolved_at``
  are written when those events happen and are NULL until they do, so the
  investigation surface has no invented timeline available to draw.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d3b8c1f60a72"
down_revision: str | None = "b6e2c8d5710f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigation_findings",
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("detection_run_id", sa.String(length=36), nullable=True),
        sa.Column("kpi_definition_id", sa.String(length=36), nullable=False),
        sa.Column("kpi_key", sa.String(length=80), nullable=False),
        sa.Column("kpi_name", sa.String(length=200), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("dimension", sa.String(length=120), nullable=True),
        sa.Column("entity", sa.String(length=200), nullable=True),
        sa.Column("path", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_email", sa.String(length=255), nullable=True),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("updated_by_email", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        # The note outlives the number it was written about, for the same reason a
        # contribution run does: "who concluded what, and when" is the part an
        # audit needs longest.
        sa.ForeignKeyConstraint(
            ["detection_run_id"], ["detection_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["kpi_definition_id"], ["kpi_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("investigation_findings") as batch_op:
        batch_op.create_index(
            "ix_investigation_findings_company_id", ["company_id"], unique=False
        )
        batch_op.create_index(
            "ix_investigation_findings_detection_run_id", ["detection_run_id"], unique=False
        )
        batch_op.create_index(
            "ix_investigation_findings_kpi_definition_id",
            ["kpi_definition_id"],
            unique=False,
        )
        batch_op.create_index("ix_investigation_findings_kpi_key", ["kpi_key"], unique=False)
        batch_op.create_index(
            "ix_investigation_findings_target_date", ["target_date"], unique=False
        )
        batch_op.create_index("ix_investigation_findings_status", ["status"], unique=False)
        batch_op.create_index(
            "ix_investigation_findings_lookup",
            ["company_id", "kpi_key", "target_date"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("investigation_findings") as batch_op:
        batch_op.drop_index("ix_investigation_findings_lookup")
        batch_op.drop_index("ix_investigation_findings_status")
        batch_op.drop_index("ix_investigation_findings_target_date")
        batch_op.drop_index("ix_investigation_findings_kpi_key")
        batch_op.drop_index("ix_investigation_findings_kpi_definition_id")
        batch_op.drop_index("ix_investigation_findings_detection_run_id")
        batch_op.drop_index("ix_investigation_findings_company_id")
    op.drop_table("investigation_findings")
