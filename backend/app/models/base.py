"""Shared column mixins and enumerations for platform metadata tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow


def new_id() -> str:
    return str(uuid.uuid4())


class UUIDPrimaryKey:
    """String UUID primary key — portable across SQLite and PostgreSQL."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Enumerations. Stored as strings so the database stays human-readable and
# adding a value never requires a type migration.
# ---------------------------------------------------------------------------
class CompanyStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class MembershipStatus(StrEnum):
    INVITED = "INVITED"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class DataSourceType(StrEnum):
    SUPABASE = "SUPABASE"
    POSTGRESQL = "POSTGRESQL"
    SQLITE = "SQLITE"
    SNOWFLAKE = "SNOWFLAKE"
    BIGQUERY = "BIGQUERY"


class ConnectionStatus(StrEnum):
    UNTESTED = "UNTESTED"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"


class RefreshFrequency(StrEnum):
    REALTIME = "REALTIME"
    MINUTES_15 = "MINUTES_15"
    HOURLY = "HOURLY"
    HOURS_2 = "HOURS_2"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    UNKNOWN = "UNKNOWN"


REFRESH_INTERVAL_SECONDS: dict[str, int | None] = {
    RefreshFrequency.REALTIME: 60,
    RefreshFrequency.MINUTES_15: 15 * 60,
    RefreshFrequency.HOURLY: 60 * 60,
    RefreshFrequency.HOURS_2: 2 * 60 * 60,
    RefreshFrequency.DAILY: 24 * 60 * 60,
    RefreshFrequency.WEEKLY: 7 * 24 * 60 * 60,
    RefreshFrequency.UNKNOWN: None,
}


class SemanticType(StrEnum):
    """What a column *means*, inferred from profiling — not its storage type."""

    NUMERIC_MEASURE = "NUMERIC_MEASURE"
    IDENTIFIER = "IDENTIFIER"
    CATEGORICAL = "CATEGORICAL"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    BOOLEAN_FLAG = "BOOLEAN_FLAG"
    TEXT = "TEXT"
    UNKNOWN = "UNKNOWN"


class Classification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class QualityStatus(StrEnum):
    GOOD = "GOOD"
    WARNING = "WARNING"
    POOR = "POOR"
    UNKNOWN = "UNKNOWN"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class RelationshipType(StrEnum):
    ONE_TO_ONE = "ONE_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    MANY_TO_MANY = "MANY_TO_MANY"
    UNKNOWN = "UNKNOWN"


class JoinSafetyLevel(StrEnum):
    SAFE = "SAFE"
    SAFE_WITH_AGGREGATION = "SAFE_WITH_AGGREGATION"
    RISKY = "RISKY"
    UNKNOWN = "UNKNOWN"


class ReconciliationStatus(StrEnum):
    DIRECTLY_COMPATIBLE = "DIRECTLY_COMPATIBLE"
    REQUIRES_AGGREGATION = "REQUIRES_AGGREGATION"
    REQUIRES_DIMENSION_MAPPING = "REQUIRES_DIMENSION_MAPPING"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


class DocumentType(StrEnum):
    """Sprint 1 stores both kinds; only metadata + versioning, no embeddings.

    The REFERENCE / EVENT split matters later: reference documents describe how
    the company operates, event documents describe what happened.
    """

    KPI_HANDBOOK = "KPI_HANDBOOK"
    FINANCE_POLICY = "FINANCE_POLICY"
    PRICING_POLICY = "PRICING_POLICY"
    RETURNS_POLICY = "RETURNS_POLICY"
    INVENTORY_POLICY = "INVENTORY_POLICY"
    BUSINESS_RULES = "BUSINESS_RULES"
    ORG_STRUCTURE = "ORG_STRUCTURE"
    OPERATIONS_GUIDE = "OPERATIONS_GUIDE"
    SUPPLIER_INCIDENT = "SUPPLIER_INCIDENT"
    CAMPAIGN_NOTE = "CAMPAIGN_NOTE"
    OPERATIONS_INCIDENT = "OPERATIONS_INCIDENT"
    MANAGEMENT_ANNOUNCEMENT = "MANAGEMENT_ANNOUNCEMENT"
    OTHER = "OTHER"


REFERENCE_DOCUMENT_TYPES = {
    DocumentType.KPI_HANDBOOK,
    DocumentType.FINANCE_POLICY,
    DocumentType.PRICING_POLICY,
    DocumentType.RETURNS_POLICY,
    DocumentType.INVENTORY_POLICY,
    DocumentType.BUSINESS_RULES,
    DocumentType.ORG_STRUCTURE,
    DocumentType.OPERATIONS_GUIDE,
}


class DocumentClass(StrEnum):
    REFERENCE = "REFERENCE"
    EVENT = "EVENT"


class DocumentStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


class KpiStatus(StrEnum):
    """The governed KPI lifecycle. A definition never jumps straight to ACTIVE."""

    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    DEPRECATED = "DEPRECATED"


KPI_TRANSITIONS: dict[str, set[str]] = {
    KpiStatus.DRAFT: {KpiStatus.PROPOSED, KpiStatus.REJECTED},
    KpiStatus.PROPOSED: {KpiStatus.UNDER_REVIEW, KpiStatus.DRAFT, KpiStatus.REJECTED},
    KpiStatus.UNDER_REVIEW: {KpiStatus.APPROVED, KpiStatus.DRAFT, KpiStatus.REJECTED},
    KpiStatus.APPROVED: {KpiStatus.ACTIVE, KpiStatus.DEPRECATED},
    KpiStatus.ACTIVE: {KpiStatus.DEPRECATED},
    KpiStatus.REJECTED: {KpiStatus.DRAFT},
    KpiStatus.DEPRECATED: set(),
}


class TimeGrain(StrEnum):
    HOUR = "HOUR"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    QUARTER = "QUARTER"
    YEAR = "YEAR"


class Aggregation(StrEnum):
    SUM = "SUM"
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"


class KpiKind(StrEnum):
    SIMPLE = "SIMPLE"
    RATIO = "RATIO"


class DriverType(StrEnum):
    VOLUME = "VOLUME"
    PRICE = "PRICE"
    MIX = "MIX"
    SUPPLY = "SUPPLY"
    MARKETING = "MARKETING"
    SEASONALITY = "SEASONALITY"
    EXTERNAL = "EXTERNAL"
    OTHER = "OTHER"


class ValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIPPED = "SKIPPED"


class ValidationTest(StrEnum):
    """The nine governance checks required before a KPI may be activated."""

    FORMULA_PARSES = "FORMULA_PARSES"
    COLUMNS_EXIST = "COLUMNS_EXIST"
    TIME_FIELD_VALID = "TIME_FIELD_VALID"
    DIMENSIONS_EXIST = "DIMENSIONS_EXIST"
    AGGREGATION_VALID = "AGGREGATION_VALID"
    DUPLICATE_COUNTING = "DUPLICATE_COUNTING"
    GRAIN_COMPATIBLE = "GRAIN_COMPATIBLE"
    ACCESS_POLICY_VALID = "ACCESS_POLICY_VALID"
    RECONCILES_TO_SOURCE = "RECONCILES_TO_SOURCE"
