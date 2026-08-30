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
from app.models.profiling import TableProfile
from app.models.source import DataSource, SourceTable
from app.services import audit
from app.services.analysis_views import (
    column_payload as _column_payload,
)
from app.services.analysis_views import (
    freshness_payload as _freshness_payload,
)
from app.services.analysis_views import (
    grain_payload as _grain_payload,
)
from app.services.analysis_views import (
    latest_health as _latest_health,
)
from app.services.analysis_views import (
    reconciliation_payload as _reconciliation_payload,
)
from app.services.analysis_views import (
    relationship_payload as _relationship_payload,
)
from app.services.analysis_views import (
    relationship_summary as _relationship_summary,
)
from app.services.analysis_views import (
    scoped_tables as _scoped_tables,
)
from app.services.analysis_views import (
    table_profile_view as _table_profile_view,
)
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
    return _table_profile_view(session, table, access)


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
        health = _latest_health(session, table)
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


# The payload shapers this module used to define now live in
# ``app.services.analysis_views`` and are imported above under their original
# names. They moved unchanged: the Copilot's governed tools answer from the same
# code, so an explanation can never disagree with the screen it explains.
