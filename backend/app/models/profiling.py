"""Deterministic profiling results: profiles, quality, grain, relationships,
join safety and cross-source reconciliation.

Nothing in this module is statistical inference or ML. Every value here comes
from an aggregate query pushed down to the source database, so the same inputs
always produce the same catalog.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import (
    JoinSafetyLevel,
    QualityStatus,
    ReconciliationStatus,
    RelationshipType,
    Timestamped,
    UUIDPrimaryKey,
)


class TableProfile(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "table_profiles"
    __table_args__ = (UniqueConstraint("source_table_id", name="uq_table_profile"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profiled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    profiled_column_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Columns the profiling user was not entitled to read. Recorded rather than
    # silently omitted, so the catalog states what it could not see.
    withheld_column_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    withheld_columns: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    completeness_pct: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str] = mapped_column(
        String(20), default=QualityStatus.UNKNOWN, nullable=False
    )
    # Warnings are stored, never auto-repaired.
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class ColumnProfile(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "column_profiles"
    __table_args__ = (UniqueConstraint("source_column_id", name="uq_column_profile"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_column_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_columns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profiled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    row_count: Mapped[int | None] = mapped_column(Integer)
    null_count: Mapped[int | None] = mapped_column(Integer)
    null_pct: Mapped[float | None] = mapped_column(Float)
    distinct_count: Mapped[int | None] = mapped_column(Integer)
    distinct_pct: Mapped[float | None] = mapped_column(Float)

    # Stored as text so one shape covers numeric, date and categorical columns.
    min_value: Mapped[str | None] = mapped_column(String(300))
    max_value: Mapped[str | None] = mapped_column(String(300))
    mean_value: Mapped[float | None] = mapped_column(Float)
    zero_count: Mapped[int | None] = mapped_column(Integer)
    negative_count: Mapped[int | None] = mapped_column(Integer)
    blank_count: Mapped[int | None] = mapped_column(Integer)

    sample_values: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_unique: Mapped[bool | None] = mapped_column(Boolean)
    is_candidate_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    quality_status: Mapped[str] = mapped_column(
        String(20), default=QualityStatus.UNKNOWN, nullable=False
    )
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # True when profiling was skipped because the caller lacked entitlement.
    access_withheld: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TableGrain(Base, UUIDPrimaryKey, Timestamped):
    """What one row of the table represents.

    ``inferred_grain`` comes from uniqueness scans over candidate column sets —
    the data decides, not a hardcoded assumption such as "sales = order".
    """

    __tablename__ = "table_grains"
    __table_args__ = (UniqueConstraint("source_table_id", name="uq_table_grain"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    declared_grain: Mapped[str | None] = mapped_column(String(300))
    inferred_grain: Mapped[str | None] = mapped_column(String(300))
    grain_columns: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(String(80))
    row_count: Mapped[int | None] = mapped_column(Integer)
    distinct_combinations: Mapped[int | None] = mapped_column(Integer)
    is_unique: Mapped[bool | None] = mapped_column(Boolean)
    time_column: Mapped[str | None] = mapped_column(String(200))
    time_grain: Mapped[str | None] = mapped_column(String(20))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class TableRelationship(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "table_relationships"
    __table_args__ = (
        UniqueConstraint(
            "source_table_id",
            "source_column",
            "target_table_id",
            "target_column",
            name="uq_relationship_identity",
        ),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_column: Mapped[str] = mapped_column(String(200), nullable=False)
    target_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_column: Mapped[str] = mapped_column(String(200), nullable=False)

    relationship_type: Mapped[str] = mapped_column(
        String(20), default=RelationshipType.UNKNOWN, nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    # declared_fk | name_and_containment | name_match_only
    method: Mapped[str | None] = mapped_column(String(80))
    is_declared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    source_distinct_count: Mapped[int | None] = mapped_column(Integer)
    target_distinct_count: Mapped[int | None] = mapped_column(Integer)
    orphan_count: Mapped[int | None] = mapped_column(Integer)
    orphan_pct: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class JoinSafety(Base, UUIDPrimaryKey, Timestamped):
    """Guards against the most dangerous BI failure: a correct-looking KPI
    inflated by a fan-out join."""

    __tablename__ = "join_safety"
    __table_args__ = (UniqueConstraint("relationship_id", name="uq_join_safety_relationship"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relationship_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("table_relationships.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_is_unique: Mapped[bool | None] = mapped_column(Boolean)
    target_is_unique: Mapped[bool | None] = mapped_column(Boolean)
    source_uniqueness_ratio: Mapped[float | None] = mapped_column(Float)
    target_uniqueness_ratio: Mapped[float | None] = mapped_column(Float)
    fan_out_factor: Mapped[float | None] = mapped_column(Float)
    max_fan_out: Mapped[int | None] = mapped_column(Integer)
    duplicate_key_rate: Mapped[float | None] = mapped_column(Float)
    expected_cardinality: Mapped[str | None] = mapped_column(String(20))
    observed_cardinality: Mapped[str | None] = mapped_column(String(20))
    safety_level: Mapped[str] = mapped_column(
        String(30), default=JoinSafetyLevel.UNKNOWN, nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    guidance: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class SourceReconciliation(Base, UUIDPrimaryKey, Timestamped):
    """Can two tables safely cooperate in one analysis?

    Sprint 1 records the answer as metadata only. It does not build
    multi-source KPIs — that is Sprint 2's job, and it will read this table
    instead of guessing.
    """

    __tablename__ = "source_reconciliations"
    __table_args__ = (
        UniqueConstraint("left_table_id", "right_table_id", name="uq_reconciliation_pair"),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    left_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    right_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(30), default=ReconciliationStatus.UNKNOWN, nullable=False
    )
    left_grain: Mapped[str | None] = mapped_column(String(300))
    right_grain: Mapped[str | None] = mapped_column(String(300))
    left_time_grain: Mapped[str | None] = mapped_column(String(20))
    right_time_grain: Mapped[str | None] = mapped_column(String(20))
    shared_dimensions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    unmapped_dimensions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    time_overlap_days: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    guidance: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
