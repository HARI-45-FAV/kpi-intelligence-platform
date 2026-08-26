"""Cross-source reconciliation metadata.

Sprint 1 does not compute multi-source KPIs. It answers a narrower and more
useful question first: *can these two tables safely cooperate in one analysis,
and if so under what transformation?*

Two tables at different grains cannot simply be joined. Orders at order grain
and marketing at (day, region, sector, channel) grain share a day and a region
but nothing finer, so any comparison must aggregate first. Recording that as
metadata means Sprint 2 reads the answer instead of rediscovering it — or worse,
assuming it.

The verdicts follow the specification: DIRECTLY_COMPATIBLE, REQUIRES_AGGREGATION,
REQUIRES_DIMENSION_MAPPING, UNSAFE, UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.models.base import ReconciliationStatus, SemanticType, TimeGrain
from app.models.profiling import SourceReconciliation, TableGrain
from app.models.source import SourceHealth, SourceTable

_GRAIN_ORDER = {
    TimeGrain.HOUR: 0,
    TimeGrain.DAY: 1,
    TimeGrain.WEEK: 2,
    TimeGrain.MONTH: 3,
    TimeGrain.QUARTER: 4,
    TimeGrain.YEAR: 5,
}


@dataclass(slots=True)
class ReconciliationOutcome:
    pairs: int = 0
    directly_compatible: int = 0
    requires_aggregation: int = 0
    requires_dimension_mapping: int = 0
    unsafe: int = 0
    unknown: int = 0
    # Tables excluded because they have no time axis — dimension lookups rather
    # than time series.
    skipped_no_time_axis: int = 0
    records: list[SourceReconciliation] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pairs_analysed": self.pairs,
            "directly_compatible": self.directly_compatible,
            "requires_aggregation": self.requires_aggregation,
            "requires_dimension_mapping": self.requires_dimension_mapping,
            "unsafe": self.unsafe,
            "unknown": self.unknown,
            "skipped_no_time_axis": self.skipped_no_time_axis,
        }


def analyse_reconciliation(
    session: Session, tables: list[SourceTable]
) -> ReconciliationOutcome:
    outcome = ReconciliationOutcome()
    grains = _grains(session, tables)
    health = _latest_health(session, tables)

    # Reconciliation is a question about *time series*: can these two be placed
    # on a common axis and compared? A dimension lookup has no such axis, and
    # structure alone cannot tell the difference — product_master.launch_date
    # looks exactly like orders.order_date to a schema reader. So the
    # administrator's declared primary time column is the signal, consistent
    # with the explicit-scope principle used throughout Sprint 1.
    timed = [table for table in tables if _declared_time_column(table)]
    outcome.skipped_no_time_axis = len(tables) - len(timed)

    for index, left in enumerate(timed):
        for right in timed[index + 1 :]:
            left_grain = grains.get(left.id)
            right_grain = grains.get(right.id)
            record = _upsert(session, left, right)

            shared, unmapped = _dimension_overlap(left, right)
            overlap_days = _time_overlap_days(health.get(left.id), health.get(right.id))
            status, reason, guidance = _classify(
                left=left,
                right=right,
                left_grain=left_grain,
                right_grain=right_grain,
                shared=shared,
                unmapped=unmapped,
                time_overlap_days=overlap_days,
            )

            record.status = status
            record.left_grain = left_grain.inferred_grain if left_grain else None
            record.right_grain = right_grain.inferred_grain if right_grain else None
            record.left_time_grain = left_grain.time_grain if left_grain else None
            record.right_time_grain = right_grain.time_grain if right_grain else None
            record.shared_dimensions = shared
            record.unmapped_dimensions = unmapped
            record.time_overlap_days = overlap_days
            record.reason = reason
            record.guidance = guidance
            record.evidence = {
                "left": left.qualified_name,
                "right": right.qualified_name,
                "same_source": left.data_source_id == right.data_source_id,
                "left_time_column": left_grain.time_column if left_grain else None,
                "right_time_column": right_grain.time_column if right_grain else None,
                "left_freshness": (
                    health[left.id].freshness_status if left.id in health else None
                ),
                "right_freshness": (
                    health[right.id].freshness_status if right.id in health else None
                ),
                "time_overlap_days": overlap_days,
            }

            outcome.pairs += 1
            outcome.records.append(record)
            _tally(outcome, status)

    return outcome


def _declared_time_column(table: SourceTable) -> str | None:
    """The administrator's designated time axis for this table, if any."""
    selection = table.selection
    return selection.primary_time_column if selection else None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _classify(
    *,
    left: SourceTable,
    right: SourceTable,
    left_grain: TableGrain | None,
    right_grain: TableGrain | None,
    shared: list[str],
    unmapped: list[str],
    time_overlap_days: int | None,
) -> tuple[str, str, str]:
    if left_grain is None or right_grain is None:
        return (
            ReconciliationStatus.UNKNOWN,
            "One or both tables have no detected grain.",
            "Profile and detect grain on both tables first.",
        )

    # No common history is the one genuinely unsafe case: whatever the grains
    # say, there is nothing to compare.
    if time_overlap_days is not None and time_overlap_days <= 0:
        return (
            ReconciliationStatus.UNSAFE,
            (
                f"{left.table_name} and {right.table_name} cover no overlapping "
                "period, so no comparison is possible."
            ),
            "Backfill one source, or restrict analysis to a period both cover.",
        )

    left_time = left_grain.time_grain
    right_time = right_grain.time_grain
    left_rank = _GRAIN_ORDER.get(left_time) if left_time else None
    right_rank = _GRAIN_ORDER.get(right_time) if right_time else None

    # A transaction-level timestamp has no fixed grain, so it rolls up to
    # anything. Treat it as the finest available.
    if left_rank is None:
        left_rank = 0
        left_time = "transaction"
    if right_rank is None:
        right_rank = 0
        right_time = "transaction"

    coarser_rank = max(left_rank, right_rank)
    common = left_time if left_rank == coarser_rank else right_time
    finer = right.table_name if left_rank == coarser_rank else left.table_name
    dimension_list = ", ".join(shared) if shared else None

    # Both sides have a time axis, so they can always be aligned on time. What
    # shared dimensions decide is how *finely* they can be aligned — which is
    # why a missing dimension is an aggregation constraint, not a hard failure.
    if not shared:
        return (
            ReconciliationStatus.REQUIRES_AGGREGATION,
            (
                f"Only the time axis is common: {left.table_name} and "
                f"{right.table_name} share no business dimension."
            ),
            (
                f"Aggregate both sides to {common} and compare totals only. "
                "Any finer breakdown needs a conforming dimension."
            ),
        )

    if unmapped:
        return (
            ReconciliationStatus.REQUIRES_DIMENSION_MAPPING,
            (
                f"Shared on {dimension_list}, but {', '.join(unmapped)} exists on "
                "only one side."
            ),
            (
                f"Aggregate to {common} x ({dimension_list}) and either map "
                f"{', '.join(unmapped)} explicitly or exclude it from combined analysis."
            ),
        )

    if left_rank != right_rank:
        return (
            ReconciliationStatus.REQUIRES_AGGREGATION,
            (
                f"{finer} is finer than {common} grain; the two cannot be joined "
                "row for row."
            ),
            f"Aggregate {finer} to {common} on {dimension_list} before comparing.",
        )

    # Same time grain and identical dimensions, but the row grains may still
    # differ (order grain vs day-region grain both roll up to DAY).
    if (left_grain.grain_columns or []) != (right_grain.grain_columns or []):
        return (
            ReconciliationStatus.REQUIRES_AGGREGATION,
            (
                f"Both sides are at {common} time grain but different row grains "
                f"({left_grain.inferred_grain} vs {right_grain.inferred_grain})."
            ),
            f"Aggregate both sides to {common} x ({dimension_list}) before comparing.",
        )

    return (
        ReconciliationStatus.DIRECTLY_COMPATIBLE,
        f"Matching {common} grain and identical dimensions ({dimension_list}).",
        "Can be combined directly on the shared dimensions.",
    )


