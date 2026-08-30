"""Detection: company comparison configuration and persisted detection runs.

The separation this module exists to enforce:

* **KPI registration** (``app.models.kpi``) says *what* a KPI means and *where*
  its data lives — source table, formula, time field.
* **A bucket configuration** (here) says *when* history is comparable for a
  company — which weekdays, which week of the month, which months or season,
  which event dates.
* **The detection engine** (``app.services.detection``) says *how* to detect:
  actual, expected, deviation, status.

Because the second is data and not code, the same engine serves a company whose
peak is Friday and one whose peak is Tuesday with no branching. Nothing in this
module names a company, a table, a column, a weekday or an event.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import (
    BucketConfigSource,
    BucketConfigStatus,
    TimeGrain,
    Timestamped,
    UUIDPrimaryKey,
    UtcDateTime,
)


class CompanyBucketConfig(Base, UUIDPrimaryKey, Timestamped):
    """A company's answer to "which past days are comparable to this one?".

    Versioned and approved rather than edited in place, for the same reason KPI
    versions are: a detection result stays explainable only if the configuration
    that produced it can still be read back exactly as it was.

    ``kpi_key`` is NULL for the company-wide default. A row that names a KPI key
    overrides the default for that KPI alone — a company whose Revenue peaks on
    weekends but whose Support Tickets peak on Mondays needs two rows, not two
    code paths.
    """

    __tablename__ = "company_bucket_configs"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "config_key", "version", name="uq_bucket_config_company_key_version"
        ),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    config_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # NULL = company-wide default. Set = override for this KPI key only.
    kpi_key: Mapped[str | None] = mapped_column(String(80), index=True)

    status: Mapped[str] = mapped_column(
        String(20), default=BucketConfigStatus.DRAFT, nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # The validated configuration itself: the five fixed slots, each with the
    # company's own values. Stored normalised by ``validate_bucket_config`` so
    # the engine never re-interprets free-form input at detection time.
    buckets: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Company-tunable search behaviour. Kept beside the buckets because they are
    # business judgements ("we only trust the last two years"), not algorithm
    # constants.
    lookback_days: Mapped[int | None] = mapped_column(Integer)
    min_reference_points: Mapped[int | None] = mapped_column(Integer)
    max_reference_points: Mapped[int | None] = mapped_column(Integer)

    # Provenance. A model may draft this; only a human approval makes it usable.
    source: Mapped[str] = mapped_column(
        String(20), default=BucketConfigSource.MANUAL, nullable=False
    )
    source_document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("company_documents.id", ondelete="SET NULL")
    )
    source_document_version_id: Mapped[str | None] = mapped_column(String(36))
    extraction_model: Mapped[str | None] = mapped_column(String(120))
    extraction_notes: Mapped[str | None] = mapped_column(Text)

    proposed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    approval_reason: Mapped[str | None] = mapped_column(Text)


class AgentRun(Base, UUIDPrimaryKey, Timestamped):
    """One explicit batch execution requested by a user."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_company_target", "company_id", "target_date"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="RUNNING")
    kpi_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    abnormal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_confidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    executed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class DetectionRun(Base, UUIDPrimaryKey, Timestamped):
    """One evaluation of one KPI on one date, kept for audit and for the UI.

    Everything needed to re-explain the number is stored: the reference dates,
    their values, the robust statistics, the tolerance that applied and the
    bucket configuration version in force. The business surface reads only the
    first handful of columns; the rest exist so a challenged result can be
    defended without re-running anything.
    """

    __tablename__ = "detection_runs"
    __table_args__ = (
        Index("ix_detection_runs_lookup", "company_id", "kpi_version_id", "target_date"),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kpi_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_key: Mapped[str] = mapped_column(String(80), nullable=False)
    kpi_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kpi_version: Mapped[int] = mapped_column(Integer, nullable=False)

    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    time_grain: Mapped[str] = mapped_column(String(20), default=TimeGrain.DAY, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str | None] = mapped_column(String(10))

    # --- The business answer -------------------------------------------------
    actual_value: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    deviation_absolute: Mapped[float | None] = mapped_column(Float)
    deviation_pct: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    comparison_label: Mapped[str | None] = mapped_column(String(200))
    headline: Mapped[str | None] = mapped_column(Text)

    # --- How the expected value was reached ----------------------------------
    bucket_applied: Mapped[str] = mapped_column(String(30), nullable=False)
    buckets_applied: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    bucket_config_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("company_bucket_configs.id", ondelete="SET NULL")
    )
    bucket_config_key: Mapped[str | None] = mapped_column(String(80))
    bucket_config_version: Mapped[int | None] = mapped_column(Integer)
    bucket_signature: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    reference_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reference_dates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    reference_values: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # --- Deterministic statistics -------------------------------------------
    median_value: Mapped[float | None] = mapped_column(Float)
    mad: Mapped[float | None] = mapped_column(Float)
    dispersion_basis: Mapped[str | None] = mapped_column(String(40))
    modified_z_score: Mapped[float | None] = mapped_column(Float)
    z_threshold: Mapped[float | None] = mapped_column(Float)

    tolerance_pct: Mapped[float | None] = mapped_column(Float)
    tolerance_absolute: Mapped[float | None] = mapped_column(Float)
    breached_tolerance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    statistically_significant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    yoy_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    yoy_adjustment_factor: Mapped[float | None] = mapped_column(Float)

    # --- Provenance ----------------------------------------------------------
    method: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    query_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    executed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    executed_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, index=True
    )


