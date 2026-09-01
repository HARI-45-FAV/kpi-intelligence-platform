"""The monitoring dashboard: one governed read of what has actually been evaluated.

This is the landing screen, and its whole discipline is that every number on it is
a count of stored rows. Nothing here queries a data source, projects a trend, or
fills a gap. A company that has never run detection gets zeros and a window with no
dates -- not a shape the screen has to guess at, and not a demo figure standing in
for a real one.

Three things this endpoint deliberately refuses to do, because each would make the
dashboard say something untrue:

* **It does not imply a scheduler.** There is none in this version. ``monitoring_note``
  says so in words, and ``last_evaluated_at`` is the timestamp of the most recent
  *stored* evaluation rather than a "monitoring since" date, so a company whose last
  run was in March sees March.
* **It does not fold unknown statuses into known ones.** A stored row may carry a
  verdict from an earlier schema. Counting it as NORMAL would misreport it and
  dropping it would stop the tiles summing to the total, so unrecognised verdicts
  are counted, named, and visible.
* **It does not invent a movement's importance.** "Biggest movement" is ranked by
  the deviation the engine already stored, and a KPI whose run has no deviation is
  absent from that list rather than ranked at zero.

Gated on ``analytics.read`` -- the permission that lets somebody see a result at
all -- and scoped, like every read here, to the company resolved from the request
rather than the one named in the path.

One permission subtlety worth stating, because it is easy to get wrong in an
aggregate endpoint: ``analytics.read`` buys the *verdicts*, not the investigation.
A VIEWER holds ``analytics.read`` without ``investigation.read``, so the findings
someone wrote -- their titles, their notes, the author's email -- and the existence
of a stored breakdown are attached only when the caller also holds
``investigation.read``. Aggregating several subjects into one response does not
merge their permissions, and a dashboard is exactly where that mistake would be
invisible.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.clock import utcnow
from app.core.deps import AccessContext, SessionDep, require_permissions
from app.models.base import DetectionStatus, FindingStatus
from app.models.detection import ContributionRun, DetectionRun
from app.models.investigation import InvestigationFinding
from app.models.kpi import KpiDefinition
from app.schemas import MonitoringOut

router = APIRouter(tags=["monitoring"])

#: Said on the screen, not just in the docs. The platform evaluates a KPI when
#: somebody runs detection; describing that as "monitoring" without qualification
#: would be a claim about a scheduler this version does not have.
MONITORING_NOTE = (
    "Detection runs when it is triggered — this platform has no scheduler in this "
    "version. Every figure below counts evaluations that have already been stored, "
    "so a KPI absent from the window has not been evaluated in it rather than "
    "having passed."
)

#: The three verdicts the engine can reach. Anything else on a stored row came from
#: an earlier schema and is reported as unrecognised rather than reinterpreted.
_KNOWN_STATUSES = tuple(str(status) for status in DetectionStatus)

#: How many rows each list carries. Small on purpose: a dashboard list is a way in
#: to the Result page, not a substitute for the result history.
_LIST_LIMIT = 8
_RUN_LIMIT = 12


def _movement_rank(run: DetectionRun) -> float:
    """Size of a movement, regardless of direction, for ranking only.

    Percentage first because it compares across KPIs of different magnitudes;
    absolute deviation as the fallback for a KPI whose expectation was zero, where
    a percentage is undefined rather than large.
    """

    if run.deviation_pct is not None:
        return abs(run.deviation_pct)
    if run.deviation_absolute is not None:
        return abs(run.deviation_absolute)
    return 0.0


@router.get(
    "/companies/{company_id}/monitoring",
    response_model=MonitoringOut,
    summary="What has been evaluated, what moved, and what is still open",
)
def monitoring_dashboard(
    company_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("analytics.read")),
    window_days: int = Query(default=90, ge=1, le=730),
) -> MonitoringOut:
    """One call for the monitoring screen.

    The window bounds which evaluations are counted, and is reported back so the
    screen can say what period its tiles describe. ``window_from``/``window_to``
    are the earliest and latest *stored* target dates inside it, not the requested
    boundaries, so an empty window reads as empty instead of as a range in which
    nothing happened to be found.
    """

    company_id_scoped = access.company.id
    today = utcnow().date()
    window_start = today - timedelta(days=window_days - 1)

    # Whether this caller may see the investigation layer at all. Everything below
    # that touches a finding or a stored breakdown is behind this flag, so a reader
    # holding only analytics.read gets verdicts and movements and learns nothing
    # about who has been investigating what.
    may_investigate = access.has("investigation.read")

    definitions = list(
        session.scalars(
            select(KpiDefinition)
            .where(KpiDefinition.company_id == company_id_scoped)
            .order_by(KpiDefinition.name)
        )
    )
    by_key = {definition.kpi_key: definition for definition in definitions}

    # Every stored evaluation inside the window, newest first. Read once and
    # bucketed in Python rather than counted with several aggregate queries: the
    # volumes here are one company's runs over one window, and one pass keeps the
    # tallies, the lists and the "latest per KPI" map guaranteed consistent with
    # each other.
    runs = list(
        session.scalars(
            select(DetectionRun)
            .where(
                DetectionRun.company_id == company_id_scoped,
                DetectionRun.target_date >= window_start,
                DetectionRun.target_date <= today,
            )
            .order_by(DetectionRun.target_date.desc(), DetectionRun.executed_at.desc())
        )
    )

    tally = {name: 0 for name in _KNOWN_STATUSES}
    unrecognised = 0
    unrecognised_statuses: set[str] = set()
    latest: dict[str, DetectionRun] = {}
    evaluated_per_kpi: dict[str, int] = {}
    window_dates: list[date] = []

    for run in runs:
        if run.status in tally:
            tally[run.status] += 1
        else:
            unrecognised += 1
            unrecognised_statuses.add(str(run.status))
        latest.setdefault(run.kpi_key, run)
        evaluated_per_kpi[run.kpi_key] = evaluated_per_kpi.get(run.kpi_key, 0) + 1
        window_dates.append(run.target_date)

    # Which movements already have a stored breakdown, and which carry open notes.
    # The dashboard uses both to label its call to action honestly: "investigate"
    # where nothing has been analysed, "review" where somebody already has.
    #
    # Both are investigation facts, so neither is read for a caller without
    # investigation.read. The absence is represented as None rather than False --
    # "not disclosed to you" and "no breakdown exists" are different answers, and
    # the screen must not print the second when it means the first.
    analysed_runs: set[str] = set()
    open_by_run: dict[str, int] = {}
    if may_investigate:
        analysed_runs = {
            row
            for row in session.scalars(
                select(ContributionRun.detection_run_id).where(
                    ContributionRun.company_id == company_id_scoped,
                    ContributionRun.detection_run_id.is_not(None),
                )
            )
            if row
        }
        for run_id, count in session.execute(
            select(InvestigationFinding.detection_run_id, func.count())
            .where(
                InvestigationFinding.company_id == company_id_scoped,
                InvestigationFinding.detection_run_id.is_not(None),
                InvestigationFinding.status != str(FindingStatus.RESOLVED),
            )
            .group_by(InvestigationFinding.detection_run_id)
        ):
            if run_id:
                open_by_run[run_id] = int(count)

    def movement(run: DetectionRun) -> dict:
        definition = by_key.get(run.kpi_key)
        return {
            "detection_run_id": run.id,
            "kpi_id": definition.id if definition else run.kpi_definition_id,
            "kpi_key": run.kpi_key,
            "kpi_name": run.kpi_name,
            "target_date": run.target_date,
            "status": run.status,
            "actual_value": run.actual_value,
            "expected_value": run.expected_value,
            "deviation_absolute": run.deviation_absolute,
            "deviation_pct": run.deviation_pct,
            "unit": run.unit,
            "currency": run.currency,
            "headline": run.headline,
            "has_contribution": (run.id in analysed_runs) if may_investigate else None,
            "open_findings": open_by_run.get(run.id, 0) if may_investigate else None,
        }

    # Biggest movements: one row per KPI, so a single volatile KPI cannot fill the
    # list with its own history and hide a second KPI that also moved.
    best_per_kpi: dict[str, DetectionRun] = {}
    for run in runs:
        if run.deviation_pct is None and run.deviation_absolute is None:
            continue
        current = best_per_kpi.get(run.kpi_key)
        if current is None or _movement_rank(run) > _movement_rank(current):
            best_per_kpi[run.kpi_key] = run
    biggest = sorted(best_per_kpi.values(), key=_movement_rank, reverse=True)[:_LIST_LIMIT]

    abnormal = [
        run for run in runs if run.status == str(DetectionStatus.ABNORMAL)
    ][:_LIST_LIMIT]

    kpis: list[dict] = []
    for definition in definitions:
        run = latest.get(definition.kpi_key)
        kpis.append(
            {
                "kpi_id": definition.id,
                "kpi_key": definition.kpi_key,
                "kpi_name": definition.name,
                "lifecycle_status": str(definition.status),
                "active_version": definition.current_version,
                "latest_status": run.status if run else None,
                "latest_target_date": run.target_date if run else None,
                "latest_deviation_pct": run.deviation_pct if run else None,
                "latest_executed_at": run.executed_at if run else None,
                "evaluated_in_window": evaluated_per_kpi.get(definition.kpi_key, 0),
            }
        )

    # The investigation layer, for callers entitled to it. A reader without
    # investigation.read gets an empty list and null tallies -- not zeros, which
    # would assert that nobody has written a finding.
    findings: list[InvestigationFinding] = []
    finding_tally = {name: 0 for name in (str(s) for s in FindingStatus)}
    if may_investigate:
        findings = list(
            session.scalars(
                select(InvestigationFinding)
                .where(InvestigationFinding.company_id == company_id_scoped)
                .order_by(InvestigationFinding.updated_at.desc())
            )
        )
        for finding in findings:
            if finding.status in finding_tally:
                finding_tally[finding.status] += 1

    # The most recent stored evaluation *ever*, not merely inside the window. A
    # dashboard whose window happens to exclude the last run should say when that
    # run was, rather than imply nothing has ever been evaluated.
    last_evaluated_at = session.scalar(
        select(func.max(DetectionRun.executed_at)).where(
            DetectionRun.company_id == company_id_scoped
        )
    )

    return MonitoringOut(
        window_days=window_days,
        window_from=min(window_dates) if window_dates else None,
        window_to=max(window_dates) if window_dates else None,
        last_evaluated_at=last_evaluated_at,
        counts={
            "kpis_monitored": len(definitions),
            "evaluated": len(runs),
            "normal": tally[str(DetectionStatus.NORMAL)],
            "abnormal": tally[str(DetectionStatus.ABNORMAL)],
            "low_confidence": tally[str(DetectionStatus.LOW_CONFIDENCE)],
            "unrecognised": unrecognised,
            "unrecognised_statuses": sorted(unrecognised_statuses),
            "not_evaluated": sum(
                1 for definition in definitions if definition.kpi_key not in latest
            ),
        },
        kpis=kpis,
        biggest_movements=[movement(run) for run in biggest],
        recent_abnormal=[movement(run) for run in abnormal],
        recent_runs=[
            {
                "detection_run_id": run.id,
                "agent_run_id": run.agent_run_id,
                "kpi_id": (
                    by_key[run.kpi_key].id
                    if run.kpi_key in by_key
                    else run.kpi_definition_id
                ),
                "kpi_key": run.kpi_key,
                "kpi_name": run.kpi_name,
                "target_date": run.target_date,
                "status": run.status,
                "deviation_pct": run.deviation_pct,
                "executed_at": run.executed_at,
            }
            for run in sorted(runs, key=lambda item: item.executed_at, reverse=True)[
                :_RUN_LIMIT
            ]
        ],
        findings_open=finding_tally[str(FindingStatus.OPEN)] if may_investigate else None,
        findings_in_progress=(
            finding_tally[str(FindingStatus.IN_PROGRESS)] if may_investigate else None
        ),
        findings_resolved=(
            finding_tally[str(FindingStatus.RESOLVED)] if may_investigate else None
        ),
        recent_findings=[
            {
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
            for finding in findings[:_LIST_LIMIT]
        ],
        monitoring_note=MONITORING_NOTE,
    )
