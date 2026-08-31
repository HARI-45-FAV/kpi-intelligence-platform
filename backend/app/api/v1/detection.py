"""The detection API: run detection, and govern the configuration it reads.

Three concerns, deliberately kept apart, with a distinct permission on each:

* ``detection.configure`` drafts and edits a company's comparison policy -- which
  past days count as comparable to today;
* ``kpi.approve`` approves that policy, because an unreviewed comparison basis
  silently changes every number computed after it, and an approver signing off a
  KPI's meaning is the right person to sign off how it is compared;
* ``detection.run`` executes detection, which reads the company's own source.

Nothing here names a company, a table, a column, a weekday, a month or an event.
A request names a KPI; the KPI's *registration* supplies the source, the formula
and the time field; the company's *approved configuration* supplies which history
is comparable; and one engine -- identical for every tenant -- supplies the
arithmetic. Adding a company or a KPI is data entry, not a code change.

The response is split for the same reason the engine's result is: ``result`` is
what a business surface may render, and ``evidence`` -- reference dates, median,
MAD, modified z-score, the generated aggregate's source binding -- is returned
only to callers already entitled to read KPI definitions, and is meant for the
governance surface rather than the dashboard.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.connectors.registry import build_connector
from app.copilot.text import extract_text, unreadable_reason
from app.core.clock import utcnow
from app.core.deps import (
    AccessContext,
    CurrentUser,
    SessionDep,
    load_scoped,
    require_permissions,
    resolve_access,
)
from app.core.errors import Conflict, NotFound, PlatformError, ValidationFailure
from app.core.telemetry import llm_usage_of, usage_of
from app.models.base import (
    BUCKET_CONFIG_TRANSITIONS,
    BucketConfigSource,
    BucketConfigStatus,
    KpiStatus,
)
from app.models.detection import AgentRun, AgentRunExplanation, CompanyBucketConfig, DetectionRun
from app.models.document import CompanyDocument
from app.models.kpi import KpiDefinition, KpiVersion
from app.models.source import DataSource
from app.schemas import (
    BatchDetectionRequest,
    AgentRunOut,
    BucketConfigApproveRequest,
    BucketConfigExtractRequest,
    BucketConfigRequest,
    DetectionRunOut,
    KpiTransitionRequest,
    ResultItemOut,
    ResultSummaryOut,
    RunDetectionRequest,
)
from app.services import audit
from app.services import documents as document_service
from app.services.bucket_config import BucketConfig, describe_buckets, validate_bucket_config
from app.services.bucket_extraction import extract_bucket_config
from app.services.detection import (
    UNCONFIGURED_WARNING,
    DetectionOutcome,
    KpiBinding,
    config_payload,
    detect,
    load_bucket_config_row,
    persist_run,
    plan_comparison,
    policy_for,
    resolve_binding,
)
from app.services.kpi_execution import can_execute, unsupported_reason
from app.services.observability import log_detection

router = APIRouter(tags=["detection"])


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------
@contextmanager
def _connector_pool(
    request: Request,
) -> Iterator[Callable[[DataSource], DataSourceConnector]]:
    """Lend one connector per data source for the life of a request.

    A batch run over several KPIs sharing a source opens one connection, not one
    per KPI, and every connector's query count lands on this request's telemetry
    row on the way out.

    The gate is *capability*, not source technology: a source qualifies when
    :func:`app.services.kpi_execution.can_execute` says a KPI can be evaluated on
    it -- by pushing the aggregate into SQL, or by reading and aggregating one
    bounded window. Refusing everything that is not a SQL connection would make
    detection unavailable on exactly the source most companies connect.
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
# Resolving "which KPI"
# ---------------------------------------------------------------------------
def _pick_version(definition: KpiDefinition) -> KpiVersion:
    """The version detection should run against: live first, else approved."""

    versions = sorted(definition.versions, key=lambda item: item.version)
    for wanted in (KpiStatus.ACTIVE, KpiStatus.APPROVED):
        matching = [version for version in versions if version.status == wanted]
        if matching:
            return matching[-1]
    raise Conflict(
        f"'{definition.name}' has no approved version. Detection runs against "
        "approved definitions only, so that every number traces back to a meaning "
        "the business agreed to.",
        details={
            "kpi_key": definition.kpi_key,
            "versions": [
                {"version": version.version, "status": version.status} for version in versions
            ],
        },
    )


def _resolve_version(session: Session, access: AccessContext, kpi_id: str) -> KpiVersion:
    """Accept a KPI key, a definition id or a version id -- all scoped to the caller.

    The flexibility is for callers, not for the engine: a dashboard holds KPI
    keys, the registry screen holds definition ids and an audit trail holds
    version ids. Every path ends at one governed version inside the caller's own
    company.
    """

    wanted = (kpi_id or "").strip()
    company_id = access.company.id

    version = session.get(KpiVersion, wanted)
    if version is not None and version.company_id == company_id:
        return version

    definition = session.get(KpiDefinition, wanted)
    if definition is not None and definition.company_id == company_id:
        return _pick_version(definition)

    by_key = session.scalars(
        select(KpiDefinition).where(
            KpiDefinition.company_id == company_id,
            func.lower(KpiDefinition.kpi_key) == wanted.lower(),
        )
    ).first()
    if by_key is not None:
        return _pick_version(by_key)

    raise NotFound(
        f"No KPI matching '{kpi_id}' is registered in this company. A KPI key, a KPI "
        "id or a KPI version id is accepted."
    )


