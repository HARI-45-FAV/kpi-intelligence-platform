"""source governance metadata and membership access scope

Additive only. Every column below is new, and each carries a server default so
existing rows become valid without being rewritten. The defaults are dropped
again once applied so the ORM stays the single place a default is declared.

Nothing existing is altered or removed: AgentRun, DetectionRun, KPI definitions
and previously recorded detection results are untouched by this revision.

The one data statement is a backfill of ``table_grains.grain_status``. A grain
that an administrator had already declared is DECLARED; everything else stays
PROPOSED, because inference on its own has never been more than a proposal.

Revision ID: a1d4f7b2c903
Revises: 7c1f2e9a4b6d
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a1d4f7b2c903"
down_revision: str | None = "7c1f2e9a4b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------- membership
    # Access scope on the membership, not on the user: the same person may be
    # entitled to different domains and document scopes in different companies.
    with op.batch_alter_table("company_users") as batch_op:
        batch_op.add_column(
            sa.Column("allowed_domains", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column(
                "allowed_document_scopes", sa.JSON(), nullable=False, server_default="[]"
            )
        )
    with op.batch_alter_table("company_users") as batch_op:
        batch_op.alter_column("allowed_domains", server_default=None)
        batch_op.alter_column("allowed_document_scopes", server_default=None)

    # -------------------------------------------------------------- data sources
    with op.batch_alter_table("data_sources") as batch_op:
        # Where a source that has no driver actually lives. Never a credential.
        batch_op.add_column(sa.Column("connection_reference", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("business_calendar_id", sa.String(length=36), nullable=True))
        # Derived governance rollup, written only by an explicit profile or health
        # check. Null means "never measured", which is the honest starting state.
        batch_op.add_column(sa.Column("grain", sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("coverage_start", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("coverage_end", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("completeness_pct", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("quality_score", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "health_status", sa.String(length=20), nullable=False, server_default="UNKNOWN"
            )
        )
        batch_op.add_column(
            sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("health_reason", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_data_sources_business_calendar_id",
            "company_calendars",
            ["business_calendar_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("data_sources") as batch_op:
        batch_op.alter_column("health_status", server_default=None)

    # ------------------------------------------------------------- source tables
    with op.batch_alter_table("source_tables") as batch_op:
        # Human-owned naming and description. Discovery never writes these.
        batch_op.add_column(sa.Column("display_name", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        # Candidate lists rather than single answers: collapsing them to one value
        # would hide exactly the ambiguity a reviewer needs to resolve.
        batch_op.add_column(
            sa.Column(
                "primary_identifier_candidates", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch_op.add_column(
            sa.Column("time_field_candidates", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("company_field_candidates", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column(
                "candidates_status", sa.String(length=20), nullable=False, server_default="PROPOSED"
            )
        )
        batch_op.add_column(sa.Column("profiled_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("source_tables") as batch_op:
        batch_op.alter_column("primary_identifier_candidates", server_default=None)
        batch_op.alter_column("time_field_candidates", server_default=None)
        batch_op.alter_column("company_field_candidates", server_default=None)
        batch_op.alter_column("candidates_status", server_default=None)

    # ------------------------------------------------------------ source columns
    # candidate_role is the machine's proposal and is rewritten on every profile;
    # confirmed_role is a review decision and is never overwritten. Existing rows
    # start at UNKNOWN/PROPOSED because nothing has proposed a role for them yet —
    # the next discovery or profile fills them in.
    with op.batch_alter_table("source_columns") as batch_op:
        batch_op.add_column(
            sa.Column(
                "candidate_role", sa.String(length=30), nullable=False, server_default="UNKNOWN"
            )
        )
        batch_op.add_column(sa.Column("confirmed_role", sa.String(length=30), nullable=True))
        batch_op.add_column(
            sa.Column(
                "role_status", sa.String(length=20), nullable=False, server_default="PROPOSED"
            )
        )
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
    with op.batch_alter_table("source_columns") as batch_op:
        batch_op.alter_column("candidate_role", server_default=None)
        batch_op.alter_column("role_status", server_default=None)

    # -------------------------------------------------------------- table grains
    with op.batch_alter_table("table_grains") as batch_op:
        batch_op.add_column(
            sa.Column(
                "grain_status", sa.String(length=20), nullable=False, server_default="PROPOSED"
            )
        )
        batch_op.add_column(sa.Column("confirmed_grain", sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column("confirmed_by", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_table_grains_confirmed_by", "users", ["confirmed_by"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("table_grains") as batch_op:
        batch_op.alter_column("grain_status", server_default=None)

    # A grain an administrator had already declared keeps that authority. Note
    # that no row becomes CONFIRMED here: confirmation is a decision a person
    # makes, and this migration is not that person.
    op.execute(
        "UPDATE table_grains SET grain_status = 'DECLARED' "
        "WHERE declared_grain IS NOT NULL AND declared_grain != ''"
    )


def downgrade() -> None:
    with op.batch_alter_table("table_grains") as batch_op:
        batch_op.drop_constraint("fk_table_grains_confirmed_by", type_="foreignkey")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("confirmed_by")
        batch_op.drop_column("confirmed_grain")
        batch_op.drop_column("grain_status")

    with op.batch_alter_table("source_columns") as batch_op:
        batch_op.drop_column("description")
        batch_op.drop_column("role_status")
        batch_op.drop_column("confirmed_role")
        batch_op.drop_column("candidate_role")

    with op.batch_alter_table("source_tables") as batch_op:
        batch_op.drop_column("profiled_at")
        batch_op.drop_column("candidates_status")
        batch_op.drop_column("company_field_candidates")
        batch_op.drop_column("time_field_candidates")
        batch_op.drop_column("primary_identifier_candidates")
        batch_op.drop_column("description")
        batch_op.drop_column("display_name")

    with op.batch_alter_table("data_sources") as batch_op:
        batch_op.drop_constraint("fk_data_sources_business_calendar_id", type_="foreignkey")
        batch_op.drop_column("health_reason")
        batch_op.drop_column("health_checked_at")
        batch_op.drop_column("health_status")
        batch_op.drop_column("quality_score")
        batch_op.drop_column("completeness_pct")
        batch_op.drop_column("coverage_end")
        batch_op.drop_column("coverage_start")
        batch_op.drop_column("last_refresh_at")
        batch_op.drop_column("grain")
        batch_op.drop_column("business_calendar_id")
        batch_op.drop_column("connection_reference")

    with op.batch_alter_table("company_users") as batch_op:
        batch_op.drop_column("allowed_document_scopes")
        batch_op.drop_column("allowed_domains")
