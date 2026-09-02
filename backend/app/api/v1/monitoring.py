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

The headline panel follows the same rule one step further. Its sentences are
assembled in ``headline()`` from the KPI's own name, the run's own date and the
figures the engine computed, with a contributor named only where a stored
breakdown named one -- so a headline is reproducible, and no model is involved in
deciding what this company's results say. Where no contributor is known, the panel
says why rather than leaving a gap a reader would fill in.

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
from app.core.errors import ValidationFailure
from app.models.base import DetectionStatus, FindingStatus
from app.models.detection import ContributionRun, DetectionRun
from app.models.investigation import InvestigationFinding
from app.models.kpi import KpiDefinition
from app.schemas import MonitoringOut
from app.services.explanation import kpi_label

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
_HEADLINE_LIMIT = 10

#: The periods the headline panel offers, and the one it opens on. Fixed rather
#: than free-form because each is a period a business reader recognises -- a week,
#: a fortnight, a month, a quarter -- and "the last 53 days" is not. Sent to the
#: client in the response so the set of offered windows is decided here, in one
#: place, rather than being hardcoded again on the screen.
FINDINGS_WINDOW_OPTIONS: tuple[int, ...] = (7, 14, 30, 90)
FINDINGS_WINDOW_DEFAULT = 7


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


def _direction(run: DetectionRun) -> str | None:
    """"above" or "below", from the sign the engine already stored.

    Read off ``deviation_absolute`` first: it keeps its sign even where the
    expectation was zero and the percentage is therefore absent.
    """

    for value in (run.deviation_absolute, run.deviation_pct):
        if value is not None and value != 0:
            return "above" if value > 0 else "below"
    return None


def _date_label(target: date, today: date) -> str:
    """"28 Aug", or "28 Aug 2025" once the year stops being obvious.

    A headline in a 7-day window does not need a year and reads worse with one; a
    90-day window can cross a new year, where omitting it would be ambiguous.
    """

    return target.strftime("%d %b") if target.year == today.year else target.strftime("%d %b %Y")


