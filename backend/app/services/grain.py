"""Grain detection: what does one row of this table represent?

The data decides. Nothing here knows that "sales" ought to be one row per order
line — it discovers the minimal column combination that uniquely identifies a
row, using ``COUNT(DISTINCT ...)`` pushed into the source.

Cost is bounded deliberately. A combinatorial search over n candidate columns is
O(2^n) queries; this uses **greedy forward selection** instead, which is O(n*k)
and deterministic. Greedy can theoretically miss a smaller key that only becomes
unique through a specific pair, so the outcome records the method and the
achieved uniqueness ratio rather than claiming certainty.

A surrogate primary key is reported separately from the *business* grain. Knowing
``sales`` has one row per ``order_line_id`` is far less useful than knowing it has
one row per ``(order_id, product_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.core.config import settings
from app.models.base import SemanticType, TimeGrain
from app.models.profiling import ColumnProfile, TableGrain, TableProfile
from app.models.source import SourceColumn, SourceTable
from app.services.classification import is_key_candidate, is_time_candidate

# Keep the candidate pool small enough that the scan stays cheap on a wide table.
MAX_CANDIDATES = 6
# Above this, a "categorical" column is really an identifier and adding it to a
# grain guess tells us nothing.
SUB_DAILY_DISTINCT_PER_DAY = 4.0


@dataclass(slots=True)
class GrainOutcome:
    source_table_id: str
    row_count: int | None
    grain_columns: list[str] = field(default_factory=list)
    distinct_combinations: int | None = None
    is_unique: bool | None = None
    confidence: float | None = None
    method: str = "uniqueness_scan"
    inferred_grain: str | None = None
    row_identity_columns: list[str] = field(default_factory=list)
    time_column: str | None = None
    time_grain: str | None = None
    evidence: dict = field(default_factory=dict)
    evidence_note: str | None = None


def detect_grain(
    session: Session,
    table: SourceTable,
    connector: DataSourceConnector,
) -> TableGrain:
    profiles = _profiles_by_column(session, table)
    row_count = _row_count(session, table, connector)
    outcome = GrainOutcome(source_table_id=table.id, row_count=row_count)

    if not row_count:
        outcome.method = "not_applicable"
        outcome.inferred_grain = "empty table"
        outcome.evidence = {"reason": "table has no rows"}
        return _persist(session, table, outcome)

    columns = [
        column
        for column in sorted(table.columns, key=lambda c: c.ordinal_position)
        if is_key_candidate(column.semantic_type)
        and not (profiles.get(column.id) and profiles[column.id].access_withheld)
    ]

    # --- Row identity: a single unique column, preferring the declared PK ----
    unique_singles = [
        column
        for column in columns
        if profiles.get(column.id) and profiles[column.id].is_unique
    ]
    declared_pk = [c for c in unique_singles if c.is_primary_key]
    identity = declared_pk[0] if declared_pk else (unique_singles[0] if unique_singles else None)
    if identity is not None:
        outcome.row_identity_columns = [identity.column_name]

    # --- Business grain: greedy search, excluding the surrogate identity -----
    searchable = [
        column
        for column in columns
        if identity is None or column.column_name != identity.column_name
    ]
    searchable = _rank_candidates(searchable, profiles)[:MAX_CANDIDATES]

    best_columns, best_distinct = _greedy_search(
        connector, table, searchable, profiles, row_count
    )
    business_grain_is_unique = bool(
        best_columns and best_distinct and best_distinct >= row_count
    )

    if business_grain_is_unique:
        # A composite of business columns identifies a row: more informative
        # than the surrogate key. order_items -> (order_id, product_id).
        outcome.grain_columns = [c.column_name for c in best_columns]
        outcome.distinct_combinations = best_distinct
        outcome.is_unique = True
        outcome.confidence = 1.0
    elif identity is not None:
        # No business composite is unique, so the key itself *is* the grain.
        # orders -> one row per order_id. Reporting the partial composite here
        # would claim a grain the data does not support.
        outcome.grain_columns = [identity.column_name]
        outcome.distinct_combinations = row_count
        outcome.is_unique = True
        outcome.confidence = 1.0
        outcome.method = (
            "declared_primary_key" if identity.is_primary_key else "unique_column"
        )
        outcome.evidence_note = (
            f"No combination of business columns is unique, so "
            f"{identity.column_name} defines the grain."
        )
    elif best_columns and best_distinct:
        # Nothing is unique. Report the best partial grain with its real
        # uniqueness ratio rather than implying certainty.
        outcome.grain_columns = [c.column_name for c in best_columns]
        outcome.distinct_combinations = best_distinct
        outcome.is_unique = False
        outcome.confidence = round(min(1.0, best_distinct / row_count), 4)
        outcome.method = "uniqueness_scan_partial"
    else:
        outcome.method = "inconclusive"
        outcome.confidence = 0.0

    outcome.time_column, outcome.time_grain = _detect_time_grain(
        table, columns, profiles, outcome.grain_columns
    )
    outcome.inferred_grain = _describe(outcome, table)
    outcome.evidence = {
        "row_count": row_count,
        "candidates_considered": [c.column_name for c in searchable],
        "row_identity": outcome.row_identity_columns,
        "distinct_combinations": outcome.distinct_combinations,
        "uniqueness_ratio": outcome.confidence,
        "business_grain_found": business_grain_is_unique,
        "best_composite": [c.column_name for c in best_columns],
        "best_composite_distinct": best_distinct,
        "search": "greedy_forward_selection",
        "max_columns": settings.grain_max_candidate_columns,
        "note": outcome.evidence_note
        or (
            "Greedy selection is bounded to keep query cost linear; a smaller "
            "composite key that only becomes unique as a specific pair may not "
            "be found."
        ),
    }
    return _persist(session, table, outcome)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _greedy_search(
    connector: DataSourceConnector,
    table: SourceTable,
    candidates: list[SourceColumn],
    profiles: dict[str, ColumnProfile],
    row_count: int,
) -> tuple[list[SourceColumn], int | None]:
    """Add the most discriminating column each round until rows are unique."""
    if not candidates:
        return ([], None)

    chosen: list[SourceColumn] = []
    remaining = list(candidates)
    current_distinct = 0
    max_columns = max(1, settings.grain_max_candidate_columns)

    while remaining and len(chosen) < max_columns:
        best_column: SourceColumn | None = None
        best_count = current_distinct

        for candidate in remaining:
            if not chosen:
                # Single-column counts are already known from profiling: free.
                profile = profiles.get(candidate.id)
                count = profile.distinct_count if profile else None
            else:
                count = connector.count_distinct_combination(
                    table.schema_name,
                    table.table_name,
                    [c.column_name for c in chosen] + [candidate.column_name],
                )
            if count is not None and count > best_count:
                best_count = count
                best_column = candidate

        if best_column is None:
            # No remaining column adds any discrimination.
            break

        chosen.append(best_column)
        remaining.remove(best_column)
        current_distinct = best_count

        if current_distinct >= row_count:
            break

    if not chosen:
        return ([], None)
    return (chosen, current_distinct)


def _rank_candidates(
    columns: list[SourceColumn], profiles: dict[str, ColumnProfile]
) -> list[SourceColumn]:
    """Highest cardinality first — those narrow the grain fastest."""

    def key(column: SourceColumn) -> tuple[int, int]:
        profile = profiles.get(column.id)
        distinct = profile.distinct_count if profile and profile.distinct_count else 0
        # Prefer declared keys at equal cardinality.
        priority = 1 if (column.is_primary_key or column.is_foreign_key) else 0
        return (-distinct, -priority)

    return sorted(columns, key=key)


# ---------------------------------------------------------------------------
# Time grain
# ---------------------------------------------------------------------------
def _detect_time_grain(
    table: SourceTable,
    columns: list[SourceColumn],
    profiles: dict[str, ColumnProfile],
    grain_columns: list[str],
) -> tuple[str | None, str | None]:
    time_columns = [c for c in columns if is_time_candidate(c.semantic_type)]
    if not time_columns:
        return (None, None)

    declared = table.selection.primary_time_column if table.selection else None
    chosen = next((c for c in time_columns if c.column_name == declared), None)
    if chosen is None:
        # Prefer a time column that participates in the grain: that is the one
        # the table is actually organised by.
        chosen = next((c for c in time_columns if c.column_name in grain_columns), None)
    if chosen is None:
        chosen = min(
            time_columns,
            key=lambda c: (profiles[c.id].null_pct if profiles.get(c.id) and profiles[c.id].null_pct is not None else 100.0),
        )

    if chosen.semantic_type == SemanticType.DATE:
        return (chosen.column_name, TimeGrain.DAY)

    # A timestamp that is part of the grain is a snapshot cadence; one that is
    # not is an event time, and the table has no fixed time grain.
    if chosen.column_name not in grain_columns:
        return (chosen.column_name, None)

    profile = profiles.get(chosen.id)
    span_days = _span_days(profile)
    if profile and profile.distinct_count and span_days:
        per_day = profile.distinct_count / span_days
        if per_day > SUB_DAILY_DISTINCT_PER_DAY:
            return (chosen.column_name, TimeGrain.HOUR)
    return (chosen.column_name, TimeGrain.DAY)


def _span_days(profile: ColumnProfile | None) -> float | None:
    if profile is None or not profile.min_value or not profile.max_value:
        return None
    start = _parse(profile.min_value)
    end = _parse(profile.max_value)
    if start is None or end is None:
        return None
    return max(1.0, (end - start).total_seconds() / 86400.0)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Description and persistence
# ---------------------------------------------------------------------------
def _describe(outcome: GrainOutcome, table: SourceTable) -> str:
    if not outcome.grain_columns:
        return "grain could not be determined from the data"
    columns = ", ".join(outcome.grain_columns)
    if outcome.is_unique:
        return f"one row per ({columns})"
    pct = (outcome.confidence or 0) * 100
    return f"approximately one row per ({columns}) — {pct:.1f}% unique"


def _profiles_by_column(session: Session, table: SourceTable) -> dict[str, ColumnProfile]:
    rows = session.scalars(
        select(ColumnProfile).where(ColumnProfile.source_table_id == table.id)
    )
    return {row.source_column_id: row for row in rows}


def _row_count(
    session: Session, table: SourceTable, connector: DataSourceConnector
) -> int | None:
    profile = session.scalar(select(TableProfile).where(TableProfile.source_table_id == table.id))
    if profile is not None and profile.row_count is not None:
        return profile.row_count
    return connector.count_rows(table.schema_name, table.table_name)


def _persist(session: Session, table: SourceTable, outcome: GrainOutcome) -> TableGrain:
    grain = session.scalar(select(TableGrain).where(TableGrain.source_table_id == table.id))
    if grain is None:
        grain = TableGrain(company_id=table.company_id, source_table_id=table.id)
        session.add(grain)
        session.flush()

    grain.declared_grain = table.selection.declared_grain if table.selection else None
    grain.inferred_grain = outcome.inferred_grain
    grain.grain_columns = outcome.grain_columns
    grain.confidence = outcome.confidence
    grain.method = outcome.method
    grain.row_count = outcome.row_count
    grain.distinct_combinations = outcome.distinct_combinations
    grain.is_unique = outcome.is_unique
    grain.time_column = outcome.time_column
    grain.time_grain = outcome.time_grain
    grain.evidence = outcome.evidence
    return grain