# ---------------------------------------------------------------------------
# Resolving "which history is comparable"
# ---------------------------------------------------------------------------
def _config_for(
    session: Session, company_id: str, kpi_key: str | None
) -> tuple[BucketConfig, CompanyBucketConfig | None]:
    """The approved policy in force for this KPI, or the documented fallback.

    The engine owns this lookup: an explicitly requested analysis of one part of
    the business has to be compared against the same approved history a scheduled
    run uses, and two copies of "which row is in force" could drift apart.
    """

    return policy_for(session, company_id, kpi_key)


# ---------------------------------------------------------------------------
# Running detection
# ---------------------------------------------------------------------------
def _execute(
    session: Session,
    access: AccessContext,
    acquire: Callable[[DataSource], DataSourceConnector],
    version: KpiVersion,
    target_date: date,
    *,
    persist: bool,
    coverage_cache: dict | None = None,
) -> tuple[DetectionOutcome, DetectionRun | None, KpiBinding]:
    """The ten steps of the specified flow, in order, for one KPI on one date."""

    # 1-2. The KPI contract, and the source + formula + time field it registered.
    binding = resolve_binding(session, version)

    # 3. The company's approved bucket configuration.
    config, config_row = _config_for(session, access.company.id, binding.kpi_key)

    # 4-9. Actual, comparable dates, reference values, expected, MAD, modified
    # z-score and the business tolerance -- all inside the engine, deterministic.
    connector = acquire(binding.data_source)
    outcome = detect(
        connector,
        binding,
        config,
        target_date,
        materiality=version.materiality,
        config_row=config_row,
        coverage_cache=coverage_cache,
    )

    # 10. Persist, so the result can be shown, audited and re-explained later.
    run = (
        persist_run(session, outcome, executed_by_user_id=access.user.id) if persist else None
    )

    # One structured line per KPI, so a single Run Agent execution can be traced
    # end to end from the logs alone: source, formula, bucket, reference dates and
    # values, actual, expected, deviation, status. Credential-free by construction,
    # and carrying the run id so a log line joins to the stored result.
    log_detection(outcome, run_id=run.id if run is not None else None)

    return outcome, run, binding


def _response(
    outcome: DetectionOutcome,
    run: DetectionRun | None,
    access: AccessContext,
) -> dict:
    """Business answer always; technical evidence only for entitled callers."""

    payload: dict = {
        "result": outcome.business_view(),
        "run_id": run.id if run is not None else None,
        "agent_run_id": run.agent_run_id if run is not None else None,
        "persisted": run is not None,
    }
    if access.has("kpi.read"):
        payload["evidence"] = outcome.evidence()
    return payload


def _response_from_stored(run: DetectionRun, access: AccessContext) -> dict:
    payload: dict = {
        "result": {
            "kpi_key": run.kpi_key,
            "target_date": run.target_date.isoformat(),
            "actual": run.actual_value,
            "expected": run.expected_value,
            "deviation_pct": run.deviation_pct,
            "deviation_absolute": run.deviation_absolute,
            "status": run.status,
            "comparison": run.comparison_label,
            "headline": run.headline,
            "unit": run.unit,
            "currency": run.currency,
        },
        "run_id": run.id,
        "persisted": True,
        "agent_run_id": run.agent_run_id,
    }
    if access.has("kpi.read"):
        payload["evidence"] = run.evidence
    return payload


def _audit_run(
    session: Session,
    request: Request,
    access: AccessContext,
    outcome: DetectionOutcome,
    run: DetectionRun | None,
) -> None:
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.DETECTION_RUN,
        resource_type="kpi_version",
        resource_id=outcome.kpi_version_id,
        resource_label=f"{outcome.kpi_name} v{outcome.kpi_version}",
        summary=(
            f"Detection on {outcome.target_date.isoformat()}: {outcome.status}"
            f" ({outcome.comparison_label.lower()})."
        ),
        outcome="SUCCESS",
        details={
            "kpi_key": outcome.kpi_key,
            "target_date": outcome.target_date.isoformat(),
            "status": str(outcome.status),
            "actual": outcome.actual,
            "expected": outcome.expected,
            "deviation_pct": outcome.deviation_pct,
            "bucket_applied": str(outcome.bucket_applied),
            "bucket_config_key": outcome.bucket_config_key,
            "bucket_config_version": outcome.bucket_config_version,
            "reference_count": len(outcome.references),
            "query_count": outcome.query_count,
            "duration_ms": outcome.duration_ms,
            "run_id": run.id if run is not None else None,
        },
        request=request,
    )


@router.post(
    "/companies/{company_id}/run-detection",
    summary="Evaluate one KPI on one date",
)
def run_detection(
    payload: RunDetectionRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("detection.run")),
) -> dict:
    """Actual, expected, deviation and status for one KPI on one date.

    The company in the path is the authorisation boundary; a ``company_id`` in
    the body is accepted for symmetry with the flat form and must agree with it,
    so a body cannot redirect an authorised request at another tenant.
    """

    if payload.company_id and payload.company_id != access.company.id:
        raise ValidationFailure(
            "The company in the request body does not match the company in the URL.",
            details={"path_company_id": access.company.id, "body_company_id": payload.company_id},
        )

    version = _resolve_version(session, access, payload.kpi_id)
    with _connector_pool(request) as acquire:
        outcome, run, _binding = _execute(
            session, access, acquire, version, payload.target_date, persist=payload.persist
        )
    _audit_run(session, request, access, outcome, run)
    return _response(outcome, run, access)


