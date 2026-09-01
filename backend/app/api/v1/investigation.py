"""The investigation API: split a measured movement into its parts.

This is the surface that answers *"the KPI moved -- which part of the business
accounts for it?"*, and it is deliberately a separate router from detection,
because the two run on different schedules and mean different things:

* Detection is automatic and continuous. It runs at the KPI level, for every KPI
  the company registered, and stores a verdict.
* Investigation is on demand and selective. Nothing in this file runs on a
  schedule and nothing sweeps every entity. A share of a movement is arithmetic
  and never a verdict: a part accounting for most of a movement is where the
  movement happened, not something wrong. One named entity *is* judged -- but only
  when a person asks for it by name, by the same engine that judges the KPI, and
  the result is neither persisted nor read by any other surface.

Two modes, kept apart on purpose, and two shared reads in front of them:

``GET  /companies/{id}/investigation/dimensions``
    Which breakdowns this KPI has, from its own registration. The manual form
    reads its options from here rather than offering a list of columns.
``GET  /companies/{id}/investigation/entities``
    Whether a KPI and date can be investigated at all -- decided by whether the
    agent run for that date recorded a result -- and, when they can, the
    dimension's largest values read from the source. This is what makes choosing
    an entity a selection rather than a typing exercise, and it is also the gate:
    a date with no run returns no entities and reads nothing.
``POST /companies/{id}/investigation/contribution``
    **Mode 1 -- from a movement.** The movement the agent run recorded for a KPI
    and date, apportioned across one approved dimension, ranked, Top-K, with the
    next dimension the KPI's own hierarchy allows a drill-down to go to. Region ->
    Sector -> Product is a company's declared hierarchy, not this file's.
``POST /companies/{id}/investigation/analysis``
    **Mode 2 -- manual analysis.** KPI, date, dimension, and the entity a person
    chose. Not root-cause descent: one named part of the business is read on its
    own and judged by the detection engine, and nothing else is read. (The same
    endpoint still ranks a dimension's contributors when no entity is named, which
    is the manual way to *find* the entity worth naming.)

The two modes never share an execution path. Mode 1 descends a hierarchy from a
recorded movement; mode 2 answers about one entity somebody chose. What they do
share is the engine underneath -- the KPI's registered source and formula, the
approved comparison policy that decides which history an expectation rests on, and
one detection engine for every verdict -- because two engines would be two answers.

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

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.connectors.registry import build_connector
from app.copilot import explain as explain_service
from app.core.clock import utcnow
from app.core.deps import (
    AccessContext,
    SessionDep,
    load_scoped,
    require_permissions,
)
from app.core.errors import Conflict, ValidationFailure
from app.core.telemetry import llm_usage_of, usage_of
from app.models.base import FINDING_TRANSITIONS, FindingStatus
from app.models.detection import AgentRun, ContributionRun, DetectionRun
from app.models.investigation import InvestigationFinding
from app.models.kpi import KpiVersion
from app.models.source import DataSource
from app.schemas import (
    ContributionRequest,
    FindingCreate,
    FindingUpdate,
    ManualAnalysisRequest,
    NodeExplainRequest,
)
from app.services import audit
from app.services import contribution as contribution_service
from app.services.detection import KpiBinding, resolve_binding
from app.services.kpi_execution import can_execute, unsupported_reason

# One rule for "which governed version is this request about", shared with the
# detection API rather than restated here: a KPI key, a definition id or a version
# id, always resolved inside the caller's own company, always landing on a version
# the business approved. An investigation that accepted a different version than
# detection ran on would apportion a movement that was never measured.
from app.api.v1.detection import _resolve_version

router = APIRouter(tags=["investigation"])

#: What a business reader is told when the date they picked was never analysed.
#: Phrased as an instruction rather than an error, because it is one: the movement
#: an investigation splits is the movement the agent run measured, so there is
#: nothing to split until that run has happened.
NO_RUN_MESSAGE = (
    "Agent run not available for this date — investigation cannot be performed. "
    "Run the KPI analysis first, then this movement can be investigated."
)


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
def _find_run(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    target_date: date,
) -> DetectionRun | None:
    """The most recent stored detection run for this KPI and date, if there is one."""

    return session.scalars(
        select(DetectionRun)
        .where(
            DetectionRun.company_id == access.company.id,
            DetectionRun.kpi_version_id == version.id,
            DetectionRun.target_date == target_date,
        )
        .order_by(DetectionRun.executed_at.desc())
    ).first()


def _run_state(session: Session, run: DetectionRun | None) -> str | None:
    """Whether the agent run that produced this result finished.

    A result reached by a batch agent run carries that run's state; one produced by
    a direct request is complete by the fact of existing, because it is written
    once the engine has a number. Reported so a reader can see *why* an
    investigation is offered, rather than being told to trust that it is.
    """

    if run is None:
        return None
    if not run.agent_run_id:
        return "COMPLETED"
    sweep = session.get(AgentRun, run.agent_run_id)
    return "COMPLETED" if sweep is None else sweep.status


def _stored_run(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    target_date: date,
) -> DetectionRun:
    """The one gate both modes pass through: was this date analysed at all?

    Requiring a recorded run is the point, and it is the same requirement for a
    guided drill-down and for a single named entity -- otherwise the strictest path
    on the screen would be reachable by typing around the loosest. The movement
    being investigated is the movement the business already saw on the detection
    surface, computed by the engine from the company's approved comparison policy --
    not a fresh expectation invented for the investigation. With no run for the
    date, the honest answer is to run the analysis first, which is a different
    button with a different permission.
    """

    run = _find_run(session, access, version, target_date)
    if run is not None:
        return run

    raise Conflict(
        f"'{version.definition.name}' has no stored detection result for "
        f"{target_date.isoformat()}, so there is no measured movement to break down. "
        + NO_RUN_MESSAGE,
        details={
            "kpi_key": version.definition.kpi_key,
            "target_date": target_date.isoformat(),
            "run_available": False,
            "message": NO_RUN_MESSAGE,
        },
    )


def _payload(
    analysis: contribution_service.ContributionAnalysis,
    access: AccessContext,
    stored: ContributionRun | None = None,
    *,
    run_state: str | None = None,
) -> dict:
    """Business answer always; method only for callers entitled to read KPIs."""

    result = analysis.business_view()
    if run_state:
        result["run_state"] = run_state
    out: dict = {"result": result}
    if access.has("kpi.read"):
        evidence = analysis.evidence()
        if stored is not None:
            evidence["contribution_run_id"] = stored.id
        out["evidence"] = evidence
    return out


def _dimension_view(session: Session, version: KpiVersion) -> list[dict]:
    """The breakdowns this KPI offers, each with where a drill-down may go next."""

    return [
        {
            "name": row.dimension_name,
            "is_default": row.is_default_breakdown,
            "hierarchy": contribution_service.next_dimensions(session, version, row),
            "approx_cardinality": row.approx_cardinality,
            "notes": row.notes,
        }
        for row in contribution_service.available_dimensions(session, version)
    ]


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
    return {
        "kpi_key": version.definition.kpi_key,
        "kpi_name": version.definition.name,
        "kpi_version": version.version,
        "dimensions": _dimension_view(session, version),
    }


# ---------------------------------------------------------------------------
# Whether this date can be investigated at all, and what is in the dimension
# ---------------------------------------------------------------------------
@router.get(
    "/companies/{company_id}/investigation/entities",
    summary="Whether a date can be investigated, and the dimension's largest values",
)
def list_entities(
    company_id: str,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
    kpi_id: str = Query(min_length=1, max_length=80),
    target_date: date = Query(),
    dimension: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Answer two questions the investigation surface has to ask before it opens.

    *Can this date be investigated?* -- decided solely by whether a detection run
    was stored for it. A date with no run returns ``run_available: false`` and an
    empty list, and reads nothing from the company's source: computing a breakdown
    for an unanalysed date would put a number on screen that the platform never
    measured and no one could reproduce.

    *Which parts of the business are worth choosing?* -- read from the source, per
    KPI, per date, ranked by size and filtered to what the caller may see. Nothing
    is enumerated in code, so the list follows the data rather than the other way
    round. Sizes are not verdicts: none of these entities has been analysed, and
    that is exactly what the ``Investigate`` action on each one is for.
    """

    version = _resolve_version(session, access, kpi_id)
    run = _find_run(session, access, version, target_date)

    out: dict = {
        "kpi_key": version.definition.kpi_key,
        "kpi_name": version.definition.name,
        "kpi_version": version.version,
        "target_date": target_date.isoformat(),
        "run_available": run is not None,
        "run_state": _run_state(session, run),
        "kpi_status": None if run is None else run.status,
        "message": None if run is not None else NO_RUN_MESSAGE,
        "dimensions": _dimension_view(session, version),
        "dimension": None,
        "next_dimensions": [],
        "entities": [],
    }
    if run is None:
        return out

    chosen = contribution_service.resolve_dimension(session, version, dimension)
    out["dimension"] = chosen.dimension_name
    out["next_dimensions"] = contribution_service.next_dimensions(session, version, chosen)

    binding = resolve_binding(session, version)
    with _connector_pool(request) as acquire:
        connector = acquire(binding.data_source)
        out["entities"] = contribution_service.top_entities(
            connector, access, binding, chosen, run.target_date, limit=limit
        )
    return out


