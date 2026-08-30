"""Audit trail, runtime telemetry and the dashboard summary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.clock import as_utc, utcnow
from app.core.deps import AccessContext, SessionDep, require_permissions
from app.llm.config import get_llm_config
from app.models.base import KpiStatus, QualityStatus
from app.models.catalog import CatalogVersion
from app.models.document import CompanyDocument
from app.models.kpi import KpiDefinition, KpiVersion
from app.models.observability import AuditLog, ExecutionLog, SystemEvent
from app.models.profiling import TableProfile
from app.models.source import DataSource, SelectedTable, SourceHealth, SourceTable
from app.schemas import AuditLogOut, ExecutionLogOut, SystemEventOut

router = APIRouter(tags=["observability"])


@router.get("/companies/{company_id}/audit", response_model=list[AuditLogOut])
def list_audit(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = None,
    resource_type: str | None = None,
    access: AccessContext = Depends(require_permissions("audit.read")),
) -> list[AuditLogOut]:
    query = select(AuditLog).where(AuditLog.company_id == access.company.id)
    if action:
        query = query.where(AuditLog.action == action)
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
    rows = session.scalars(
        query.order_by(AuditLog.occurred_at.desc()).limit(limit).offset(offset)
    )
    return [AuditLogOut.model_validate(row) for row in rows]


@router.get("/companies/{company_id}/telemetry", response_model=list[ExecutionLogOut])
def list_telemetry(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    service: str | None = None,
    access: AccessContext = Depends(require_permissions("telemetry.read")),
) -> list[ExecutionLogOut]:
    query = select(ExecutionLog).where(ExecutionLog.company_id == access.company.id)
    if service:
        query = query.where(ExecutionLog.service == service)
    rows = session.scalars(query.order_by(ExecutionLog.started_at.desc()).limit(limit))
    return [ExecutionLogOut.model_validate(row) for row in rows]


@router.get("/companies/{company_id}/telemetry/summary")
def telemetry_summary(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("telemetry.read")),
) -> dict:
    """Latency and connector cost, with the LLM/non-LLM split made explicit.

    Sprint 1 performs zero model calls, and saying so with numbers is more
    convincing than saying so in prose.
    """
    base = select(ExecutionLog).where(ExecutionLog.company_id == access.company.id).subquery()
    totals = session.execute(
        select(
            func.count(base.c.id),
            func.avg(base.c.duration_ms),
            func.max(base.c.duration_ms),
            func.sum(base.c.query_count),
            func.sum(base.c.query_duration_ms),
            func.sum(base.c.rows_returned),
            func.sum(base.c.llm_calls),
            func.sum(base.c.prompt_tokens),
            func.sum(base.c.completion_tokens),
            func.sum(base.c.estimated_cost_usd),
        )
    ).one()

    per_service = session.execute(
        select(
            ExecutionLog.service,
            func.count(ExecutionLog.id),
            func.avg(ExecutionLog.duration_ms),
            func.max(ExecutionLog.duration_ms),
            func.sum(ExecutionLog.query_count),
        )
        .where(ExecutionLog.company_id == access.company.id)
        .group_by(ExecutionLog.service)
        .order_by(func.count(ExecutionLog.id).desc())
    ).all()

    errors = session.scalar(
        select(func.count(ExecutionLog.id)).where(
            ExecutionLog.company_id == access.company.id, ExecutionLog.status == "ERROR"
        )
    )

    def _round(value, digits=1):
        return round(float(value), digits) if value is not None else None

    return {
        "requests": int(totals[0] or 0),
        "errors": int(errors or 0),
        "latency_ms": {"avg": _round(totals[1]), "max": int(totals[2]) if totals[2] else None},
        "connector": {
            "queries": int(totals[3] or 0),
            "query_ms": int(totals[4] or 0),
            "rows_returned": int(totals[5] or 0),
        },
        "llm": {
            "calls": int(totals[6] or 0),
            "prompt_tokens": int(totals[7] or 0),
            "completion_tokens": int(totals[8] or 0),
            "estimated_cost_usd": _round(totals[9], 4) or 0.0,
        },
        "by_service": [
            {
                "service": row[0],
                "requests": int(row[1] or 0),
                "avg_ms": _round(row[2]),
                "max_ms": int(row[3]) if row[3] else None,
                "connector_queries": int(row[4] or 0),
            }
            for row in per_service
        ],
        "processing_split": {
            "deterministic": [
                "KPI calculation (SQL)",
                "aggregation and profiling (SQL pushdown)",
                "grain, relationship and join-safety detection",
                "quality and freshness assessment",
                "KPI validation",
                "governed knowledge retrieval for the Copilot (lexical, no model)",
                "security, lineage, audit and telemetry",
            ],
            # Driven by configuration rather than written down, so this list cannot
            # claim model work on a deployment that has no model -- or deny it on
            # one that does.
            "llm": (
                [
                    "Copilot question understanding and governed tool selection",
                    "explanation of retrieved KPI, validation and document evidence",
                ]
                if get_llm_config().is_available
                else []
            ),
            "note": (
                "Every number in this platform is produced deterministically. A model, "
                "when one is configured, only selects governed read-only tools and "
                "explains the evidence they return; it never calculates a value, "
                "queries tenant data or writes to the platform."
            ),
        },
    }


@router.get("/companies/{company_id}/activity", response_model=list[SystemEventOut])
def list_activity(
    session: SessionDep,
    limit: int = Query(default=25, ge=1, le=200),
    access: AccessContext = Depends(require_permissions("company.read")),
) -> list[SystemEventOut]:
    rows = session.scalars(
        select(SystemEvent)
        .where(SystemEvent.company_id == access.company.id)
        .order_by(SystemEvent.occurred_at.desc())
        .limit(limit)
    )
    return [SystemEventOut.model_validate(row) for row in rows]


@router.get("/companies/{company_id}/dashboard")
def dashboard(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> dict:
    """The Sprint 1 dashboard: system status, KPI registry state, activity.

    Deliberately reports *governance* state, not business performance. There are
    no baselines or movements yet, and showing an invented trend line would
    misrepresent what the platform currently knows.
    """
    company_id = access.company.id

    sources = list(
        session.scalars(select(DataSource).where(DataSource.company_id == company_id))
    )
    connected = sum(1 for source in sources if source.connection_status == "CONNECTED")

    selected_tables = list(
        session.scalars(
            select(SourceTable)
            .join(SelectedTable, SelectedTable.source_table_id == SourceTable.id)
            .where(SourceTable.company_id == company_id, SelectedTable.enabled.is_(True))
        )
    )
    table_ids = [table.id for table in selected_tables]

    profiles = (
        list(session.scalars(select(TableProfile).where(TableProfile.source_table_id.in_(table_ids))))
        if table_ids
        else []
    )
    quality_scores = [p.quality_score for p in profiles if p.quality_score is not None]
    quality_counts = {
        status_value: sum(1 for p in profiles if p.quality_status == status_value)
        for status_value in (QualityStatus.GOOD, QualityStatus.WARNING, QualityStatus.POOR)
    }

    health_rows = (
        list(
            session.scalars(
                select(SourceHealth)
                .where(SourceHealth.source_table_id.in_(table_ids))
                .order_by(SourceHealth.checked_at.asc())
            )
        )
        if table_ids
        else []
    )
    latest_health = {row.source_table_id: row for row in health_rows}
    stale = [row for row in latest_health.values() if row.freshness_status == "STALE"]
    most_recent = max(
        (as_utc(row.coverage_end) for row in latest_health.values() if row.coverage_end),
        default=None,
    )

    definitions = list(
        session.scalars(select(KpiDefinition).where(KpiDefinition.company_id == company_id))
    )
    versions = list(
        session.scalars(select(KpiVersion).where(KpiVersion.company_id == company_id))
    )
    status_counts: dict[str, int] = {}
    for version in versions:
        status_counts[version.status] = status_counts.get(version.status, 0) + 1

    active_versions = [v for v in versions if v.status == KpiStatus.ACTIVE]
    kpi_summary = []
    for version in sorted(active_versions, key=lambda v: v.definition.name):
        kpi_summary.append(
            {
                "kpi_id": version.definition.kpi_key,
                "kpi_version_id": version.id,
                "name": version.definition.name,
                "version": version.version,
                "formula": version.formula_expression,
                "unit": version.unit,
                "currency": version.currency,
                "time_grain": version.time_grain,
                "dimensions": [d.dimension_name for d in version.dimensions if d.allowed],
                # No value here: producing one requires the monitoring engine,
                # which is Sprint 2. The registry state is what Sprint 1 knows.
                "value": None,
                "value_note": "Use the KPI preview endpoint to compute a value for a window.",
            }
        )

    documents = session.scalar(
        select(func.count(CompanyDocument.id)).where(CompanyDocument.company_id == company_id)
    )
    catalog_version = session.scalar(
        select(func.max(CatalogVersion.version)).where(CatalogVersion.company_id == company_id)
    )
    events = list(
        session.scalars(
            select(SystemEvent)
            .where(SystemEvent.company_id == company_id)
            .order_by(SystemEvent.occurred_at.desc())
            .limit(8)
        )
    )

    overall_quality = (
        QualityStatus.UNKNOWN
        if not quality_scores
        else QualityStatus.GOOD
        if sum(quality_scores) / len(quality_scores) >= 95
        else QualityStatus.WARNING
        if sum(quality_scores) / len(quality_scores) >= 80
        else QualityStatus.POOR
    )

    return {
        "company": {
            "id": access.company.id,
            "name": access.company.company_name,
            "status": access.company.status,
            "currency": access.company.currency,
            "timezone": access.company.timezone,
        },
        "system_status": {
            "data_sources": {"total": len(sources), "connected": connected},
            "selected_tables": len(selected_tables),
            "profiled_tables": len(profiles),
            "kpis": {
                "total": len(definitions),
                "active": len(active_versions),
                "by_version_status": status_counts,
            },
            "documents": int(documents or 0),
            "catalog_version": catalog_version,
            "data_quality": {
                "status": overall_quality,
                "avg_score": round(sum(quality_scores) / len(quality_scores), 2)
                if quality_scores
                else None,
                "tables_by_status": quality_counts,
            },
            "freshness": {
                "stale_tables": [
                    session.get(SourceTable, row.source_table_id).qualified_name
                    for row in stale
                    if session.get(SourceTable, row.source_table_id) is not None
                ],
                "last_source_data_at": most_recent,
                "checked_tables": len(latest_health),
            },
            "checked_at": utcnow(),
        },
        "kpi_summary": kpi_summary,
        "recent_activity": [SystemEventOut.model_validate(event).model_dump() for event in events],
        "sprint_scope": {
            "delivered": (
                "Multi-tenant foundation, source registry, access-aware profiling, "
                "grain/relationship/join-safety/freshness/reconciliation metadata, "
                "versioned semantic catalog, document store, governed KPI contracts, "
                "and a company-scoped Copilot that retrieves and explains all of it."
            ),
            "not_yet": (
                "Monitoring, expected-value baselines, anomaly detection, contribution "
                "analysis, automated investigation, narratives over computed KPI "
                "results, and recommendations. The Copilot explains governed "
                "definitions and metadata; it does not assess or forecast values."
            ),
        },
    }
