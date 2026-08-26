"""Join-safety analysis.

This exists to prevent the most dangerous failure mode in BI: a KPI that looks
correct and is wrong. Joining a fact table to something whose key repeats
multiplies the fact rows, and ``SUM`` over the multiplied rows silently inflates
the number. Nothing in the query errors; the dashboard just lies.

For every detected relationship the analysis measures, in the database:

* uniqueness of both sides
* average and worst-case fan-out
* duplicate-key rate on the parent
* orphan rate — how many rows an inner join would drop

and reduces that to SAFE / SAFE_WITH_AGGREGATION / RISKY / UNKNOWN with concrete
guidance. KPI validation reads this verdict before allowing activation.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.models.base import JoinSafetyLevel, RelationshipType
from app.models.profiling import ColumnProfile, JoinSafety, TableRelationship
from app.models.source import SourceTable

# Average rows per parent key above which a join materially multiplies facts.
FAN_OUT_WARNING = 1.05
FAN_OUT_RISKY = 2.0
# Worst-case multiplication for a single key.
MAX_FAN_OUT_RISKY = 5
# Rows an inner join would silently discard.
ORPHAN_LOSS_WARNING_PCT = 0.5


@dataclass(slots=True)
class JoinSafetyOutcome:
    analysed: int = 0
    safe: int = 0
    safe_with_aggregation: int = 0
    risky: int = 0
    unknown: int = 0

    def as_dict(self) -> dict:
        return {
            "analysed": self.analysed,
            "safe": self.safe,
            "safe_with_aggregation": self.safe_with_aggregation,
            "risky": self.risky,
            "unknown": self.unknown,
        }


def analyse_join_safety(
    session: Session,
    relationships: list[TableRelationship],
    tables_by_id: dict[str, SourceTable],
    connectors: dict[str, DataSourceConnector],
) -> JoinSafetyOutcome:
    outcome = JoinSafetyOutcome()
    profiles = _profiles_by_table_column(session, list(tables_by_id.values()))

    for relationship in relationships:
        child = tables_by_id.get(relationship.source_table_id)
        parent = tables_by_id.get(relationship.target_table_id)
        if child is None or parent is None:
            continue

        child_profile = profiles.get((child.id, relationship.source_column))
        parent_profile = profiles.get((parent.id, relationship.target_column))
        connector = connectors.get(parent.data_source_id)

        record = _upsert(session, relationship)
        record.source_is_unique = child_profile.is_unique if child_profile else None
        record.target_is_unique = parent_profile.is_unique if parent_profile else None
        record.source_uniqueness_ratio = _uniqueness_ratio(child_profile)
        record.target_uniqueness_ratio = _uniqueness_ratio(parent_profile)

        # Average fan-out: how many parent rows a single child row will match.
        record.fan_out_factor = _fan_out(parent_profile)
        record.duplicate_key_rate = _duplicate_rate(parent_profile)

        # Worst case matters more than the average: one key matching 40 rows
        # will distort a total even if the mean looks harmless.
        if connector is not None and record.target_is_unique is not True:
            record.max_fan_out = connector.max_group_size(
                parent.schema_name, parent.table_name, relationship.target_column
            )
        elif record.target_is_unique:
            record.max_fan_out = 1

        record.expected_cardinality = _expected_cardinality(relationship)
        record.observed_cardinality = relationship.relationship_type

        level, reason, guidance = _classify(
            relationship=relationship,
            record=record,
            child=child,
            parent=parent,
        )
        record.safety_level = level
        record.reason = reason
        record.guidance = guidance
        record.evidence = {
            "child": f"{child.table_name}.{relationship.source_column}",
            "parent": f"{parent.table_name}.{relationship.target_column}",
            "child_rows": child_profile.row_count if child_profile else None,
            "parent_rows": parent_profile.row_count if parent_profile else None,
            "parent_distinct": parent_profile.distinct_count if parent_profile else None,
            "orphan_pct": relationship.orphan_pct,
            "cardinality": relationship.relationship_type,
            "method": relationship.method,
            "cross_source": child.data_source_id != parent.data_source_id,
        }

        outcome.analysed += 1
        if level == JoinSafetyLevel.SAFE:
            outcome.safe += 1
        elif level == JoinSafetyLevel.SAFE_WITH_AGGREGATION:
            outcome.safe_with_aggregation += 1
        elif level == JoinSafetyLevel.RISKY:
            outcome.risky += 1
        else:
            outcome.unknown += 1

    return outcome


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _classify(
    *,
    relationship: TableRelationship,
    record: JoinSafety,
    child: SourceTable,
    parent: SourceTable,
) -> tuple[str, str, str]:
    child_ref = f"{child.table_name}.{relationship.source_column}"
    parent_ref = f"{parent.table_name}.{relationship.target_column}"
    orphan_note = ""
    if relationship.orphan_pct and relationship.orphan_pct > ORPHAN_LOSS_WARNING_PCT:
        orphan_note = (
            f" An inner join also drops {relationship.orphan_pct:.2f}% of "
            f"{child.table_name} rows that have no match."
        )

    if record.target_is_unique is None:
        return (
            JoinSafetyLevel.UNKNOWN,
            f"Uniqueness of {parent_ref} could not be measured, so fan-out is unknown.",
            "Profile both tables before relying on this join in a KPI.",
        )

    if record.target_is_unique:
        return (
            JoinSafetyLevel.SAFE,
            f"{parent_ref} is unique, so joining does not multiply {child.table_name} rows."
            + orphan_note,
            (
                "Safe to join directly."
                if not orphan_note
                else "Safe to join; use a LEFT JOIN to avoid dropping unmatched rows."
            ),
        )

    max_fan_out = record.max_fan_out or 0
    fan_out = record.fan_out_factor or 1.0

    if relationship.relationship_type == RelationshipType.MANY_TO_MANY:
        return (
            JoinSafetyLevel.RISKY,
            (
                f"Neither {child_ref} nor {parent_ref} is unique "
                f"(worst case {max_fan_out or 'unknown'} matching rows per key). "
                "A direct join multiplies rows on both sides." + orphan_note
            ),
            (
                f"Do not aggregate a measure across this join. Pre-aggregate "
                f"{parent.table_name} to the grain of {child.table_name}, or add "
                "the missing key columns so the join is one-to-many."
            ),
        )

    if max_fan_out >= MAX_FAN_OUT_RISKY or fan_out >= FAN_OUT_RISKY:
        return (
            JoinSafetyLevel.RISKY,
            (
                f"{parent_ref} repeats: on average {fan_out:.2f} and at worst "
                f"{max_fan_out} rows per key. Joining inflates every "
                f"{child.table_name} measure by that factor." + orphan_note
            ),
            (
                f"Aggregate {parent.table_name} to one row per "
                f"{relationship.target_column} before joining, or join on the full "
                "key so the match is unique."
            ),
        )

    if fan_out > FAN_OUT_WARNING or max_fan_out > 1:
        return (
            JoinSafetyLevel.SAFE_WITH_AGGREGATION,
            (
                f"{parent_ref} is not unique (average {fan_out:.2f}, worst case "
                f"{max_fan_out} rows per key), so a direct join duplicates some "
                f"{child.table_name} rows." + orphan_note
            ),
            (
                f"Aggregate {parent.table_name} to one row per "
                f"{relationship.target_column} first, then join."
            ),
        )

    return (
        JoinSafetyLevel.SAFE,
        f"Measured fan-out from {parent_ref} is 1.0; the join does not duplicate rows."
        + orphan_note,
        "Safe to join directly.",
    )


def _expected_cardinality(relationship: TableRelationship) -> str:
    """What the declaration implies, against which the observation is compared."""
    if relationship.is_declared:
        return RelationshipType.MANY_TO_ONE
    if relationship.method == "shared_dimension":
        return RelationshipType.MANY_TO_MANY
    return RelationshipType.UNKNOWN


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------
def _uniqueness_ratio(profile: ColumnProfile | None) -> float | None:
    if profile is None or not profile.row_count or profile.distinct_count is None:
        return None
    non_null = profile.row_count - (profile.null_count or 0)
    if non_null <= 0:
        return None
    return round(min(1.0, profile.distinct_count / non_null), 4)


def _fan_out(profile: ColumnProfile | None) -> float | None:
    """Average rows per distinct key on the parent side."""
    if profile is None or profile.distinct_count in (None, 0) or profile.row_count is None:
        return None
    non_null = profile.row_count - (profile.null_count or 0)
    if non_null <= 0:
        return None
    return round(non_null / profile.distinct_count, 4)


def _duplicate_rate(profile: ColumnProfile | None) -> float | None:
    ratio = _uniqueness_ratio(profile)
    return None if ratio is None else round(1.0 - ratio, 4)


def _profiles_by_table_column(
    session: Session, tables: list[SourceTable]
) -> dict[tuple[str, str], ColumnProfile]:
    if not tables:
        return {}
    table_ids = [table.id for table in tables]
    columns_by_id = {
        column.id: column.column_name for table in tables for column in table.columns
    }
    rows = session.scalars(
        select(ColumnProfile).where(ColumnProfile.source_table_id.in_(table_ids))
    )
    return {
        (row.source_table_id, columns_by_id[row.source_column_id]): row
        for row in rows
        if row.source_column_id in columns_by_id
    }


def _upsert(session: Session, relationship: TableRelationship) -> JoinSafety:
    record = session.scalar(
        select(JoinSafety).where(JoinSafety.relationship_id == relationship.id)
    )
    if record is None:
        record = JoinSafety(
            company_id=relationship.company_id, relationship_id=relationship.id
        )
        session.add(record)
        session.flush()
    return record