# ---------------------------------------------------------------------------
# Mode 1: from a movement -- the guided descent down the KPI's own hierarchy
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
    """Root-cause descent: one recorded movement, one level at a time.

    Nothing here is chosen by the caller except *where in the hierarchy they are*.
    The movement comes from the recorded run, the dimension defaults to the KPI's
    own, the next level comes from that dimension's declared hierarchy, and every
    ancestor in ``path`` is re-checked for approval and entitlement before a query
    is built. This is the path an ABNORMAL verdict leads to, and it is the only
    path on this surface that descends.
    """

    version = _resolve_version(session, access, payload.kpi_id)
    run = _stored_run(session, access, version, payload.target_date)
    binding = resolve_binding(session, version)

    # Ancestors first: each one is an approved dimension and a permitted value, and
    # a refusal here stops the query being built at all.
    selections = [
        contribution_service.resolve_selection(
            session, version, access, step.dimension, step.value
        )
        for step in payload.path
    ]
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
    return _payload(analysis, access, stored, run_state=_run_state(session, run))


# ---------------------------------------------------------------------------
# Mode 2: manual analysis -- one part of the business, chosen and read on its own
# ---------------------------------------------------------------------------
def _entity_analysis(
    payload: ManualAnalysisRequest,
    request: Request,
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    binding: KpiBinding,
    dimension: contribution_service.Dimension,
    run: DetectionRun,
) -> dict:
    """Read one named entity across its own window and let the engine judge it.

    This is manual analysis proper, and it descends nothing: a person named a
    dimension and a value, so exactly that value is read -- one query per day
    through the KPI's own formula, narrowed by a governed filter -- and exactly
    that value is classified. Nothing on this platform analyses every entity, on a
    schedule or otherwise, and an entity is judged only because somebody asked.
    """

    selection = contribution_service.resolve_selection(
        session, version, access, dimension.dimension_name, payload.entity or ""
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
        # An entity the source never matched is not an analysis with empty figures;
        # it is a selection that does not exist here. Said plainly rather than
        # rendered as a row of dashes a reader would have to interpret.
        touched = any(
            point["value"] is not None or (point.get("matched_rows") or 0) > 0
            for point in profile.points
        )
        if not touched:
            raise Conflict(
                f"'{selection.value}' has no recorded {dimension.dimension_name} "
                f"activity for {binding.name} in the {payload.lookback_days} day(s) to "
                f"{payload.target_date.isoformat()}, so there is nothing to analyse. "
                "Choose one of the values measured on this date.",
                details={
                    "kpi_key": profile.kpi_key,
                    "dimension": dimension.dimension_name,
                    "entity": selection.value,
                    "target_date": payload.target_date.isoformat(),
                    "entity_available": False,
                },
            )
        # Entity-level detection, for this one entity, because it was asked for.
        # The engine decides the verdict against the company's approved comparison
        # policy; nothing here does.
        profile = contribution_service.classify_entity(
            session,
            connector,
            binding,
            dimension,
            selection,
            payload.target_date,
            profile=profile,
            kpi_actual=run.actual_value,
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
            # The verdict is audited because it is a judgement about a named part of
            # the business, and because "who asked, and what were they told" is the
            # question an audit trail exists to answer.
            "entity_status": profile.status,
        },
        request=request,
    )
    session.commit()

    out: dict = {"mode": "entity", "result": profile.business_view()}
    if access.has("kpi.read"):
        out["evidence"] = {
            "kpi_version": profile.kpi_version,
            "queries": profile.queries,
            "comparison_label": profile.comparison_label,
            "reference_dates": profile.reference_dates,
        }
    return out