class ContributionRun(Base, UUIDPrimaryKey, Timestamped):
    """One investigation: a stored detection movement, split across one dimension.

    Kept for the same reasons a :class:`DetectionRun` is, and stored alongside it
    rather than in a separate history: an investigation is a read of the company's
    own business data, broken down the way a particular person chose, and "which
    part of the business did we look at, on whose behalf, and what did it say" has
    to remain answerable months later.

    Two things this table is careful about:

    * **It points at the run it split.** ``detection_run_id`` is the movement being
      apportioned. A contribution result with no run behind it would be an
      expectation this platform never published, so the link is the record that the
      parts belong to a whole the business actually saw.
    * **It stores no verdict about a contributor.** There is a ``kpi_status``,
      carried over unchanged from the detection run, and there is deliberately no
      status column for any entity. The largest contributor is the largest
      contributor; contribution ranks parts, it does not judge them, and a column
      here would eventually be read as though it did.

    ``contributors`` holds the ranked parts as returned — label, actual, expected,
    change, share — so a result can be re-displayed without re-querying the source,
    which is what makes an old investigation reproducible even after the underlying
    rows have moved on.
    """

    __tablename__ = "contribution_runs"
    __table_args__ = (
        Index(
            "ix_contribution_runs_lookup",
            "company_id",
            "kpi_version_id",
            "target_date",
        ),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detection_runs.id", ondelete="SET NULL"), index=True
    )
    kpi_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_key: Mapped[str] = mapped_column(String(80), nullable=False)
    kpi_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kpi_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Carried from the KPI version so a stored breakdown can be re-displayed with
    #: the same units it was read in, without resolving the version again.
    unit: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str | None] = mapped_column(String(10))

    # --- What was broken down, and how deep -----------------------------------
    dimension: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The ancestors chosen before this level, as ``[{"dimension": ..., "value": ...}]``.
    path: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: ``AUTOMATIC`` for the investigation flow, ``MANUAL`` for the dimensional form.
    entry_point: Mapped[str] = mapped_column(String(20), default="AUTOMATIC", nullable=False)

    # --- The movement that was split, as detection measured it ----------------
    kpi_actual: Mapped[float | None] = mapped_column(Float)
    kpi_expected: Mapped[float | None] = mapped_column(Float)
    kpi_movement: Mapped[float | None] = mapped_column(Float)
    #: The KPI's verdict, carried through. It belongs to the KPI and to no part of it.
    kpi_status: Mapped[str | None] = mapped_column(String(20))

    # --- The split ------------------------------------------------------------
    contributors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ranked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    explained_pct: Mapped[float | None] = mapped_column(Float)
    unexplained_pct: Mapped[float | None] = mapped_column(Float)
    leader_entity: Mapped[str | None] = mapped_column(String(200))
    leader_share_pct: Mapped[float | None] = mapped_column(Float)
    leader_is_sufficient: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    additive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    shares_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Provenance ----------------------------------------------------------
    reference_dates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    #: How many entity values the caller's row scope kept out of the ranking. A
    #: non-zero value is why the visible shares may not sum to the whole.
    withheld_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    queries: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    query_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    executed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, index=True)
