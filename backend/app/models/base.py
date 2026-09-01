"""Shared column mixins and enumerations for platform metadata tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.clock import as_utc, utcnow


def new_id() -> str:
    return str(uuid.uuid4())


class UtcDateTime(TypeDecorator):
    """A timestamp that is UTC-aware on every backend, SQLite included.

    SQLite has no timezone type. ``DateTime(timezone=True)`` is accepted there and
    then silently dropped, so a value written as aware reads back naive — while
    ``utcnow()`` and anything still sitting unflushed in the session stay aware.
    Comparing the two raises ``can't compare offset-naive and offset-aware
    datetimes``, and *which* comparison raises depends on which rows happen to be
    in the identity map, so the failure surfaces late and looks intermittent.

    Normalising in both directions removes the choice: naive input is taken as UTC
    on the way in, and every value comes back UTC-aware on the way out. The
    emitted DDL is unchanged — the implementation is still
    ``DateTime(timezone=True)`` — so this needs no migration.

    This is the floor, not the whole story: a value assigned in the current unit
    of work has never round-tripped through the database, so it never passes
    through ``process_result_value``. Code that mixes freshly-assigned values with
    loaded ones still has to normalise at the point of comparison, which is what
    ``app.core.clock.as_utc`` is for.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        return as_utc(value)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return as_utc(value)


class UUIDPrimaryKey:
    """String UUID primary key — portable across SQLite and PostgreSQL."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, onupdate=utcnow, nullable=False
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
    """What kind of system a source is.

    The list is wider than the set of *implemented* connectors on purpose: a
    company's data landscape has to be describable before it is readable, and a
    CSV extract that feeds a KPI is a governed source whether or not the platform
    can query it live. ``connectors.registry`` marks which types have a driver;
    the ones that do not are registry-and-metadata only.
    """

    SUPABASE = "SUPABASE"
    POSTGRESQL = "POSTGRESQL"
    SQLITE = "SQLITE"
    SNOWFLAKE = "SNOWFLAKE"
    BIGQUERY = "BIGQUERY"
    API = "API"
    CSV = "CSV"
    FILE = "FILE"


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


class ColumnRole(StrEnum):
    """The *business* role a column plays, in governance vocabulary.

    Deliberately separate from ``SemanticType``. ``SemanticType`` is what the
    profiler can prove about storage and cardinality, and the KPI, grain and
    detection engines are built on it — widening it would change their
    behaviour. ``ColumnRole`` is the reviewable business reading of the same
    column: a proposal until a human confirms it, which is why every column
    carries both a ``candidate_role`` and a ``confirmed_role``.
    """

    IDENTIFIER = "IDENTIFIER"
    TIME = "TIME"
    MEASURE = "MEASURE"
    DIMENSION = "DIMENSION"
    CURRENCY = "CURRENCY"
    QUANTITY = "QUANTITY"
    STATUS = "STATUS"
    TEXT = "TEXT"
    UNKNOWN = "UNKNOWN"


class MetadataStatus(StrEnum):
    """Whether a piece of governed metadata is still a machine proposal.

    The profiler may only ever write PROPOSED. CONFIRMED is reachable only
    through an explicit review call by a human with ``source.manage``, so a
    screen can always tell the two apart.
    """

    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"


class GrainStatus(StrEnum):
    """How much authority a table's recorded grain carries.

    Grain cannot always be inferred, and pretending otherwise is how a KPI ends
    up double-counting. DECLARED means an administrator stated it up front,
    PROPOSED means detection inferred it and nobody has checked, CONFIRMED means
    a human reviewed the inference and accepted it.
    """

    DECLARED = "DECLARED"
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"


class QualityStatus(StrEnum):
    GOOD = "GOOD"
    WARNING = "WARNING"
    POOR = "POOR"
    UNKNOWN = "UNKNOWN"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class SourceHealthStatus(StrEnum):
    """Whole-source health, rolled up deterministically from measurements.

    Never model-derived: the inputs are the declared refresh cadence, the
    measured lag of each table's time column, and the profiled completeness and
    quality scores. Freshness outranks quality because a source that stopped
    loading makes its own quality figures out of date — a DEGRADED verdict on
    three-day-old numbers would understate the problem.
    """

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
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


class BucketType(StrEnum):
    """The comparison slots the detection engine knows how to fill.

    This list is deliberately closed: the *slots* are part of the algorithm, so
    the engine can reason about precedence and about which historical dates are
    comparable. What goes *into* a slot -- which weekdays, which weeks, which
    months, which event dates -- comes entirely from a company's configuration,
    so no company or calendar assumption lives in code.
    """

    SAME_DAY_OF_WEEK = "SAME_DAY_OF_WEEK"
    SAME_WEEK_OF_MONTH = "SAME_WEEK_OF_MONTH"
    SAME_MONTH_OR_SEASON = "SAME_MONTH_OR_SEASON"
    BUSINESS_EVENT = "BUSINESS_EVENT"
    YOY_PERIOD = "YOY_PERIOD"
    # Not configurable, and not a company pattern: the documented floor used
    # when a target date matches none of the company's configured slots. Named
    # so a result never has to claim a comparison basis it did not use.
    TRAILING_PERIOD = "TRAILING_PERIOD"


class DetectionStatus(StrEnum):
    NORMAL = "NORMAL"
    ABNORMAL = "ABNORMAL"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


class BucketConfigStatus(StrEnum):
    """A bucket configuration is governed like a KPI: proposed, then approved.

    Only APPROVED configurations are readable by the detection engine, which is
    what keeps an unreviewed model extraction out of the numbers.

    NEEDS_REVIEW is the honest landing place for an extraction that produced
    something, but not something usable -- no slot the document supported, event
    dates that were discarded as ungrounded, or keys outside the contract. It is
    deliberately *not* PROPOSED: proposing it would invite an approval click on a
    configuration that cannot select a single comparable date, and it is
    deliberately not an error either, because the partial result and its reasons
    are exactly what a reviewer needs in order to finish the job by hand.
    """

    DRAFT = "DRAFT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"


class BucketConfigSource(StrEnum):
    MANUAL = "MANUAL"
    LLM_EXTRACTION = "LLM_EXTRACTION"


class FindingStatus(StrEnum):
    """Where a person's investigation note stands.

    Deliberately about the *investigation*, never about the KPI. A detection
    verdict is the engine's and has three values of its own; this is the human
    workflow beside it, and conflating the two would let a reader close an
    anomaly by editing a note.
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"