@router.post("/run-detection", summary="Evaluate one KPI on one date (company in the body)")
def run_detection_flat(
    payload: RunDetectionRequest,
    session: SessionDep,
    request: Request,
    user: CurrentUser,
) -> dict:
    """The specified flat form: ``{company_id, kpi_id, target_date}``.

    Identical behaviour, and identical enforcement: the body's ``company_id`` is
    a claim, and ``resolve_access`` turns it into an entitlement by looking up the
    caller's membership, role and permissions before anything else happens.
    """

    if not payload.company_id:
        raise ValidationFailure(
            "company_id is required on this form of the request. The company-scoped "
            "route takes it from the URL instead."
        )
    access = resolve_access(request, session, user, payload.company_id)
    access.require("detection.run")

    version = _resolve_version(session, access, payload.kpi_id)
    with _connector_pool(request) as acquire:
        outcome, run, _binding = _execute(
            session, access, acquire, version, payload.target_date, persist=payload.persist
        )
    _audit_run(session, request, access, outcome, run)
    return _response(outcome, run, access)


@router.post(
    "/companies/{company_id}/run-detection/batch",
    summary="Evaluate several KPIs on one date",
)
def run_detection_batch(
    payload: BatchDetectionRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("detection.run")),
) -> dict:
    """One date, several KPIs, one page load.

    A KPI that cannot be evaluated -- unapproved, unbound, wrong grain -- is
    reported beside the ones that could, rather than failing the whole request:
    a dashboard should render what it can and say plainly what it could not.
    """

    if payload.company_id and payload.company_id != access.company.id:
        raise ValidationFailure(
            "The company in the request body does not match the company in the URL.",
            details={"path_company_id": access.company.id, "body_company_id": payload.company_id},
        )

    wanted = payload.kpi_ids or [
        definition.kpi_key
        for definition in session.scalars(
            select(KpiDefinition)
            .where(KpiDefinition.company_id == access.company.id)
            .order_by(KpiDefinition.name)
        )
    ]

    started_at = utcnow()
    agent_run = AgentRun(
        company_id=access.company.id,
        target_date=payload.target_date,
        status="RUNNING",
        kpi_count=len(wanted[:25]),
        executed_by_user_id=access.user.id,
        started_at=started_at,
    )
    session.add(agent_run)
    session.flush()

    results: list[dict] = []
    skipped: list[dict] = []
    # Several KPIs usually share one source table, and its data coverage is a
    # property of the table, not of the KPI: measured once, reused for the batch.
    coverage_cache: dict = {}
    with _connector_pool(request) as acquire:
        for kpi_id in wanted[:25]:
            try:
                version = _resolve_version(session, access, kpi_id)
                outcome, run, _binding = _execute(
                    session,
                    access,
                    acquire,
                    version,
                    payload.target_date,
                    # An explicit Agent Run is always auditable and replayable
                    # from stored results; the batch flag cannot disable that.
                    persist=True,
                    coverage_cache=coverage_cache,
                )
                if run is not None:
                    run.agent_run_id = agent_run.id
                agent_run.processed_count += 1
                if str(outcome.status) == "NORMAL":
                    agent_run.normal_count += 1
                elif str(outcome.status) == "ABNORMAL":
                    agent_run.abnormal_count += 1
                else:
                    agent_run.low_confidence_count += 1
            except PlatformError as exc:
                skipped.append({"kpi_id": kpi_id, "reason": exc.message})
                agent_run.error_count += 1
                agent_run.errors = [*agent_run.errors, {"kpi_id": kpi_id, "reason": exc.message}]
                continue
            except Exception:
                reason = "The KPI could not be evaluated because an unexpected execution error occurred."
                skipped.append({"kpi_id": kpi_id, "reason": reason})
                agent_run.error_count += 1
                agent_run.errors = [*agent_run.errors, {"kpi_id": kpi_id, "reason": reason}]
                continue
            results.append(_response(outcome, run, access))

    agent_run.status = "COMPLETED" if not skipped else "COMPLETED_WITH_ERRORS"
    agent_run.duration_ms = int((utcnow() - started_at).total_seconds() * 1000)
    agent_run.completed_at = utcnow()
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.AGENT_RUN,
        resource_type="agent_run",
        resource_id=agent_run.id,
        resource_label=payload.target_date.isoformat(),
        summary=(
            f"Detection on {payload.target_date.isoformat()} for {len(results)} KPI(s)"
            + (f"; {len(skipped)} skipped." if skipped else ".")
        ),
        details={
            "target_date": payload.target_date.isoformat(),
            "statuses": {
                item["result"]["kpi_key"]: item["result"]["status"] for item in results
            },
            "skipped": skipped,
            "agent_run_id": agent_run.id,
        },
        outcome="SUCCESS" if not skipped else "PARTIAL_FAILURE",
        request=request,
    )
    return {
        "target_date": payload.target_date,
        "agent_run_id": agent_run.id,
        "agent_run": AgentRunOut.model_validate(agent_run).model_dump(),
        "results": results,
        "skipped": skipped,
        "counts": {"evaluated": len(results), "skipped": len(skipped)},
    }


