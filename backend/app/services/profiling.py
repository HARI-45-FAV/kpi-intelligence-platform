"""Access-aware data profiling and quality assessment.

Three properties this module is built to guarantee:

* **Pushdown.** Every statistic is an aggregate computed inside the source
  database. No table is streamed into application memory to work out a null
  percentage.
* **Access-aware, not redacted-after.** A column the caller is not entitled to
  read is never queried. It is recorded as *withheld*, so the catalog states
  what it could not see instead of quietly presenting a partial picture as
  complete.
* **No silent repair.** Defects become stored warnings. Sprint 1 does not clean,
  impute or coerce anything — the later confidence engine needs the defects
  intact to discount evidence built on them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ColumnStats, DataSourceConnector
from app.core.clock import utcnow
from app.core.deps import AccessContext
from app.models.base import QualityStatus, SemanticType
from app.models.profiling import ColumnProfile, TableProfile
from app.models.source import SourceColumn, SourceTable
from app.services.classification import refine_semantic_type
from app.connectors.sql import classify_type_family

# Thresholds for turning raw statistics into a quality verdict.
NULL_PCT_WARNING = 10.0
NULL_PCT_POOR = 40.0
QUALITY_GOOD_SCORE = 95.0
QUALITY_WARNING_SCORE = 80.0


@dataclass(slots=True)
class ProfileOutcome:
    table: SourceTable
    profile: TableProfile
    column_profiles: list[ColumnProfile] = field(default_factory=list)
    withheld: list[dict] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "source_table_id": self.table.id,
            "table": self.table.qualified_name,
            "row_count": self.profile.row_count,
            "profiled_columns": self.profile.profiled_column_count,
            "withheld_columns": self.profile.withheld_column_count,
            "completeness_pct": self.profile.completeness_pct,
            "quality_score": self.profile.quality_score,
            "quality_status": self.profile.quality_status,
            "warnings": self.profile.warnings,
            "duration_ms": self.duration_ms,
        }


def profile_table(
    session: Session,
    table: SourceTable,
    connector: DataSourceConnector,
    access: AccessContext,
) -> ProfileOutcome:
    """Profile every column the caller is entitled to read."""
    started = time.perf_counter()
    now = utcnow()

    row_count = connector.count_rows(table.schema_name, table.table_name)
    profile = _upsert_table_profile(session, table)
    profile.profiled_at = now
    profile.row_count = row_count

    outcome = ProfileOutcome(table=table, profile=profile)
    completeness_values: list[float] = []
    penalty = 0.0
    table_warnings: list[str] = []

    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        if not access.can_read_column(column, table_name=table.table_name):
            reason = access.withheld_reason(column)
            outcome.withheld.append({"column": column.column_name, "reason": reason})
            _record_withheld(session, table, column, now, reason)
            continue

        stats = connector.profile_column(
            table.schema_name,
            table.table_name,
            column.column_name,
            type_family=classify_type_family(column.data_type),
        )
        column_profile, warnings, status = _persist_column_profile(
            session, table, column, stats, now
        )
        outcome.column_profiles.append(column_profile)

        if stats.null_pct is not None:
            completeness_values.append(100.0 - stats.null_pct)
        if status == QualityStatus.POOR:
            penalty += 6.0
        elif status == QualityStatus.WARNING:
            penalty += 2.0
        table_warnings.extend(f"{column.column_name}: {w}" for w in warnings)

    profile.profiled_column_count = len(outcome.column_profiles)
    profile.withheld_column_count = len(outcome.withheld)
    profile.withheld_columns = outcome.withheld

    if row_count == 0:
        profile.completeness_pct = 0.0
        profile.quality_score = 0.0
        profile.quality_status = QualityStatus.POOR
        table_warnings.insert(0, "table is empty")
    elif completeness_values:
        completeness = sum(completeness_values) / len(completeness_values)
        profile.completeness_pct = round(completeness, 3)
        profile.quality_score = round(max(0.0, min(100.0, completeness - penalty)), 3)
        profile.quality_status = _score_to_status(profile.quality_score)
    else:
        profile.completeness_pct = None
        profile.quality_score = None
        profile.quality_status = QualityStatus.UNKNOWN
        table_warnings.insert(0, "no columns were readable under the current entitlement")

    if outcome.withheld:
        table_warnings.append(
            f"{len(outcome.withheld)} column(s) withheld by access policy; "
            "profile is intentionally incomplete"
        )

    profile.warnings = table_warnings
    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    profile.duration_ms = outcome.duration_ms
    return outcome


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _upsert_table_profile(session: Session, table: SourceTable) -> TableProfile:
    profile = session.scalar(
        select(TableProfile).where(TableProfile.source_table_id == table.id)
    )
    if profile is None:
        profile = TableProfile(company_id=table.company_id, source_table_id=table.id, profiled_at=utcnow())
        session.add(profile)
        session.flush()
    return profile


def _upsert_column_profile(
    session: Session, table: SourceTable, column: SourceColumn
) -> ColumnProfile:
    profile = session.scalar(
        select(ColumnProfile).where(ColumnProfile.source_column_id == column.id)
    )
    if profile is None:
        profile = ColumnProfile(
            company_id=table.company_id,
            source_table_id=table.id,
            source_column_id=column.id,
            profiled_at=utcnow(),
        )
        session.add(profile)
        session.flush()
    return profile


def _record_withheld(
    session: Session, table: SourceTable, column: SourceColumn, now, reason: str
) -> ColumnProfile:
    profile = _upsert_column_profile(session, table, column)
    profile.profiled_at = now
    profile.access_withheld = True
    profile.quality_status = QualityStatus.UNKNOWN
    profile.warnings = [f"not profiled: {reason}"]
    # Any previously computed statistics are cleared: keeping them would expose
    # exactly what the entitlement is meant to hide.
    for attribute in (
        "row_count", "null_count", "null_pct", "distinct_count", "distinct_pct",
        "min_value", "max_value", "mean_value", "zero_count", "negative_count", "blank_count",
    ):
        setattr(profile, attribute, None)
    profile.sample_values = []
    profile.is_unique = None
    profile.is_candidate_key = False
    return profile


def _persist_column_profile(
    session: Session,
    table: SourceTable,
    column: SourceColumn,
    stats: ColumnStats,
    now,
) -> tuple[ColumnProfile, list[str], str]:
    profile = _upsert_column_profile(session, table, column)
    warnings, status = assess_column_quality(column, stats)

    profile.profiled_at = now
    profile.access_withheld = False
    profile.row_count = stats.row_count
    profile.null_count = stats.null_count
    profile.null_pct = stats.null_pct
    profile.distinct_count = stats.distinct_count
    profile.distinct_pct = stats.distinct_pct
    profile.min_value = _truncate(stats.min_value)
    profile.max_value = _truncate(stats.max_value)
    profile.mean_value = stats.mean_value
    profile.zero_count = stats.zero_count
    profile.negative_count = stats.negative_count
    profile.blank_count = stats.blank_count
    profile.sample_values = stats.sample_values
    profile.is_unique = stats.is_unique
    profile.is_candidate_key = bool(
        stats.is_unique and (column.is_primary_key or column.semantic_type == SemanticType.IDENTIFIER)
    )
    profile.quality_status = status
    profile.warnings = warnings

    # Second-pass semantic refinement, now that cardinality is known.
    column.semantic_type = refine_semantic_type(
        current=column.semantic_type,
        column_name=column.column_name,
        type_family=classify_type_family(column.data_type),
        is_primary_key=column.is_primary_key,
        is_foreign_key=column.is_foreign_key,
        stats=stats,
    )
    return profile, warnings, status


def assess_column_quality(column: SourceColumn, stats: ColumnStats) -> tuple[list[str], str]:
    """Turn statistics into warnings and a status. Deterministic, no repair."""
    warnings: list[str] = []
    status = QualityStatus.GOOD

    if not stats.row_count:
        return (["no rows to assess"], QualityStatus.UNKNOWN)

    null_pct = stats.null_pct or 0.0
    if null_pct >= NULL_PCT_POOR:
        warnings.append(f"{null_pct:.2f}% null")
        status = QualityStatus.POOR
    elif null_pct >= NULL_PCT_WARNING:
        warnings.append(f"{null_pct:.2f}% null")
        status = QualityStatus.WARNING
    elif null_pct > 0 and column.semantic_type == SemanticType.NUMERIC_MEASURE:
        # Below the warning threshold the table is still healthy, but a measure
        # column is a special case: rows with a null contribute nothing to a SUM,
        # so the aggregate is quietly computed over fewer rows than it appears.
        # Recorded as a note without downgrading the status.
        warnings.append(
            f"{null_pct:.2f}% null in a measure column: those rows contribute "
            "nothing to SUM or AVG"
        )

    # A declared NOT NULL column holding nulls means the declaration and the
    # data disagree — worth surfacing loudly.
    if not column.is_nullable and (stats.null_count or 0) > 0:
        warnings.append("declared NOT NULL but contains nulls")
        status = QualityStatus.POOR

    if stats.distinct_count == 0:
        warnings.append("no non-null values")
        status = QualityStatus.POOR
    elif stats.distinct_count == 1 and stats.row_count > 1:
        warnings.append("single distinct value: no analytical variance")
        status = max(status, QualityStatus.WARNING, key=_severity)

    if column.is_primary_key and stats.is_unique is False:
        warnings.append("declared primary key is not unique in the data")
        status = QualityStatus.POOR

    if (stats.negative_count or 0) > 0 and column.semantic_type == SemanticType.NUMERIC_MEASURE:
        warnings.append(f"{stats.negative_count} negative value(s)")
        status = max(status, QualityStatus.WARNING, key=_severity)

    if (stats.blank_count or 0) > 0:
        warnings.append(f"{stats.blank_count} blank string(s)")
        status = max(status, QualityStatus.WARNING, key=_severity)

    return (warnings, status)


_SEVERITY = {
    QualityStatus.GOOD: 0,
    QualityStatus.UNKNOWN: 1,
    QualityStatus.WARNING: 2,
    QualityStatus.POOR: 3,
}


def _severity(status: str) -> int:
    return _SEVERITY.get(status, 0)


def _score_to_status(score: float | None) -> str:
    if score is None:
        return QualityStatus.UNKNOWN
    if score >= QUALITY_GOOD_SCORE:
        return QualityStatus.GOOD
    if score >= QUALITY_WARNING_SCORE:
        return QualityStatus.WARNING
    return QualityStatus.POOR


def _truncate(value: object, limit: int = 300) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
