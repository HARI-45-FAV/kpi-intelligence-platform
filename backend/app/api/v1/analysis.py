"""Profiling, quality, grain, relationships, join safety, freshness and
cross-source reconciliation.

Every endpoint here refuses tables outside the company's approved data scope, and
profiling runs under the caller's own entitlement rather than a service account —
so a column the caller may not read is never queried in the first place.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.connectors.registry import build_connector
from app.core.deps import (
    AccessContext,
    SessionDep,
    load_scoped,
    load_selected_table,
    require_permissions,
)
from app.core.errors import ValidationFailure
from app.core.telemetry import usage_of
from app.models.base import JoinSafetyLevel
from app.models.profiling import (
    ColumnProfile,
    JoinSafety,
    SourceReconciliation,
    TableGrain,
    TableProfile,
    TableRelationship,
)
from app.models.source import DataSource, SelectedTable, SourceHealth, SourceTable
from app.services import audit
from app.services.freshness import check_freshness
from app.services.grain import detect_grain
from app.services.join_safety import analyse_join_safety
from app.services.profiling import profile_table
from app.services.reconciliation import analyse_reconciliation
from app.services.relationships import detect_relationships

router = APIRouter(tags=["analysis"])


# ---------------------------------------------------------------------------
# Connector lifecycle
# ---------------------------------------------------------------------------
@contextmanager
def _connectors_for(
    session: Session, tables: list[SourceTable], request: Request
) -> Iterator[dict[str, DataSourceConnector]]:
    """One connector per distinct data source, closed on the way out."""
    built: dict[str, DataSourceConnector] = {}
    try:
        for source_id in {table.data_source_id for table in tables}:
            source = session.get(DataSource, source_id)
            if source is not None:
                built[source_id] = build_connector(source)
        yield built
    finally:
        usage = usage_of(request)
        for connector in built.values():
            usage.absorb(connector)
            connector.close()


def _scoped_tables(session: Session, access: AccessContext) -> list[SourceTable]:
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


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/tables/{table_id}/profile")
def run_profile(
    table_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> dict:
    table = load_selected_table(session, table_id, access)
    with _connectors_for(session, [table], request) as connectors:
        connector = connectors[table.data_source_id]
        outcome = profile_table(session, table, connector, access)

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.PROFILE_RUN,
        resource_type="source_table",
        resource_id=table.id,
        resource_label=table.qualified_name,
        summary=(
            f"Profiled {outcome.profile.profiled_column_count} column(s); "
            f"{outcome.profile.withheld_column_count} withheld by access policy."
        ),
        details=outcome.as_dict(),
        request=request,
    )
    return {**outcome.as_dict(), "columns": _column_payload(session, table, access)}


@router.get("/companies/{company_id}/tables/{table_id}/profile")
def get_profile(
    table_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> dict:
    table: SourceTable = load_scoped(session, SourceTable, table_id, access)
    profile = session.scalar(select(TableProfile).where(TableProfile.source_table_id == table.id))
    grain = session.scalar(select(TableGrain).where(TableGrain.source_table_id == table.id))
    health = session.scalar(
        select(SourceHealth)
        .where(SourceHealth.source_table_id == table.id)
        .order_by(SourceHealth.checked_at.desc())
        .limit(1)
    )
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
        "grain": _grain_payload(grain),
        "freshness": _freshness_payload(health),
        "columns": _column_payload(session, table, access),
    }


# ---------------------------------------------------------------------------
# Grain
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/tables/{table_id}/grain")
def run_grain_detection(
    table_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> dict:
    table = load_selected_table(session, table_id, access)
    profile = session.scalar(select(TableProfile).where(TableProfile.source_table_id == table.id))
    if profile is None:
        raise ValidationFailure(
            f"{table.qualified_name} must be profiled before grain can be detected — "
            "the scan uses per-column cardinality to pick candidate keys.",
            details={"source_table_id": table.id},
        )

    with _connectors_for(session, [table], request) as connectors:
        grain = detect_grain(session, table, connectors[table.data_source_id])

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.GRAIN_DETECTED,
        resource_type="source_table",
        resource_id=table.id,
        resource_label=table.qualified_name,
        summary=f"Inferred grain: {grain.inferred_grain}",
        details={"method": grain.method, "confidence": grain.confidence, "columns": grain.grain_columns},
        request=request,
    )
    return _grain_payload(grain)


# ---------------------------------------------------------------------------
# Relationships and join safety
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/analysis/relationships")
def run_relationship_detection(
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> dict:
    tables = _scoped_tables(session, access)
    if len(tables) < 2:
        raise ValidationFailure(
            "At least two tables must be in the analytical scope to detect relationships."
        )

    with _connectors_for(session, tables, request) as connectors:
        outcome = detect_relationships(session, tables, connectors)
        session.flush()
        safety = analyse_join_safety(
            session, outcome.relationships, {t.id: t for t in tables}, connectors
        )

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.RELATIONSHIPS_DETECTED,
        resource_type="company",
        resource_id=access.company.id,
        resource_label=access.company.company_name,
        summary=(
            f"{len(outcome.relationships)} relationship(s) detected; "
            f"{safety.risky} rated RISKY."
        ),
        details={**outcome.as_dict(), "join_safety": safety.as_dict()},
        request=request,
    )
    return {
        **outcome.as_dict(),
        "join_safety": safety.as_dict(),
        "relationships": _relationship_payload(session, access),
    }


@router.get("/companies/{company_id}/analysis/relationships")
def list_relationships(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> dict:
    relationships = _relationship_payload(session, access)
    return {
        "relationships": relationships,
        "summary": _relationship_summary(relationships),
    }


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/analysis/freshness")
def run_freshness_check(
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> dict:
    tables = _scoped_tables(session, access)
    if not tables:
        raise ValidationFailure("No tables are in the analytical scope.")

    by_source: dict[str, list[SourceTable]] = {}
    for table in tables:
        by_source.setdefault(table.data_source_id, []).append(table)

    combined: list[dict] = []
    with _connectors_for(session, tables, request) as connectors:
        for source_id, source_tables in by_source.items():
            source = session.get(DataSource, source_id)
            connector = connectors.get(source_id)
            if source is None or connector is None:
                continue
            report = check_freshness(session, source, source_tables, connector)
            combined.extend(report.as_dict()["tables"])

    stale = [item for item in combined if item["status"] == "STALE"]
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.FRESHNESS_CHECKED,
        resource_type="company",
        resource_id=access.company.id,
        resource_label=access.company.company_name,
        summary=f"Checked {len(combined)} table(s); {len(stale)} stale.",
        details={"stale": [item["table"] for item in stale]},
        request=request,
    )
    if stale:
        audit.event(
            session,
            company_id=access.company.id,
            category="FRESHNESS",
            severity="WARNING",
            title="Stale sources detected",
            message=", ".join(item["table"] for item in stale),
        )
    return {
        "checked": len(combined),
        "fresh": sum(1 for item in combined if item["status"] == "FRESH"),
        "stale": len(stale),
        "unknown": sum(1 for item in combined if item["status"] == "UNKNOWN"),
        "tables": combined,
        "note": (
            "Freshness is recorded, never corrected. A stale source stays marked "
            "stale so later confidence scoring can discount evidence built on it."
        ),
    }


@router.get("/companies/{company_id}/analysis/freshness")
def get_freshness(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> dict:
    tables = _scoped_tables(session, access)
    payload = []
    for table in tables:
        health = session.scalar(
            select(SourceHealth)
            .where(SourceHealth.source_table_id == table.id)
            .order_by(SourceHealth.checked_at.desc())
            .limit(1)
        )
        payload.append({"table": table.qualified_name, **(_freshness_payload(health) or {})})
    return {"tables": payload}


# ---------------------------------------------------------------------------
# Cross-source reconciliation
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/analysis/reconciliation")
def run_reconciliation(
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> dict:
    tables = _scoped_tables(session, access)
    outcome = analyse_reconciliation(session, tables)
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.RECONCILIATION_ANALYSED,
        resource_type="company",
        resource_id=access.company.id,
        resource_label=access.company.company_name,
        summary=f"Analysed {outcome.pairs} cross-source pair(s).",
        details=outcome.as_dict(),
        request=request,
    )
    return {
        **outcome.as_dict(),
        "pairs": _reconciliation_payload(session, access),
        "note": (
            "Sprint 1 records whether sources can safely cooperate. It does not "
            "compute multi-source KPIs — that is Sprint 2, which reads this metadata. "
            "Only tables with a declared primary time column participate: a "
            "dimension lookup has no time axis to align on."
        ),
    }


@router.get("/companies/{company_id}/analysis/reconciliation")
def get_reconciliation(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> dict:
    return {"pairs": _reconciliation_payload(session, access)}


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/analysis/run")
def run_full_analysis(
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> dict:
    """Profile, detect grain, relationships, join safety, freshness and
    reconciliation across the whole approved scope.

    Ordered by dependency: profiling supplies the cardinality that grain
    detection needs, grain supplies the time grain that reconciliation needs.
    """
    tables = _scoped_tables(session, access)
    if not tables:
        raise ValidationFailure(
            "No tables are in the analytical scope. Select tables under Data Scope first."
        )

    steps: dict[str, object] = {}
    with _connectors_for(session, tables, request) as connectors:
        profiles = []
        grains = []
        for table in tables:
            connector = connectors.get(table.data_source_id)
            if connector is None:
                continue
            outcome = profile_table(session, table, connector, access)
            profiles.append(outcome.as_dict())
            session.flush()
            grain = detect_grain(session, table, connector)
            grains.append({"table": table.qualified_name, **_grain_payload(grain)})
            session.flush()

        steps["profiling"] = {"tables": profiles}
        steps["grain"] = {"tables": grains}

        relationships = detect_relationships(session, tables, connectors)
        session.flush()
        safety = analyse_join_safety(
            session, relationships.relationships, {t.id: t for t in tables}, connectors
        )
        session.flush()
        steps["relationships"] = relationships.as_dict()
        steps["join_safety"] = safety.as_dict()

        freshness_rows: list[dict] = []
        by_source: dict[str, list[SourceTable]] = {}
        for table in tables:
            by_source.setdefault(table.data_source_id, []).append(table)
        for source_id, source_tables in by_source.items():
            source = session.get(DataSource, source_id)
            connector = connectors.get(source_id)
            if source is None or connector is None:
                continue
            freshness_rows.extend(
                check_freshness(session, source, source_tables, connector).as_dict()["tables"]
            )
        session.flush()
        steps["freshness"] = {
            "checked": len(freshness_rows),
            "stale": sum(1 for row in freshness_rows if row["status"] == "STALE"),
            "tables": freshness_rows,
        }

        reconciliation = analyse_reconciliation(session, tables)
        steps["reconciliation"] = reconciliation.as_dict()

    usage = usage_of(request)
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.PROFILE_RUN,
        resource_type="company",
        resource_id=access.company.id,
        resource_label=access.company.company_name,
        summary=f"Full analysis across {len(tables)} table(s).",
        details={
            "tables": [t.qualified_name for t in tables],
            "connector_queries": usage.query_count,
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="PROFILING",
        title="Data analysis completed",
        message=f"{len(tables)} table(s) profiled and catalogued.",
    )
    return {
        "tables_analysed": len(tables),
        "connector_queries": usage.query_count,
        "connector_query_ms": usage.query_duration_ms,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Payload shaping
# ---------------------------------------------------------------------------
def _column_payload(session: Session, table: SourceTable, access: AccessContext) -> list[dict]:
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


def _grain_payload(grain: TableGrain | None) -> dict:
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


def _freshness_payload(health: SourceHealth | None) -> dict | None:
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


def _relationship_payload(session: Session, access: AccessContext) -> list[dict]:
    tables = {table.id: table for table in _scoped_tables(session, access)}
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


def _relationship_summary(relationships: list[dict]) -> dict:
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


def _reconciliation_payload(session: Session, access: AccessContext) -> list[dict]:
    tables = {table.id: table for table in _scoped_tables(session, access)}
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