@router.get(
    "/companies/{company_id}/agent-runs",
    response_model=list[AgentRunOut],
    summary="Stored Agent Run history",
)
def list_agent_runs(
    session: SessionDep,
    limit: int = 50,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> list[AgentRunOut]:
    """Read persisted batch executions without rerunning any KPI."""

    statement = (
        select(AgentRun)
        .where(AgentRun.company_id == access.company.id)
        .order_by(AgentRun.target_date.desc(), AgentRun.started_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return [AgentRunOut.model_validate(run) for run in session.scalars(statement)]


@router.get(
    "/companies/{company_id}/agent-runs/{agent_run_id}",
    summary="One stored Agent Run with its KPI results",
)
def get_agent_run(
    agent_run_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> dict:
    agent_run: AgentRun = load_scoped(session, AgentRun, agent_run_id, access)
    results = list(
        session.scalars(
            select(DetectionRun)
            .where(
                DetectionRun.company_id == access.company.id,
                DetectionRun.agent_run_id == agent_run.id,
            )
            .order_by(DetectionRun.kpi_name)
        )
    )
    return {
        "agent_run": AgentRunOut.model_validate(agent_run).model_dump(),
        "results": [_response_from_stored(run, access) for run in results],
    }


# ---------------------------------------------------------------------------
# Stored results
# ---------------------------------------------------------------------------
def _run_out(run: DetectionRun) -> DetectionRunOut:
    return DetectionRunOut.model_validate(run)


def _result_item(run: DetectionRun, explanation: AgentRunExplanation | None) -> ResultItemOut:
    """One stored result, with a generated explanation only if one really exists.

    Nothing in the platform writes ``agent_run_explanations`` today -- explanation
    generation is the Copilot's job and it is off by default -- so the common case
    is ``explanation is None``. That case reports ``NOT_GENERATED`` and
    ``NOT_SENT`` rather than defaulting to ``READY`` and ``EMAIL_SENT``: the
    screen previously claimed every historical row had a finished explanation and
    a delivered email, and this platform has no email engine at all.

    ``top_driver`` is the engine's own deterministic headline and is always
    present, which is what the screen shows instead.
    """

    return ResultItemOut(
        id=run.id,
        kpi_key=run.kpi_key,
        kpi_name=run.kpi_name,
        target_date=run.target_date,
        status=run.status,
        actual_value=run.actual_value,
        expected_value=run.expected_value,
        deviation_absolute=run.deviation_absolute,
        deviation_pct=run.deviation_pct,
        unit=run.unit,
        currency=run.currency,
        top_driver=run.headline,
        ai_explanation=explanation.explanation_text if explanation else None,
        explanation_status=explanation.status if explanation else "NOT_GENERATED",
        explanation_generated_at=explanation.generated_at if explanation else None,
        email_status=explanation.email_status if explanation else "NOT_SENT",
    )


@router.get(
    "/companies/{company_id}/results",
    summary="Aggregated agent-run result history",
)
def list_result_history(
    session: SessionDep,
    status: str | None = None,
    limit: int = 200,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> dict:
    """Read the stored queue of KPI results with their explanation summary.

    This is the same underlying source as the agent-run and detection-run tables,
    collapsed into one business answer list for the Results screen.
    """

    statement = select(DetectionRun).where(DetectionRun.company_id == access.company.id)
    if status:
        statement = statement.where(DetectionRun.status == status)
    statement = statement.order_by(DetectionRun.target_date.desc(), DetectionRun.executed_at.desc())
    runs = list(session.scalars(statement.limit(max(1, min(limit, 500)))))

    if not runs:
        return {
            "summary": ResultSummaryOut(
                total_runs=0,
                anomalies=0,
                abnormal=0,
                normal=0,
                low_confidence=0,
                kpi_count=0,
            ).model_dump(),
            "items": [],
        }

    # Deduplicated: a page of 500 rows is only a handful of distinct KPIs and
    # dates, and two 500-element IN lists would push past SQLite's bind-parameter
    # ceiling on older builds.
    explanation_rows = session.scalars(
        select(AgentRunExplanation).where(
            AgentRunExplanation.company_id == access.company.id,
            AgentRunExplanation.kpi_key.in_({run.kpi_key for run in runs}),
            AgentRunExplanation.target_date.in_({run.target_date for run in runs}),
        )
    ).all()
    explanations = {
        (row.kpi_key, row.target_date): row for row in explanation_rows
    }

    items = [
        _result_item(run, explanations.get((run.kpi_key, run.target_date)))
        for run in runs
    ]

    summary = ResultSummaryOut(
        total_runs=len(items),
        anomalies=sum(1 for item in items if item.status == "ABNORMAL"),
        abnormal=sum(1 for item in items if item.status == "ABNORMAL"),
        normal=sum(1 for item in items if item.status == "NORMAL"),
        low_confidence=sum(1 for item in items if item.status == "LOW_CONFIDENCE"),
        kpi_count=len({item.kpi_key for item in items}),
    )

    return {
        "summary": summary.model_dump(),
        "items": [item.model_dump(mode="json") for item in items],
    }


@router.get(
    "/companies/{company_id}/detection-runs",
    response_model=list[DetectionRunOut],
    summary="Stored detection results",
)
def list_detection_runs(
    session: SessionDep,
    kpi_key: str | None = None,
    target_date: date | None = None,
    limit: int = 50,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> list[DetectionRunOut]:
    """Business-facing history: the answer, never the statistics behind it."""

    statement = select(DetectionRun).where(DetectionRun.company_id == access.company.id)
    if kpi_key:
        statement = statement.where(DetectionRun.kpi_key == kpi_key)
    if target_date is not None:
        statement = statement.where(DetectionRun.target_date == target_date)
    statement = statement.order_by(
        DetectionRun.target_date.desc(), DetectionRun.executed_at.desc()
    ).limit(max(1, min(limit, 200)))
    return [_run_out(run) for run in session.scalars(statement)]


@router.get(
    "/companies/{company_id}/detection-runs/{run_id}",
    summary="One stored detection result",
)
def get_detection_run(
    run_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> dict:
    """The stored answer, with the evidence attached for entitled callers."""

    run: DetectionRun = load_scoped(session, DetectionRun, run_id, access)
    payload: dict = {
        "result": {
            "kpi": run.kpi_name,
            "kpi_key": run.kpi_key,
            "target_date": run.target_date.isoformat(),
            "actual": run.actual_value,
            "expected": run.expected_value,
            "deviation_pct": run.deviation_pct,
            "deviation_absolute": run.deviation_absolute,
            "status": run.status,
            "comparison": run.comparison_label,
            "headline": run.headline,
            "unit": run.unit,
            "currency": run.currency,
        },
        "run_id": run.id,
        "executed_at": run.executed_at,
    }
    if access.has("kpi.read"):
        payload["evidence"] = run.evidence
    return payload


@router.get(
    "/companies/{company_id}/detection/overview",
    summary="What can be detected, and what was detected last",
)
def detection_overview(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> dict:
    """One call for the monitoring screen: which KPIs are detectable, and the
    latest stored result for each.

    A KPI that cannot be detected carries the governance reason -- unapproved, no
    time field, no source binding, wrong grain -- because that is actionable,
    where an empty list is not.
    """

    definitions = list(
        session.scalars(
            select(KpiDefinition)
            .where(KpiDefinition.company_id == access.company.id)
            .order_by(KpiDefinition.name)
        )
    )

    latest: dict[str, DetectionRun] = {}
    for run in session.scalars(
        select(DetectionRun)
        .where(DetectionRun.company_id == access.company.id)
        .order_by(DetectionRun.target_date.desc(), DetectionRun.executed_at.desc())
    ):
        latest.setdefault(run.kpi_key, run)

    kpis: list[dict] = []
    for definition in definitions:
        entry: dict = {
            "kpi_id": definition.id,
            "kpi_key": definition.kpi_key,
            "name": definition.name,
            "detectable": False,
            "blocked_reason": None,
            "unit": None,
            "currency": None,
            "kpi_version": None,
            "latest_run": None,
        }
        try:
            version = _pick_version(definition)
            binding = resolve_binding(session, version)
        except PlatformError as exc:
            entry["blocked_reason"] = exc.message
        else:
            entry.update(
                detectable=True,
                unit=binding.version.unit,
                currency=binding.version.currency,
                kpi_version=binding.version.version,
            )
        run = latest.get(definition.kpi_key)
        if run is not None:
            entry["latest_run"] = _run_out(run).model_dump()
        kpis.append(entry)

    company_config = load_bucket_config_row(session, access.company.id, None)
    overrides = [
        _config_out(row)
        for row in session.scalars(
            select(CompanyBucketConfig).where(
                CompanyBucketConfig.company_id == access.company.id,
                CompanyBucketConfig.status == BucketConfigStatus.APPROVED,
                CompanyBucketConfig.kpi_key.is_not(None),
            )
        )
    ]

    return {
        "kpis": kpis,
        "counts": {
            "total": len(kpis),
            "detectable": sum(1 for entry in kpis if entry["detectable"]),
        },
        "configuration": {
            "company_default": _config_out(company_config) if company_config else None,
            "kpi_overrides": overrides,
            "note": (
                None
                if company_config or overrides
                else UNCONFIGURED_WARNING
            ),
        },
    }


# ---------------------------------------------------------------------------
# Comparison configuration: the company's "when is history comparable?"
# ---------------------------------------------------------------------------
def _config_out(row: CompanyBucketConfig) -> dict:
    buckets = row.buckets or {}
    enabled = [
        key
        for key, value in buckets.items()
        if isinstance(value, dict) and value.get("enabled") is True
    ]
    return {
        "id": row.id,
        "config_key": row.config_key,
        "name": row.name,
        "description": row.description,
        "kpi_key": row.kpi_key,
        "scope": "kpi" if row.kpi_key else "company",
        "status": row.status,
        "version": row.version,
        "buckets": buckets,
        "enabled_slots": sorted(enabled),
        "lookback_days": row.lookback_days,
        "min_reference_points": row.min_reference_points,
        "max_reference_points": row.max_reference_points,
        "source": row.source,
        "source_document_id": row.source_document_id,
        "extraction_model": row.extraction_model,
        "extraction_notes": row.extraction_notes,
        "proposed_by_user_id": row.proposed_by_user_id,
        "approved_by_user_id": row.approved_by_user_id,
        "approved_at": row.approved_at,
        "approval_reason": row.approval_reason,
        "allowed_transitions": sorted(
            str(target) for target in BUCKET_CONFIG_TRANSITIONS.get(row.status, ())
        ),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _next_version(session: Session, company_id: str, config_key: str) -> int:
    highest = session.scalar(
        select(func.max(CompanyBucketConfig.version)).where(
            CompanyBucketConfig.company_id == company_id,
            CompanyBucketConfig.config_key == config_key,
        )
    )
    return int(highest or 0) + 1


def _assert_kpi_key(session: Session, access: AccessContext, kpi_key: str | None) -> None:
    """A policy scoped to a KPI must name a KPI this company actually registered."""

    if not kpi_key:
        return
    exists = session.scalar(
        select(func.count())
        .select_from(KpiDefinition)
        .where(
            KpiDefinition.company_id == access.company.id,
            KpiDefinition.kpi_key == kpi_key,
        )
    )
    if not exists:
        raise NotFound(
            f"No KPI with key '{kpi_key}' is registered in this company, so a "
            "comparison policy cannot be scoped to it."
        )


def _transition(row: CompanyBucketConfig, target: BucketConfigStatus) -> None:
    allowed = BUCKET_CONFIG_TRANSITIONS.get(row.status, ())
    if target not in allowed:
        raise Conflict(
            f"A {row.status} comparison configuration cannot move to {target}."
            + (f" Allowed: {', '.join(str(a) for a in allowed)}." if allowed else ""),
            details={"status": row.status, "allowed": [str(a) for a in allowed]},
        )
    row.status = target


@router.get(
    "/companies/{company_id}/bucket-configs",
    summary="Comparison configurations for this company",
)
def list_bucket_configs(
    session: SessionDep,
    status_filter: str | None = None,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    statement = select(CompanyBucketConfig).where(
        CompanyBucketConfig.company_id == access.company.id
    )
    if status_filter:
        statement = statement.where(CompanyBucketConfig.status == status_filter.upper())
    rows = list(
        session.scalars(
            statement.order_by(
                CompanyBucketConfig.config_key, CompanyBucketConfig.version.desc()
            )
        )
    )
    in_force = load_bucket_config_row(session, access.company.id, None)
    return {
        "configurations": [_config_out(row) for row in rows],
        "company_default_in_force": in_force.id if in_force else None,
        "note": (
            "The engine reads APPROVED configurations only. A KPI-scoped configuration "
            "overrides the company default for that KPI alone."
        ),
    }


@router.get(
    "/companies/{company_id}/bucket-configs/{config_id}",
    summary="One comparison configuration",
)
def get_bucket_config(
    config_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    """One configuration, with the validator's own view of it beside the stored row.

    A NEEDS_REVIEW row is *expected* to fail validation -- that is why it is in
    review -- so the failure is reported as a field rather than as an error status.
    Refusing to render the row would hide the one screen on which a reviewer could
    fix it.
    """

    row: CompanyBucketConfig = load_scoped(session, CompanyBucketConfig, config_id, access)
    try:
        config = validate_bucket_config(config_payload(row))
    except PlatformError as exc:
        return {
            **_config_out(row),
            "normalised": None,
            "warnings": [],
            "usable": False,
            "unusable_reason": exc.message,
        }
    return {
        **_config_out(row),
        "normalised": config.as_dict(),
        "warnings": list(config.warnings),
        "usable": True,
        "unusable_reason": None,
    }


@router.post(
    "/companies/{company_id}/bucket-configs",
    status_code=status.HTTP_201_CREATED,
    summary="Draft a comparison configuration",
)
def create_bucket_config(
    payload: BucketConfigRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("detection.configure")),
) -> dict:
    """Create a DRAFT policy. Validated on the way in, by the same code the
    engine trusts, so an unusable configuration is refused here rather than
    producing plausible-looking numbers later."""

    _assert_kpi_key(session, access, payload.kpi_key)
    config = validate_bucket_config(payload.buckets)

    row = CompanyBucketConfig(
        company_id=access.company.id,
        config_key=payload.config_key,
        name=payload.name,
        description=payload.description,
        kpi_key=payload.kpi_key,
        status=BucketConfigStatus.DRAFT,
        version=_next_version(session, access.company.id, payload.config_key),
        buckets=config.as_dict(),
        lookback_days=config.lookback_days,
        min_reference_points=config.min_reference_points,
        max_reference_points=config.max_reference_points,
        source=BucketConfigSource.MANUAL,
        proposed_by_user_id=access.user.id,
    )
    session.add(row)
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.BUCKET_CONFIG_CREATED,
        resource_type="bucket_config",
        resource_id=row.id,
        resource_label=f"{row.name} v{row.version}",
        summary=f"Drafted comparison configuration '{row.config_key}' v{row.version}.",
        new_version=str(row.version),
        details={
            "kpi_key": row.kpi_key,
            "enabled_slots": [str(bucket) for bucket in config.enabled_buckets],
            "warnings": list(config.warnings),
        },
        request=request,
    )
    return {**_config_out(row), "warnings": list(config.warnings)}


@router.put(
    "/companies/{company_id}/bucket-configs/{config_id}",
    summary="Edit a draft comparison configuration",
)
def update_bucket_config(
    config_id: str,
    payload: BucketConfigRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("detection.configure")),
) -> dict:
    """Edit a policy that is not yet approved.

    An APPROVED policy is immutable, for the same reason an ACTIVE KPI version
    is: a stored result names the configuration that produced it, and editing
    that row in place would make every past result unexplainable. Editing a
    PROPOSED policy returns it to DRAFT, because the reviewer approved something
    else.
    """

    row: CompanyBucketConfig = load_scoped(session, CompanyBucketConfig, config_id, access)
    if row.status not in (BucketConfigStatus.DRAFT, BucketConfigStatus.PROPOSED):
        raise Conflict(
            f"This configuration is {row.status} and cannot be edited. Create a new "
            "version instead, so results computed under the old one stay explainable."
        )
    _assert_kpi_key(session, access, payload.kpi_key)
    config = validate_bucket_config(payload.buckets)

    was_proposed = row.status == BucketConfigStatus.PROPOSED
    row.config_key = payload.config_key
    row.name = payload.name
    row.description = payload.description
    row.kpi_key = payload.kpi_key
    row.buckets = config.as_dict()
    row.lookback_days = config.lookback_days
    row.min_reference_points = config.min_reference_points
    row.max_reference_points = config.max_reference_points
    if was_proposed:
        row.status = BucketConfigStatus.DRAFT
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.BUCKET_CONFIG_UPDATED,
        resource_type="bucket_config",
        resource_id=row.id,
        resource_label=f"{row.name} v{row.version}",
        summary=(
            f"Edited comparison configuration '{row.config_key}' v{row.version}"
            + (" and returned it to DRAFT." if was_proposed else ".")
        ),
        details={
            "enabled_slots": [str(bucket) for bucket in config.enabled_buckets],
            "warnings": list(config.warnings),
        },
        request=request,
    )
    return {**_config_out(row), "warnings": list(config.warnings)}


@router.post(
    "/companies/{company_id}/bucket-configs/{config_id}/propose",
    summary="Submit a comparison configuration for approval",
)
def propose_bucket_config(
    config_id: str,
    payload: KpiTransitionRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("detection.configure")),
) -> dict:
    row: CompanyBucketConfig = load_scoped(session, CompanyBucketConfig, config_id, access)
    validate_bucket_config(config_payload(row))
    _transition(row, BucketConfigStatus.PROPOSED)
    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.BUCKET_CONFIG_UPDATED,
        resource_type="bucket_config",
        resource_id=row.id,
        resource_label=f"{row.name} v{row.version}",
        summary=f"Submitted comparison configuration '{row.config_key}' v{row.version} for approval.",
        details={"reason": payload.reason},
        request=request,
    )
    return _config_out(row)


@router.post(
    "/companies/{company_id}/bucket-configs/{config_id}/approve",
    summary="Approve a comparison configuration",
)
def approve_bucket_config(
    config_id: str,
    payload: BucketConfigApproveRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.approve")),
) -> dict:
    """Make a policy usable by the engine.

    Approval is the gate the whole LLM boundary rests on: a drafted
    configuration -- extracted from documentation or typed by hand -- changes no
    number until a person with approval rights has read it. Previously approved
    policies covering the same scope are archived here, so "which policy was in
    force" has exactly one answer.
    """

    row: CompanyBucketConfig = load_scoped(session, CompanyBucketConfig, config_id, access)
    config = validate_bucket_config(config_payload(row))
    _transition(row, BucketConfigStatus.APPROVED)
    row.approved_by_user_id = access.user.id
    row.approved_at = utcnow()
    row.approval_reason = payload.reason

    superseded: list[str] = []
    for other in session.scalars(
        select(CompanyBucketConfig).where(
            CompanyBucketConfig.company_id == access.company.id,
            CompanyBucketConfig.status == BucketConfigStatus.APPROVED,
            CompanyBucketConfig.id != row.id,
        )
    ):
        if other.kpi_key == row.kpi_key:
            other.status = BucketConfigStatus.ARCHIVED
            superseded.append(f"{other.config_key} v{other.version}")
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.BUCKET_CONFIG_APPROVED,
        resource_type="bucket_config",
        resource_id=row.id,
        resource_label=f"{row.name} v{row.version}",
        summary=(
            f"Approved comparison configuration '{row.config_key}' v{row.version}"
            + (f", superseding {', '.join(superseded)}." if superseded else ".")
        ),
        new_version=str(row.version),
        details={
            "reason": payload.reason,
            "kpi_key": row.kpi_key,
            "enabled_slots": [str(bucket) for bucket in config.enabled_buckets],
            "source": row.source,
            "extraction_model": row.extraction_model,
            "superseded": superseded,
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="DETECTION",
        title="Comparison configuration approved",
        message=(
            f"{row.name} v{row.version} now decides which history detection compares "
            f"{'this KPI' if row.kpi_key else 'every KPI'} against."
        ),
    )
    return {**_config_out(row), "superseded": superseded, "warnings": list(config.warnings)}


@router.post(
    "/companies/{company_id}/bucket-configs/{config_id}/archive",
    summary="Archive a comparison configuration",
)
def archive_bucket_config(
    config_id: str,
    payload: KpiTransitionRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.approve")),
) -> dict:
    row: CompanyBucketConfig = load_scoped(session, CompanyBucketConfig, config_id, access)
    _transition(row, BucketConfigStatus.ARCHIVED)
    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.BUCKET_CONFIG_ARCHIVED,
        resource_type="bucket_config",
        resource_id=row.id,
        resource_label=f"{row.name} v{row.version}",
        summary=f"Archived comparison configuration '{row.config_key}' v{row.version}.",
        details={"reason": payload.reason},
        request=request,
    )
    return _config_out(row)


@router.get(
    "/companies/{company_id}/bucket-configs/{config_id}/preview",
    summary="Which past dates this configuration would compare against",
)
def preview_bucket_config(
    config_id: str,
    session: SessionDep,
    target_date: date | None = None,
    access: AccessContext = Depends(require_permissions("detection.configure")),
) -> dict:
    """Show a reviewer the calendar consequence of a policy before approving it.

    Dates only -- no KPI is measured here, so nothing in this response depends on
    the company's data. It answers "would this actually select the days we mean?",
    which is the question an approver has and cannot answer from JSON.
    """

    row: CompanyBucketConfig = load_scoped(session, CompanyBucketConfig, config_id, access)
    config = validate_bucket_config(config_payload(row))
    day = target_date or utcnow().date()
    primary, applied, dates, decisions = plan_comparison(config, day)
    return {
        "config_id": row.id,
        "status": row.status,
        "target_date": day,
        "comparison": {
            "label": describe_buckets(config, day, applied),
            "bucket_applied": str(primary),
            "buckets_applied": [str(bucket) for bucket in applied],
            "decisions": [decision.as_dict() for decision in decisions],
        },
        "comparable_dates": [item.isoformat() for item in dates[:60]],
        "comparable_date_count": len(dates),
        "warnings": list(config.warnings),
        "note": (
            "Calendar preview only. No KPI value is computed and the source is not "
            "queried."
        ),
    }


# ---------------------------------------------------------------------------
# The LLM boundary: documentation in, configuration draft out
# ---------------------------------------------------------------------------
def _document_text(
    session: Session, access: AccessContext, document_id: str
) -> tuple[str, CompanyDocument, str | None]:
    document: CompanyDocument = load_scoped(session, CompanyDocument, document_id, access)
    document_service.assert_readable(document, access)
    version = document_service.resolve_version(document, None)
    data, content_type = document_service.read_content(version)
    text = extract_text(data, content_type, version.original_filename)
    if text is None:
        raise ValidationFailure(
            f"'{document.title}' cannot be read as text: "
            + unreadable_reason(content_type, version.original_filename)
        )
    return text, document, version.id


@router.post(
    "/companies/{company_id}/bucket-configs/extract",
    status_code=status.HTTP_201_CREATED,
    summary="Draft a comparison configuration from company documentation",
)
async def extract_bucket_config_endpoint(
    payload: BucketConfigExtractRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("detection.configure")),
) -> dict:
    """The only route on which a language model touches detection.

    It reads prose and returns a comparison *policy* -- which weekdays, weeks,
    months or event dates a company treats as comparable. It cannot return a
    value, an expectation, a median, a deviation or a verdict: the extracted
    object is validated against the same five-slot contract the engine reads, and
    any other key is discarded and reported.

    Where the draft lands is decided by the draft, not by this route. A usable
    extraction is stored PROPOSED, ready for approval. One that produced nothing
    usable -- no slot the document actually supported, or event dates the document
    does not contain -- is stored NEEDS_REVIEW with its reasons attached, because
    the alternatives are both worse: proposing it invites an approval click on a
    policy that cannot select a single comparable date, and failing outright throws
    away the partial result a reviewer needs in order to finish the job by hand.
    Either way the engine ignores it until someone with approval rights approves it.
    """

    if bool(payload.document_id) == bool(payload.text):
        raise ValidationFailure(
            "Supply either a document_id from this company's document store or the "
            "text to read -- not both, and not neither."
        )

    _assert_kpi_key(session, access, payload.kpi_key)

    document: CompanyDocument | None = None
    document_version_id: str | None = None
    if payload.document_id:
        text, document, document_version_id = _document_text(
            session, access, payload.document_id
        )
    else:
        text = payload.text or ""

    draft = await extract_bucket_config(text, usage_sink=llm_usage_of(request))
    landed = (
        BucketConfigStatus.NEEDS_REVIEW if draft.needs_review else BucketConfigStatus.PROPOSED
    )

    row = CompanyBucketConfig(
        company_id=access.company.id,
        config_key=payload.config_key,
        name=payload.name,
        description=(
            f"Drafted from '{document.title}'." if document is not None else "Drafted from supplied text."
        ),
        kpi_key=payload.kpi_key,
        status=landed,
        version=_next_version(session, access.company.id, payload.config_key),
        buckets=draft.config.as_dict(),
        lookback_days=draft.config.lookback_days,
        min_reference_points=draft.config.min_reference_points,
        max_reference_points=draft.config.max_reference_points,
        source=BucketConfigSource.LLM_EXTRACTION,
        source_document_id=document.id if document is not None else None,
        source_document_version_id=document_version_id,
        extraction_model=draft.model,
        # Everything a reviewer needs in order to act, in the column the review
        # screen reads: what was discarded and why, what is still missing, and how
        # much of the document was actually put in front of the model.
        extraction_notes="\n".join([*draft.review_reasons, *draft.notes]) or None,
        proposed_by_user_id=access.user.id,
    )
    session.add(row)
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.BUCKET_CONFIG_EXTRACTED,
        resource_type="bucket_config",
        resource_id=row.id,
        resource_label=f"{row.name} v{row.version}",
        summary=(
            f"Drafted comparison configuration '{row.config_key}' v{row.version} from "
            + (f"'{document.title}'" if document is not None else "supplied text")
            + f" using {draft.model or 'the configured model'}; stored as {landed}."
        ),
        details={
            "model": draft.model,
            "document_id": row.source_document_id,
            "kpi_key": row.kpi_key,
            "status": str(landed),
            "returned_keys": draft.raw_keys,
            "rejected_keys": draft.rejected_keys,
            "enabled_slots": [str(bucket) for bucket in draft.config.enabled_buckets],
            "needs_review": draft.needs_review,
            "review_reasons": draft.review_reasons,
            "retrieval": draft.retrieval,
            "warnings": list(draft.config.warnings),
        },
        request=request,
    )
    return {
        **_config_out(row),
        "extraction": draft.as_dict(),
        "needs_review": draft.needs_review,
        "review_reasons": draft.review_reasons,
        "warnings": list(draft.config.warnings),
        "note": (
            "A model proposed which past days are comparable. Every number -- actual, "
            "expected, median, deviation, status -- is computed by the engine, and this "
            "draft changes nothing until it is approved."
            + (
                " This extraction is incomplete: read the reasons, fix what is missing, "
                "then propose it."
                if draft.needs_review
                else ""
            )
        ),
    }
