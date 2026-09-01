"""Phase A smoke checks: exercise the new backend contracts on real dev data.

Not a test suite -- the pytest suite is that. This is the "does it actually work
against the rows this prototype really has" pass, run because a contract that only
type-checks is a contract nobody has used.
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import select

from app.api.v1.detection import _stored_result, explain_result_endpoint
from app.api.v1.investigation import (
    create_finding,
    delete_finding,
    explain_investigation_node,
    list_findings,
    update_finding,
)
from app.api.v1.monitoring import monitoring_dashboard
from app.core.database import SessionLocal
from app.core.deps import resolve_access_context
from app.models.detection import ContributionRun
from app.models.tenant import Company, CompanyUser, Role, User
from app.schemas import FindingCreate, FindingUpdate, NodeExplainRequest, ResultExplainRequest


class FakeRequest:
    """Enough of a Request for audit + telemetry, and nothing more."""

    def __init__(self) -> None:
        self.state = type("S", (), {"request_id": "smoke-phase-a"})()
        self.headers: dict[str, str] = {}
        self.client = None


def rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def access_for_role(session, company, role_name: str | None = None):
    stmt = select(CompanyUser).where(
        CompanyUser.company_id == company.id, CompanyUser.status == "ACTIVE"
    )
    for link in session.scalars(stmt):
        role = session.get(Role, link.role_id)
        if role_name is None or role.role_key == role_name:
            user = session.get(User, link.user_id)
            return resolve_access_context(session, user, company), user, role
    return None, None, None


def main() -> int:
    session = SessionLocal()
    request = FakeRequest()
    company = session.scalars(select(Company)).first()
    access, user, role = access_for_role(session, company)
    print(f"company={company.company_name}  user={user.email}  role={role.role_key}")

    # ---------------------------------------------------------------- monitoring
    rule("GET /monitoring")
    view = monitoring_dashboard(company.id, session, access=access, window_days=730)
    d = view.model_dump()
    print(f"window {d['window_days']}d  {d['window_from']} -> {d['window_to']}")
    print(f"last_evaluated_at: {d['last_evaluated_at']}")
    print("counts:", json.dumps(d["counts"], default=str))
    total = (
        d["counts"]["normal"]
        + d["counts"]["abnormal"]
        + d["counts"]["low_confidence"]
        + d["counts"]["unrecognised"]
    )
    print(f"verdicts sum to {total}, evaluated={d['counts']['evaluated']}  -> "
          f"{'OK' if total == d['counts']['evaluated'] else 'MISMATCH'}")
    for k in d["kpis"]:
        print(f"  KPI {k['kpi_key']:<14} {k['lifecycle_status']:<10} v{k['active_version']} "
              f"| latest {k['latest_status']} {k['latest_target_date']} "
              f"| in window {k['evaluated_in_window']}")
    print("biggest movements:")
    for m in d["biggest_movements"]:
        print(f"  {m['kpi_key']:<14} {m['target_date']} {m['status']:<15} "
              f"pct={m['deviation_pct']} analysed={m['has_contribution']} "
              f"open={m['open_findings']}")
    print(f"recent_abnormal={len(d['recent_abnormal'])}  recent_runs={len(d['recent_runs'])}")
    print(f"findings O/P/R = {d['findings_open']}/{d['findings_in_progress']}/{d['findings_resolved']}")

    # Pick a real analysed movement to explain and annotate.
    contribution = session.scalars(
        select(ContributionRun)
        .where(
            ContributionRun.company_id == company.id,
            ContributionRun.detection_run_id.is_not(None),
            ContributionRun.dimension.is_not(None),
        )
        .order_by(ContributionRun.executed_at.desc())
        .limit(1)
    ).first()
    if contribution is None:
        print("no stored contribution run -- cannot smoke the node path")
        return 1
    kpi_key = contribution.kpi_key
    target = contribution.target_date
    dimension = contribution.dimension
    leader = (contribution.contributors or [{}])[0]
    entity = leader.get("entity")
    print(f"\nchosen node: {kpi_key} {target} {dimension}={entity}")

    # ------------------------------------------------------------ result explain
    rule("POST /results/explain")
    run = _stored_result(
        session,
        access,
        __import__("app.api.v1.detection", fromlist=["x"])._resolve_version(
            session, access, kpi_key
        ),
        target,
    )
    print(f"run {run.id[:8]} status={run.status} actual={run.actual_value} "
          f"expected={run.expected_value} z={run.modified_z_score}")
    body = asyncio.run(
        explain_result_endpoint(
            ResultExplainRequest(kpi_id=kpi_key, target_date=target, use_model=False),
            session,
            request,
            access=access,
        )
    )
    ex = body["explanation"]
    print(f"subject={ex['subject']!r} scope={ex['scope']!r} "
          f"confidence={ex['confidence']['level']} model_written={ex['model_written']} "
          f"citations={len(ex['citations'])} facts={'yes' if ex.get('facts') else 'no'}")
    for section in ex["sections"]:
        print(f"\n-- {section['heading']}\n{section['body']}")

    # -------------------------------------------------------------- node explain
    rule("POST /investigation/explain")
    body = asyncio.run(
        explain_investigation_node(
            company.id,
            NodeExplainRequest(
                kpi_id=kpi_key,
                target_date=target,
                dimension=dimension,
                entity=entity,
                path=[],
                use_model=False,
            ),
            request,
            session,
            access=access,
        )
    )
    ex = body["explanation"]
    print(f"subject={ex['subject']!r} scope={ex['scope']!r} "
          f"confidence={ex['confidence']['level']} sections={len(ex['sections'])}")
    for section in ex["sections"]:
        print(f"\n-- {section['heading']}\n{section['body']}")

    # ------------------------------------------------------------------ findings
    rule("findings CRUD")
    created = create_finding(
        company.id,
        FindingCreate(
            kpi_id=kpi_key,
            target_date=target,
            title="Smoke check: verify the North uplift against the promo calendar",
            note="Recorded by the Phase A smoke script.",
            status="OPEN",
            dimension=dimension,
            entity=entity,
            path=[],
        ),
        request,
        session,
        access=access,
    )["finding"]
    print(f"created  {created['id'][:8]} status={created['status']} "
          f"scope={created['scope_label']!r} run={str(created['detection_run_id'])[:8]} "
          f"resolved_at={created['resolved_at']}")

    listed = list_findings(company.id, session, access=access, kpi_id=kpi_key)
    print(f"listed   {len(listed['findings'])} for {kpi_key}  counts={listed['counts']}")

    moved = update_finding(
        company.id,
        created["id"],
        FindingUpdate(status="IN_PROGRESS", note="Pulled the promo calendar."),
        request,
        session,
        access=access,
    )["finding"]
    print(f"progress status={moved['status']} resolved_at={moved['resolved_at']}")

    resolved = update_finding(
        company.id,
        created["id"],
        FindingUpdate(status="RESOLVED"),
        request,
        session,
        access=access,
    )["finding"]
    print(f"resolved status={resolved['status']} resolved_at={resolved['resolved_at']}")

    reopened = update_finding(
        company.id,
        created["id"],
        FindingUpdate(status="OPEN"),
        request,
        session,
        access=access,
    )["finding"]
    print(f"reopened status={reopened['status']} resolved_at={reopened['resolved_at']} "
          f"-> {'cleared OK' if reopened['resolved_at'] is None else 'STALE TIMESTAMP'}")

    # Negative checks.
    for label, kwargs in (
        ("bad status", {"status": "CLOSED"}),
    ):
        try:
            update_finding(
                company.id, created["id"], FindingUpdate(**kwargs), request, session,
                access=access,
            )
            print(f"{label}: ACCEPTED -- should not have been")
        except Exception as exc:  # noqa: BLE001
            print(f"{label}: refused -> {type(exc).__name__}: {exc}")

    try:
        create_finding(
            company.id,
            FindingCreate(
                kpi_id=kpi_key, target_date=target, title="entity with no dimension",
                entity="North", path=[],
            ),
            request, session, access=access,
        )
        print("entity without dimension: ACCEPTED -- should not have been")
    except Exception as exc:  # noqa: BLE001
        print(f"entity without dimension: refused -> {type(exc).__name__}: {exc}")

    try:
        create_finding(
            company.id,
            FindingCreate(
                kpi_id=kpi_key, target_date=target, title="unapproved dimension",
                dimension="not_a_dimension", path=[],
            ),
            request, session, access=access,
        )
        print("unapproved dimension: ACCEPTED -- should not have been")
    except Exception as exc:  # noqa: BLE001
        print(f"unapproved dimension: refused -> {type(exc).__name__}: {exc}")

    print("deleted:", delete_finding(company.id, created["id"], request, session, access=access))

    session.close()
    print("\nPhase A smoke: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
