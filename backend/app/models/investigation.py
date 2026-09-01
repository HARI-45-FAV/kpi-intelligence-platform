"""Investigation findings: what a person concluded, and where they concluded it.

Everything else the investigation surface produces is a *measurement* — a
movement apportioned across a dimension, stored as a
:class:`~app.models.detection.ContributionRun` so it can be re-displayed and
defended. This table holds the other half: the sentence a human wrote about it.

Three properties the design turns on:

* **It is anchored, not free-floating.** A finding names the KPI, the date and
  — optionally — the dimension, drill path and entity it was written against.
  A note that says "this looks like a pricing change" is worth very little
  without "…about North Region's contribution to Net Revenue on 2026-08-11",
  and the anchor is what lets the investigation surface show a person's earlier
  conclusion when they return to the same node.
* **Its status is the investigation's, not the KPI's.** ``status`` moves
  OPEN → IN PROGRESS → RESOLVED and back; the detection verdict is untouched by
  it. Nobody closes an anomaly by writing a note, and this table has no column
  that could be mistaken for a verdict about the business.
* **Every timestamp is real.** ``created_at``, ``updated_at`` and
  ``resolved_at`` are written when the events they name actually happen. There
  is no synthesised history here, and the investigation UI has none to draw.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import FindingStatus, Timestamped, UUIDPrimaryKey, UtcDateTime


class InvestigationFinding(Base, UUIDPrimaryKey, Timestamped):
    """One person's written conclusion about one movement, or one part of it."""

    __tablename__ = "investigation_findings"
    __table_args__ = (
        Index(
            "ix_investigation_findings_lookup",
            "company_id",
            "kpi_key",
            "target_date",
        ),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The movement this finding is about. Kept as SET NULL for the same reason a
    #: contribution run is: the note survives the deletion of the number, because
    #: "who concluded what" is the part an audit needs longest.
    detection_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detection_runs.id", ondelete="SET NULL"), index=True
    )
    kpi_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    kpi_name: Mapped[str] = mapped_column(String(200), nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # --- Where in the investigation this was written --------------------------
    #: NULL means the finding is about the KPI's whole movement rather than any
    #: one part of it — the root node of the investigation.
    dimension: Mapped[str | None] = mapped_column(String(120))
    #: The value within ``dimension`` this note is about, when it is about one.
    entity: Mapped[str | None] = mapped_column(String(200))
    #: The ancestors chosen before this node, ``[{"dimension": ..., "value": ...}]``,
    #: in the same shape a contribution run stores. This is what makes a finding
    #: written three levels deep re-locatable.
    path: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # --- What was concluded ---------------------------------------------------
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default=FindingStatus.OPEN, nullable=False, index=True
    )

    # --- Who, and when ---------------------------------------------------------
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_email: Mapped[str | None] = mapped_column(String(255))
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_email: Mapped[str | None] = mapped_column(String(255))
    #: Written only when the status actually becomes RESOLVED, and cleared when it
    #: stops being RESOLVED. A timestamp that is present is a timestamp that means
    #: something happened.
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    def scope_label(self) -> str:
        """How this finding's anchor reads, for a list that mixes several nodes."""
        if self.dimension and self.entity:
            return f"{self.dimension}: {self.entity}"
        if self.dimension:
            return f"By {self.dimension}"
        return "Whole KPI movement"