def _movement_phrase(run: DetectionRun) -> str:
    """The movement in words, from the figures the engine stored.

    Never computed here beyond taking an absolute value and rounding for display:
    the percentage and the absolute deviation are both read straight off the row,
    and a run carrying neither is described as having moved abnormally rather than
    being given a number it does not have.
    """

    direction = _direction(run)
    suffix = f" {direction} expectation" if direction else " against expectation"
    if run.deviation_pct is not None:
        return f"{abs(run.deviation_pct):,.1f}%{suffix}"
    if run.deviation_absolute is not None:
        return f"{abs(run.deviation_absolute):,.0f}{suffix}"
    return "abnormally"


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
    findings_window_days: int = Query(
        default=FINDINGS_WINDOW_DEFAULT,
        description=(
            "Period the headline panel covers. One of 7, 14, 30 or 90 days; any "
            "other value is refused rather than rounded, so the screen and the "
            "server never disagree about what a headline list describes."
        ),
    ),
) -> MonitoringOut:
    """One call for the monitoring screen.

    The window bounds which evaluations are counted, and is reported back so the
    screen can say what period its tiles describe. ``window_from``/``window_to``
    are the earliest and latest *stored* target dates inside it, not the requested
    boundaries, so an empty window reads as empty instead of as a range in which
    nothing happened to be found.

    ``findings_window_days`` is separate and deliberately narrower. The tiles
    describe a long period because a count over a quarter is useful; a headline
    list over a quarter is not a list of news. The two windows move independently.
    """

    if findings_window_days not in FINDINGS_WINDOW_OPTIONS:
        raise ValidationFailure(
            f"findings_window_days must be one of {', '.join(str(o) for o in FINDINGS_WINDOW_OPTIONS)}. "
            f"Received {findings_window_days}."
        )

    company_id_scoped = access.company.id
    today = utcnow().date()
    window_start = today - timedelta(days=window_days - 1)
    findings_start = today - timedelta(days=findings_window_days - 1)

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
    #
    # The scan covers whichever of the two windows reaches further back, because
    # they move independently -- a caller can ask for a quarter of headlines beside
    # a week of tiles. The tallies below then use only the tile window, so widening
    # the scan cannot change a single count.
    scan_start = min(window_start, findings_start)
    scanned = list(
        session.scalars(
            select(DetectionRun)
            .where(
                DetectionRun.company_id == company_id_scoped,
                DetectionRun.target_date >= scan_start,
                DetectionRun.target_date <= today,
            )
            .order_by(DetectionRun.target_date.desc(), DetectionRun.executed_at.desc())
        )
    )
    runs = [run for run in scanned if run.target_date >= window_start]

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
    leader_by_run: dict[str, ContributionRun] = {}
    if may_investigate:
        # One pass over the stored breakdowns, giving both facts the dashboard
        # needs: which movements have been analysed at all, and what the analysis
        # concluded led each one.
        #
        # Ordered shallowest-then-newest, and taken with setdefault, so the row kept
        # per movement is the *top-level* apportionment. A drill-down is a narrower
        # question -- "within Depot North, which product?" -- and its leader is not
        # the leader of the movement.
        for contribution in session.scalars(
            select(ContributionRun)
            .where(
                ContributionRun.company_id == company_id_scoped,
                ContributionRun.detection_run_id.is_not(None),
            )
            .order_by(ContributionRun.depth.asc(), ContributionRun.executed_at.desc())
        ):
            run_id = contribution.detection_run_id
            if not run_id:
                continue
            analysed_runs.add(run_id)
            leader_by_run.setdefault(run_id, contribution)
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

    def contributor(run_id: str) -> dict:
        """The leading contributor as the investigation stored it, or nulls.

        Every value is copied. Nothing is ranked, summed or apportioned here: this
        endpoint reports what the contribution service concluded, and a movement
        nobody has analysed gets nulls and a reason rather than a nominee.
        """

        if not may_investigate:
            return {
                "contributor_dimension": None,
                "contributor_entity": None,
                "contributor_share_pct": None,
                "contributor_is_sufficient": None,
            }
        found = leader_by_run.get(run_id)
        if found is None or not found.leader_entity:
            return {
                "contributor_dimension": None,
                "contributor_entity": None,
                "contributor_share_pct": None,
                "contributor_is_sufficient": None,
            }
        return {
            "contributor_dimension": found.dimension,
            "contributor_entity": found.leader_entity,
            "contributor_share_pct": found.leader_share_pct,
            "contributor_is_sufficient": found.leader_is_sufficient,
        }

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
            # Carried so the movement rows can offer the Investigation action and,
            # where an investigation has already run, name what it found -- without
            # the screen making a second call per row.
            "can_investigate": may_investigate,
            **contributor(run.id),
        }

    def headline(run: DetectionRun) -> dict:
        """One abnormal movement, as a sentence, from stored values only.

        The sentence is assembled from three things the platform already holds: the
        KPI's own name, the run's own date, and the movement the engine computed.
        A contributor is named only where a stored breakdown named one, and how
        strongly it is named follows the breakdown's own judgement:
        ``leader_is_sufficient`` is the difference between "accounts for most of it"
        and "no single area accounts for most of it".

        **A share is a size, not a cause.** The platform measures where a movement
        sits; nothing in it establishes why. So no branch of this function says
        "drove", "caused" or "because of" -- the verb is "accounts for" throughout,
        the same wording the explanation service uses, and a headline can therefore
        never assert more than the breakdown measured. That is why this wording lives
        in code rather than in a prompt.
        """

        definition = by_key.get(run.kpi_key)
        found = contributor(run.id)
        when = _date_label(run.target_date, today)
        phrase = _movement_phrase(run)
        entity = found["contributor_entity"]
        share = found["contributor_share_pct"]
        # Magnitude only: the movement's own direction is already in `phrase`, and a
        # signed share here would read as a second, contradictory direction.
        share_text = f" ({abs(share):,.1f}% of the movement)" if share is not None else ""
        # The reader's name for the KPI, not the developer's: a headline reading
        # "net_revenue moved 12% below expectation" puts a column name in front of a
        # business audience. `kpi_key` is untouched and is still what links filter on.
        label = kpi_label(run.kpi_name)

        if entity and found["contributor_is_sufficient"]:
            text = (
                f"{label} moved {phrase} on {when}, with {entity} accounting for most "
                f"of it{share_text}."
            )
        elif entity:
            # The breakdown ran and found no single sufficient area. Saying so is the
            # finding; naming the largest without it would imply one.
            text = (
                f"{label} moved {phrase} on {when}; no single area accounts for most of "
                f"it, and {entity} is the largest contributor{share_text}."
            )
        else:
            text = f"{label} moved {phrase} on {when}."

        # Why no contributor is named, said plainly. A blank space where a cause
        # should be invites the reader to supply one; this says who could supply it.
        note: str | None = None
        if not entity:
            if not may_investigate:
                note = "Contributor analysis is not visible to your role."
            elif run.id in analysed_runs:
                note = (
                    "A breakdown has been run and did not find a single leading "
                    "contributor."
                )
            else:
                note = "No breakdown has been run for this movement yet."

        return {
            "detection_run_id": run.id,
            "kpi_id": definition.id if definition else run.kpi_definition_id,
            "kpi_key": run.kpi_key,
            "kpi_name": run.kpi_name,
            "target_date": run.target_date,
            "status": run.status,
            "headline": text,
            "deviation_pct": run.deviation_pct,
            "deviation_absolute": run.deviation_absolute,
            "actual_value": run.actual_value,
            "expected_value": run.expected_value,
            "unit": run.unit,
            "currency": run.currency,
            "direction": _direction(run),
            "contributor_note": note,
            "can_investigate": may_investigate,
            **found,
        }

    # The headline panel: abnormal movements in the selected period, largest first,
    # then most recent. Ranked by size rather than recency because the panel answers
    # "what mattered", and a reader scanning a fortnight wants the biggest thing in
    # it at the top.
    #
    # Restricted to ABNORMAL on purpose. A NORMAL verdict is not news, and a
    # LOW_CONFIDENCE one is the engine saying it could not reach a verdict -- putting
    # either in a headline list would assert something the run does not.
    findings_runs = [
        run
        for run in scanned
        if run.target_date >= findings_start and run.status == str(DetectionStatus.ABNORMAL)
    ]
    findings_dates = [run.target_date for run in findings_runs]
    headline_runs = sorted(
        findings_runs,
        key=lambda item: (_movement_rank(item), item.target_date),
        reverse=True,
    )[:_HEADLINE_LIMIT]

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
        findings_window_days=findings_window_days,
        findings_window_options=list(FINDINGS_WINDOW_OPTIONS),
        findings_window_from=min(findings_dates) if findings_dates else None,
        findings_window_to=max(findings_dates) if findings_dates else None,
        headlines=[headline(run) for run in headline_runs],
        headline_total=len(findings_runs),
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
