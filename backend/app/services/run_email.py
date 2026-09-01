"""The post-run summary mail, composed from what the run already stored.

**Why this reads storage rather than recomputing anything.** The mail and the
Results screen must agree, and the only way to guarantee that is to give them one
source. So this module runs no query against a company's data, applies no
threshold and forms no verdict: it loads the stored :class:`DetectionRun` rows for
one Agent Run and renders them, and for the narrative half it calls the same
``build_result_explanation`` the screen calls. If the mail and the screen ever
disagree it is a bug in one renderer, not two analyses drifting apart.

**Duplicate prevention.** Sending is keyed on an audit row for the Agent Run
(``RUN_SUMMARY_EMAILED``), so:

* a run that completes sends once;
* reopening that date replays stored results and sends nothing, because the guard
  is checked before anything is composed;
* an authorised re-run is a *new* ``AgentRun`` row, so it sends its own summary —
  which is the point of authorising it — and the original stays untouched.

The guard is a stored row rather than an in-process flag because a second worker,
a retried request and a restarted process must all reach the same answer.

**A failure here never fails the run.** The work is finished and persisted before
this is called. A missing mail host, a refused connection or a rejected recipient
is reported as the state of the summary and recorded, and the request that
triggered the run still returns its results.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import AccessContext
from app.models.detection import AgentRun, DetectionRun
from app.models.observability import AuditLog
from app.notifications import EmailMessage, build_email_provider, load_email_config
from app.notifications.provider import EmailProvider
from app.services import audit
from app.services.explanation import (
    build_result_explanation,
    format_pct,
    format_signed,
    format_value,
    kpi_label,
)

#: Sections carried into the mail, in this order. A summary that reprinted the
#: whole explanation would be a wall of text nobody reads on a phone; these four
#: are the chain requirement asks for -- what happened, who accounts for it, how
#: much weight it carries, and what to do next.
_MAIL_SECTIONS: tuple[str, ...] = (
    "TOP CONTRIBUTORS",
    "CONFIDENCE LEVEL",
    "RECOMMENDED NEXT STEP",
)

_RULE = "-" * 68


def already_sent(session: Session, company_id: str, agent_run_id: str) -> bool:
    """Whether this Agent Run's summary has already gone out."""

    return (
        session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.company_id == company_id,
                AuditLog.action == audit.AuditAction.RUN_SUMMARY_EMAILED,
                AuditLog.resource_id == agent_run_id,
                AuditLog.outcome == "SUCCESS",
            )
            .limit(1)
        )
        is not None
    )


def _result_block(session: Session, access: AccessContext, run: DetectionRun) -> str:
    """One KPI's stored result, as text.

    Every figure is a stored column rendered through the same formatter the
    explanation service uses, so the mail cannot round a number differently from
    the screen.
    """

    lines = [
        f"{kpi_label(run.kpi_name)} — {run.target_date.isoformat()}",
        f"  Status      : {run.status}",
        f"  Actual      : {format_value(run.actual_value, run.unit, run.currency)}",
        f"  Expected    : {format_value(run.expected_value, run.unit, run.currency)}",
        f"  Deviation   : {format_signed(run.deviation_absolute, run.unit, run.currency)}"
        f" ({format_pct(run.deviation_pct)})",
    ]

    explanation = build_result_explanation(session, access, run)
    for heading in _MAIL_SECTIONS:
        body = (explanation.sections.get(heading) or "").strip()
        if body:
            lines.append(f"  {heading.title()}:")
            lines.append(f"    {body}")
    return "\n".join(lines)


def compose_run_summary(
    session: Session, access: AccessContext, agent_run: AgentRun
) -> EmailMessage | None:
    """The mail for one completed Agent Run, or ``None`` if it evaluated nothing.

    Public because it is worth testing without a mail server: the composition is
    where a summary could go wrong, and it is pure with respect to the transport.
    """

    runs = list(
        session.scalars(
            select(DetectionRun)
            .where(
                DetectionRun.company_id == access.company.id,
                DetectionRun.agent_run_id == agent_run.id,
            )
            .order_by(DetectionRun.status, DetectionRun.kpi_name)
        )
    )
    if not runs:
        return None

    config = load_email_config()
    date_label = agent_run.target_date.isoformat()
    abnormal = [run for run in runs if run.status == "ABNORMAL"]
    prefix = f"{config.subject_prefix} " if config.subject_prefix else ""
    subject = (
        f"{prefix}{access.company.company_name} — {date_label} — "
        f"{len(abnormal)} of {len(runs)} KPI(s) outside tolerance"
    )

    header = [
        f"KPI results for {access.company.company_name} on {date_label}.",
        "",
        f"Evaluated : {len(runs)}",
        f"Abnormal  : {len(abnormal)}",
        f"Normal    : {sum(1 for run in runs if run.status == 'NORMAL')}",
        f"Not judgeable : {sum(1 for run in runs if run.status == 'LOW_CONFIDENCE')}",
    ]
    if agent_run.error_count:
        header.append(f"Skipped   : {agent_run.error_count}")

    blocks = [_result_block(session, access, run) for run in runs]
    footer = [
        _RULE,
        "Every figure above is the value this platform stored for the run; nothing "
        "was recomputed to write this message.",
        "Contribution is not causation: the shares report what was measured, not "
        "why the movement happened.",
        f"Prepared for {access.user.email} from Agent Run {agent_run.id}.",
    ]

    body = "\n".join([*header, "", _RULE, "", f"\n\n{_RULE}\n\n".join(blocks), "", *footer])
    return EmailMessage(subject=subject, body=body, recipients=config.recipients)


def send_run_summary(
    session: Session,
    access: AccessContext,
    agent_run: AgentRun,
    *,
    provider: EmailProvider | None = None,
) -> dict[str, object]:
    """Send the summary for a completed Agent Run, at most once.

    Returns the state of the summary rather than raising, and records what it did.
    The ``provider`` argument exists so a test can substitute a transport; the
    default is whatever the deployment configured, resolved through the single
    dispatch point in ``app.notifications``.
    """

    if already_sent(session, access.company.id, agent_run.id):
        return {
            "sent": False,
            "reason": "A summary for this Agent Run has already been sent.",
            "duplicate": True,
        }

    transport = provider or build_email_provider()
    message = compose_run_summary(session, access, agent_run)
    if message is None:
        return {
            "sent": False,
            "reason": "The run stored no results, so there is nothing to summarise.",
            "duplicate": False,
        }

    outcome = transport.send(message)
    # Recorded either way. A summary that could not be sent is exactly the thing an
    # operator needs to find later, and a row that only appears on success would
    # make a silently unconfigured deployment indistinguishable from a delivered one.
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.RUN_SUMMARY_EMAILED,
        resource_type="agent_run",
        resource_id=agent_run.id,
        resource_label=agent_run.target_date.isoformat(),
        summary=(
            f"Run summary for {agent_run.target_date.isoformat()} sent to "
            f"{outcome.recipient_count} recipient(s)."
            if outcome.sent
            else f"Run summary for {agent_run.target_date.isoformat()} was not sent."
        ),
        # No recipient addresses and no message body: a mailing list is personal
        # data, and the body is reproducible from the stored results at any time.
        details={"subject": message.subject, **outcome.as_dict()},
        outcome="SUCCESS" if outcome.sent else "FAILURE",
    )
    return {**outcome.as_dict(), "duplicate": False}
