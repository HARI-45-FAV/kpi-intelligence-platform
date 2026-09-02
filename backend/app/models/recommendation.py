"""What a reader did with a recommendation.

The recommendations themselves are never stored. They are derived deterministically
from rows the platform already holds — a detection verdict, a stored breakdown, the
company's registered drivers — so persisting them would create a second copy that
could quietly disagree with the evidence it came from. Re-deriving is cheap and
always current; a saved recommendation would age badly the moment someone drilled
deeper.

What *is* worth keeping is the human half: whether the suggestion was any use, and
whether anybody acted on it. That is not derivable from anything, and it is the
only signal the platform can learn from here.

Two boundaries this table holds:

* **It judges the recommendation, never the KPI.** There is no column here that
  touches a detection verdict or an investigation status. Marking an action
  "not useful" says the advice missed; it does not un-flag an anomaly, and no
  reader of the results page will see a measured verdict change because of a
  thumbs-down.
* **Action status is self-reported.** This platform performs no business action
  and verifies none. ``action_status`` is one person telling their colleagues
  where a review got to — advisory, not a workflow state anything depends on.

Keyed by ``recommendation_key``, which the engine derives from the lever and the
target area rather than from a position in a list, so a reader's feedback stays
attached to the advice they were actually given even after a deeper breakdown
reorders the cards.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import (
    RecommendationActionStatus,
    RecommendationUsefulness,
    Timestamped,
    UUIDPrimaryKey,
    UtcDateTime,
)


class RecommendationFeedback(Base, UUIDPrimaryKey, Timestamped):
    """One person's response to one recommendation on one stored result."""

    __tablename__ = "recommendation_feedback"
    __table_args__ = (
        # One row per person per recommendation: submitting again is a correction of
        # what they said before, not a second opinion from the same reader.
        UniqueConstraint(
            "company_id",
            "detection_run_id",
            "recommendation_key",
            "created_by_user_id",
            name="uq_recommendation_feedback_reader",
        ),
        Index(
            "ix_recommendation_feedback_lookup",
            "company_id",
            "detection_run_id",
        ),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The result the recommendation was shown against. CASCADE rather than SET NULL:
    #: feedback on advice about a deleted movement has nothing left to mean, unlike a
    #: written finding, which stands on its own words.
    detection_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("detection_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    #: The engine's stable identity for one recommendation: lever plus target area.
    recommendation_key: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Denormalised so a reviewer reading this table months later can see which
    #: lever was suggested without re-deriving the whole recommendation set.
    lever_key: Mapped[str | None] = mapped_column(String(80))
    target_entity: Mapped[str | None] = mapped_column(String(300))

    usefulness: Mapped[str] = mapped_column(
        String(20), default=RecommendationUsefulness.USEFUL, nullable=False
    )
    action_status: Mapped[str] = mapped_column(
        String(20), default=RecommendationActionStatus.NOT_STARTED, nullable=False
    )
    #: Optional. What the reader wanted to say that the three buttons could not.
    comment: Mapped[str | None] = mapped_column(Text)

    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_email: Mapped[str | None] = mapped_column(String(255))
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, index=True)
