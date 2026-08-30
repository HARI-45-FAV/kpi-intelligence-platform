"""The deterministic KPI detection engine.

One algorithm, no company knowledge. Everything company- or KPI-specific is an
input:

* **what and where** come from the governed KPI registration -- source table,
  formula spec, time field, tolerance (``app.models.kpi``);
* **when history is comparable** comes from the company's approved bucket
  configuration (``app.services.bucket_config``);
* **how to detect** is this module, and it is identical for every company.

Two boundaries are load-bearing and are enforced by construction rather than by
convention:

1. **No query is composed from free text.** Every value -- the actual and each
   historical reference -- is produced by
   :func:`app.services.kpi_execution.execute_kpi_any` from the formula spec that
   was validated and approved during KPI registration. The engine chooses *which
   day* to ask about; it never chooses what to select or from where. It does not
   know, either, whether the answer came back over SQL or over a REST source:
   that is the dispatcher's business, so a KPI means the same thing on both.
2. **No model touches a number.** A language model may draft a bucket
   configuration (see :mod:`app.services.bucket_extraction`), and that draft has
   to be approved by a human before this module can read it. Nothing in this
   file calls a model, and every quantity below -- actual, expected, median,
   MAD, modified z-score, deviation, status -- comes from arithmetic on values
   the database returned.

Aggregation is pushed as far into the source as that source allows: one bounded
read per date, and a query budget the company controls through
``max_reference_points``.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.core.clock import utcnow
from app.core.errors import Conflict, NotFound
from app.models.base import (
    BucketConfigStatus,
    BucketType,
    DetectionStatus,
    KpiStatus,
    SemanticType,
    TimeGrain,
)
from app.models.detection import CompanyBucketConfig, DetectionRun
from app.models.kpi import KpiDefinition, KpiMaterialityRule, KpiVersion
from app.models.source import DataSource, SourceColumn, SourceTable
from app.services.bucket_config import (
    BUCKET_PRECEDENCE,
    DAYS_IN_YEAR,
    BucketConfig,
    describe_buckets,
    select_comparable_dates,
    trailing_dates,
    validate_bucket_config,
)
from app.services.kpi_coverage import Coverage, CoverageKey, source_coverage
from app.services.kpi_execution import execute_kpi_any, execution_mode
from app.services.kpi_formula import FormulaSpec, spec_from_stored
from app.services.kpi_sql import KpiResult, KpiValue
from app.services.robust_stats import (
    Dispersion,
    DispersionBasis,
    median,
    modified_z_score,
    parse_z_threshold,
)

#: Only versions the business has signed off on may be detected against. A DRAFT
#: definition has no agreed meaning, so a number computed from it would be
#: unattributable.
DETECTABLE_STATUSES = frozenset({KpiStatus.ACTIVE, KpiStatus.APPROVED})

#: Minimum points on each side of the year boundary before a year-over-year
#: factor is trusted at all.
YOY_MIN_POINTS_PER_ERA = 3

#: The factor is only applied inside this band. Outside it, the two eras are
#: describing different businesses (a launch, a migration, a gap in history)
#: rather than growth, and scaling by it would fabricate an expectation.
YOY_FACTOR_MIN = 0.5
YOY_FACTOR_MAX = 2.0

#: The movement floor for a KPI that registers no relative materiality threshold,
#: as a percentage of that KPI's own expected level.
#:
#: A modified z-score says "how unusual against this KPI's own history". It says
#: nothing about whether the movement is worth a person's attention: a KPI whose
#: comparable history is exceptionally tight produces a large score for a
#: fractional move, and one whose history is noisy produces a small score for a
#: large one. So significance alone is not allowed to raise an abnormal verdict --
#: the movement must also clear a floor expressed *relative to the expectation*.
#:
#: Relative is the whole point. The floor is a ratio, never a magnitude, so it
#: cannot encode an assumption about how big a KPI's numbers are, and two KPIs on
#: entirely different scales are each judged against their own level rather than
#: against each other. A KPI that registers a relative threshold supplies its own
#: floor and this default is not consulted; it exists only for KPIs whose
#: registration states none.
DEFAULT_RELATIVE_FLOOR_PCT = 1.0


# ---------------------------------------------------------------------------
# Resolved KPI binding: the "what and where", read from KPI registration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KpiBinding:
    """Everything the engine needs about a KPI, and nothing it invented itself."""

    definition: KpiDefinition
    version: KpiVersion
    table: SourceTable
    data_source: DataSource
    spec: FormulaSpec
    time_field: str
    time_is_timestamp: bool
    time_field_note: str

    @property
    def kpi_key(self) -> str:
        return self.definition.kpi_key

    @property
    def name(self) -> str:
        return self.definition.name

    def window_for(self, day: date) -> tuple[date | datetime, date | datetime]:
        """Inclusive bounds that select exactly ``day`` from the time field.

        A DATE column is bounded by the date itself; a TIMESTAMP column is
        bounded by the first and last instant of the day, so an order placed at
        14:00 is not silently excluded. The distinction is read from the profiled
        column type -- never guessed from the column's name.
        """

        if self.time_is_timestamp:
            return (datetime.combine(day, time.min), datetime.combine(day, time.max))
        return (day, day)

    def describe_source(self) -> dict:
        return {
            "schema": self.table.schema_name,
            "table": self.table.table_name,
            "time_field": self.time_field,
            "formula": self.version.formula_expression,
            "data_source_id": self.data_source.id,
            "source_type": self.data_source.source_type,
        }


def resolve_binding(session: Session, version: KpiVersion) -> KpiBinding:
    """Load the source binding a KPI version was registered with.

    Every failure here is a governance failure, reported as such: the engine
    refuses to substitute a table, a column or a default when the registration
    is incomplete.
    """

    if version.status not in DETECTABLE_STATUSES:
        raise Conflict(
            f"'{version.definition.name}' v{version.version} is {version.status}. "
            "Detection runs against approved definitions only, so that every "
            "number can be traced to a meaning the business agreed to."
        )
    if version.time_grain and version.time_grain != TimeGrain.DAY:
        raise Conflict(
            f"'{version.definition.name}' is registered at {version.time_grain} grain. "
            "This engine evaluates a single target date, so it would report a "
            "partial period rather than the KPI as defined."
        )
    if not version.time_field:
        raise Conflict(
            f"'{version.definition.name}' has no time field registered, so there is "
            "no way to say which rows belong to the target date."
        )

    table = None
    if version.primary_source_table_id:
        table = session.get(SourceTable, version.primary_source_table_id)
    if table is None:
        source_definition = version.source_definition or {}
        schema = source_definition.get("schema")
        table_name = source_definition.get("table")
        qualified_name = source_definition.get("qualified_name")
        data_source_name = source_definition.get("data_source")

        if not table_name and qualified_name:
            parts = qualified_name.split(".", 1)
            if len(parts) == 2:
                schema, table_name = parts

        if table_name:
            statement = select(SourceTable).where(
                SourceTable.company_id == version.company_id,
                SourceTable.table_name == table_name,
            )
            if schema:
                statement = statement.where(SourceTable.schema_name == schema)
            if data_source_name:
                data_source = session.scalars(
                    select(DataSource).where(
                        DataSource.company_id == version.company_id,
                        DataSource.name == data_source_name,
                    )
                ).first()
                if data_source is not None:
                    statement = statement.where(SourceTable.data_source_id == data_source.id)
            table = session.scalars(statement).first()

        if table is None:
            raise Conflict(
                f"'{version.definition.name}' has no source table binding. Detection "
                "reads the registered source; it does not search for one."
            )

        version.primary_source_table_id = table.id
        version.primary_data_source_id = table.data_source_id

    data_source = session.get(DataSource, table.data_source_id)
    if data_source is None:
        raise NotFound("The KPI's data source is no longer registered.")

    spec = spec_from_stored(
        version.formula_spec,
        expression=version.formula_expression,
        default_table=table.table_name,
    )

    column = session.scalars(
        select(SourceColumn).where(
            SourceColumn.source_table_id == table.id,
            SourceColumn.column_name == version.time_field,
        )
    ).first()
    if column is None:
        is_timestamp = False
        note = (
            "The time field is not in the profiled column list; treated as a date "
            "for day boundaries."
        )
    elif column.semantic_type == SemanticType.TIMESTAMP:
        is_timestamp = True
        note = f"Time field profiled as {column.semantic_type} ({column.data_type}); the whole day is included."
    else:
        is_timestamp = False
        note = f"Time field profiled as {column.semantic_type} ({column.data_type}); compared as a date."

    return KpiBinding(
        definition=version.definition,
        version=version,
        table=table,
        data_source=data_source,
        spec=spec,
        time_field=version.time_field,
        time_is_timestamp=is_timestamp,
        time_field_note=note,
    )


# ---------------------------------------------------------------------------
# Bucket configuration lookup
# ---------------------------------------------------------------------------
def load_bucket_config_row(
    session: Session, company_id: str, kpi_key: str | None
) -> CompanyBucketConfig | None:
    """The approved configuration in force: KPI-specific first, else company-wide.

    Only APPROVED rows are visible here. That is the whole guard between an
    unreviewed extraction and the numbers a business acts on.
    """

    statement = select(CompanyBucketConfig).where(
        CompanyBucketConfig.company_id == company_id,
        CompanyBucketConfig.status == BucketConfigStatus.APPROVED,
    )
    rows = list(session.scalars(statement))
    if not rows:
        return None

    def newest(candidates: list[CompanyBucketConfig]) -> CompanyBucketConfig:
        return sorted(candidates, key=lambda row: (row.version, row.created_at))[-1]

    if kpi_key:
        specific = [row for row in rows if row.kpi_key == kpi_key]
        if specific:
            return newest(specific)
    generic = [row for row in rows if not row.kpi_key]
    return newest(generic) if generic else None


#: What the engine says when a company has approved no comparison policy yet.
#: Detection still answers -- with a trailing window and this warning attached --
#: rather than refusing, because "no seasonal pattern configured yet" is a normal
#: state for a company that has just connected its data.
UNCONFIGURED_WARNING = (
    "This company has no approved comparison policy, so the engine compared recent "
    "days and claims no weekly, monthly or seasonal pattern. Approve a comparison "
    "configuration to make the expectation calendar-aware."
)


def config_payload(row: CompanyBucketConfig) -> dict:
    """Rebuild the validator's input from a stored row.

    The search-budget columns are authoritative over the copies inside
    ``buckets``: they are the ones an administrator edits and the ones queries can
    read without opening the JSON.
    """

    payload = dict(row.buckets or {})
    for column in ("lookback_days", "min_reference_points", "max_reference_points"):
        value = getattr(row, column)
        if value is not None:
            payload[column] = value
    return payload


def policy_for(
    session: Session, company_id: str, kpi_key: str | None
) -> tuple[BucketConfig, CompanyBucketConfig | None]:
    """The approved comparison policy in force for this KPI, or the fallback.

    One accessor, so that every surface which needs to know *what counts as
    comparable history* -- a scheduled run, a re-explanation, an explicitly
    requested analysis of one part of the business -- reads the same approved row
    and gets the same warning when there is none.
    """

    row = load_bucket_config_row(session, company_id, kpi_key)
    if row is None:
        return BucketConfig(warnings=(UNCONFIGURED_WARNING,)), None
    return validate_bucket_config(config_payload(row)), row


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReferencePoint:
    day: date
    value: float

    def as_dict(self) -> dict:
        return {"date": self.day.isoformat(), "value": self.value}


@dataclass
class BucketDecision:
    bucket: BucketType
    role: str  # PRIMARY | REFINEMENT | REJECTED | FALLBACK
    reference_count: int
    note: str

    def as_dict(self) -> dict:
        return {
            "bucket": str(self.bucket),
            "role": self.role,
            "reference_count": self.reference_count,
            "note": self.note,
        }


@dataclass
class DetectionOutcome:
    """The full result. Two renderings: business-facing and technical."""

    company_id: str
    kpi_definition_id: str
    kpi_version_id: str
    kpi_version: int
    kpi_key: str
    kpi_name: str
    target_date: date
    time_grain: str
    unit: str | None
    currency: str | None

    actual: float | None
    expected: float | None
    deviation_absolute: float | None
    deviation_pct: float | None
    status: DetectionStatus
    comparison_label: str
    headline: str

    bucket_applied: BucketType
    buckets_applied: list[BucketType]
    bucket_decisions: list[BucketDecision]
    bucket_config_id: str | None
    bucket_config_key: str | None
    bucket_config_version: int | None
    bucket_signature: dict

    references: list[ReferencePoint]
    median_value: float | None
    dispersion: Dispersion | None
    modified_z: float | None
    z_threshold: float
    z_threshold_note: str

    tolerance_pct: float | None
    tolerance_absolute: float | None
    breached_tolerance: bool
    statistically_significant: bool
    # The scale-free movement floor that applied, and whether the movement cleared
    # it. Kept beside the tolerance because it is the same kind of judgement: how
    # much movement matters for *this* KPI, expressed against its own level.
    relative_floor_pct: float | None
    movement_is_material: bool

    yoy_applied: bool
    yoy_factor: float | None

    method: str
    reason: str
    notes: list[str] = field(default_factory=list)
    query_count: int = 0
    duration_ms: int = 0
    source: dict = field(default_factory=dict)

    # -- renderings ---------------------------------------------------------
    def business_view(self) -> dict:
        """What the business surface is allowed to show.

        KPI, actual, expected, deviation, status and -- because it is the one
        piece of reasoning a business reader actually wants -- the plain-language
        comparison basis. No bucket types, no SQL, no median, no MAD, no
        z-score, no reference dates.
        """

        return {
            "kpi": self.kpi_name,
            "kpi_key": self.kpi_key,
            "target_date": self.target_date.isoformat(),
            "actual": self.actual,
            "expected": self.expected,
            "deviation_pct": self.deviation_pct,
            "deviation_absolute": self.deviation_absolute,
            "status": str(self.status),
            "comparison": self.comparison_label,
            "headline": self.headline,
            "unit": self.unit,
            "currency": self.currency,
        }

    def evidence(self) -> dict:
        """The technical record, for governance and audit surfaces only."""

        return {
            "kpi_version": self.kpi_version,
            "kpi_version_id": self.kpi_version_id,
            "source": self.source,
            "bucket": {
                "applied": str(self.bucket_applied),
                "all_applied": [str(b) for b in self.buckets_applied],
                "decisions": [d.as_dict() for d in self.bucket_decisions],
                "config_id": self.bucket_config_id,
                "config_key": self.bucket_config_key,
                "config_version": self.bucket_config_version,
                "signature": self.bucket_signature,
            },
            "reference": {
                "count": len(self.references),
                "points": [point.as_dict() for point in self.references],
            },
            "statistics": {
                "median": self.median_value,
                "mad": None if self.dispersion is None else self.dispersion.mad,
                "dispersion": None if self.dispersion is None else self.dispersion.value,
                "dispersion_basis": None if self.dispersion is None else self.dispersion.basis,
                "modified_z_score": self.modified_z,
                "z_threshold": self.z_threshold,
                "z_threshold_note": self.z_threshold_note,
                "statistically_significant": self.statistically_significant,
            },
            "tolerance": {
                "relative_pct": self.tolerance_pct,
                "absolute": self.tolerance_absolute,
                "breached": self.breached_tolerance,
                "relative_floor_pct": self.relative_floor_pct,
                "movement_is_material": self.movement_is_material,
            },
            "year_over_year": {"applied": self.yoy_applied, "factor": self.yoy_factor},
            "method": self.method,
            "reason": self.reason,
            "notes": self.notes,
            "query_count": self.query_count,
            "duration_ms": self.duration_ms,
        }

    def as_dict(self) -> dict:
        return {"result": self.business_view(), "evidence": self.evidence()}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
def _kpi_read(
    connector: DataSourceConnector, binding: KpiBinding, day: date
) -> KpiResult:
    """One bounded read for one day, from the KPI's own registered formula.

    Which execution path this takes -- an aggregate pushed into SQL, or a
    filtered projection over REST -- is decided by
    :func:`app.services.kpi_execution.execute_kpi_any` from the connector alone.
    The engine passes the same spec and the same window either way.
    """

    start, end = binding.window_for(day)
    return execute_kpi_any(
        connector,
        binding.spec,
        schema=binding.table.schema_name,
        table=binding.table.table_name,
        time_column=binding.time_field,
        start=start,
        end=end,
    )


#: How a day is read. The engine's own reader is the default and the only one used
#: for a KPI: a scheduled run always measures the KPI exactly as registered.
#:
#: It is a parameter so that an explicitly requested analysis of *one part* of the
#: business can be classified by this engine rather than by a second one -- the part
#: is read through a narrowed formula, and everything after the read (comparable
#: dates, expectation, dispersion, threshold, verdict, wording) is untouched. No
#: caller inside the platform's scheduled path passes it.
Reader = Callable[[DataSourceConnector, KpiBinding, date], KpiResult]


def _kpi_value(
    connector: DataSourceConnector, binding: KpiBinding, day: date
) -> KpiValue | None:
    return _kpi_read(connector, binding, day).scalar


def actual_value(
    connector: DataSourceConnector, binding: KpiBinding, target_date: date
) -> KpiValue | None:
    """The KPI on the target date, from its registered source and formula."""

    return _kpi_value(connector, binding, target_date)


def _choose_buckets(
    config: BucketConfig, target_date: date
) -> tuple[BucketType, list[BucketType], list[date], list[BucketDecision]]:
    """Pick the comparison basis and the comparable dates it selects.

    The precedence order and the refinement rule are part of the algorithm; the
    values every predicate tests against come from the configuration. A
    refinement is only accepted while it leaves enough reference points to keep
    the median meaningful -- narrowing to two Fridays in week 3 of December is
    more precise and less trustworthy, and the engine prefers trustworthy.
    """

    decisions: list[BucketDecision] = []
    applicable = list(config.applicable_buckets(target_date))

    if not applicable:
        dates = trailing_dates(
            target_date,
            days=config.effective_lookback_days,
            limit=config.max_reference_points,
        )
        decisions.append(
            BucketDecision(
                bucket=BucketType.TRAILING_PERIOD,
                role="FALLBACK",
                reference_count=len(dates),
                note=(
                    "No configured bucket describes this date, so the engine fell "
                    "back to its documented trailing window and claims no seasonal "
                    "pattern."
                ),
            )
        )
        return BucketType.TRAILING_PERIOD, [BucketType.TRAILING_PERIOD], dates, decisions

    primary = applicable[0]
    dates = select_comparable_dates(config, target_date, [primary])
    decisions.append(
        BucketDecision(
            bucket=primary,
            role="PRIMARY",
            reference_count=len(dates),
            note="Most specific configured bucket that describes the target date.",
        )
    )

    applied = [primary]
    for bucket in BUCKET_PRECEDENCE:
        if bucket == primary or bucket not in applicable:
            continue
        if bucket == BucketType.YOY_PERIOD:
            decisions.append(
                BucketDecision(
                    bucket=bucket,
                    role="REJECTED",
                    reference_count=len(dates),
                    note=(
                        "Year-over-year is used to adjust the expected level, not to "
                        "narrow the comparable set, which it would reduce to a single "
                        "anniversary."
                    ),
                )
            )
            continue
        refined = [day for day in dates if config.comparable(bucket, target_date, day)]
        if len(refined) >= config.min_reference_points:
            dates = refined
            applied.append(bucket)
            decisions.append(
                BucketDecision(
                    bucket=bucket,
                    role="REFINEMENT",
                    reference_count=len(refined),
                    note="Narrowed the comparable set and still left enough history.",
                )
            )
        else:
            decisions.append(
                BucketDecision(
                    bucket=bucket,
                    role="REJECTED",
                    reference_count=len(refined),
                    note=(
                        f"Would leave {len(refined)} comparable date(s), below the "
                        f"configured minimum of {config.min_reference_points}."
                    ),
                )
            )

    return primary, applied, dates, decisions


def plan_comparison(
    config: BucketConfig, target_date: date
) -> tuple[BucketType, list[BucketType], list[date], list[BucketDecision]]:
    """Which past dates a configuration would compare a date against.

    The same selection the engine performs, exposed without touching a source, so
    a reviewer can see the calendar consequence of a policy before approving it.
    No KPI is measured and no query is issued -- only dates come back.
    """

    return _choose_buckets(config, target_date)


def _budget_dates(config: BucketConfig, target_date: date, dates: list[date]) -> list[date]:
    """Trim the comparable set to the query budget, most recent first.

    Recency matters more than volume: 26 recent comparable days describe the
    business as it is now, while five years of them describe its history. When
    the company enabled year-over-year, the same budget is granted separately to
    the prior year so an anniversary comparison remains possible.
    """

    recent = [day for day in dates if (target_date - day).days <= DAYS_IN_YEAR]
    trimmed = recent[: config.max_reference_points]
    if not config.yoy_period.enabled:
        return trimmed
    prior = [
        day for day in dates if DAYS_IN_YEAR < (target_date - day).days <= 2 * DAYS_IN_YEAR
    ]
    return trimmed + prior[: config.max_reference_points]


def _year_over_year(
    config: BucketConfig, target_date: date, points: list[ReferencePoint]
) -> tuple[list[ReferencePoint], bool, float | None, str]:
    """Split the reference set at the year boundary and derive a growth factor.

    Returns the points the expectation is actually built from. When both eras are
    populated the recent year is the expectation and the prior year is the
    growth reference -- multiplying the recent median by a growth factor would
    project a year into the future, which is not what "expected today" means.
    """

    if not config.yoy_period.enabled:
        return points, False, None, "Year-over-year comparison is not enabled for this company."

    recent = [p for p in points if (target_date - p.day).days <= DAYS_IN_YEAR]
    prior = [p for p in points if (target_date - p.day).days > DAYS_IN_YEAR]

    if len(recent) < YOY_MIN_POINTS_PER_ERA or len(prior) < YOY_MIN_POINTS_PER_ERA:
        return (
            recent or points,
            False,
            None,
            (
                f"History does not span a full year on both sides "
                f"({len(recent)} recent, {len(prior)} prior-year comparable dates; "
                f"{YOY_MIN_POINTS_PER_ERA} needed each). No year-over-year reference."
            ),
        )

    prior_median = median([p.value for p in prior])
    if prior_median <= 0:
        return (
            recent,
            False,
            None,
            "The prior-year comparable median is zero or negative, so a growth factor is undefined.",
        )

    factor = median([p.value for p in recent]) / prior_median
    if not YOY_FACTOR_MIN <= factor <= YOY_FACTOR_MAX:
        return (
            recent,
            False,
            round(factor, 6),
            (
                f"The year-over-year factor ({factor:.2f}x) is outside the stable band "
                f"[{YOY_FACTOR_MIN:g}x, {YOY_FACTOR_MAX:g}x], so the two years are not "
                "comparable and the factor was not applied."
            ),
        )

    return (
        recent,
        True,
        round(factor, 6),
        (
            f"History spans more than a year and is stable ({factor:.2f}x year over year), "
            "so the expectation is based on the most recent year and the prior year is "
            "kept as the growth reference."
        ),
    )


def _deviation(actual: float, expected: float) -> tuple[float, float | None, str | None]:
    absolute = actual - expected
    if expected == 0:
        if actual == 0:
            return absolute, 0.0, None
        return (
            absolute,
            None,
            "The expected value is zero, so a percentage deviation is undefined; "
            "the absolute movement is reported instead.",
        )
    return absolute, (absolute / abs(expected)) * 100.0, None


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.1f}%"


def detect(
    connector: DataSourceConnector,
    binding: KpiBinding,
    config: BucketConfig,
    target_date: date,
    *,
    materiality: KpiMaterialityRule | None = None,
    config_row: CompanyBucketConfig | None = None,
    coverage_cache: dict[CoverageKey, Coverage] | None = None,
    read: Reader | None = None,
) -> DetectionOutcome:
    """Run detection for one KPI on one date. Deterministic, top to bottom."""

    started = _time.perf_counter()
    read_day = read or _kpi_read
    version = binding.version
    notes: list[str] = [binding.time_field_note]
    notes.extend(config.warnings)
    queries = 0
    # How this source is being read, and the exact read used for the actual. Both
    # are structural (path, columns, bounds) and carry no credential and no row
    # content, so they are safe to store in the run's evidence and to log.
    source_detail = {**binding.describe_source(), "execution": execution_mode(connector)}

    # What period this source actually holds, established once from the registered
    # time field. Without it, a date the source has no data for reads back as a
    # zero (see :mod:`app.services.kpi_coverage`) and quietly becomes history.
    coverage = source_coverage(
        connector,
        schema=binding.table.schema_name,
        table=binding.table.table_name,
        time_column=binding.time_field,
        cache=coverage_cache,
    )
    if coverage.known:
        queries += 1
    source_detail["coverage"] = coverage.as_dict()

    def finish(**overrides: Any) -> DetectionOutcome:
        base: dict[str, Any] = {
            "company_id": version.company_id,
            "kpi_definition_id": version.kpi_id,
            "kpi_version_id": version.id,
            "kpi_version": version.version,
            "kpi_key": binding.kpi_key,
            "kpi_name": binding.name,
            "target_date": target_date,
            "time_grain": version.time_grain,
            "unit": version.unit,
            "currency": version.currency,
            "actual": None,
            "expected": None,
            "deviation_absolute": None,
            "deviation_pct": None,
            "status": DetectionStatus.LOW_CONFIDENCE,
            "comparison_label": "Recent days",
            "headline": "",
            "bucket_applied": BucketType.TRAILING_PERIOD,
            "buckets_applied": [],
            "bucket_decisions": [],
            "bucket_config_id": config_row.id if config_row else None,
            "bucket_config_key": config_row.config_key if config_row else None,
            "bucket_config_version": config_row.version if config_row else None,
            "bucket_signature": config.signature(target_date),
            "references": [],
            "median_value": None,
            "dispersion": None,
            "modified_z": None,
            "z_threshold": 0.0,
            "z_threshold_note": "",
            "tolerance_pct": materiality.relative_threshold_pct if materiality else None,
            "tolerance_absolute": materiality.absolute_threshold if materiality else None,
            "breached_tolerance": False,
            "statistically_significant": False,
            "relative_floor_pct": None,
            "movement_is_material": False,
            "yoy_applied": False,
            "yoy_factor": None,
            "method": "",
            "reason": "",
            "notes": notes,
            "query_count": queries,
            "duration_ms": int((_time.perf_counter() - started) * 1000),
            "source": source_detail,
        }
        base.update(overrides)
        return DetectionOutcome(**base)

    # --- 1. Actual, from the registered source and formula -----------------
    if not coverage.contains(target_date):
        # The source holds no data for this date at all. The KPI's null_handling
        # would turn that into a 0.0 and the surface would show it as a measured
        # value; saying so plainly is the only honest option.
        return finish(
            reason=(
                f"{binding.name} cannot be measured on {target_date.isoformat()}: the "
                f"registered source's data coverage on {binding.time_field} is "
                f"{coverage.describe()}, and this date falls outside it. No value was "
                "reported rather than treating an absent date as zero."
            ),
            method="No read was attempted: the target date is outside the source's data coverage.",
            headline=f"{binding.name} has no source data for {target_date.isoformat()}.",
        )

    reading = read_day(connector, binding, target_date)
    measured = reading.scalar
    queries += 1
    source_detail["query"] = reading.sql
    if measured is not None and measured.note:
        notes.append(f"Target date: {measured.note}")
    if measured is None or measured.value is None:
        detail = measured.note if measured and measured.note else "the source returned no value"
        return finish(
            reason=(
                f"The KPI could not be measured on {target_date.isoformat()}: {detail}. "
                "No expectation was computed, because there is nothing to compare."
            ),
            method="Aggregate evaluated at the registered source. No expectation computed.",
            headline=f"{binding.name} could not be measured on {target_date.isoformat()}.",
        )
    actual = float(measured.value)

    # --- 2. Which history is comparable ------------------------------------
    primary, applied, comparable, decisions = _choose_buckets(config, target_date)
    comparison_label = describe_buckets(config, target_date, applied)
    budgeted = _budget_dates(config, target_date, comparable)

    # --- 3. One KPI value per comparable date ------------------------------
    # Dates the source has no data for are dropped before any read: they cannot
    # produce an observation, and a KPI whose contract treats null as zero would
    # otherwise hand back a 0.0 that is indistinguishable from a real quiet day.
    in_coverage = [day for day in budgeted if coverage.contains(day)]
    uncovered_days = len(budgeted) - len(in_coverage)
    if uncovered_days:
        notes.append(
            f"{uncovered_days} comparable date(s) fall outside the source's data coverage "
            f"({coverage.describe()}) and were excluded without being read."
        )

    points: list[ReferencePoint] = []
    undefined_days = 0
    empty_days = 0
    for day in in_coverage:
        value = read_day(connector, binding, day).scalar
        queries += 1
        if value is None or value.value is None:
            undefined_days += 1
            continue
        if value.observed is False:
            # Inside the coverage window but holding no row: a genuine gap in the
            # source rather than a measured zero. Excluded, and counted separately
            # so the reason can say which kind of exclusion happened.
            empty_days += 1
            continue
        points.append(ReferencePoint(day=day, value=float(value.value)))
    if undefined_days:
        notes.append(
            f"{undefined_days} comparable date(s) produced no defined value and were excluded."
        )
    if empty_days:
        notes.append(
            f"{empty_days} comparable date(s) held no source row and were excluded rather "
            "than counted as zero."
        )
    points.sort(key=lambda point: point.day, reverse=True)

    common = {
        "actual": actual,
        "bucket_applied": primary,
        "buckets_applied": applied,
        "bucket_decisions": decisions,
        "comparison_label": comparison_label,
        "references": points,
    }

    if not points:
        if uncovered_days and not in_coverage:
            shortfall = (
                f"Every comparable date {comparison_label.lower()} selects for "
                f"{target_date.isoformat()} falls outside the source's data coverage "
                f"({coverage.describe()}), so there is no history to compare against."
            )
        elif empty_days and not undefined_days:
            shortfall = (
                "Every comparable date held no source row, so there is no observed "
                "history to compare against."
            )
        else:
            shortfall = (
                "No comparable historical date produced a value, so there is no "
                "basis for an expectation."
            )
        return finish(
            **common,
            status=DetectionStatus.LOW_CONFIDENCE,
            reason=(
                f"{shortfall} Detection reports low confidence rather than inventing one."
            ),
            method=(
                f"{comparison_label} over the last {config.effective_lookback_days} days; "
                "one bounded read per date at the registered source."
            ),
            headline=f"{binding.name} has no comparable history for {target_date.isoformat()}.",
        )

    # --- 4. Optional stable year-over-year re-basing -----------------------
    effective, yoy_applied, yoy_factor, yoy_note = _year_over_year(config, target_date, points)
    notes.append(yoy_note)
    if len(effective) < config.min_reference_points and len(points) >= config.min_reference_points:
        notes.append(
            "The most recent year alone had too few comparable dates, so the whole "
            "reference set was kept."
        )
        effective = points
        yoy_applied = False

    values = [point.value for point in effective]

    # --- 5. Expected value: robust median of the reference set -------------
    expected = median(values)
    deviation_absolute, deviation_pct, deviation_note = _deviation(actual, expected)
    if deviation_note:
        notes.append(deviation_note)

    # --- 6. Robust dispersion and the modified z-score ---------------------
    score = modified_z_score(actual, values, center=expected)
    if score.note:
        notes.append(score.note)
    z_threshold, z_note = parse_z_threshold(materiality.statistical_rule if materiality else None)

    tolerance_pct = materiality.relative_threshold_pct if materiality else None
    tolerance_absolute = materiality.absolute_threshold if materiality else None
    tolerance_stated = tolerance_pct is not None or tolerance_absolute is not None

    breached = False
    if tolerance_pct is not None and deviation_pct is not None:
        breached = breached or abs(deviation_pct) >= tolerance_pct
    if tolerance_absolute is not None:
        breached = breached or abs(deviation_absolute) >= tolerance_absolute

    significant = score.score is not None and abs(score.score) >= z_threshold

    # --- 6b. The KPI's own movement floor, as a ratio ----------------------
    # Significance is measured against this KPI's own history, so it is already
    # scale-aware in one sense: no KPI is compared to another's spread. What it is
    # not is *material* -- a KPI whose comparable days are nearly identical scores
    # highly on a movement the business would never act on. The floor below closes
    # that gap, and closes it in the only dimensionless way available: a percentage
    # of the KPI's own expected level. Nothing here consults how large the KPI's
    # numbers are, so a KPI counted in single units and one measured in millions
    # meet exactly the same rule and neither is judged against the other's figures.
    #
    # When the KPI registers a relative tolerance, that IS its floor: the business
    # has already stated how much movement matters for this measure, and the engine
    # does not second-guess it. Only a KPI whose registration states none falls back
    # to the documented platform default.
    if tolerance_pct is not None:
        relative_floor_pct = tolerance_pct
        floor_source = "the KPI's registered relative tolerance"
    else:
        relative_floor_pct = DEFAULT_RELATIVE_FLOOR_PCT
        floor_source = "the platform's default relative movement floor"
    if deviation_pct is None:
        # Expected zero against a non-zero actual: the relative movement is
        # undefined because it is unbounded, not because it is small. A floor
        # cannot exclude it.
        movement_is_material = True
    else:
        movement_is_material = abs(deviation_pct) >= relative_floor_pct

    # Statistical unusualness only becomes a verdict once the movement is also
    # material for this KPI. Business tolerance keeps its own independent route to
    # ABNORMAL below: an explicitly registered threshold is a business instruction,
    # not a statistical claim, and history cannot overrule it.
    statistically_abnormal = significant and movement_is_material

    # --- 7. Classification -------------------------------------------------
    reason_parts: list[str] = []
    if len(effective) < config.min_reference_points:
        status = DetectionStatus.LOW_CONFIDENCE
        reason_parts.append(
            f"Only {len(effective)} comparable date(s) were available; the configuration "
            f"requires {config.min_reference_points} before a verdict is trusted."
        )
        if version.sparse_history_strategy:
            reason_parts.append(
                f"The KPI declares a '{version.sparse_history_strategy}' strategy for "
                "sparse history; substituting a peer baseline is a separate, "
                "explicitly-requested analysis and is not applied silently here."
            )
    elif _too_little_history(version, target_date, effective):
        status = DetectionStatus.LOW_CONFIDENCE
        reason_parts.append(
            f"The comparable history spans less than the {version.min_history_days} days "
            "this KPI's registration requires before it is considered reliable."
        )
    elif score.score is None and not tolerance_stated:
        status = DetectionStatus.LOW_CONFIDENCE
        reason_parts.append(
            "Every comparable value was identical and the KPI has no materiality "
            "threshold, so there is no defensible basis for calling this normal or not."
        )
    elif statistically_abnormal and (breached or not tolerance_stated):
        status = DetectionStatus.ABNORMAL
        reason_parts.append(
            f"The movement is statistically significant (modified z-score "
            f"{score.score:.2f} against a {z_note})"
        )
        reason_parts.append(
            _materiality_sentence(breached, tolerance_stated, tolerance_pct, tolerance_absolute)
        )
    elif statistically_abnormal or breached:
        status = DetectionStatus.ABNORMAL
        if statistically_abnormal and not breached:
            reason_parts.append(
                f"The movement is statistically unusual (modified z-score {score.score:.2f}) "
                f"and is {_format_pct(deviation_pct)} against the expected level, past "
                f"{floor_source} ({relative_floor_pct:g}%), so it is classified as abnormal."
            )
        elif breached and not statistically_abnormal:
            reason_parts.append(
                "The movement breaches the business tolerance but is within the normal "
                "variability of comparable history, so the registered tolerance classifies "
                "it as abnormal."
            )
        else:
            reason_parts.append("The movement is outside the registered tolerance and history.")
    else:
        status = DetectionStatus.NORMAL
        if score.score is None:
            reason_parts.append(
                "Comparable history has no measurable spread and the movement is inside "
                "the business tolerance."
            )
        elif significant:
            # Unusual for this KPI's history, but too small a share of its own level
            # to matter. Saying both is what keeps the verdict defensible: the
            # statistic is reported, and so is the reason it did not decide.
            reason_parts.append(
                f"The movement is unusual against the spread of {comparison_label.lower()} "
                f"(modified z-score {score.score:.2f}), but at {_format_pct(deviation_pct)} "
                f"of the expected level it stays within {floor_source} "
                f"({relative_floor_pct:g}%), so it is not material for this KPI."
            )
        else:
            reason_parts.append(
                f"The movement is within the normal variability of {comparison_label.lower()} "
                f"(modified z-score {score.score:.2f} against a {z_note})."
            )

    # A SQL source computes the aggregate itself; a REST source cannot, so the
    # day's rows are read and aggregated deterministically. The method says which,
    # because "no rows left the source" is a claim only one of them can make.
    evaluation = (
        "a single aggregate pushed down to the source"
        if source_detail["execution"] == "sql_pushdown"
        else "one bounded, filtered read of that date aggregated deterministically"
    )
    method = (
        f"Expected value = median of {len(effective)} comparable KPI value(s) selected by "
        f"{comparison_label.lower()} from the KPI's registered source. Each value is "
        f"{evaluation}. Deviation, {score.basis.lower().replace('_', ' ')} and the modified "
        f"z-score are computed deterministically. The verdict combines that score with this "
        f"KPI's own relative movement floor ({relative_floor_pct:g}% of the expected level, "
        f"from {floor_source}) and its registered business tolerance, so it is judged against "
        "its own history and its own scale rather than against another KPI's numbers."
    )
    if version.expected_baseline_method and version.expected_baseline_method != "NOT_CONFIGURED":
        notes.append(
            f"The KPI registration names '{version.expected_baseline_method}' as its expected "
            "baseline method; the comparable-bucket median was used."
        )

    headline = _headline(
        binding.name, status, deviation_pct, deviation_absolute, comparison_label, version.direction
    )

    return finish(
        **common,
        expected=expected,
        deviation_absolute=deviation_absolute,
        deviation_pct=deviation_pct,
        status=status,
        headline=headline,
        median_value=expected,
        dispersion=score.dispersion,
        modified_z=score.score,
        z_threshold=z_threshold,
        z_threshold_note=z_note,
        tolerance_pct=tolerance_pct,
        tolerance_absolute=tolerance_absolute,
        breached_tolerance=breached,
        statistically_significant=significant,
        relative_floor_pct=relative_floor_pct,
        movement_is_material=movement_is_material,
        yoy_applied=yoy_applied,
        yoy_factor=yoy_factor,
        method=method,
        reason=" ".join(part for part in reason_parts if part),
    )


def _too_little_history(
    version: KpiVersion, target_date: date, points: list[ReferencePoint]
) -> bool:
    if not version.min_history_days or not points:
        return False
    oldest = min(point.day for point in points)
    return (target_date - oldest).days < version.min_history_days


def _materiality_sentence(
    breached: bool, stated: bool, tolerance_pct: float | None, tolerance_absolute: float | None
) -> str:
    if not stated:
        return (
            "and the KPI carries no materiality threshold, so statistical significance "
            "alone decides."
        )
    parts: list[str] = []
    if tolerance_pct is not None:
        parts.append(f"{tolerance_pct:g}% relative")
    if tolerance_absolute is not None:
        parts.append(f"{tolerance_absolute:g} absolute")
    return f"and it breaches the business tolerance ({' or '.join(parts)})."


def _headline(
    name: str,
    status: DetectionStatus,
    deviation_pct: float | None,
    deviation_absolute: float | None,
    comparison_label: str,
    direction: str,
) -> str:
    if status == DetectionStatus.LOW_CONFIDENCE:
        return f"{name}: not enough comparable history to judge this date."

    movement = _format_pct(deviation_pct)
    if deviation_pct is None:
        movement = f"{deviation_absolute:+,.2f}" if deviation_absolute is not None else "n/a"
    basis = comparison_label.lower()

    if status == DetectionStatus.NORMAL:
        return f"{name} is in line with {basis} ({movement})."
    falling = (deviation_absolute or 0) < 0
    better = (direction == "LOWER_IS_BETTER") == falling
    tone = "below" if falling else "above"
    qualifier = "" if better else " and against the KPI's preferred direction"
    if status == DetectionStatus.ABNORMAL:
        return f"{name} is {movement} {tone} {basis}{qualifier}."
    return f"{name} is drifting {tone} {basis} ({movement}){qualifier}."


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def persist_run(
    session: Session,
    outcome: DetectionOutcome,
    *,
    executed_by_user_id: str | None = None,
    agent_run_id: str | None = None,
) -> DetectionRun:
    """Store the run so the result can be shown, audited and re-explained."""

    run = DetectionRun(
        company_id=outcome.company_id,
        agent_run_id=agent_run_id,
        kpi_definition_id=outcome.kpi_definition_id,
        kpi_version_id=outcome.kpi_version_id,
        kpi_key=outcome.kpi_key,
        kpi_name=outcome.kpi_name,
        kpi_version=outcome.kpi_version,
        target_date=outcome.target_date,
        time_grain=outcome.time_grain,
        unit=outcome.unit,
        currency=outcome.currency,
        actual_value=outcome.actual,
        expected_value=outcome.expected,
        deviation_absolute=outcome.deviation_absolute,
        deviation_pct=outcome.deviation_pct,
        status=str(outcome.status),
        comparison_label=outcome.comparison_label,
        headline=outcome.headline,
        bucket_applied=str(outcome.bucket_applied),
        buckets_applied=[str(bucket) for bucket in outcome.buckets_applied],
        bucket_config_id=outcome.bucket_config_id,
        bucket_config_key=outcome.bucket_config_key,
        bucket_config_version=outcome.bucket_config_version,
        bucket_signature=outcome.bucket_signature,
        reference_count=len(outcome.references),
        reference_dates=[point.day.isoformat() for point in outcome.references],
        reference_values=[point.value for point in outcome.references],
        median_value=outcome.median_value,
        mad=None if outcome.dispersion is None else outcome.dispersion.mad,
        dispersion_basis=(
            DispersionBasis.NONE if outcome.dispersion is None else outcome.dispersion.basis
        ),
        modified_z_score=outcome.modified_z,
        z_threshold=outcome.z_threshold,
        tolerance_pct=outcome.tolerance_pct,
        tolerance_absolute=outcome.tolerance_absolute,
        breached_tolerance=outcome.breached_tolerance,
        statistically_significant=outcome.statistically_significant,
        yoy_applied=outcome.yoy_applied,
        yoy_adjustment_factor=outcome.yoy_factor,
        method=outcome.method,
        reason=outcome.reason,
        evidence=outcome.evidence(),
        query_count=outcome.query_count,
        duration_ms=outcome.duration_ms,
        executed_by_user_id=executed_by_user_id,
        executed_at=utcnow(),
    )
    session.add(run)
    session.flush()
    return run
