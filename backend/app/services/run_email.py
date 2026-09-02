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

**Who it goes to is read from the membership table, never from a literal.** The
recipients are this company's own registered users, resolved at send time from
``company_users`` — so adding an analyst to a company adds them to its summaries,
and removing them removes them, with no environment change and no address written
into this codebase. ``EMAIL_RECIPIENTS`` is the fallback for a company that has no
entitled member, which is the only case where a deployment-wide list is the right
answer. See :func:`resolve_recipients` for what "entitled" means and why.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import AccessContext
from app.models.base import MembershipStatus
from app.models.detection import AgentRun, DetectionRun
from app.models.observability import AuditLog
from app.models.tenant import CompanyUser, Permission, Role, RolePermission, User
from app.notifications import EmailConfig, EmailMessage, build_email_provider, load_email_config
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
#: are the chain the requirement asks for -- who accounts for the movement, what
#: the platform can say about it, what to do next, and how much weight to put on
#: all three.
_MAIL_SECTIONS: tuple[str, ...] = (
    "TOP CONTRIBUTORS",
    "WHAT HAPPENED",
    "RECOMMENDED NEXT STEP",
    "CONFIDENCE LEVEL",
)

#: What a member must hold to be sent a summary.
#:
#: Both, not either. ``analytics.read`` is the entitlement to a stored verdict and
#: its figures, and ``investigation.read`` is the entitlement to apportionment --
#: and the body carries a TOP CONTRIBUTORS section whenever the run has a stored
#: breakdown. One run produces one body, so the alternative to requiring both would
#: be either mailing apportionment to someone not entitled to it, or composing two
#: different versions of one answer and hoping nobody compares them. Requiring both
#: makes the audience narrower and the guarantee simple: nothing in this mail is
#: something its reader could not have opened in the application themselves.
REQUIRED_PERMISSIONS: tuple[str, ...] = ("analytics.read", "investigation.read")

_RULE = "-" * 68


@dataclass(frozen=True, slots=True)
class RecipientSet:
    """Who a summary is addressed to, and how they were found.

    ``source`` is recorded in the audit trail. The addresses are not: it answers
    "was this a company's own users or the deployment's fallback list" -- which is
    the question an operator asks when a summary reached the wrong people -- without
    writing a mailing list into every audit row.
    """

    addresses: tuple[str, ...]
    source: str
    reason: str | None = None


def _entitled_member_emails(session: Session, company_id: str) -> tuple[str, ...]:
    """Active members of this company whose role grants every required permission.

    One query, grouped and counted, rather than a permission check per member: the
    ``HAVING`` clause is what makes "holds all of them" a property of the row set
    instead of something this function assembles in Python and could get wrong.
    Ordered by address so a company's summaries address the same list in the same
    order every time.
    """

    rows = session.execute(
        select(User.email)
        .join(CompanyUser, CompanyUser.user_id == User.id)
        .join(Role, Role.id == CompanyUser.role_id)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            CompanyUser.company_id == company_id,
            CompanyUser.status == MembershipStatus.ACTIVE,
            User.is_active.is_(True),
            Permission.key.in_(REQUIRED_PERMISSIONS),
        )
        .group_by(User.id, User.email)
        .having(func.count(func.distinct(Permission.key)) == len(REQUIRED_PERMISSIONS))
        .order_by(User.email)
    ).all()
    return tuple(str(row[0]) for row in rows)


def resolve_recipients(
    session: Session, company_id: str, config: EmailConfig | None = None
) -> RecipientSet:
    """The addresses for one company's run summary.

    Registered users first, the configured list second, nothing third -- and the
    third case is a state with a reason, not an exception, for the same reason an
    unconfigured mail host is: the run has already finished and been stored.

    The fallback is not permission-checked, and that is deliberate. It is an
    operator's own list in that operator's own environment file; the platform has no
    membership row to check it against, and silently dropping addresses an operator
    configured would be harder to diagnose than honouring them.
    """

    resolved = config or load_email_config()
    members = _entitled_member_emails(session, company_id)
    if members:
        return RecipientSet(addresses=members, source="REGISTERED_USERS")
    if resolved.recipients:
        return RecipientSet(addresses=resolved.recipients, source="CONFIGURED_FALLBACK")
    return RecipientSet(
        addresses=(),
        source="NONE",
        reason=(
            "No active member of this company holds "
            f"{' and '.join(REQUIRED_PERMISSIONS)}, and no fallback list is "
            "configured (EMAIL_RECIPIENTS)."
        ),
    )


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
    session: Session,
    access: AccessContext,
    agent_run: AgentRun,
    *,
    recipients: RecipientSet | None = None,
) -> EmailMessage | None:
    """The mail for one completed Agent Run, or ``None`` if it evaluated nothing.

    Public because it is worth testing without a mail server: the composition is
    where a summary could go wrong, and it is pure with respect to the transport.

    ``recipients`` is accepted so the caller that audits the send can resolve the
    list once and record which source it came from. Left out, this resolves its own.
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
    addressed = recipients or resolve_recipients(session, access.company.id, config)
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
        "was recomputed to write this message, and no language model produced or "
        "adjusted any number in it.",
        "Contribution is not causation: the shares report what was measured, not "
        "why the movement happened.",
        # Whose entitlement the wording reflects, and why the recipients are who they
        # are. One run produces one body, so a reader is owed the fact that it was
        # assembled under the permissions of the person who ran the detection.
        f"Assembled from Agent Run {agent_run.id} under the entitlement of "
        f"{access.user.email}, who ran this detection.",
        (
            "Addressed to the registered users of this company who may see stored "
            "results and contribution analysis."
            if addressed.source == "REGISTERED_USERS"
            else "Addressed to the recipient list this deployment configures, because "
            "no member of this company currently holds both entitlements."
        ),
    ]

    body = "\n".join([*header, "", _RULE, "", f"\n\n{_RULE}\n\n".join(blocks), "", *footer])
    return EmailMessage(subject=subject, body=body, recipients=addressed.addresses)


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

    config = load_email_config()
    transport = provider or build_email_provider(config)
    addressed = resolve_recipients(session, access.company.id, config)
    # Nobody to address is its own state, and it is checked before composing: there
    # is no point assembling an explanation per KPI for a message that cannot leave.
    if not addressed.addresses:
        # Both facts, when there are two. A deployment that is switched off *and* has
        # nobody to address should not have to fix one problem to discover the other.
        reason = addressed.reason or "There is nobody to send this summary to."
        if config.unavailable_reason:
            reason = f"{config.unavailable_reason} {reason}"
        return {
            "sent": False,
            "reason": reason,
            "recipient_source": addressed.source,
            "recipient_count": 0,
            "duplicate": False,
        }

    message = compose_run_summary(session, access, agent_run, recipients=addressed)
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
        # Where the list came from is recorded, because "the wrong people received
        # this" is answered by the source, not by the addresses.
        details={
            "subject": message.subject,
            "recipient_source": addressed.source,
            **outcome.as_dict(),
        },
        outcome="SUCCESS" if outcome.sent else "FAILURE",
    )
    return {**outcome.as_dict(), "recipient_source": addressed.source, "duplicate": False}