def _dimension_overlap(left: SourceTable, right: SourceTable) -> tuple[list[str], list[str]]:
    """Categorical columns present on both sides, and those present on one only."""
    left_dims = _dimension_names(left)
    right_dims = _dimension_names(right)
    shared = sorted(left_dims & right_dims)
    unmapped = sorted(left_dims ^ right_dims)
    return (shared, unmapped)


def _dimension_names(table: SourceTable) -> set[str]:
    return {
        column.column_name.lower()
        for column in table.columns
        if column.semantic_type in {SemanticType.CATEGORICAL, SemanticType.BOOLEAN_FLAG}
    }


def _time_overlap_days(
    left: SourceHealth | None, right: SourceHealth | None
) -> int | None:
    """Days of history both sources actually cover."""
    if left is None or right is None:
        return None
    starts = [as_utc(left.coverage_start), as_utc(right.coverage_start)]
    ends = [as_utc(left.coverage_end), as_utc(right.coverage_end)]
    if any(value is None for value in starts + ends):
        return None
    start: datetime = max(starts)  # type: ignore[assignment]
    end: datetime = min(ends)  # type: ignore[assignment]
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 86400)


def _tally(outcome: ReconciliationOutcome, status: str) -> None:
    if status == ReconciliationStatus.DIRECTLY_COMPATIBLE:
        outcome.directly_compatible += 1
    elif status == ReconciliationStatus.REQUIRES_AGGREGATION:
        outcome.requires_aggregation += 1
    elif status == ReconciliationStatus.REQUIRES_DIMENSION_MAPPING:
        outcome.requires_dimension_mapping += 1
    elif status == ReconciliationStatus.UNSAFE:
        outcome.unsafe += 1
    else:
        outcome.unknown += 1


def _grains(session: Session, tables: list[SourceTable]) -> dict[str, TableGrain]:
    if not tables:
        return {}
    rows = session.scalars(
        select(TableGrain).where(TableGrain.source_table_id.in_([t.id for t in tables]))
    )
    return {row.source_table_id: row for row in rows}


def _latest_health(session: Session, tables: list[SourceTable]) -> dict[str, SourceHealth]:
    if not tables:
        return {}
    rows = session.scalars(
        select(SourceHealth)
        .where(SourceHealth.source_table_id.in_([t.id for t in tables]))
        .order_by(SourceHealth.checked_at.asc())
    )
    return {row.source_table_id: row for row in rows if row.source_table_id}


def _upsert(
    session: Session, left: SourceTable, right: SourceTable
) -> SourceReconciliation:
    # Deterministic orientation so re-running does not create mirrored rows.
    first, second = sorted((left, right), key=lambda t: t.id)
    record = session.scalar(
        select(SourceReconciliation).where(
            SourceReconciliation.left_table_id == first.id,
            SourceReconciliation.right_table_id == second.id,
        )
    )
    if record is None:
        record = SourceReconciliation(
            company_id=first.company_id,
            left_table_id=first.id,
            right_table_id=second.id,
        )
        session.add(record)
        session.flush()
    return record
