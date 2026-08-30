"""KPI governance: definitions, versions, dimensions, drivers, materiality,
access policy, lineage and validation.

Two rules shape this module:

1. **The business owns the meaning.** The platform may *propose* a KPI from the
   data, but nothing becomes ACTIVE without an explicit human approval, and the
   approval is recorded with who/when/why.
2. **Declaring a dimension is not scheduling work.** ``kpi_dimensions`` says
   "region is a valid way to slice Revenue" — it does *not* mean anomaly
   detection runs per region. Monitoring happens at the KPI level; entity-level
   analysis stays selective.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import (
    Aggregation,
    DriverType,
    KpiKind,
    KpiStatus,
    TimeGrain,
    Timestamped,
    UUIDPrimaryKey,
    UtcDateTime,
    ValidationStatus,
)


class KpiDefinition(Base, UUIDPrimaryKey, Timestamped):
    """Stable identity of a KPI. Meaning lives in the versions."""

    __tablename__ = "kpi_definitions"
    __table_args__ = (UniqueConstraint("company_id", "kpi_key", name="uq_kpi_company_key"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=KpiStatus.DRAFT, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(String(36))
    owner_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )

    versions: Mapped[list["KpiVersion"]] = relationship(
        back_populates="definition",
        cascade="all, delete-orphan",
        order_by="KpiVersion.version",
        foreign_keys="KpiVersion.kpi_id",
    )


class KpiVersion(Base, UUIDPrimaryKey, Timestamped):
    """One immutable statement of what a KPI means.

    Editing an ACTIVE KPI never mutates the version in place: it creates v(n+1)
    in DRAFT, so insights already emitted keep pointing at the exact definition
    that produced them.
    """

    __tablename__ = "kpi_versions"
    __table_args__ = (UniqueConstraint("kpi_id", "version", name="uq_kpi_version"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=KpiStatus.DRAFT, nullable=False)

    # ---- Business meaning --------------------------------------------
    business_definition: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str | None] = mapped_column(String(8))
    # Higher-is-better / lower-is-better matters to Sprint 2's narratives.
    direction: Mapped[str] = mapped_column(String(20), default="HIGHER_IS_BETTER", nullable=False)

    # ---- Calculation --------------------------------------------------
    kind: Mapped[str] = mapped_column(String(20), default=KpiKind.SIMPLE, nullable=False)
    # Human-readable rendering, e.g. "SUM(sales.revenue)". Derived from the
    # structured spec below -- never executed as raw SQL.
    formula_expression: Mapped[str] = mapped_column(Text, nullable=False)
    # The governed machine-readable contract that SQL is generated from.
    formula_spec: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    aggregation: Mapped[str | None] = mapped_column(String(20))
    numerator: Mapped[dict | None] = mapped_column(JSON)
    denominator: Mapped[dict | None] = mapped_column(JSON)
    filters: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    null_handling: Mapped[str] = mapped_column(String(30), default="TREAT_AS_ZERO", nullable=False)

    # ---- Source binding -----------------------------------------------
    primary_data_source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("data_sources.id", ondelete="SET NULL")
    )
    primary_source_table_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="SET NULL")
    )
    source_definition: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # ---- Time ----------------------------------------------------------
    time_field: Mapped[str | None] = mapped_column(String(200))
    time_grain: Mapped[str] = mapped_column(String(20), default=TimeGrain.DAY, nullable=False)
    calendar_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("company_calendars.id", ondelete="SET NULL")
    )
    timezone: Mapped[str | None] = mapped_column(String(64))

    # ---- Behaviour (stored now, consumed by Sprint 2) -----------------
    expected_baseline_method: Mapped[str] = mapped_column(
        String(40), default="NOT_CONFIGURED", nullable=False
    )
    seasonality_expectation: Mapped[str | None] = mapped_column(String(60))
    sparse_history_strategy: Mapped[str] = mapped_column(
        String(40), default="PEER_BASELINE", nullable=False
    )
    min_history_days: Mapped[int | None] = mapped_column(Integer)

    # ---- Governance provenance ---------------------------------------
    # MANUAL == an administrator typed it; DISCOVERY == the platform proposed it.
    proposal_origin: Mapped[str] = mapped_column(String(20), default="MANUAL", nullable=False)
    discovery_evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # A reference document may *support* the definition, but it is never the
    # quantitative source.
    definition_document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("company_documents.id", ondelete="SET NULL")
    )
    definition_document_version: Mapped[int | None] = mapped_column(Integer)
    definition_source: Mapped[str | None] = mapped_column(String(200))

    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    reviewed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    approval_reason: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    deprecated_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    supersedes_version: Mapped[int | None] = mapped_column(Integer)

    # ---- Validation state ---------------------------------------------
    last_validation_status: Mapped[str | None] = mapped_column(String(20))
    last_validated_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_validation_run_id: Mapped[str | None] = mapped_column(String(36))

    definition: Mapped[KpiDefinition] = relationship(
        back_populates="versions", foreign_keys=[kpi_id]
    )
    dimensions: Mapped[list["KpiDimension"]] = relationship(
        back_populates="kpi_version", cascade="all, delete-orphan"
    )
    drivers: Mapped[list["KpiDriver"]] = relationship(
        back_populates="kpi_version", cascade="all, delete-orphan"
    )
    materiality: Mapped["KpiMaterialityRule | None"] = relationship(
        back_populates="kpi_version", cascade="all, delete-orphan", uselist=False
    )
    access_policies: Mapped[list["KpiAccessPolicy"]] = relationship(
        back_populates="kpi_version", cascade="all, delete-orphan"
    )
    lineage: Mapped[list["KpiLineage"]] = relationship(
        back_populates="kpi_version", cascade="all, delete-orphan"
    )
    validation_runs: Mapped[list["KpiValidationRun"]] = relationship(
        back_populates="kpi_version",
        cascade="all, delete-orphan",
        order_by="KpiValidationRun.started_at",
    )

    @property
    def is_editable(self) -> bool:
        return self.status in {KpiStatus.DRAFT, KpiStatus.PROPOSED, KpiStatus.REJECTED}


class KpiDimension(Base, UUIDPrimaryKey, Timestamped):
    """A governed, valid way to slice this KPI. Not a monitoring instruction."""

    __tablename__ = "kpi_dimensions"
    __table_args__ = (
        UniqueConstraint("kpi_version_id", "dimension_name", name="uq_kpi_dimension"),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dimension_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_table_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="SET NULL")
    )
    source_table: Mapped[str | None] = mapped_column(String(200))
    source_column: Mapped[str] = mapped_column(String(200), nullable=False)
    hierarchy: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default_breakdown: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approx_cardinality: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    kpi_version: Mapped[KpiVersion] = relationship(back_populates="dimensions")


class KpiDriver(Base, UUIDPrimaryKey, Timestamped):
    """A candidate explanatory factor, registered now for later investigation."""

    __tablename__ = "kpi_drivers"
    __table_args__ = (UniqueConstraint("kpi_version_id", "driver_name", name="uq_kpi_driver"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    driver_name: Mapped[str] = mapped_column(String(120), nullable=False)
    driver_type: Mapped[str] = mapped_column(String(30), default=DriverType.OTHER, nullable=False)
    source_table_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="SET NULL")
    )
    source_table: Mapped[str | None] = mapped_column(String(200))
    source_column: Mapped[str | None] = mapped_column(String(200))
    # Whether the business can actually pull this lever -- decides later whether
    # a driver can become a recommended action.
    controllable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    measurement_method: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    kpi_version: Mapped[KpiVersion] = relationship(back_populates="drivers")


class KpiMaterialityRule(Base, UUIDPrimaryKey, Timestamped):
    """Thresholds that decide, in Sprint 2, whether a movement deserves attention.

    Stored in Sprint 1; no monitoring runs against them yet.
    """

    __tablename__ = "kpi_materiality_rules"
    __table_args__ = (UniqueConstraint("kpi_version_id", name="uq_kpi_materiality"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relative_threshold_pct: Mapped[float | None] = mapped_column(Float)
    absolute_threshold: Mapped[float | None] = mapped_column(Float)
    # e.g. "z_score>2" or "outside_p10_p90" -- evaluated in Sprint 2.
    statistical_rule: Mapped[str | None] = mapped_column(String(120))
    business_criticality: Mapped[str] = mapped_column(String(20), default="MEDIUM", nullable=False)
    priority_policy: Mapped[str | None] = mapped_column(String(120))
    # How many consecutive periods a movement must persist before it counts.
    persistence_periods: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    kpi_version: Mapped[KpiVersion] = relationship(back_populates="materiality")


class KpiAccessPolicy(Base, UUIDPrimaryKey, Timestamped):
    """Row-, column- and domain-level entitlement for one role on one KPI."""

    __tablename__ = "kpi_access_policies"
    __table_args__ = (UniqueConstraint("kpi_version_id", "role_key", name="uq_kpi_access_role"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_key: Mapped[str] = mapped_column(String(40), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # SELF_SCOPE means "restrict to the scope on the user's membership".
    row_scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    column_scope: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    domain_scope: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    aggregate_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    kpi_version: Mapped[KpiVersion] = relationship(back_populates="access_policies")


class KpiLineage(Base, UUIDPrimaryKey, Timestamped):
    """Column-level answer to "where did this number come from?".

    Derived from the structured formula spec, so it cannot drift away from the
    calculation the way hand-maintained lineage does.
    """

    __tablename__ = "kpi_lineage"

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NUMERATOR | DENOMINATOR | TIME | DIMENSION | DRIVER | FILTER
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    data_source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("data_sources.id", ondelete="SET NULL")
    )
    data_source_name: Mapped[str | None] = mapped_column(String(160))
    source_table_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="SET NULL")
    )
    schema_name: Mapped[str | None] = mapped_column(String(160))
    table_name: Mapped[str | None] = mapped_column(String(200))
    column_name: Mapped[str | None] = mapped_column(String(200))
    transformation: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    kpi_version: Mapped[KpiVersion] = relationship(back_populates="lineage")


class KpiValidationRun(Base, UUIDPrimaryKey, Timestamped):
    """Header for one execution of the governance check suite."""

    __tablename__ = "kpi_validation_runs"

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    overall_status: Mapped[str] = mapped_column(
        String(20), default=ValidationStatus.SKIPPED, nullable=False
    )
    passed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warned_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    summary: Mapped[str | None] = mapped_column(Text)

    kpi_version: Mapped[KpiVersion] = relationship(back_populates="validation_runs")
    checks: Mapped[list["KpiValidationCheck"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class KpiValidationCheck(Base, UUIDPrimaryKey, Timestamped):
    """One governance check within a validation run."""

    __tablename__ = "kpi_validation_checks"

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    validation_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("kpi_validation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kpi_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kpi_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_type: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    expected: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    # A failed blocking check prevents activation; a failed advisory check does not.
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    run: Mapped[KpiValidationRun] = relationship(back_populates="checks")


VALID_AGGREGATIONS = {a.value for a in Aggregation}
