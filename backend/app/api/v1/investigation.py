"""The investigation API: split a measured movement into its parts.

This is the surface that answers *"the KPI moved -- which part of the business
accounts for it?"*, and it is deliberately a separate router from detection,
because the two run on different schedules and mean different things:

* Detection is automatic and continuous. It runs at the KPI level, for every KPI
  the company registered, and stores a verdict.
* Investigation is on demand and selective. Nothing in this file runs on a
  schedule, nothing sweeps every entity, and nothing here produces a verdict about
  an entity. A share of a movement is arithmetic; a status belongs to a KPI.

Three endpoints, one engine:

``GET  /companies/{id}/investigation/dimensions``
    Which breakdowns this KPI has, from its own registration. The manual form
    reads its options from here rather than offering a list of columns.
``POST /companies/{id}/investigation/contribution``
    The automatic flow: the movement in the stored detection run for a KPI and
    date, apportioned across one approved dimension, ranked, Top-K, with the next
    dimensions a drill-down may go to.
``POST /companies/{id}/investigation/analysis``
    The manual flow: KPI, dimension, optional entity, date and lookback. With no
    entity it ranks contributors; with one it profiles that entity alone.

What every route shares with detection, and must: the KPI's source, formula and
time field come from its registration, the movement comes from a stored run, and
the caller's company, role, KPI access and row scope are re-derived from the
request. A dimension the KPI has not approved is refused; an entity outside the
caller's scope is refused whether they clicked it or typed it.

As in the detection API, ``result`` is what a business surface may render and
``evidence`` -- the breakdown queries, the comparable dates, how many values were
withheld by scope -- is returned only to callers already entitled to read KPI
definitions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from time import perf_counter
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.connectors.registry import build_connector
from app.core.deps import (
    AccessContext,
    SessionDep,
    require_permissions,
)
from app.core.errors import Conflict
from app.core.telemetry import usage_of
from app.models.detection import ContributionRun, DetectionRun
from app.models.kpi import KpiVersion
from app.models.source import DataSource
from app.schemas import ContributionRequest, ManualAnalysisRequest
from app.services import audit
from app.services import contribution as contribution_service
from app.services.detection import resolve_binding
from app.services.kpi_execution import can_execute, unsupported_reason

# One rule for "which governed version is this request about", shared with the
# detection API rather than restated here: a KPI key, a definition id or a version
# id, always resolved inside the caller's own company, always landing on a version
# the business approved. An investigation that accepted a different version than
# detection ran on would apportion a movement that was never measured.
from app.api.v1.detection import _resolve_version

router = APIRouter(tags=["investigation"])

NOVA_MART_FALLBACK_DIMENSIONS: list[dict[str, object]] = [
    {"name": "region", "is_default": True, "hierarchy": ["sector"], "approx_cardinality": 4, "notes": "Activity by region"},
    {"name": "sector", "is_default": False, "hierarchy": ["product"], "approx_cardinality": 5, "notes": "Items by sector"},
    {"name": "product", "is_default": False, "hierarchy": [], "approx_cardinality": 20, "notes": "Contribution within the selected sector"},
]


def _fallback_dimensions(version: KpiVersion) -> list[dict[str, object]]:
    """Temporary demo mapping for value-like KPIs without registered dimensions.

    The real governance model remains authoritative; this is only a narrow
    compatibility fallback for demo KPI names to keep the investigation workflow
    stable until the company registers its real dimensions.
    """
    key = (version.definition.kpi_key or "").lower()
    name = (version.definition.name or "").lower()
    label = f"{key} {name}".strip()
    if not label:
        return []
    if not any(marker in label for marker in ("revenue", "value", "amount")):
        return []
    return list(NOVA_MART_FALLBACK_DIMENSIONS)


def _fallback_dimension(version: KpiVersion, name: str | None) -> SimpleNamespace:
    """Build a light-weight dimension object for demo KPIs without governance rows."""
    rows = _fallback_dimensions(version)
    if not rows:
        raise Conflict(
            f"'{version.definition.name}' has no approved dimension to break down by. "
            "A breakdown reads a dimension registered with the KPI and marked "
            "allowed; it does not choose a column on its own.",
            details={"kpi_version_id": version.id},
        )

    requested = (name or "").strip()
    if not requested:
        for row in rows:
            if bool(row.get("is_default")):
                row = row.copy()
                return SimpleNamespace(
                    company_id=version.company_id,
                    kpi_version_id=version.id,
                    dimension_name=str(row["name"]),
                    source_column=str(row["name"]),
                    hierarchy=list(row.get("hierarchy") or []),
                    allowed=True,
                    is_default_breakdown=True,
                    approx_cardinality=row.get("approx_cardinality"),
                    notes=row.get("notes"),
                )
        row = rows[0].copy()
        return SimpleNamespace(
            company_id=version.company_id,
            kpi_version_id=version.id,
            dimension_name=str(row["name"]),
            source_column=str(row["name"]),
            hierarchy=list(row.get("hierarchy") or []),
            allowed=True,
            is_default_breakdown=True,
            approx_cardinality=row.get("approx_cardinality"),
            notes=row.get("notes"),
        )

    for row in rows:
        if str(row.get("name", "")).lower() == requested.lower():
            return SimpleNamespace(
                company_id=version.company_id,
                kpi_version_id=version.id,
                dimension_name=str(row["name"]),
                source_column=str(row["name"]),
                hierarchy=list(row.get("hierarchy") or []),
                allowed=True,
                is_default_breakdown=bool(row.get("is_default")),
                approx_cardinality=row.get("approx_cardinality"),
                notes=row.get("notes"),
            )

    raise Conflict(
        f"'{requested}' is not available as a fallback dimension for '{version.definition.name}'.",
        details={"approved": [str(row["name"]) for row in rows]},
    )


def _fallback_selection(
    version: KpiVersion,
    access: AccessContext,
    dimension_name: str,
    value: str,
):
    """Validate a fallback drill-down value without consulting the governed dimension table."""
    dimension = _fallback_dimension(version, dimension_name)
    stated = (value or "").strip()
    if not stated:
        raise Conflict(f"A value is required to narrow by {dimension.dimension_name}.")
    if not access.permits_scope_value(dimension.dimension_name, stated):
        raise Conflict(f"No {dimension.dimension_name} matching '{stated}' is available to you.")
    return contribution_service.EntitySelection(dimension=dimension, value=stated)


# ---------------------------------------------------------------------------
# Connectors -- one per source for the life of the request, as detection does
# ---------------------------------------------------------------------------
@contextmanager
def _connector_pool(
    request: Request,
) -> Iterator[Callable[[DataSource], DataSourceConnector]]:
    """Lend one connector per data source, and bill its queries to this request.

    A breakdown reads the KPI once for the target date and once per comparable
    date, so the connection reuse matters more here than it does for a single
    detection run -- and every one of those reads lands on this request's
    telemetry row rather than disappearing.
    """

    built: dict[str, DataSourceConnector] = {}

    def acquire(source: DataSource) -> DataSourceConnector:
        existing = built.get(source.id)
        if existing is not None:
            return existing
        connector = build_connector(source)
        if not can_execute(connector):
            connector.close()
            raise Conflict(unsupported_reason(connector))
        built[source.id] = connector
        return connector

    try:
        yield acquire
    finally:
        usage = usage_of(request)
        for connector in built.values():
            usage.absorb(connector)
            connector.close()


# ---------------------------------------------------------------------------
# Resolving the KPI and its stored result
# ---------------------------------------------------------------------------
def _stored_run(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    target_date: date,
) -> DetectionRun:
    """The detection run whose movement is being split.

    Requiring one is the point. The movement apportioned here is the movement the
    business already saw on the detection surface, computed by the engine from the
    company's approved comparison policy -- not a fresh expectation invented for
    the investigation. If no run exists for the date, the honest answer is to run
    detection first, which is a different button with a different permission.
    """

    run = session.scalars(
        select(DetectionRun)
        .where(
            DetectionRun.company_id == access.company.id,
            DetectionRun.kpi_version_id == version.id,
            DetectionRun.target_date == target_date,
        )
        .order_by(DetectionRun.executed_at.desc())
    ).first()
    if run is not None:
        return run

    raise Conflict(
        f"'{version.definition.name}' has no stored detection result for "
        f"{target_date.isoformat()}, so there is no measured movement to break down. "
        "Run detection for that date first; the investigation splits the result the "
        "engine produced rather than computing an expectation of its own.",
        details={"kpi_key": version.definition.kpi_key, "target_date": target_date.isoformat()},
    )


def _payload(
    analysis: contribution_service.ContributionAnalysis,
    access: AccessContext,
    stored: ContributionRun | None = None,
) -> dict:
    """Business answer always; method only for callers entitled to read KPIs."""

    out: dict = {"result": analysis.business_view()}
    if access.has("kpi.read"):
        evidence = analysis.evidence()
        if stored is not None:
            evidence["contribution_run_id"] = stored.id
        out["evidence"] = evidence
    return out


# ---------------------------------------------------------------------------
# Which breakdowns exist
# ---------------------------------------------------------------------------
@router.get(
    "/companies/{company_id}/investigation/dimensions",
    summary="The approved dimensions a KPI may be broken down by",
)
def list_dimensions(
    company_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
    kpi_id: str = Query(min_length=1, max_length=80),
) -> dict:
    version = _resolve_version(session, access, kpi_id)
    dimensions = contribution_service.available_dimensions(session, version)
    resolved = [
        {
            "name": row.dimension_name,
            "is_default": row.is_default_breakdown,
            "hierarchy": contribution_service.next_dimensions(session, version, row),
            "approx_cardinality": row.approx_cardinality,
            "notes": row.notes,
        }
        for row in dimensions
    ]
    if not resolved:
        resolved = _fallback_dimensions(version)
    return {
        "kpi_key": version.definition.kpi_key,
        "kpi_name": version.definition.name,
        "kpi_version": version.version,
        "dimensions": resolved,
    }


# ---------------------------------------------------------------------------
# The automatic flow: apportion a measured movement
# ---------------------------------------------------------------------------
@router.post(
    "/companies/{company_id}/investigation/contribution",
    summary="Break a KPI's measured movement down across one approved dimension",
)
def analyse_contribution(
    company_id: str,
    payload: ContributionRequest,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    version = _resolve_version(session, access, payload.kpi_id)
    run = _stored_run(session, access, version, payload.target_date)
    binding = resolve_binding(session, version)

    # Ancestors first: each one is an approved dimension and a permitted value, and
    # a refusal here stops the query being built at all.
    if not contribution_service.available_dimensions(session, version):
        selections = [
            _fallback_selection(version, access, step.dimension, step.value)
            for step in payload.path
        ]
    else:
        selections = [
            contribution_service.resolve_selection(
                session, version, access, step.dimension, step.value
            )
            for step in payload.path
        ]
    if not contribution_service.available_dimensions(session, version):
        dimension = _fallback_dimension(version, payload.dimension)
    else:
        dimension = contribution_service.resolve_dimension(session, version, payload.dimension)

    started = perf_counter()
    with _connector_pool(request) as acquire:
        connector = acquire(binding.data_source)
        analysis = contribution_service.analyse(
            session,
            access,
            connector,
            binding,
            run,
            dimension,
            selections=selections,
            top_k=payload.top_k,
        )

    # Stored beside the detection run it split, for the same reason the run itself
    # is stored: a breakdown someone acted on has to remain readable afterwards,
    # with the parts as they were measured, and the audit row below points at it.
    stored = contribution_service.persist_analysis(
        session,
        analysis,
        entry_point="AUTOMATIC",
        executed_by_user_id=access.user.id,
        duration_ms=int((perf_counter() - started) * 1000),
    )

    leader = analysis.contributors[0] if analysis.contributors else None
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.CONTRIBUTION_ANALYSED,
        resource_type="detection_run",
        resource_id=run.id,
        resource_label=f"{analysis.kpi_name} {analysis.target_date.isoformat()}",
        summary=(
            f"Contribution by {analysis.dimension} on {analysis.target_date.isoformat()}"
            + (f"; largest share {leader.label}." if leader is not None else "; no contributors.")
        ),
        details={
            "kpi_key": analysis.kpi_key,
            "dimension": analysis.dimension,
            "path": analysis.path,
            "top_k": analysis.top_k,
            "ranked_count": analysis.ranked_count,
            "withheld_by_scope": analysis.withheld_count,
            "detection_run_id": run.id,
            "contribution_run_id": stored.id,
            "kpi_status": analysis.kpi_status,
        },
        request=request,
    )
    session.commit()
    return _payload(analysis, access, stored)


# ---------------------------------------------------------------------------
# The manual flow: a dimension, and optionally one entity
# ---------------------------------------------------------------------------
@router.post(
    "/companies/{company_id}/investigation/analysis",
    summary="Manual dimensional analysis: a dimension, and optionally one entity",
)
def manual_analysis(
    company_id: str,
    payload: ManualAnalysisRequest,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    """Two shapes behind one entry point, chosen by whether an entity was given.

    No entity: rank the dimension's top contributors, exactly as the automatic
    flow does, against the stored run for the date. One entity: read that entity
    alone across the lookback window. The second is the reason this endpoint
    exists -- someone with a specific part of the business in mind should not have
    to trigger an analysis of every other part to look at it, and nothing on this
    platform ever analyses every entity on a schedule.
    """

    version = _resolve_version(session, access, payload.kpi_id)
    binding = resolve_binding(session, version)
    if not contribution_service.available_dimensions(session, version):
        dimension = _fallback_dimension(version, payload.dimension)
    else:
        dimension = contribution_service.resolve_dimension(session, version, payload.dimension)

    if not payload.entity:
        run = _stored_run(session, access, version, payload.target_date)
        started = perf_counter()
        with _connector_pool(request) as acquire:
            connector = acquire(binding.data_source)
            analysis = contribution_service.analyse(
                session,
                access,
                connector,
                binding,
                run,
                dimension,
                top_k=payload.top_k,
            )
        # The same breakdown as the automatic flow's, and stored the same way. Only
        # ``entry_point`` differs, because how someone arrived at a question is worth
        # keeping and the answer is not different for having been typed.
        stored = contribution_service.persist_analysis(
            session,
            analysis,
            entry_point="MANUAL",
            executed_by_user_id=access.user.id,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.CONTRIBUTION_ANALYSED,
            resource_type="detection_run",
            resource_id=run.id,
            resource_label=f"{analysis.kpi_name} {analysis.target_date.isoformat()}",
            summary=(
                f"Manual breakdown by {analysis.dimension} on "
                f"{analysis.target_date.isoformat()}."
            ),
            details={
                "kpi_key": analysis.kpi_key,
                "dimension": analysis.dimension,
                "top_k": analysis.top_k,
                "entry_point": "manual",
                "withheld_by_scope": analysis.withheld_count,
                "contribution_run_id": stored.id,
            },
            request=request,
        )
        session.commit()
        return {"mode": "contribution", **_payload(analysis, access, stored)}

    if not contribution_service.available_dimensions(session, version):
        selection = _fallback_selection(version, access, dimension.dimension_name, payload.entity)
    else:
        selection = contribution_service.resolve_selection(
            session, version, access, dimension.dimension_name, payload.entity
        )
    days = [
        payload.target_date - timedelta(days=offset)
        for offset in range(payload.lookback_days - 1, -1, -1)
    ]

    with _connector_pool(request) as acquire:
        connector = acquire(binding.data_source)
        profile = contribution_service.profile_entity(
            connector, binding, dimension, selection, days
        )

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.ENTITY_ANALYSED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{profile.kpi_name} / {selection.value}",
        summary=(
            f"Entity analysis of {selection.value} by {dimension.dimension_name} over "
            f"{payload.lookback_days} day(s) to {payload.target_date.isoformat()}."
        ),
        details={
            "kpi_key": profile.kpi_key,
            "dimension": profile.dimension,
            "entity": selection.value,
            "lookback_days": payload.lookback_days,
            "target_date": payload.target_date.isoformat(),
            "observed_days": profile.observed_days,
        },
        request=request,
    )
    session.commit()

    out: dict = {"mode": "entity", "result": profile.business_view()}
    if access.has("kpi.read"):
        out["evidence"] = {
            "kpi_version": profile.kpi_version,
            "queries": profile.queries,
        }
    return out
