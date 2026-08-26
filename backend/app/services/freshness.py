"""Source freshness and coverage.

Freshness is recorded, never corrected. A source that is three days behind stays
recorded as STALE with its measured lag, because the later confidence engine has
to be able to discount evidence built on stale data — and because a KPI computed
over a partially-loaded day is wrong in a way no amount of statistics can fix.

Lag is measured against the *declared* refresh cadence on the data source, so
"daily marketing data is 20 hours old" reads as FRESH while "15-minute sales data
is 20 hours old" reads as STALE.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.core.clock import as_utc, utcnow
from app.models.base import (
    REFRESH_INTERVAL_SECONDS,
    FreshnessStatus,
    SemanticType,
)
from app.models.profiling import TableGrain, TableProfile
from app.models.source import DataSource, SourceHealth, SourceTable

# A source is stale once it exceeds its expected interval by this multiple.
STALE_TOLERANCE = 2.0


@dataclass(slots=True)
class FreshnessOutcome:
    source_table_id: str
    table: str
    time_column: str | None
    status: str
    lag_seconds: int | None
    expected_interval_seconds: int | None
    coverage_start: str | None
    coverage_end: str | None
    row_count: int | None
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "source_table_id": self.source_table_id,
            "table": self.table,
            "time_column": self.time_column,
            "status": self.status,
            "lag_seconds": self.lag_seconds,
            "lag_human": _humanise(self.lag_seconds),
            "expected_interval_seconds": self.expected_interval_seconds,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "row_count": self.row_count,
            "note": self.note,
        }


@dataclass(slots=True)
class FreshnessReport:
    results: list[FreshnessOutcome] = field(default_factory=list)

    @property
    def fresh(self) -> int:
        return sum(1 for r in self.results if r.status == FreshnessStatus.FRESH)

    @property
    def stale(self) -> int:
        return sum(1 for r in self.results if r.status == FreshnessStatus.STALE)

    @property
    def unknown(self) -> int:
        return sum(1 for r in self.results if r.status == FreshnessStatus.UNKNOWN)

    def as_dict(self) -> dict:
        return {
            "checked": len(self.results),
            "fresh": self.fresh,
            "stale": self.stale,
            "unknown": self.unknown,
            "tables": [r.as_dict() for r in self.results],
        }


def check_freshness(
    session: Session,
    source: DataSource,
    tables: list[SourceTable],
    connector: DataSourceConnector,
) -> FreshnessReport:
    report = FreshnessReport()
    now = utcnow()
    expected_interval = REFRESH_INTERVAL_SECONDS.get(source.refresh_frequency)

    for table in tables:
        time_column = resolve_time_column(session, table)
        metadata = connector.get_refresh_metadata(
            table.schema_name, table.table_name, time_column
        )
        coverage_end = as_utc(metadata.coverage_end)
        coverage_start = as_utc(metadata.coverage_start)

        lag_seconds: int | None = None
        status = FreshnessStatus.UNKNOWN
        note: str | None = None

        if time_column is None:
            note = (
                "No time column identified; freshness cannot be determined. "
                "Set a primary time column under Data Scope."
            )
        elif coverage_end is None:
            note = "Time column contains no values."
        else:
            lag_seconds = max(0, int((now - coverage_end).total_seconds()))
            if expected_interval is None:
                note = (
                    "Source refresh cadence is UNKNOWN, so lag cannot be judged. "
                    "Set the cadence on the data source."
                )
            else:
                status = (
                    FreshnessStatus.FRESH
                    if lag_seconds <= expected_interval * STALE_TOLERANCE
                    else FreshnessStatus.STALE
                )
                if status == FreshnessStatus.STALE:
                    note = (
                        f"Latest row is {_humanise(lag_seconds)} old against an expected "
                        f"{_humanise(expected_interval)} cadence."
                    )

        health = SourceHealth(
            company_id=table.company_id,
            data_source_id=source.id,
            source_table_id=table.id,
            checked_at=now,
            time_column=time_column,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            last_refresh_at=coverage_end,
            freshness_lag_seconds=lag_seconds,
            expected_interval_seconds=expected_interval,
            freshness_status=status,
            row_count=metadata.row_count,
            completeness_pct=_completeness(session, table),
            quality_score=_quality_score(session, table),
            details={
                "refresh_frequency": source.refresh_frequency,
                "tolerance_multiple": STALE_TOLERANCE,
                "note": note,
            },
        )
        session.add(health)

        report.results.append(
            FreshnessOutcome(
                source_table_id=table.id,
                table=table.qualified_name,
                time_column=time_column,
                status=status,
                lag_seconds=lag_seconds,
                expected_interval_seconds=expected_interval,
                coverage_start=coverage_start.isoformat() if coverage_start else None,
                coverage_end=coverage_end.isoformat() if coverage_end else None,
                row_count=metadata.row_count,
                note=note,
            )
        )

    return report


def resolve_time_column(session: Session, table: SourceTable) -> str | None:
    """Administrator's choice first, then the inferred grain, then a heuristic."""
    if table.selection is not None and table.selection.primary_time_column:
        return table.selection.primary_time_column

    grain = session.scalar(select(TableGrain).where(TableGrain.source_table_id == table.id))
    if grain is not None and grain.time_column:
        return grain.time_column

    temporal = [
        column
        for column in sorted(table.columns, key=lambda c: c.ordinal_position)
        if column.semantic_type in {SemanticType.DATE, SemanticType.TIMESTAMP}
    ]
    if not temporal:
        return None
    # Prefer a name that reads like an event time over a bookkeeping timestamp.
    for preference in ("date", "_at", "time", "timestamp"):
        for column in temporal:
            lowered = column.column_name.lower()
            if preference in lowered and not lowered.startswith(("created", "updated", "modified")):
                return column.column_name
    return temporal[0].column_name


def latest_health(session: Session, table_ids: list[str]) -> dict[str, SourceHealth]:
    """Most recent observation per table."""
    if not table_ids:
        return {}
    rows = session.scalars(
        select(SourceHealth)
        .where(SourceHealth.source_table_id.in_(table_ids))
        .order_by(SourceHealth.checked_at.asc())
    )
    # Later rows overwrite earlier ones, leaving the newest per table.
    return {row.source_table_id: row for row in rows if row.source_table_id}


def _completeness(session: Session, table: SourceTable) -> float | None:
    profile = session.scalar(select(TableProfile).where(TableProfile.source_table_id == table.id))
    return profile.completeness_pct if profile else None


def _quality_score(session: Session, table: SourceTable) -> float | None:
    profile = session.scalar(select(TableProfile).where(TableProfile.source_table_id == table.id))
    return profile.quality_score if profile else None


def _humanise(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.1f} hr"
    return f"{hours / 24:.1f} days"
