"""recommendation feedback: what a reader did with the suggested action

Revision ID: f4a7d2e9c318
Revises: d3b8c1f60a72

The recommendations shown on a result are not stored. They are derived on read
from rows that already exist — the detection verdict, any stored contribution run,
the KPI version's registered drivers and criticality — so there is deliberately no
``recommendations`` table here. A persisted copy would be a second version of the
evidence, free to drift from the numbers it was derived from the moment somebody
drilled a level deeper.

This table holds the half that cannot be derived: whether the advice was useful,
and whether anyone acted on it.

Three deliberate properties:

* **Nothing here can change a verdict.** There is no detection status and no
  investigation status column. ``usefulness`` is about the recommendation;
  ``action_status`` is about a person's own review. A thumbs-down never un-flags
  an anomaly.
* **One row per reader per recommendation.** The unique constraint makes a second
  submission a correction rather than a duplicate vote, which is what an upsert
  endpoint needs to be idempotent.
* **CASCADE on the run.** Unlike an investigation finding, which stands on its own
  sentence, feedback about advice on a deleted movement has nothing left to mean.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f4a7d2e9c318"
down_revision: str | None = "d3b8c1f60a72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_feedback",
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("detection_run_id", sa.String(length=36), nullable=False),
        sa.Column("kpi_key", sa.String(length=80), nullable=False),
        sa.Column("recommendation_key", sa.String(length=300), nullable=False),
        sa.Column("lever_key", sa.String(length=80), nullable=True),
        sa.Column("target_entity", sa.String(length=300), nullable=True),
        sa.Column("usefulness", sa.String(length=20), nullable=False),
        sa.Column("action_status", sa.String(length=20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_email", sa.String(length=255), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["detection_run_id"], ["detection_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "detection_run_id",
            "recommendation_key",
            "created_by_user_id",
            name="uq_recommendation_feedback_reader",
        ),
    )
    with op.batch_alter_table("recommendation_feedback") as batch_op:
        batch_op.create_index(
            "ix_recommendation_feedback_company_id", ["company_id"], unique=False
        )
        batch_op.create_index(
            "ix_recommendation_feedback_detection_run_id", ["detection_run_id"], unique=False
        )
        batch_op.create_index("ix_recommendation_feedback_kpi_key", ["kpi_key"], unique=False)
        batch_op.create_index(
            "ix_recommendation_feedback_submitted_at", ["submitted_at"], unique=False
        )
        batch_op.create_index(
            "ix_recommendation_feedback_lookup",
            ["company_id", "detection_run_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("recommendation_feedback") as batch_op:
        batch_op.drop_index("ix_recommendation_feedback_lookup")
        batch_op.drop_index("ix_recommendation_feedback_submitted_at")
        batch_op.drop_index("ix_recommendation_feedback_kpi_key")
        batch_op.drop_index("ix_recommendation_feedback_detection_run_id")
        batch_op.drop_index("ix_recommendation_feedback_company_id")
    op.drop_table("recommendation_feedback")
