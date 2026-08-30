"""Deterministic source governance: column roles, table candidates, source health.

Three jobs, all of them arithmetic:

* **Column roles** — the business reading of a column (money, quantity, status,
  ...), proposed from the name, the declared type and the profiled cardinality.
  Kept apart from ``semantic_type`` on purpose: the KPI, grain and detection
  engines compute against ``semantic_type``, and a governance review must be able
  to disagree with the profiler without moving those numbers.
* **Table candidates** — which columns *could* identify a row, carry its time
  axis, or scope it to a tenant. Lists, not answers. A table with three plausible
  date columns has an ambiguity a reviewer needs to see, and collapsing it to one
  guess would hide exactly the thing that makes a KPI silently wrong.
* **Source health** — one status per source, rolled up from the per-table
  freshness observations and profiles that already exist. No model is consulted;
  the inputs are the declared cadence, the measured lag, the profiled
  completeness and the quality score.

Everything written here carries PROPOSED status. Only an explicit human review
call may write CONFIRMED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import as_utc, iso as _iso, utcnow
from app.models.base import (
    ColumnRole,
    FreshnessStatus,
    GrainStatus,
    MetadataStatus,
    RefreshFrequency,
    SemanticType,
    SourceHealthStatus,
)
from app.models.profiling import ColumnProfile, TableGrain, TableProfile
from app.models.source import DataSource, SourceColumn, SourceHealth, SourceTable

# --- Role vocabulary -------------------------------------------------------
# Substring hints, matched against the lowered column name. Deliberately narrow:
# a wrong CURRENCY proposal on a quantity column is worse than a plain MEASURE,
# because a reviewer skims confident-looking labels and stops reading.
_CURRENCY_HINTS = (
    "amount", "revenue", "price", "cost", "sales", "subtotal", "gross", "net",
    "margin", "fee", "charge", "discount", "tax", "payment", "balance", "salary",
    "spend", "budget", "invoice", "usd", "eur", "inr", "gbp", "_amt", "amt_",
)
_QUANTITY_HINTS = (
    "quantity", "qty", "count", "units", "num_", "_num", "volume", "weight",
    "duration", "minutes", "hours", "days", "visits", "clicks", "impressions",
    "sessions", "attempts", "items",
)
_STATUS_HINTS = (
    "status", "state", "stage", "phase", "flag", "is_", "has_", "active",
    "enabled", "disabled", "deleted", "cancelled", "canceled", "approved",
    "verified", "result", "outcome", "priority", "severity",
)
# Columns that scope a row to a tenant. A table carrying one of these is
# multi-tenant, and a KPI that forgets to filter on it reads across companies —
# so the candidate list exists to make that column impossible to overlook.
_COMPANY_HINTS = (
    "company_id", "companyid", "company_key", "org_id", "organisation_id",
    "organization_id", "tenant_id", "client_id", "business_id", "workspace_id",
    "merchant_id", "entity_id",
)
# Time columns that record when the *row* was written rather than when the
# business event happened. Still candidates — sometimes they are all there is —
# but ranked below event times.
_BOOKKEEPING_PREFIXES = (
    "created", "updated", "modified", "inserted", "loaded", "synced", "ingested",
)
_EVENT_TIME_HINTS = ("date", "_at", "time", "timestamp", "occurred", "ordered", "placed", "shipped")

# A status-ish column stops behaving like a status once it has this many values.
_STATUS_MAX_DISTINCT = 60
# At or above this share of distinct values a text column is too unique to be a
# category and behaves like a reference instead.
_IDENTIFIER_MIN_DISTINCT_PCT = 95.0
# How many candidates of each kind to keep. Enough to show the ambiguity, few
# enough that a review screen stays readable.
MAX_CANDIDATES = 5


def _matches(name: str, hints: tuple[str, ...]) -> bool:
    return any(hint in name for hint in hints)


# ---------------------------------------------------------------------------
# Column roles
# ---------------------------------------------------------------------------
def propose_column_role(column: SourceColumn, profile: ColumnProfile | None = None) -> str:
    """The business reading of one column, from structure plus profiled counts.

    Falls back to the semantic type whenever the name says nothing useful — an
    honest MEASURE beats a guessed CURRENCY. Returns UNKNOWN only when even the
    semantic type is unknown, so a reviewer can tell "nothing to go on" apart
    from "read but unremarkable".
    """
    name = (column.column_name or "").lower()
    semantic = column.semantic_type

    # Structure wins over naming: a declared key is an identifier whatever it is
    # called, and a temporal column is a time axis whatever it is called.
    if column.is_primary_key or column.is_foreign_key:
        return ColumnRole.IDENTIFIER
    if semantic in {SemanticType.DATE, SemanticType.TIMESTAMP}:
        return ColumnRole.TIME
    if semantic == SemanticType.IDENTIFIER:
        return ColumnRole.IDENTIFIER
    if semantic == SemanticType.BOOLEAN_FLAG:
        return ColumnRole.STATUS

    distinct_pct = profile.distinct_pct if profile else None
    distinct_count = profile.distinct_count if profile else None

    if semantic == SemanticType.NUMERIC_MEASURE:
        # Money and counts are both measures; which one changes how the value may
        # be aggregated and how it must be formatted, so the split is kept.
        if _matches(name, _CURRENCY_HINTS):
            return ColumnRole.CURRENCY
        if _matches(name, _QUANTITY_HINTS):
            return ColumnRole.QUANTITY
        return ColumnRole.MEASURE

    if semantic == SemanticType.CATEGORICAL:
        if _matches(name, _STATUS_HINTS) and (
            distinct_count is None or distinct_count <= _STATUS_MAX_DISTINCT
        ):
            return ColumnRole.STATUS
        return ColumnRole.DIMENSION

    if semantic == SemanticType.TEXT:
        # Free text that turns out to be unique is an identifier in practice (an
        # order reference, an email) whatever its declared type says.
        if distinct_pct is not None and distinct_pct >= _IDENTIFIER_MIN_DISTINCT_PCT:
            return ColumnRole.IDENTIFIER
        if _matches(name, _STATUS_HINTS) and (
            distinct_count is not None and distinct_count <= _STATUS_MAX_DISTINCT
        ):
            return ColumnRole.STATUS
        return ColumnRole.TEXT

    return ColumnRole.UNKNOWN


def apply_column_role(column: SourceColumn, profile: ColumnProfile | None = None) -> str:
    """Write the proposal onto the column, never touching a confirmed decision.

    ``candidate_role`` is the machine's opinion and is rewritten on every profile.
    ``confirmed_role`` is a human's and is never written here — that asymmetry is
    the whole point of keeping two fields.
    """
    column.candidate_role = propose_column_role(column, profile)
    if column.confirmed_role is None:
        column.role_status = MetadataStatus.PROPOSED
    return column.candidate_role


# ---------------------------------------------------------------------------
# Table candidates
# ---------------------------------------------------------------------------
def _rank_time_candidates(columns: list[SourceColumn]) -> list[str]:
    """Event times before bookkeeping times, otherwise column order."""
    event: list[str] = []
    bookkeeping: list[str] = []
    other: list[str] = []
    for column in columns:
        lowered = column.column_name.lower()
        if lowered.startswith(_BOOKKEEPING_PREFIXES):
            bookkeeping.append(column.column_name)
        elif _matches(lowered, _EVENT_TIME_HINTS):
            event.append(column.column_name)
        else:
            other.append(column.column_name)
    return event + other + bookkeeping


def propose_table_candidates(
    table: SourceTable,
    profiles: dict[str, ColumnProfile] | None = None,
    *,
    columns: list[SourceColumn] | None = None,
) -> dict[str, list[str]]:
    """Which columns could identify, date-stamp or tenant-scope a row.

    Measured uniqueness is used when profiling has produced it and structure when
    it has not, so this is callable straight after discovery as well as after a
    profile. Withheld columns are excluded: a column the profiling user could not
    read must not be proposed as a governed identifier on its name alone.

    ``columns`` lets a caller pass columns it is mid-way through writing, before
    the relationship on ``table`` has been refreshed.
    """
    profiles = profiles or {}
    source_columns = columns if columns is not None else list(table.columns)
    columns_in_order = [
        column
        for column in sorted(source_columns, key=lambda c: c.ordinal_position)
        if not (profiles.get(column.id) and profiles[column.id].access_withheld)
    ]

    identifiers: list[str] = []
    # Measured uniqueness is the strongest evidence; a declared primary key is
    # next; a name that merely reads like a key is the weakest and is only used
    # when nothing better exists.
    for column in columns_in_order:
        profile = profiles.get(column.id)
        if profile is not None and profile.is_unique:
            identifiers.append(column.column_name)
    for column in columns_in_order:
        if column.is_primary_key and column.column_name not in identifiers:
            identifiers.append(column.column_name)
    if not identifiers:
        identifiers = [
            column.column_name
            for column in columns_in_order
            if column.semantic_type == SemanticType.IDENTIFIER
        ]

    times = _rank_time_candidates(
        [
            c
            for c in columns_in_order
            if c.semantic_type in {SemanticType.DATE, SemanticType.TIMESTAMP}
        ]
    )
    company_fields = [
        column.column_name
        for column in columns_in_order
        if _matches(column.column_name.lower(), _COMPANY_HINTS)
    ]

    return {
        "primary_identifier_candidates": identifiers[:MAX_CANDIDATES],
        "time_field_candidates": times[:MAX_CANDIDATES],
        "company_field_candidates": company_fields[:MAX_CANDIDATES],
    }


def apply_table_candidates(
    table: SourceTable,
    profiles: dict[str, ColumnProfile] | None = None,
    *,
    columns: list[SourceColumn] | None = None,
) -> dict[str, list[str]]:
    """Write proposals onto the table unless a reviewer has confirmed them.

    A CONFIRMED candidate set is a statement about the business, and no automated
    pass may overwrite one. Re-running discovery or profiling on a confirmed table
    therefore changes nothing here — which is what makes the confirmation worth
    making.
    """
    current = {
        "primary_identifier_candidates": list(table.primary_identifier_candidates or []),
        "time_field_candidates": list(table.time_field_candidates or []),
        "company_field_candidates": list(table.company_field_candidates or []),
    }
    if table.candidates_status == MetadataStatus.CONFIRMED:
        return current

    proposed = propose_table_candidates(table, profiles, columns=columns)
    table.primary_identifier_candidates = proposed["primary_identifier_candidates"]
    table.time_field_candidates = proposed["time_field_candidates"]
    table.company_field_candidates = proposed["company_field_candidates"]
    table.candidates_status = MetadataStatus.PROPOSED
    return proposed


# ---------------------------------------------------------------------------
# Source health
# ---------------------------------------------------------------------------
# Below these scores a source is degraded even when it is loading on time. Two
# separate gates because they fail differently: incomplete data has holes, poor
# quality data has wrong values, and a KPI can be ruined by either.
DEGRADED_QUALITY_SCORE = 70.0
DEGRADED_COMPLETENESS_PCT = 80.0


@dataclass(slots=True)
class TableHealthLine:
    """One table's contribution to its source's health.

    This is the boundary between stored rows and arithmetic, so it is where
    timestamps are normalised. The values arriving here have two different
    provenances — rows loaded from the platform database, and rows written earlier
    in *this* unit of work — and on SQLite those disagree about tzinfo: a loaded
    row comes back UTC-aware through ``UtcDateTime``, while one that was assigned
    and flushed but never expired is still the aware object it was assigned. Mixed
    provenance is the normal case for a profile run, which measures and then rolls
    up in a single session.

    Normalising once, here, is what keeps the four rollup comparisons downstream
    (``max`` of coverage starts, ``min`` of coverage ends, ``max`` of refreshes,
    ``max`` of measurements) from ever seeing a naive value beside an aware one.
    """

    source_table_id: str
    table: str
    time_column: str | None
    freshness_status: str
    lag_seconds: int | None
    coverage_start: datetime | None
    coverage_end: datetime | None
    row_count: int | None
    completeness_pct: float | None
    quality_score: float | None
    grain: str | None
    grain_status: str | None
    profiled_at: datetime | None
    checked_at: datetime | None
    note: str | None = None

    def __post_init__(self) -> None:
        self.coverage_start = as_utc(self.coverage_start)
        self.coverage_end = as_utc(self.coverage_end)
        self.profiled_at = as_utc(self.profiled_at)
        self.checked_at = as_utc(self.checked_at)

    def as_dict(self) -> dict:
        return {
            "source_table_id": self.source_table_id,
            "table": self.table,
            "time_column": self.time_column,
            "freshness_status": self.freshness_status,
            "lag_seconds": self.lag_seconds,
            "coverage_start": _iso(self.coverage_start),
            "coverage_end": _iso(self.coverage_end),
            "row_count": self.row_count,
            "completeness_pct": self.completeness_pct,
            "quality_score": self.quality_score,
            "grain": self.grain,
            "grain_status": self.grain_status,
            "profiled_at": _iso(self.profiled_at),
            "checked_at": _iso(self.checked_at),
            "note": self.note,
        }


@dataclass(slots=True)
class SourceHealthVerdict:
    """The deterministic rollup, and the evidence it was computed from.

    ``checked_at`` is when the rollup was *computed*; ``measured_at`` is when the
    newest underlying measurement was taken. They differ on a read: projecting
    stored observations is cheap and safe, re-measuring is neither, so a read
    never re-measures and says so by leaving ``measured_at`` behind.
    """

    source_id: str
    status: str
    reason: str
    checked_at: datetime
    refresh_frequency: str
    measured_at: datetime | None = None
    last_refresh_at: datetime | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    completeness_pct: float | None = None
    quality_score: float | None = None
    grain: str | None = None
    fresh_tables: int = 0
    stale_tables: int = 0
    unknown_tables: int = 0
    unprofiled_tables: int = 0
    selected_table_count: int = 0
    known_limitations: str | None = None
    tables: list[TableHealthLine] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "reason": self.reason,
            "checked_at": _iso(self.checked_at),
            "measured_at": _iso(self.measured_at),
            "refresh_frequency": self.refresh_frequency,
            "last_refresh_at": _iso(self.last_refresh_at),
            "coverage_start": _iso(self.coverage_start),
            "coverage_end": _iso(self.coverage_end),
            "completeness_pct": self.completeness_pct,
            "quality_score": self.quality_score,
            "grain": self.grain,
            "fresh_tables": self.fresh_tables,
            "stale_tables": self.stale_tables,
            "unknown_tables": self.unknown_tables,
            "unprofiled_tables": self.unprofiled_tables,
            "selected_table_count": self.selected_table_count,
            "known_limitations": self.known_limitations,
            "tables": [line.as_dict() for line in self.tables],
        }


def assess_source_health(
    session: Session, source: DataSource, tables: list[SourceTable]
) -> SourceHealthVerdict:
    """Roll the per-table measurements up into one source status. No LLM.

    Precedence is UNKNOWN → STALE → DEGRADED → HEALTHY, and it is deliberate:

    * **UNKNOWN** when nothing has been measured, or the cadence was never
      declared so lag cannot be judged. Saying nothing is honest; saying HEALTHY
      because no test failed is not.
    * **STALE** outranks DEGRADED because a source that stopped loading makes its
      own quality figures out of date. Reporting "quality 62" on three-day-old
      numbers would understate the problem.
    * **DEGRADED** when the data arrives on time but is incomplete or fails
      quality checks.
    """
    now = utcnow()
    verdict = SourceHealthVerdict(
        source_id=source.id,
        status=SourceHealthStatus.UNKNOWN,
        reason="",
        checked_at=now,
        refresh_frequency=source.refresh_frequency,
        known_limitations=source.known_limitations,
        selected_table_count=len(tables),
    )
    if not tables:
        verdict.reason = (
            "No tables are in analytical scope for this source, so there is nothing "
            "to measure. Select tables under Data Scope."
        )
        return verdict

    table_ids = [table.id for table in tables]
    health_rows = _latest_health(session, table_ids)
    profiles = {
        row.source_table_id: row
        for row in session.scalars(
            select(TableProfile).where(TableProfile.source_table_id.in_(table_ids))
        )
    }
    grains = {
        row.source_table_id: row
        for row in session.scalars(
            select(TableGrain).where(TableGrain.source_table_id.in_(table_ids))
        )
    }

    completeness_values: list[float] = []
    quality_values: list[float] = []
    coverage_starts: list[datetime] = []
    coverage_ends: list[datetime] = []
    refreshes: list[datetime] = []
    measurements: list[datetime] = []

    for table in sorted(tables, key=lambda t: (t.schema_name, t.table_name)):
        health = health_rows.get(table.id)
        profile = profiles.get(table.id)
        grain = grains.get(table.id)

        line = TableHealthLine(
            source_table_id=table.id,
            table=table.qualified_name,
            time_column=health.time_column if health else None,
            freshness_status=health.freshness_status if health else FreshnessStatus.UNKNOWN,
            lag_seconds=health.freshness_lag_seconds if health else None,
            coverage_start=health.coverage_start if health else None,
            coverage_end=health.coverage_end if health else None,
            row_count=(health.row_count if health else None)
            or (profile.row_count if profile else None),
            completeness_pct=profile.completeness_pct if profile else None,
            quality_score=profile.quality_score if profile else None,
            grain=grain.effective_grain if grain else None,
            grain_status=grain.grain_status if grain else None,
            profiled_at=table.profiled_at or (profile.profiled_at if profile else None),
            checked_at=health.checked_at if health else None,
            note=(health.details or {}).get("note") if health else "Never checked for freshness.",
        )
        verdict.tables.append(line)

        if line.freshness_status == FreshnessStatus.FRESH:
            verdict.fresh_tables += 1
        elif line.freshness_status == FreshnessStatus.STALE:
            verdict.stale_tables += 1
        else:
            verdict.unknown_tables += 1
        if profile is None:
            verdict.unprofiled_tables += 1

        if line.completeness_pct is not None:
            completeness_values.append(line.completeness_pct)
        if line.quality_score is not None:
            quality_values.append(line.quality_score)
        if line.coverage_start is not None:
            coverage_starts.append(line.coverage_start)
        if line.coverage_end is not None:
            coverage_ends.append(line.coverage_end)
        if health is not None and health.last_refresh_at is not None:
            refreshes.append(as_utc(health.last_refresh_at))
        if line.checked_at is not None:
            measurements.append(line.checked_at)
        if line.profiled_at is not None:
            measurements.append(line.profiled_at)

    # Coverage is the *intersection* across tables, not the union: an analysis
    # spanning this source can only be trusted where every table has data. The
    # widest window would promise history that one table cannot supply.
    verdict.coverage_start = max(coverage_starts) if coverage_starts else None
    verdict.coverage_end = min(coverage_ends) if coverage_ends else None
    verdict.last_refresh_at = max(refreshes) if refreshes else None
    verdict.completeness_pct = (
        round(sum(completeness_values) / len(completeness_values), 3)
        if completeness_values
        else None
    )
    # The weakest table sets the source's quality. An average would let one clean
    # table hide a broken one, and a KPI reads a single table at a time.
    verdict.quality_score = round(min(quality_values), 3) if quality_values else None
    verdict.grain = _summarise_grain(verdict.tables)
    verdict.measured_at = max(measurements) if measurements else None

    verdict.status, verdict.reason = _classify(verdict, source)
    return verdict


def _classify(verdict: SourceHealthVerdict, source: DataSource) -> tuple[str, str]:
    """Turn the rollup into a status and a sentence explaining it."""
    measured = verdict.fresh_tables + verdict.stale_tables

    if source.refresh_frequency == RefreshFrequency.UNKNOWN:
        return (
            SourceHealthStatus.UNKNOWN,
            "Refresh cadence is not declared on this source, so lag cannot be judged. "
            "Set the cadence to enable freshness monitoring.",
        )
    if measured == 0:
        return (
            SourceHealthStatus.UNKNOWN,
            f"No table in scope has a measurable time column "
            f"({verdict.unknown_tables} of {verdict.selected_table_count} unmeasured). "
            "Set a primary time column under Data Scope, then run a health check.",
        )
    if verdict.stale_tables:
        return (
            SourceHealthStatus.STALE,
            f"{verdict.stale_tables} of {measured} measured table(s) are behind the declared "
            f"{verdict.refresh_frequency.lower()} cadence. Quality figures below were computed "
            "before that lag and may themselves be out of date.",
        )
    if verdict.quality_score is not None and verdict.quality_score < DEGRADED_QUALITY_SCORE:
        return (
            SourceHealthStatus.DEGRADED,
            f"Data is arriving on time, but the weakest table scores "
            f"{verdict.quality_score:.1f} against a {DEGRADED_QUALITY_SCORE:.0f} threshold. "
            "See the per-table quality warnings.",
        )
    if (
        verdict.completeness_pct is not None
        and verdict.completeness_pct < DEGRADED_COMPLETENESS_PCT
    ):
        return (
            SourceHealthStatus.DEGRADED,
            f"Data is arriving on time, but average completeness is "
            f"{verdict.completeness_pct:.1f}% against a {DEGRADED_COMPLETENESS_PCT:.0f}% "
            "threshold. Columns have significant missing values.",
        )
    if verdict.unknown_tables:
        return (
            SourceHealthStatus.HEALTHY,
            f"{verdict.fresh_tables} measured table(s) are within the declared "
            f"{verdict.refresh_frequency.lower()} cadence. "
            f"{verdict.unknown_tables} table(s) have no measurable time column and were "
            "excluded from the freshness verdict.",
        )
    return (
        SourceHealthStatus.HEALTHY,
        f"All {verdict.fresh_tables} table(s) in scope are within the declared "
        f"{verdict.refresh_frequency.lower()} cadence"
        + (
            f", weakest quality score {verdict.quality_score:.1f}."
            if verdict.quality_score is not None
            else " and no quality problems are recorded."
        ),
    )


def persist_source_health(source: DataSource, verdict: SourceHealthVerdict) -> DataSource:
    """Write the rollup onto the source so a list screen needs no recomputation.

    Only ever called from an explicit health check or profile — never from a read
    path. A list endpoint shows the last measurement and says when it was taken;
    it does not quietly re-measure.
    """
    source.health_status = verdict.status
    source.health_reason = verdict.reason
    source.health_checked_at = verdict.checked_at
    source.last_refresh_at = verdict.last_refresh_at
    source.coverage_start = verdict.coverage_start
    source.coverage_end = verdict.coverage_end
    source.completeness_pct = verdict.completeness_pct
    source.quality_score = verdict.quality_score
    source.grain = verdict.grain
    return source


# ---------------------------------------------------------------------------
# Grain status
# ---------------------------------------------------------------------------
def resolve_grain_status(grain: TableGrain) -> str:
    """How much authority the recorded grain carries.

    CONFIRMED survives everything — it is a human decision. Otherwise an
    administrator's declaration outranks inference, and inference is only ever
    PROPOSED. Nothing automated may promote a proposal.
    """
    if grain.grain_status == GrainStatus.CONFIRMED or grain.confirmed_grain:
        return GrainStatus.CONFIRMED
    if grain.declared_grain:
        return GrainStatus.DECLARED
    return GrainStatus.PROPOSED


def _summarise_grain(lines: list[TableHealthLine]) -> str | None:
    """One sentence for the source's grain, honest about disagreement.

    Tables inside a source routinely sit at different grains — orders and order
    items in the same database — and flattening that to a single value would be a
    lie the KPI layer later pays for.
    """
    stated = [line.grain for line in lines if line.grain]
    if not stated:
        return None
    unique = sorted(set(stated))
    if len(unique) == 1:
        return unique[0]
    head = ", ".join(unique[:3])
    suffix = f" (+{len(unique) - 3} more)" if len(unique) > 3 else ""
    return f"mixed across {len(lines)} tables: {head}{suffix}"


def _latest_health(session: Session, table_ids: list[str]) -> dict[str, SourceHealth]:
    """Most recent freshness observation per table."""
    if not table_ids:
        return {}
    rows = session.scalars(
        select(SourceHealth)
        .where(SourceHealth.source_table_id.in_(table_ids))
        .order_by(SourceHealth.checked_at.asc())
    )
    # Later rows overwrite earlier ones, leaving the newest per table.
    return {row.source_table_id: row for row in rows if row.source_table_id}