#: Any status may be reached from any other: an investigation that was resolved
#: and then reopened is a normal thing to happen, and refusing it would push
#: people into writing a second note that contradicts the first.
FINDING_TRANSITIONS: dict[FindingStatus, tuple[FindingStatus, ...]] = {
    FindingStatus.OPEN: (FindingStatus.IN_PROGRESS, FindingStatus.RESOLVED),
    FindingStatus.IN_PROGRESS: (FindingStatus.OPEN, FindingStatus.RESOLVED),
    FindingStatus.RESOLVED: (FindingStatus.OPEN, FindingStatus.IN_PROGRESS),
}


BUCKET_CONFIG_TRANSITIONS: dict[BucketConfigStatus, tuple[BucketConfigStatus, ...]] = {
    BucketConfigStatus.DRAFT: (BucketConfigStatus.PROPOSED, BucketConfigStatus.ARCHIVED),
    # A reviewer who fixes what the extraction got wrong moves it to DRAFT and
    # onward; there is no path straight to APPROVED, because the whole reason the
    # row is here is that nobody has yet supplied what was missing.
    BucketConfigStatus.NEEDS_REVIEW: (
        BucketConfigStatus.DRAFT,
        BucketConfigStatus.PROPOSED,
        BucketConfigStatus.ARCHIVED,
    ),
    BucketConfigStatus.PROPOSED: (
        BucketConfigStatus.APPROVED,
        BucketConfigStatus.DRAFT,
        BucketConfigStatus.ARCHIVED,
    ),
    BucketConfigStatus.APPROVED: (BucketConfigStatus.ARCHIVED,),
    BucketConfigStatus.ARCHIVED: (),
}
