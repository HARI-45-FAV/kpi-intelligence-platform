"""Governed tools over stored contribution analyses.

These read what the investigation engine already computed and wrote down. They
compute nothing, and that boundary is the point: a share of a KPI movement is
deterministic arithmetic over the KPI's own governed formula and source, performed
by :mod:`app.services.contribution` behind an authenticated request, and a language
model must never be the thing that produces one. So there is no tool here that
takes a dimension and goes to a warehouse. There is one that reads a
:class:`~app.models.detection.ContributionRun`.

Which has a second, practical consequence worth stating: because a breakdown is
only ever *read* here, asking the Copilot about contributors costs no warehouse
query. A chat turn from the investigation panel cannot quietly re-run the
company's revenue query against every region.

Three rules the surface enforces:

**A share is not a verdict.** Nothing in a stored run says an entity is abnormal,
because nothing in the table can. The only status these tools report is the KPI's,
and every payload carries the reminder, because the misreading this prevents --
"North is 60% of the movement" becoming "North has a problem" -- is the single most
likely way this feature could mislead someone.

**A share is not a cause.** The results are amounts and percentages. What produced
the movement is not in them, is not computed anywhere on this platform, and is
listed in ``PLANNED_TOOLS`` as absent for that reason.

**No breakdown means no breakdown.** Contribution analysis runs on request, so most
KPI/date pairs have never had one, and the honest answer is to say so. Estimating
shares from a total the model can see would be inventing measurements, which is the
one thing the evidence model exists to prevent.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select

from app.copilot.context import CopilotContext
from app.copilot.evidence import contribution_run_evidence
from app.copilot.tools.base import ToolResult, ToolSpec, refuse
from app.models.detection import ContributionRun

INVESTIGATION_READ = ("investigation.read",)

_DIMENSION_ARG = {
    "type": "string",
    "description": (
        "Restrict to a breakdown by this dimension, named as the KPI registered it "
        "(for example the dimension shown on the investigation screen). Omit to take "
        "the most recent breakdown whatever dimension it used."
    ),
}

_DATE_ARG = {
    "type": "string",
    "description": (
        "The date the breakdown was run for, as YYYY-MM-DD. Omit to use the date "
        "currently in view."
    ),
}

_LIMIT_ARG = {
    "type": "integer",
    "description": "How many ranked contributors to return, largest share first.",
    "minimum": 1,
    "maximum": 25,
}

#: Attached to every payload. Repeated rather than assumed because a tool result
#: reaches the model as standalone JSON, with none of this module's context.
_NOT_A_VERDICT = (
    "A share is how much of the movement a part of the business accounts for. It is "
    "not a status, not an anomaly and not a cause. No anomaly detection has been run "
    "on any contributor listed here."
)


def _target_date(context: CopilotContext, arguments: dict[str, Any]) -> date | None:
    """The date asked about: the argument if it parses, else the one in view."""

    raw = arguments.get("date")
    if raw:
        try:
            return date.fromisoformat(str(raw).strip())
        except ValueError:
            return None
    return context.selected_date


def _latest(
    context: CopilotContext,
    *,
    target_date: date,
    dimension: str | None,
) -> ContributionRun | None:
    """The most recent stored breakdown matching the resolved coordinates.

    Scoped to this company and this KPI definition and nothing else -- the company
    from the proven membership, the KPI from the resolved context. Re-running a
    breakdown supersedes the earlier one for the same view, so the newest wins.
    """

    if context.kpi_definition is None:
        return None
    stmt = (
        select(ContributionRun)
        .where(
            ContributionRun.company_id == context.company_id,
            ContributionRun.kpi_definition_id == context.kpi_definition.id,
            ContributionRun.target_date == target_date,
        )
        .order_by(ContributionRun.executed_at.desc())
        .limit(1)
    )
    if dimension:
        stmt = stmt.where(ContributionRun.dimension == dimension)
    return context.session.scalars(stmt).first()


def _contributor_rows(run: ContributionRun, limit: int) -> list[dict[str, Any]]:
    """The ranked parts, trimmed, with no field the table does not hold.

    Copied key by key rather than passed through, so a column added to the stored
    JSON later cannot start appearing in a prompt without someone deciding it
    should.
    """

    rows: list[dict[str, Any]] = []
    for row in (run.contributors or [])[:limit]:
        rows.append(
            {
                "label": row.get("label"),
                "actual": row.get("actual"),
                "expected": row.get("expected"),
                "change": row.get("change"),
                "share_pct": row.get("share_pct"),
                "reference_count": row.get("reference_count"),
                "note": row.get("note"),
            }
        )
    return rows


def get_contribution_breakdown(
    context: CopilotContext, arguments: dict[str, Any]
) -> ToolResult:
    """The stored breakdown of a KPI movement across one approved dimension.

    Reads a recorded result; runs nothing and queries no business source. When no
    breakdown has been stored for the view, that is reported as the answer rather
    than filled in.
    """

    if context.kpi_definition is None:
        return refuse(
            "No KPI is resolved for this conversation, so there is no movement to break "
            "down. Ask about a specific KPI, or open the investigation screen for one."
        )

    target = _target_date(context, arguments)
    if target is None:
        return refuse(
            "No date is resolved for this conversation and none was supplied, so there "
            "is no breakdown to read. Contribution analysis always belongs to one date."
        )

    dimension = (arguments.get("dimension") or "").strip() or None
    run = _latest(context, target_date=target, dimension=dimension)
    if run is None:
        by = f" by {dimension}" if dimension else ""
        return refuse(
            f"No contribution analysis is stored for {context.kpi_definition.name}"
            f"{by} on {target.isoformat()}. Contribution analysis runs on request rather "
            "than continuously, so this is expected until someone runs it from the "
            "investigation screen. Do not estimate any part's share of this movement."
        )

    limit = int(arguments.get("limit") or 10)
    caveats: list[str] = []
    if run.withheld_count:
        caveats.append(
            f"{run.withheld_count} value(s) of {run.dimension} are outside this reader's "
            "data scope and are not included in the listed parts."
        )
    if not run.shares_available:
        caveats.append(
            "This KPI's parts do not sum to its total, so amounts are reported without "
            "percentage shares. Do not state a share for any part."
        )
    if run.unexplained_pct:
        caveats.append(
            f"The listed parts account for {run.explained_pct:.1f}% of the movement; "
            f"{run.unexplained_pct:.1f}% is not reconciled by this breakdown."
        )
    caveats.extend(str(warning) for warning in (run.warnings or ()))

    return ToolResult(
        data={
            "kpi_key": run.kpi_key,
            "kpi_name": run.kpi_name,
            "kpi_version": run.kpi_version,
            "target_date": run.target_date.isoformat(),
            "dimension": run.dimension,
            "within": run.path,
            "unit": run.unit,
            "currency": run.currency,
            "kpi_actual": run.kpi_actual,
            "kpi_expected": run.kpi_expected,
            "kpi_movement": run.kpi_movement,
            # The KPI's verdict, and the only one in the payload.
            "kpi_status": run.kpi_status,
            "contributors": _contributor_rows(run, limit),
            "ranked_count": run.ranked_count,
            "returned_count": min(limit, len(run.contributors or [])),
            "explained_pct": run.explained_pct,
            "unexplained_pct": run.unexplained_pct,
            "largest_contributor": run.leader_entity,
            "largest_contributor_share_pct": run.leader_share_pct,
            "analysed_at": run.executed_at.isoformat() if run.executed_at else None,
            "interpretation": _NOT_A_VERDICT,
        },
        evidence=[contribution_run_evidence(run)],
        caveats=caveats,
    )


def list_stored_contribution_analyses(
    context: CopilotContext, arguments: dict[str, Any]
) -> ToolResult:
    """Which breakdowns exist for the KPI in view, so a question can name one.

    Deliberately thin -- a date, a dimension, how deep, who ran it and when. It
    answers "has anyone looked at this, and how" without returning any part's
    figures, which keeps the shares behind the tool that carries the caveats with
    them.
    """

    if context.kpi_definition is None:
        return refuse(
            "No KPI is resolved for this conversation, so there are no breakdowns to "
            "list."
        )

    limit = int(arguments.get("limit") or 10)
    runs = list(
        context.session.scalars(
            select(ContributionRun)
            .where(
                ContributionRun.company_id == context.company_id,
                ContributionRun.kpi_definition_id == context.kpi_definition.id,
            )
            .order_by(ContributionRun.executed_at.desc())
            .limit(limit)
        )
    )
    if not runs:
        return refuse(
            f"No contribution analysis has been run for {context.kpi_definition.name}. "
            "Breakdowns are produced on request from the investigation screen, so none "
            "existing is the normal state, not a fault."
        )

    return ToolResult(
        data={
            "kpi_key": context.kpi_definition.kpi_key,
            "kpi_name": context.kpi_definition.name,
            "analyses": [
                {
                    "target_date": run.target_date.isoformat(),
                    "dimension": run.dimension,
                    "within": run.path,
                    "depth": run.depth,
                    "entry_point": run.entry_point,
                    "ranked_count": run.ranked_count,
                    "kpi_status": run.kpi_status,
                    "analysed_at": run.executed_at.isoformat() if run.executed_at else None,
                }
                for run in runs
            ],
            "interpretation": _NOT_A_VERDICT,
        }
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_contribution_breakdown",
        description=(
            "The stored breakdown of a KPI's measured movement across one of its "
            "approved dimensions: each part's actual, expected value, change and signed "
            "share of the movement, ranked largest first, with how much of the movement "
            "the listed parts account for. Reads a recorded analysis; computes nothing "
            "and queries no business data. A share says how much of a movement a part "
            "accounts for -- it is not a status, an anomaly or a cause."
        ),
        permissions=INVESTIGATION_READ,
        parameters={
            "type": "object",
            "properties": {
                "dimension": _DIMENSION_ARG,
                "date": _DATE_ARG,
                "limit": _LIMIT_ARG,
            },
            "required": [],
        },
        handler=get_contribution_breakdown,
    ),
    ToolSpec(
        name="list_stored_contribution_analyses",
        description=(
            "Which contribution analyses have been run for the KPI in view: the date, "
            "the dimension, how deep the drill-down went and when it was run. Use it to "
            "find out whether a breakdown exists before asking for its figures."
        ),
        permissions=INVESTIGATION_READ,
        parameters={
            "type": "object",
            "properties": {"limit": _LIMIT_ARG},
            "required": [],
        },
        handler=list_stored_contribution_analyses,
    ),
)
