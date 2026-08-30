"""Read-side views over the deterministic profiling and analysis results.

These shapers were written for the analysis API and now have a second reader:
the Copilot's governed tools. They live here so both surfaces answer from the
same code. If the Copilot built its own view of a table profile or a join-safety
verdict, the two could drift, and an explanation that disagrees with the screen
it is explaining is worse than no explanation.

Every function takes the caller's ``AccessContext`` and is bounded by it:

* ``scoped_tables`` returns only tables inside the caller's company *and* inside
  the administrator-approved data scope;
* ``column_payload`` decides readability per column before emitting anything, so
  a column the caller may not see arrives as ``readable: false`` with a reason
  and no profile -- never as data that was fetched and then trimmed;
* relationship and reconciliation views are restricted to scoped table ids on
  both sides of the pair.

Nothing here reads tenant business rows. It reads metadata the platform computed
earlier and stored in its own database.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import AccessContext
from app.models.base import JoinSafetyLevel
from app.models.profiling import (
    ColumnProfile,
    JoinSafety,
    SourceReconciliation,
    TableGrain,
    TableProfile,
    TableRelationship,
)
from app.models.source import SelectedTable, SourceHealth, SourceTable


def scoped_tables(session: Session, access: AccessContext) -> list[SourceTable]:
    return list(
        session.scalars(
            select(SourceTable)
            .join(SelectedTable, SelectedTable.source_table_id == SourceTable.id)
            .where(
                SourceTable.company_id == access.company.id, SelectedTable.enabled.is_(True)
            )
            .order_by(SourceTable.table_name)
        )
    )


def column_payload(session: Session, table: SourceTable, access: AccessContext) -> list[dict]:
    profiles = {
        row.source_column_id: row
        for row in session.scalars(
            select(ColumnProfile).where(ColumnProfile.source_table_id == table.id)
        )
    }
    payload: list[dict] = []
    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        readable = access.can_read_column(column, table_name=table.table_name)
        profile = profiles.get(column.id)
        entry: dict = {
            "column_name": column.column_name,
            "data_type": column.data_type,
            "semantic_type": column.semantic_type,
            "classification": column.classification,
            "is_pii": column.is_pii,
            "is_restricted": column.is_restricted,
            "is_primary_key": column.is_primary_key,
            "is_foreign_key": column.is_foreign_key,
            "readable": readable,
            "withheld_reason": None if readable else access.withheld_reason(column),
        }
        if profile is not None and not profile.access_withheld and readable:
            entry["profile"] = {
                "row_count": profile.row_count,
                "null_count": profile.null_count,
                "null_pct": profile.null_pct,
                "distinct_count": profile.distinct_count,
                "distinct_pct": profile.distinct_pct,
                "min": profile.min_value,
                "max": profile.max_value,
                "mean": profile.mean_value,
                "zero_count": profile.zero_count,
                "negative_count": profile.negative_count,
                "blank_count": profile.blank_count,
                "sample_values": profile.sample_values,
                "is_unique": profile.is_unique,
                "is_candidate_key": profile.is_candidate_key,
                "quality_status": profile.quality_status,
                "warnings": profile.warnings,
            }
        else:
            entry["profile"] = None
        payload.append(entry)
    return payload


def grain_payload(grain: TableGrain | None) -> dict:
    if grain is None:
        return {"detected": False}
    return {
        "detected": True,
        "declared_grain": grain.declared_grain,
        "inferred_grain": grain.inferred_grain,
        "grain_columns": grain.grain_columns,
        "is_unique": grain.is_unique,
        "confidence": grain.confidence,
        "method": grain.method,
        "row_count": grain.row_count,
        "distinct_combinations": grain.distinct_combinations,
        "time_column": grain.time_column,
        "time_grain": grain.time_grain,
        "evidence": grain.evidence,
    }


def freshness_payload(health: SourceHealth | None) -> dict | None:
    if health is None:
        return None
    return {
        "status": health.freshness_status,
        "time_column": health.time_column,
        "lag_seconds": health.freshness_lag_seconds,
        "expected_interval_seconds": health.expected_interval_seconds,
        "coverage_start": health.coverage_start,
        "coverage_end": health.coverage_end,
        "row_count": health.row_count,
        "checked_at": health.checked_at,
        "note": (health.details or {}).get("note"),
    }


def latest_health(session: Session, table: SourceTable) -> SourceHealth | None:
    return session.scalar(
        select(SourceHealth)
        .where(SourceHealth.source_table_id == table.id)
        .order_by(SourceHealth.checked_at.desc())
        .limit(1)
    )


def table_profile_view(session: Session, table: SourceTable, access: AccessContext) -> dict:
    """The stored profile, grain and freshness for one table, access-aware."""
    profile = session.scalar(select(TableProfile).where(TableProfile.source_table_id == table.id))
    grain = session.scalar(select(TableGrain).where(TableGrain.source_table_id == table.id))
    return {
        "source_table_id": table.id,
        "table": table.qualified_name,
        "approx_row_count": table.approx_row_count,
        "profile": (
            {
                "profiled_at": profile.profiled_at,
                "row_count": profile.row_count,
                "completeness_pct": profile.completeness_pct,
                "quality_score": profile.quality_score,
                "quality_status": profile.quality_status,
                "warnings": profile.warnings,
                "profiled_column_count": profile.profiled_column_count,
                "withheld_column_count": profile.withheld_column_count,
                "withheld_columns": profile.withheld_columns,
                "duration_ms": profile.duration_ms,
            }
            if profile
            else None
        ),
        "grain": grain_payload(grain),
        "freshness": freshness_payload(latest_health(session, table)),
        "columns": column_payload(session, table, access),
    }


def relationship_payload(session: Session, access: AccessContext) -> list[dict]:
    tables = {table.id: table for table in scoped_tables(session, access)}
    if not tables:
        return []
    rows = session.execute(
        select(TableRelationship, JoinSafety)
        .outerjoin(JoinSafety, JoinSafety.relationship_id == TableRelationship.id)
        .where(
            TableRelationship.source_table_id.in_(list(tables)),
            TableRelationship.target_table_id.in_(list(tables)),
        )
    ).all()

    payload: list[dict] = []
    for relationship, safety in rows:
        source = tables.get(relationship.source_table_id)
        target = tables.get(relationship.target_table_id)
        payload.append(
            {
                "id": relationship.id,
                "from_table": source.table_name if source else None,
                "from_column": relationship.source_column,
                "to_table": target.table_name if target else None,
                "to_column": relationship.target_column,
                "type": relationship.relationship_type,
                "method": relationship.method,
                "is_declared": relationship.is_declared,
                "confidence": relationship.confidence,
                "orphan_count": relationship.orphan_count,
                "orphan_pct": relationship.orphan_pct,
                "evidence": relationship.evidence,
                "join_safety": (
                    {
                        "level": safety.safety_level,
                        "source_is_unique": safety.source_is_unique,
                        "target_is_unique": safety.target_is_unique,
                        "fan_out_factor": safety.fan_out_factor,
                        "max_fan_out": safety.max_fan_out,
                        "duplicate_key_rate": safety.duplicate_key_rate,
                        "expected_cardinality": safety.expected_cardinality,
                        "observed_cardinality": safety.observed_cardinality,
                        "reason": safety.reason,
                        "guidance": safety.guidance,
                    }
                    if safety
                    else None
                ),
            }
        )
    return sorted(payload, key=lambda item: (item["from_table"] or "", item["from_column"]))


def relationship_summary(relationships: list[dict]) -> dict:
    """Decision-level totals over the deterministic relationship results.

    The per-relationship analysis stays exactly as computed; this only counts it,
    so the business-facing view and the analyst's technical view can never
    disagree about how many relationships need attention.

    A relationship is *material* when it can change a KPI number: anything the
    join-safety analysis did not rate SAFE, plus anything with orphan rows, since
    an inner join silently drops those.
    """
    safe = needs_attention = unsafe = unrated = 0
    material: list[str] = []
    for relationship in relationships:
        safety = relationship.get("join_safety") or {}
        level = safety.get("level")
        has_orphans = bool(relationship.get("orphan_count"))
        if level == JoinSafetyLevel.SAFE:
            safe += 1
        elif level == JoinSafetyLevel.RISKY:
            unsafe += 1
        elif level == JoinSafetyLevel.SAFE_WITH_AGGREGATION:
            needs_attention += 1
        else:
            unrated += 1
        if level != JoinSafetyLevel.SAFE or has_orphans:
            material.append(relationship["id"])

    return {
        "checked": len(relationships),
        "safe": safe,
        "needs_attention": needs_attention,
        "unsafe": unsafe,
        "unrated": unrated,
        "material_relationship_ids": material,
        "material_count": len(material),
    }


def reconciliation_payload(session: Session, access: AccessContext) -> list[dict]:
    tables = {table.id: table for table in scoped_tables(session, access)}
    if not tables:
        return []
    rows = session.scalars(
        select(SourceReconciliation).where(
            SourceReconciliation.left_table_id.in_(list(tables)),
            SourceReconciliation.right_table_id.in_(list(tables)),
        )
    )
    payload = []
    for row in rows:
        left = tables.get(row.left_table_id)
        right = tables.get(row.right_table_id)
        payload.append(
            {
                "left_table": left.table_name if left else None,
                "right_table": right.table_name if right else None,
                "status": row.status,
                "left_grain": row.left_grain,
                "right_grain": row.right_grain,
                "left_time_grain": row.left_time_grain,
                "right_time_grain": row.right_time_grain,
                "shared_dimensions": row.shared_dimensions,
                "unmapped_dimensions": row.unmapped_dimensions,
                "time_overlap_days": row.time_overlap_days,
                "reason": row.reason,
                "guidance": row.guidance,
            }
        )
    return payload