def _dimension_ranking(
    payload: ManualAnalysisRequest,
    request: Request,
    session: Session,
    access: AccessContext,
    binding: KpiBinding,
    dimension: contribution_service.Dimension,
    run: DetectionRun,
) -> dict:
    """Rank a dimension's contributors without descending into any of them.

    The manual way to *find* the value worth naming: same engine, same recorded
    movement, no hierarchy and no drill path -- so what comes back is a list of
    sizes for one dimension the caller picked, and choosing from it is a separate
    request about a single entity.
    """

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
    # Stored the same way the movement mode stores its breakdown. Only
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
    return {
        "mode": "contribution",
        **_payload(analysis, access, stored, run_state=_run_state(session, run)),
    }


@router.post(
    "/companies/{company_id}/investigation/analysis",
    summary="Manual analysis: a dimension the caller picked, and the entity they chose",
)
def manual_analysis(
    company_id: str,
    payload: ManualAnalysisRequest,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    """Validate the selection, then hand it to whichever path it named.

    Everything a manual request can get wrong is checked here, once, before any
    query is built and in the order that gives the most useful refusal: the KPI
    must resolve inside the caller's company, the dimension must be one the KPI
    approved, the date must be one the agent run analysed, and -- in
    :func:`_entity_analysis` -- the entity must be inside the caller's row scope
    and actually present in the source. The two paths below share this preamble and
    nothing else, and neither of them is the movement mode: no hierarchy is walked
    and no drill path is accepted here.
    """

    version = _resolve_version(session, access, payload.kpi_id)
    dimension = contribution_service.resolve_dimension(session, version, payload.dimension)
    if payload.entity is not None and not payload.entity.strip():
        raise ValidationFailure(
            f"Choose a {dimension.dimension_name} to analyse, or clear the selection to "
            "rank the whole dimension instead."
        )
    binding = resolve_binding(session, version)
    # The gate, before either path and before anything is read.
    run = _stored_run(session, access, version, payload.target_date)

    if payload.entity:
        return _entity_analysis(
            payload, request, session, access, version, binding, dimension, run
        )
    return _dimension_ranking(payload, request, session, access, binding, dimension, run)


# ---------------------------------------------------------------------------
# Findings: the human conclusion, stored beside the measurement
# ---------------------------------------------------------------------------
# Gated on ``investigation.read`` rather than a new write permission, and that is a
# deliberate decision worth stating. An ``investigation.write`` permission would
# draw a line between reading a breakdown and annotating one, but nothing on this
# platform grants the first without intending the second: every role that may
# investigate is a role whose conclusion the company wants recorded, and VIEWER --
# the one role that should not be writing findings -- does not hold
# ``investigation.read`` at all. The existing gate already draws the line in the
# right place, and a second permission would only create a state where somebody can
# see a movement's parts with no way to say what they concluded.
#
# What is *not* relaxed: a finding is company-scoped on read and on write, its KPI
# is re-resolved inside the caller's own company, and its dimension and entity pass
# the same approval and row-scope checks the analysis routes apply.
_FINDING_STATUSES = tuple(str(status) for status in FindingStatus)


def _finding_out(finding: InvestigationFinding) -> dict:
    """One finding, as the API returns it.

    ``scope_label`` is computed rather than stored so a list mixing root-level and
    drilled-in notes reads consistently, and no timestamp is synthesised: a null
    ``resolved_at`` means this finding has never been resolved, which the
    investigation surface is entitled to show as absence.
    """

    return {
        "id": finding.id,
        "kpi_key": finding.kpi_key,
        "kpi_name": finding.kpi_name,
        "target_date": finding.target_date,
        "title": finding.title,
        "note": finding.note,
        "status": finding.status,
        "dimension": finding.dimension,
        "entity": finding.entity,
        "path": list(finding.path or []),
        "scope_label": finding.scope_label(),
        "detection_run_id": finding.detection_run_id,
        "created_by_email": finding.created_by_email,
        "updated_by_email": finding.updated_by_email,
        "created_at": finding.created_at,
        "updated_at": finding.updated_at,
        "resolved_at": finding.resolved_at,
    }


def _validated_status(value: str | None, *, current: str | None = None) -> str:
    """Accept only a status this platform defines, and only a defined transition.

    Every transition between the three is allowed -- an investigation that was
    resolved and is reopened is a normal thing to happen, and refusing it would
    push people into writing a second note contradicting the first. What is refused
    is a status that is not one of the three, because a finding carrying an
    unrecognised state is one no screen can render honestly.
    """

    if value is None:
        return current or str(FindingStatus.OPEN)
    wanted = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    if wanted not in _FINDING_STATUSES:
        raise ValidationFailure(
            f"'{value}' is not an investigation status. Use one of: "
            + ", ".join(_FINDING_STATUSES)
            + ". This is the state of the investigation, not a verdict about the KPI."
        )
    if current is not None:
        allowed = {str(target) for target in FINDING_TRANSITIONS[FindingStatus(current)]}
        allowed.add(current)
        if wanted not in allowed:
            raise Conflict(f"An investigation cannot move from {current} to {wanted}.")
    return wanted


def _validated_anchor(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    dimension_name: str | None,
    entity: str | None,
    path: list,
) -> tuple[str | None, str | None, list[dict[str, str]]]:
    """Check a finding's anchor as strictly as an analysis of the same anchor.

    A note is not a query, so nothing here reads business data -- but a note naming
    a dimension the KPI was never approved to be split by, or an entity outside the
    caller's row scope, would put an unauthorised coordinate into a record other
    people read. So the anchor goes through ``resolve_selection``, the same
    validator the drill-down uses: approved dimension first, permitted value
    second.
    """

    steps: list[dict[str, str]] = []
    for step in path or []:
        selection = contribution_service.resolve_selection(
            session, version, access, step.dimension, step.value
        )
        steps.append(
            {"dimension": selection.dimension.dimension_name, "value": selection.value}
        )

    if dimension_name is None:
        if entity is not None:
            raise ValidationFailure(
                "An entity was named without the dimension it belongs to. A finding "
                "about one part of the business needs both; a finding about the whole "
                "movement needs neither."
            )
        return None, None, steps

    if entity is None:
        dimension = contribution_service.resolve_dimension(session, version, dimension_name)
        return dimension.dimension_name, None, steps

    selection = contribution_service.resolve_selection(
        session, version, access, dimension_name, entity
    )
    return selection.dimension.dimension_name, selection.value, steps


@router.get(
    "/companies/{company_id}/investigation/findings",
    summary="Findings recorded against a KPI, a date, or this company",
)
def list_findings(
    company_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
    kpi_id: str | None = Query(default=None, max_length=80),
    target_date: date | None = Query(default=None),
    status: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Read findings, narrowed by whichever filters the caller supplied.

    Company scope is not one of those filters -- it comes unconditionally from the
    resolved access context, never from the path parameter, so no caller can widen
    the read by asking with another company's id.
    """

    stmt = select(InvestigationFinding).where(
        InvestigationFinding.company_id == access.company.id
    )
    kpi_key = None
    if kpi_id:
        version = _resolve_version(session, access, kpi_id)
        kpi_key = version.definition.kpi_key
        stmt = stmt.where(InvestigationFinding.kpi_key == kpi_key)
    if target_date is not None:
        stmt = stmt.where(InvestigationFinding.target_date == target_date)
    if status:
        stmt = stmt.where(InvestigationFinding.status == _validated_status(status))

    rows = list(
        session.scalars(stmt.order_by(InvestigationFinding.created_at.desc()).limit(limit))
    )
    tally = {name: 0 for name in _FINDING_STATUSES}
    for row in rows:
        if row.status in tally:
            tally[row.status] += 1
    return {
        "findings": [_finding_out(row) for row in rows],
        "counts": tally,
        # The allowed statuses come from the server rather than being hardcoded in
        # the client, so a screen offering a transition can never offer one the
        # writer would reject.
        "statuses": list(_FINDING_STATUSES),
        "filters": {
            "kpi_key": kpi_key,
            "target_date": target_date.isoformat() if target_date else None,
            "status": status,
        },
    }


@router.post(
    "/companies/{company_id}/investigation/findings",
    status_code=201,
    summary="Record a finding against a movement, or one part of one",
)
def create_finding(
    company_id: str,
    payload: FindingCreate,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    """Write down what a person concluded, anchored to what they were looking at.

    The detection run is looked up but not required. A note about a date that was
    never evaluated is unusual rather than illegitimate -- somebody may be recording
    why it was not evaluated -- and refusing it would lose a genuine observation.
    Where a run exists the finding points at it, so the note and the measurement it
    was written about stay linked.
    """

    version = _resolve_version(session, access, payload.kpi_id)
    dimension_name, entity, steps = _validated_anchor(
        session, access, version, payload.dimension, payload.entity, payload.path
    )
    status = _validated_status(payload.status)
    run = _find_run(session, access, version, payload.target_date)

    finding = InvestigationFinding(
        company_id=access.company.id,
        detection_run_id=run.id if run is not None else None,
        kpi_definition_id=version.kpi_id,
        kpi_key=version.definition.kpi_key,
        kpi_name=version.definition.name,
        target_date=payload.target_date,
        dimension=dimension_name,
        entity=entity,
        path=steps,
        title=payload.title.strip(),
        note=(payload.note or "").strip() or None,
        status=status,
        created_by_user_id=access.user.id,
        created_by_email=access.user.email,
        updated_by_user_id=access.user.id,
        updated_by_email=access.user.email,
        resolved_at=utcnow() if status == str(FindingStatus.RESOLVED) else None,
    )
    session.add(finding)
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.FINDING_CREATED,
        resource_type="investigation_finding",
        resource_id=finding.id,
        resource_label=f"{finding.kpi_name} {finding.target_date.isoformat()}",
        summary=f"Finding recorded on {finding.scope_label()}: {finding.title}",
        details={
            "kpi_key": finding.kpi_key,
            "target_date": finding.target_date.isoformat(),
            "dimension": finding.dimension,
            "entity": finding.entity,
            "status": finding.status,
            "detection_run_id": finding.detection_run_id,
        },
        request=request,
    )
    session.commit()
    session.refresh(finding)
    return {"finding": _finding_out(finding)}


@router.patch(
    "/companies/{company_id}/investigation/findings/{finding_id}",
    summary="Update a finding's text, or where its investigation stands",
)
def update_finding(
    company_id: str,
    finding_id: str,
    payload: FindingUpdate,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    """Change the note or the status. The anchor is immutable.

    Deliberately no way to move a finding to another KPI, date or entity. A
    conclusion is about the thing it was written against, and re-pointing it would
    silently rewrite what somebody concluded instead of recording a new conclusion.
    """

    finding = load_scoped(session, InvestigationFinding, finding_id, access)
    before = finding.status

    if payload.title is not None:
        finding.title = payload.title.strip()
    if payload.note is not None:
        finding.note = payload.note.strip() or None
    if payload.status is not None:
        finding.status = _validated_status(payload.status, current=before)
        # Written when it happens, cleared when it stops being true. A stale
        # resolution timestamp on a reopened investigation would be a fabricated
        # event, which is exactly what this table refuses to hold.
        finding.resolved_at = (
            utcnow() if finding.status == str(FindingStatus.RESOLVED) else None
        )

    finding.updated_by_user_id = access.user.id
    finding.updated_by_email = access.user.email
    session.flush()

    changed_status = payload.status is not None and finding.status != before
    audit.record(
        session,
        access=access,
        action=(
            audit.AuditAction.FINDING_STATUS_CHANGED
            if changed_status
            else audit.AuditAction.FINDING_UPDATED
        ),
        resource_type="investigation_finding",
        resource_id=finding.id,
        resource_label=f"{finding.kpi_name} {finding.target_date.isoformat()}",
        summary=(
            f"Investigation moved {before} -> {finding.status} on {finding.scope_label()}."
            if changed_status
            else f"Finding updated on {finding.scope_label()}: {finding.title}"
        ),
        old_version=before if changed_status else None,
        new_version=finding.status if changed_status else None,
        details={
            "kpi_key": finding.kpi_key,
            "target_date": finding.target_date.isoformat(),
            "dimension": finding.dimension,
            "entity": finding.entity,
            "status": finding.status,
        },
        request=request,
    )
    session.commit()
    session.refresh(finding)
    return {"finding": _finding_out(finding)}


@router.delete(
    "/companies/{company_id}/investigation/findings/{finding_id}",
    summary="Remove a finding",
)
def delete_finding(
    company_id: str,
    finding_id: str,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    """Delete a finding, leaving the audit trail that it existed.

    The row goes; the audit entry naming its title, anchor and author stays. That
    asymmetry is the point -- a deleted conclusion should not be recoverable as
    current, and should not be erasable as history.
    """

    finding = load_scoped(session, InvestigationFinding, finding_id, access)
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.FINDING_DELETED,
        resource_type="investigation_finding",
        resource_id=finding.id,
        resource_label=f"{finding.kpi_name} {finding.target_date.isoformat()}",
        summary=f"Finding deleted on {finding.scope_label()}: {finding.title}",
        details={
            "kpi_key": finding.kpi_key,
            "target_date": finding.target_date.isoformat(),
            "dimension": finding.dimension,
            "entity": finding.entity,
            "status": finding.status,
            "title": finding.title,
            "created_by_email": finding.created_by_email,
        },
        request=request,
    )
    session.delete(finding)
    session.commit()
    return {"deleted": finding_id}


# ---------------------------------------------------------------------------
# Contextual explanation of one investigation node
# ---------------------------------------------------------------------------
@router.post(
    "/companies/{company_id}/investigation/explain",
    summary="Explain the selected node from stored evidence only",
)
async def explain_investigation_node(
    company_id: str,
    payload: NodeExplainRequest,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    """The labelled sections about whichever node the reader has selected.

    The same gate as every other route here: with no stored run for the date there
    is no measured movement, and an explanation of one would be fiction. Nothing on
    this path reads business data. It composes stored measurements, the stored
    breakdown and permission-filtered approved documents -- so the breakdown it
    quantifies is one that was already run, and a node nobody has analysed is
    reported as exactly that rather than estimated.
    """

    version = _resolve_version(session, access, payload.kpi_id)
    dimension_name, entity, steps = _validated_anchor(
        session, access, version, payload.dimension, payload.entity, payload.path
    )
    run = _stored_run(session, access, version, payload.target_date)

    result = await explain_service.explain_node(
        session,
        access,
        run,
        dimension=dimension_name,
        entity=entity,
        path=steps,
        request_id=getattr(request.state, "request_id", None),
        usage_sink=llm_usage_of(request),
        narrate_with_model=payload.use_model,
    )
    body = result.as_dict()
    # The facts block is the statistics in another shape, so it answers to the same
    # permission the detection API's evidence block does. Without it a reader still
    # gets every section; they simply cannot audit the prose against the raw
    # numbers, which is the right trade for a role not entitled to them.
    if not access.has("kpi.read"):
        body.pop("facts", None)

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.NODE_EXPLAINED,
        resource_type="detection_run",
        resource_id=run.id,
        resource_label=f"{run.kpi_name} {run.target_date.isoformat()}",
        summary=(
            f"Explained '{result.subject}' at {result.confidence.level} confidence"
            + (
                f", narrated by {result.model}."
                if result.model_written
                else ", in platform prose."
            )
        ),
        details={
            "kpi_key": run.kpi_key,
            "target_date": run.target_date.isoformat(),
            "dimension": dimension_name,
            "entity": entity,
            "confidence": result.confidence.level,
            "model_written": result.model_written,
            "model": result.model,
            "citations": len(result.citations),
        },
        request=request,
    )
    session.commit()
    return {"explanation": body}

