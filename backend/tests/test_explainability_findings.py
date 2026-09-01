"""Proof that an explanation is a reading of stored evidence, and nothing more.

Three surfaces are under test here, and they share one claim: **every figure a
reader is shown was already measured and stored, by a component that was allowed to
measure it.** The explanation layer computes no KPI, reads no data source, and adds
no number of its own. So the assertions below are mostly of one shape -- take a
figure out of the prose, and find it in the row the engine wrote.

What each part proves:

*Monitoring* -- the dashboard counts stored rows and says so. Its verdict tiles sum
to the number of evaluations it counted, including any verdict from an older schema
that it refuses to fold into a current one; its note does not claim a scheduler this
version does not have; and one company's dashboard never sees another's runs.

*Result explanation* -- six sections, in a fixed order, whose numbers are the stored
run's own columns. The two flagging tests are reported separately, because a
movement can breach a business tolerance while sitting inside normal statistical
variation, and letting either imply the other would misdescribe the verdict. The
``facts`` block that lets a reader audit the prose is gated on the same permission
as the detection API's ``evidence``.

*Node explanation* -- the same discipline applied to one part of a movement, with
its share taken from the stored breakdown rather than recomputed. A reader scoped to
one region cannot obtain an explanation of another, whether they clicked it or typed
it.

*Findings* -- a person's written conclusion, stored beside the measurement, with the
anchor validated as strictly as an analysis of the same anchor. Statuses are the
three the platform defines; ``resolved_at`` is written when a finding is resolved and
cleared when it is reopened, because a resolution timestamp on an open investigation
would be an event that never happened.

And throughout: no causal language. The engine measures shares of a movement. A
share is a size, and this suite fails if the prose upgrades one to a cause.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import create_app
from app.models.detection import DetectionRun
from app.models.investigation import InvestigationFinding
from app.models.observability import AuditLog
from app.services.explanation import NODE_SECTIONS, RESULT_SECTIONS
from tests.conftest import login
from tests.fixture_generalization import COMPANY_A_TARGET, build_company_a_source
from tests.test_detection_generalization import (
    approve_bucket_config,
    provision,
    run_detection,
)
from tests.test_investigation_contribution import register_kpi_with_dimensions

PASSWORD = "Explainability-Tests-2026"

#: Words that turn a measured share into a claim about cause. The platform measures
#: where a movement sits; nothing in it establishes why. A section that reaches for
#: one of these is making a finding no computation here has made.
CAUSAL_WORDS = (
    "caused",
    "caused by",
    "drove",
    "driven by",
    "led to",
    "resulted in",
    "responsible for",
    "blame",
    "root cause",
)

#: Phrases that would claim continuous monitoring. There is no scheduler in this
#: version, so a dashboard using one of these would be describing a feature that
#: does not exist.
SCHEDULER_CLAIMS = ("continuously", "around the clock", "24/7", "every hour", "automatically every")


def numbers_in(text: str) -> set[str]:
    """Every formatted figure in a block of prose, commas stripped."""

    return {match.replace(",", "") for match in re.findall(r"-?\d[\d,]*\.?\d*", text)}


# ---------------------------------------------------------------------------
# One tenant, provisioned the way the platform provisions one
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def module_client() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(scope="module")
def tenant(module_client, tmp_path_factory) -> dict:
    """A company with an approved KPI, an approved policy, and a stored movement.

    Plus a second company, so isolation is proven by a real neighbour rather than
    by a missing row; a regionally scoped reader; and a viewer who may read a
    result but may not investigate one.
    """

    seeded = build_company_a_source(
        tmp_path_factory.mktemp("explainability") / "aurora_explain.db"
    )
    admin, base, tables = provision(
        module_client,
        email="admin@aurora-explain.example.com",
        company_name="Aurora Retail Explain",
        source_name="Aurora Commerce",
        source_path=seeded["path"],
        scope={"orders": "order_date"},
    )
    revenue_id = register_kpi_with_dimensions(
        admin, base, source_table_id=tables["orders"]["id"]
    )
    approve_bucket_config(
        admin,
        base,
        config_key="aurora-explain-weekly",
        name="Aurora weekly trading pattern",
        buckets={
            "same_day_of_week": {"enabled": True, "days": ["FRI"]},
            "yoy_period": {"enabled": True},
        },
    )
    detection = run_detection(admin, base, revenue_id, COMPANY_A_TARGET)
    assert detection["result"]["status"] == "ABNORMAL", (
        "this suite reads an explanation of a flagged movement, so the seeded "
        "Friday collapse must still flag"
    )
    company_id = base.rsplit("/", 1)[-1]

    scoped_created = admin.post(
        f"{base}/members",
        json={
            "email": "south@aurora-explain.example.com",
            "full_name": "Sana South",
            "password": PASSWORD,
            "role_key": "REGIONAL_MANAGER",
            "row_scope": {"region": ["South"]},
        },
    )
    assert scoped_created.status_code == 201, scoped_created.text

    viewer_created = admin.post(
        f"{base}/members",
        json={
            "email": "viewer@aurora-explain.example.com",
            "full_name": "Vik Viewer",
            "password": PASSWORD,
            "role_key": "VIEWER",
        },
    )
    assert viewer_created.status_code == 201, viewer_created.text

    # The neighbour. Provisioned but deliberately never given a KPI or a run: what
    # is asserted about it is that it sees none of Aurora's.
    neighbour_admin, neighbour_base, _ = provision(
        module_client,
        email="admin@borealis-explain.example.com",
        company_name="Borealis Explain",
        source_name="Borealis Ledger",
        source_path=seeded["path"],
        scope={"orders": "order_date"},
    )

    return {
        "admin": admin,
        "base": base,
        "company_id": company_id,
        "revenue_id": revenue_id,
        "detection": detection,
        "scoped": login(
            module_client, "south@aurora-explain.example.com", PASSWORD, company_id
        ),
        "viewer": login(
            module_client, "viewer@aurora-explain.example.com", PASSWORD, company_id
        ),
        "neighbour": neighbour_admin,
        "neighbour_base": neighbour_base,
    }


@pytest.fixture(scope="module")
def stored_run(tenant) -> dict:
    """The stored detection row this suite's prose is checked against."""

    session = SessionLocal()
    try:
        run = session.scalars(
            select_run(tenant["company_id"], COMPANY_A_TARGET)
        ).first()
        assert run is not None
        return {
            column.name: getattr(run, column.name)
            for column in DetectionRun.__table__.columns
        }
    finally:
        session.close()


def select_run(company_id: str, target: date):
    from sqlalchemy import select

    return (
        select(DetectionRun)
        .where(
            DetectionRun.company_id == company_id,
            DetectionRun.target_date == target,
        )
        .order_by(DetectionRun.executed_at.desc())
        .limit(1)
    )


def explain_result(actor, base: str, **body) -> dict:
    payload = {
        "kpi_id": "revenue",
        "target_date": COMPANY_A_TARGET.isoformat(),
        "use_model": False,
        **body,
    }
    response = actor.post(f"{base}/results/explain", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["explanation"]


def explain_node(actor, base: str, **body) -> dict:
    payload = {
        "kpi_id": "revenue",
        "target_date": COMPANY_A_TARGET.isoformat(),
        "use_model": False,
        **body,
    }
    response = actor.post(f"{base}/investigation/explain", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["explanation"]


def sections_of(explanation: dict) -> dict[str, str]:
    return {item["heading"]: item["body"] for item in explanation["sections"]}


# ---------------------------------------------------------------------------
# Monitoring dashboard
# ---------------------------------------------------------------------------
def test_monitoring_counts_only_stored_evaluations(tenant) -> None:
    """Every tile is a count of rows, and the verdict tiles account for all of them."""

    response = tenant["admin"].get(f"{tenant['base']}/monitoring")
    assert response.status_code == 200, response.text
    body = response.json()
    counts = body["counts"]

    assert counts["evaluated"] == 1, "one detection has been run for this tenant"
    assert (
        counts["normal"] + counts["abnormal"] + counts["low_confidence"] + counts["unrecognised"]
        == counts["evaluated"]
    ), "a verdict tally that does not sum to the evaluations is misreporting one of them"
    assert counts["abnormal"] == 1
    assert counts["kpis_monitored"] == 1
    assert counts["not_evaluated"] == 0
    assert counts["unrecognised"] == 0
    assert counts["unrecognised_statuses"] == []

    assert body["window_from"] == COMPANY_A_TARGET.isoformat()
    assert body["window_to"] == COMPANY_A_TARGET.isoformat()
    assert body["last_evaluated_at"] is not None


def test_monitoring_does_not_claim_a_scheduler(tenant) -> None:
    """The note says detection is triggered, because that is what happens."""

    body = tenant["admin"].get(f"{tenant['base']}/monitoring").json()
    note = body["monitoring_note"].lower()
    assert "scheduler" in note
    for claim in SCHEDULER_CLAIMS:
        assert claim not in note, f"the dashboard implies continuous monitoring: {claim!r}"


def test_monitoring_surfaces_the_abnormal_movement(tenant) -> None:
    """The flagged run appears where a reader would click through from."""

    body = tenant["admin"].get(f"{tenant['base']}/monitoring").json()
    abnormal = body["recent_abnormal"]
    assert len(abnormal) == 1
    entry = abnormal[0]
    assert entry["kpi_key"] == "revenue"
    assert entry["target_date"] == COMPANY_A_TARGET.isoformat()
    assert entry["status"] == "ABNORMAL"
    # The id a Result page is opened with, and the id an investigation anchors to.
    assert entry["detection_run_id"]
    assert entry["kpi_id"] == tenant["revenue_id"]
    assert entry["has_contribution"] is False, "nothing has analysed this movement yet"
    assert entry["open_findings"] == 0

    biggest = body["biggest_movements"]
    assert [item["kpi_key"] for item in biggest] == ["revenue"]


def test_monitoring_withholds_the_investigation_layer_from_a_viewer(tenant) -> None:
    """analytics.read buys the verdicts, not other people's investigations.

    A VIEWER holds ``analytics.read`` without ``investigation.read``, so the
    dashboard owes them the tally of what was evaluated and how it moved -- and
    nothing at all about who has been investigating it. The distinction this test
    pins is between *null* and *zero*: a zero would tell the viewer that no finding
    exists, which is a disclosure about the investigation layer and, when a finding
    does exist, simply false.
    """

    body = tenant["viewer"].get(f"{tenant['base']}/monitoring").json()

    # The verdicts still arrive -- withholding is targeted, not a blanket refusal.
    assert body["counts"]["evaluated"] == 1
    assert body["counts"]["abnormal"] == 1
    assert body["recent_abnormal"], "a viewer may see that a KPI moved abnormally"

    assert body["findings_open"] is None
    assert body["findings_in_progress"] is None
    assert body["findings_resolved"] is None
    assert body["recent_findings"] == []
    for entry in body["recent_abnormal"] + body["biggest_movements"]:
        assert entry["open_findings"] is None
        assert entry["has_contribution"] is None, (
            "whether somebody has analysed a movement is investigation information"
        )

    # Meanwhile the same dashboard for a reader who *is* entitled says so plainly.
    entitled = tenant["admin"].get(f"{tenant['base']}/monitoring").json()
    assert entitled["findings_open"] is not None
    assert entitled["recent_abnormal"][0]["has_contribution"] is not None


def test_monitoring_is_company_scoped(tenant) -> None:
    """A neighbour with no runs sees no runs -- not Aurora's."""

    body = tenant["neighbour"].get(f"{tenant['neighbour_base']}/monitoring").json()
    assert body["counts"]["evaluated"] == 0
    assert body["counts"]["abnormal"] == 0
    assert body["recent_abnormal"] == []
    assert body["recent_runs"] == []
    assert body["last_evaluated_at"] is None
    assert body["window_from"] is None and body["window_to"] is None

    # And the path parameter is not the authorisation boundary: asking with
    # Aurora's id on a Borealis token must not return Aurora's dashboard.
    crossed = tenant["neighbour"].get(f"/api/v1/companies/{tenant['company_id']}/monitoring")
    assert crossed.status_code in (403, 404), crossed.text


def test_monitoring_kpi_not_evaluated_is_reported_as_such(tenant) -> None:
    """A registered KPI with no run is absent from the verdicts, and counted."""

    # A second KPI, approved but never evaluated.
    created = tenant["admin"].post(
        f"{tenant['base']}/kpis",
        json={
            "kpi_key": "order_count",
            "name": "Order Count",
            "business_definition": "Orders recognised on the order date.",
            "formula_expression": "COUNT(orders.order_id)",
            "source_table_id": tenant["admin"]
            .get(f"{tenant['base']}/tables")
            .json()[0]["id"],
            "time_field": "order_date",
            "time_grain": "DAY",
            "unit": "count",
        },
    )
    assert created.status_code == 201, created.text

    body = tenant["admin"].get(f"{tenant['base']}/monitoring").json()
    assert body["counts"]["kpis_monitored"] == 2
    assert body["counts"]["not_evaluated"] == 1
    assert body["counts"]["evaluated"] == 1, "registering a KPI evaluates nothing"

    entry = next(k for k in body["kpis"] if k["kpi_key"] == "order_count")
    assert entry["latest_status"] is None
    assert entry["latest_target_date"] is None
    assert entry["evaluated_in_window"] == 0


# ---------------------------------------------------------------------------
# Result explanation
# ---------------------------------------------------------------------------
def test_result_explanation_has_the_six_sections_in_order(tenant) -> None:
    explanation = explain_result(tenant["admin"], tenant["base"])
    assert tuple(explanation["order"]) == RESULT_SECTIONS
    assert tuple(item["heading"] for item in explanation["sections"]) == RESULT_SECTIONS
    for item in explanation["sections"]:
        assert item["body"].strip(), f"{item['heading']} is empty"


def test_result_explanation_reports_the_stored_figures(tenant, stored_run) -> None:
    """The numbers in the prose are the stored run's own columns."""

    explanation = explain_result(tenant["admin"], tenant["base"])
    sections = sections_of(explanation)
    facts = explanation["facts"]

    assert facts["actual"] == pytest.approx(stored_run["actual_value"])
    assert facts["expected"] == pytest.approx(stored_run["expected_value"])
    assert facts["movement_absolute"] == pytest.approx(stored_run["deviation_absolute"])
    assert facts["movement_pct"] == pytest.approx(stored_run["deviation_pct"])
    assert facts["verdict"] == stored_run["status"]
    assert facts["detection_run_id"] == stored_run["id"]

    statistics = facts["statistics"]
    assert statistics["modified_z_score"] == pytest.approx(stored_run["modified_z_score"])
    assert statistics["z_threshold"] == pytest.approx(stored_run["z_threshold"])
    assert statistics["median"] == pytest.approx(stored_run["median_value"])
    assert statistics["mad"] == pytest.approx(stored_run["mad"])
    assert statistics["reference_count"] == stored_run["reference_count"]
    assert statistics["bucket_applied"] == stored_run["bucket_applied"]
    assert statistics["breached_tolerance"] == stored_run["breached_tolerance"]
    assert statistics["statistically_significant"] == stored_run["statistically_significant"]

    # The verdict in the prose is the engine's, not a re-derivation.
    assert stored_run["status"] in sections["WHAT HAPPENED"]

    # And the statistics quoted under WHY IT WAS FLAGGED round to the stored ones.
    flagged = sections["WHY IT WAS FLAGGED"]
    assert f"{abs(stored_run['modified_z_score']):.2f}" in flagged
    assert f"{stored_run['z_threshold']:.2f}" in flagged
    assert str(stored_run["reference_count"]) in flagged


def test_result_explanation_keeps_the_two_tests_separate(tenant, stored_run) -> None:
    """A tolerance breach and a statistical result are reported as two findings.

    This tenant's movement is the case that makes it matter: whichever way the two
    tests land, the prose must state each on its own terms rather than presenting
    one as the reason for the other.
    """

    sections = sections_of(explain_result(tenant["admin"], tenant["base"]))
    flagged = sections["WHY IT WAS FLAGGED"]

    assert "modified z-score" in flagged
    assert "materiality test" in flagged
    if stored_run["statistically_significant"]:
        assert "the statistical test was met" in flagged
    else:
        assert "the statistical test was not met" in flagged
    if stored_run["breached_tolerance"]:
        assert "tolerance was breached" in flagged
    else:
        assert "tolerance was not breached" in flagged


def test_result_explanation_never_claims_a_cause(tenant) -> None:
    explanation = explain_result(tenant["admin"], tenant["base"])
    blob = explanation["text"].lower()
    for word in CAUSAL_WORDS:
        assert word not in blob, f"the explanation claims causation: {word!r}"
    assert "contribution is not causation" in blob


def test_result_explanation_states_its_confidence_and_limits(tenant) -> None:
    explanation = explain_result(tenant["admin"], tenant["base"])
    assert explanation["confidence"]["level"] in {"HIGH", "MEDIUM", "LOW"}
    assert explanation["confidence"]["reasons"], "a confidence level with no reasons is a label"
    assert explanation["limitations"], "every explanation states what it cannot establish"
    assert explanation["model_written"] is False, (
        "LLM_ENABLED is false in this suite, so the prose is the platform's own"
    )
    assert explanation["model"] is None


def test_result_explanation_requires_a_stored_run(tenant) -> None:
    """No measurement, no explanation -- and nothing is computed to fill the gap."""

    never_run = COMPANY_A_TARGET - timedelta(days=3)
    response = tenant["admin"].post(
        f"{tenant['base']}/results/explain",
        json={
            "kpi_id": "revenue",
            "target_date": never_run.isoformat(),
            "use_model": False,
        },
    )
    assert response.status_code == 409, response.text
    assert "no stored evaluation" in response.text.lower()


def test_result_explanation_withholds_facts_without_kpi_read(tenant, monkeypatch) -> None:
    """The audit block answers to the same permission as detection's evidence.

    Proven by a role that has ``analytics.read`` but not ``kpi.read``. No such core
    role exists -- every role that may read a result may read a definition -- so the
    membership's own permission set is narrowed for this assertion rather than the
    claim being left untested.
    """

    from app.core import deps

    original = deps.AccessContext.has

    def without_kpi_read(self, permission: str) -> bool:
        if permission == "kpi.read":
            return False
        return original(self, permission)

    monkeypatch.setattr(deps.AccessContext, "has", without_kpi_read)
    explanation = explain_result(tenant["admin"], tenant["base"])
    assert explanation.get("facts") is None, (
        "a caller who may not read KPI definitions was handed the statistics"
    )
    # The reader still gets every section: the explanation is not the audit trail.
    assert tuple(item["heading"] for item in explanation["sections"]) == RESULT_SECTIONS


def test_viewer_can_read_a_result_but_gets_no_breakdown(tenant) -> None:
    """VIEWER holds ``analytics.read`` and ``kpi.read`` but not ``investigation.read``.

    So the result explains itself, and the contributors section says there is no
    stored breakdown to report rather than quietly omitting the heading.
    """

    explanation = explain_result(tenant["viewer"], tenant["base"])
    sections = sections_of(explanation)
    assert tuple(sections) == RESULT_SECTIONS
    contributors = sections["TOP CONTRIBUTORS"].lower()
    assert "no" in contributors and "breakdown" in contributors
    assert explanation["citations"] == [], "VIEWER has no document.read"


def test_viewer_cannot_explain_an_investigation_node(tenant) -> None:
    response = tenant["viewer"].post(
        f"{tenant['base']}/investigation/explain",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "dimension": "region",
            "entity": "South",
            "use_model": False,
        },
    )
    assert response.status_code == 403, response.text


def test_result_explanation_is_audited(tenant) -> None:
    explain_result(tenant["admin"], tenant["base"])
    session = SessionLocal()
    try:
        from sqlalchemy import select

        rows = list(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.company_id == tenant["company_id"],
                    AuditLog.action == "explainability.result_explained",
                )
            )
        )
        assert rows, "an explanation somebody will act on is not logged"
        latest = rows[-1]
        assert latest.details["kpi_key"] == "revenue"
        assert latest.details["model_written"] is False
        assert latest.details["confidence"] in {"HIGH", "MEDIUM", "LOW"}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Node explanation
# ---------------------------------------------------------------------------
def test_node_explanation_quantifies_from_the_stored_breakdown(tenant) -> None:
    """Its share is the stored breakdown's share, not a fresh calculation."""

    contribution = tenant["admin"].post(
        f"{tenant['base']}/investigation/contribution",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "dimension": "region",
        },
    )
    assert contribution.status_code == 200, contribution.text
    leader = contribution.json()["result"]["contributors"][0]

    explanation = explain_node(
        tenant["admin"],
        tenant["base"],
        dimension="region",
        entity=leader["entity"],
    )
    assert tuple(explanation["order"]) == NODE_SECTIONS
    sections = sections_of(explanation)
    body = sections["CONTRIBUTION TO THE MOVEMENT"]

    # The share, formatted the way the platform formats one.
    assert f"{abs(leader['share_pct']):.1f}" in body.replace("-", "")
    assert leader["label"] in explanation["subject"] or leader["entity"] in explanation["subject"]
    assert "a share is a size, not a cause" in body.lower()


def test_node_explanation_never_claims_a_cause(tenant) -> None:
    explanation = explain_node(
        tenant["admin"], tenant["base"], dimension="region", entity="South"
    )
    blob = explanation["text"].lower()
    for word in CAUSAL_WORDS:
        assert word not in blob, f"the node explanation claims causation: {word!r}"


def test_node_explanation_respects_row_scope(tenant) -> None:
    """A reader scoped to South cannot obtain an explanation of North."""

    own = tenant["scoped"].post(
        f"{tenant['base']}/investigation/explain",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "dimension": "region",
            "entity": "South",
            "use_model": False,
        },
    )
    assert own.status_code == 200, own.text

    other = tenant["scoped"].post(
        f"{tenant['base']}/investigation/explain",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "dimension": "region",
            "entity": "North",
            "use_model": False,
        },
    )
    # 404, not 403, and deliberately so: the platform's scope resolver answers
    # "no region matching 'North' is available to you", which refuses the request
    # without confirming that North exists. Either code is a refusal; this one
    # discloses less. What must not appear either way is a figure.
    assert other.status_code in (403, 404), other.text
    assert "explanation" not in other.json()


def test_node_explanation_refuses_an_unapproved_dimension(tenant) -> None:
    response = tenant["admin"].post(
        f"{tenant['base']}/investigation/explain",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "dimension": "salesperson",
            "use_model": False,
        },
    )
    assert response.status_code in (400, 403, 404), response.text


def test_node_explanation_requires_a_stored_run(tenant) -> None:
    response = tenant["admin"].post(
        f"{tenant['base']}/investigation/explain",
        json={
            "kpi_id": "revenue",
            "target_date": (COMPANY_A_TARGET - timedelta(days=3)).isoformat(),
            "dimension": "region",
            "use_model": False,
        },
    )
    assert response.status_code == 409, response.text


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
def create_finding(actor, base: str, **body) -> dict:
    payload = {
        "kpi_id": "revenue",
        "target_date": COMPANY_A_TARGET.isoformat(),
        "title": "Check the Friday collapse against the promotion calendar",
        **body,
    }
    response = actor.post(f"{base}/investigation/findings", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["finding"]


def test_finding_anchors_to_the_stored_run(tenant) -> None:
    finding = create_finding(
        tenant["admin"], tenant["base"], dimension="region", entity="South"
    )
    assert finding["status"] == "OPEN"
    assert finding["scope_label"] == "region: South"
    assert finding["detection_run_id"], "a finding about a measured movement points at it"
    assert finding["resolved_at"] is None
    assert finding["created_by_email"] == "admin@aurora-explain.example.com"

    listed = tenant["admin"].get(
        f"{tenant['base']}/investigation/findings", params={"kpi_id": "revenue"}
    )
    assert listed.status_code == 200, listed.text
    assert finding["id"] in [item["id"] for item in listed.json()["findings"]]
    assert listed.json()["counts"]["OPEN"] >= 1

    tenant["admin"].delete(f"{tenant['base']}/investigation/findings/{finding['id']}")


def test_finding_status_moves_and_resolved_at_follows(tenant) -> None:
    """``resolved_at`` is written when it becomes true and cleared when it stops."""

    finding = create_finding(tenant["admin"], tenant["base"])
    url = f"{tenant['base']}/investigation/findings/{finding['id']}"

    progressed = tenant["admin"].patch(url, json={"status": "IN_PROGRESS"})
    assert progressed.status_code == 200, progressed.text
    assert progressed.json()["finding"]["resolved_at"] is None

    resolved = tenant["admin"].patch(url, json={"status": "RESOLVED"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["finding"]["resolved_at"] is not None

    reopened = tenant["admin"].patch(url, json={"status": "OPEN"})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["finding"]["resolved_at"] is None, (
        "a reopened investigation kept a resolution timestamp for an event that "
        "is no longer true"
    )

    tenant["admin"].delete(url)


def test_finding_note_persists_across_requests(tenant) -> None:
    """Not frontend state: written down, read back on a separate request."""

    finding = create_finding(
        tenant["admin"],
        tenant["base"],
        note="Promotion ran Thursday, not Friday. Confirmed with the trading team.",
    )
    fetched = tenant["admin"].get(
        f"{tenant['base']}/investigation/findings", params={"kpi_id": "revenue"}
    ).json()
    stored = next(item for item in fetched["findings"] if item["id"] == finding["id"])
    assert stored["note"].startswith("Promotion ran Thursday")

    session = SessionLocal()
    try:
        row = session.get(InvestigationFinding, finding["id"])
        assert row is not None and row.company_id == tenant["company_id"]
    finally:
        session.close()

    tenant["admin"].delete(f"{tenant['base']}/investigation/findings/{finding['id']}")


def test_finding_refuses_an_unknown_status(tenant) -> None:
    response = tenant["admin"].post(
        f"{tenant['base']}/investigation/findings",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "title": "Closed, apparently",
            "status": "CLOSED",
        },
    )
    assert response.status_code in (400, 422), response.text


def test_finding_refuses_an_entity_without_its_dimension(tenant) -> None:
    response = tenant["admin"].post(
        f"{tenant['base']}/investigation/findings",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "title": "An entity with no dimension",
            "entity": "South",
        },
    )
    assert response.status_code in (400, 422), response.text


def test_finding_refuses_an_out_of_scope_entity(tenant) -> None:
    """A note is not a query, but its coordinates still answer to row scope."""

    response = tenant["scoped"].post(
        f"{tenant['base']}/investigation/findings",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "title": "About a region I cannot see",
            "dimension": "region",
            "entity": "North",
        },
    )
    # Refused by the same resolver the drill-down uses, which answers 404 rather
    # than 403 so a refusal does not confirm the entity exists.
    assert response.status_code in (403, 404), response.text

    listed = tenant["scoped"].get(f"{tenant['base']}/investigation/findings").json()
    assert all(item["entity"] != "North" for item in listed["findings"])


def test_findings_are_company_scoped(tenant) -> None:
    finding = create_finding(tenant["admin"], tenant["base"])

    # The neighbour's own list is empty.
    theirs = tenant["neighbour"].get(f"{tenant['neighbour_base']}/investigation/findings")
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["findings"] == []

    # And they cannot reach Aurora's by id, on either company's path.
    for base in (tenant["neighbour_base"], tenant["base"]):
        blocked = tenant["neighbour"].patch(
            f"{base}/investigation/findings/{finding['id']}",
            json={"status": "RESOLVED"},
        )
        assert blocked.status_code in (403, 404), blocked.text

    tenant["admin"].delete(f"{tenant['base']}/investigation/findings/{finding['id']}")


def test_viewer_cannot_write_a_finding(tenant) -> None:
    response = tenant["viewer"].post(
        f"{tenant['base']}/investigation/findings",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "title": "A conclusion I am not entitled to record",
        },
    )
    assert response.status_code == 403, response.text


def test_the_results_list_reads_a_result_dimensionally_only_for_who_may(tenant) -> None:
    """A detection run has no dimension of its own; a recorded finding does.

    So the dimensional reading of a result on the Results screen -- the chips on a
    row, and the Dimension filter -- is investigation data wearing an analytics
    surface. A VIEWER holds ``analytics.read`` without ``investigation.read``, so
    for them the filter values are empty, the rows carry no dimensions, and asking
    for the narrowing is refused rather than quietly ignored: an unnarrowed list
    returned for a narrowing request is the one failure that looks like success.
    """

    finding = create_finding(
        tenant["admin"], tenant["base"], dimension="region", entity="South"
    )
    try:
        base = tenant["base"]

        seen = tenant["admin"].get(f"{base}/results")
        assert seen.status_code == 200, seen.text
        body = seen.json()
        assert "region" in body["options"]["dimensions"]
        marked = [item for item in body["items"] if item["kpi_key"] == "revenue"]
        assert marked, "the seeded movement is missing from the results list"
        assert any("region" in item["dimensions"] for item in marked)
        assert any("South" in item["entities"] for item in marked)

        narrowed = tenant["admin"].get(f"{base}/results", params={"dimension": "region"})
        assert narrowed.status_code == 200, narrowed.text
        rows = narrowed.json()["items"]
        assert rows, "a dimension somebody recorded a finding against returned nothing"
        assert all("region" in row["dimensions"] for row in rows)

        # A dimension nobody has recorded against narrows to nothing rather than
        # falling back to the unnarrowed list.
        absent = tenant["admin"].get(
            f"{base}/results", params={"dimension": "no-such-dimension"}
        )
        assert absent.status_code == 200, absent.text
        assert absent.json()["items"] == []

        restricted = tenant["viewer"].get(f"{base}/results")
        assert restricted.status_code == 200, restricted.text
        withheld = restricted.json()
        assert withheld["options"]["dimensions"] == []
        assert withheld["items"], "a viewer may still read the results themselves"
        assert all("dimensions" not in item for item in withheld["items"])

        refused = tenant["viewer"].get(f"{base}/results", params={"dimension": "region"})
        assert refused.status_code == 403, refused.text
        assert "investigation" in refused.text.lower()
    finally:
        tenant["admin"].delete(f"{tenant['base']}/investigation/findings/{finding['id']}")


def test_finding_lifecycle_is_audited(tenant) -> None:
    """Create, status change and delete each leave a trail; the delete keeps it."""

    finding = create_finding(tenant["admin"], tenant["base"], note="For the audit trail.")
    url = f"{tenant['base']}/investigation/findings/{finding['id']}"
    tenant["admin"].patch(url, json={"status": "RESOLVED"})
    tenant["admin"].delete(url)

    session = SessionLocal()
    try:
        from sqlalchemy import select

        actions = [
            row.action
            for row in session.scalars(
                select(AuditLog).where(
                    AuditLog.company_id == tenant["company_id"],
                    AuditLog.resource_id == finding["id"],
                )
            )
        ]
        assert "investigation.finding_created" in actions
        assert "investigation.finding_status_changed" in actions
        assert "investigation.finding_deleted" in actions
        # The row is gone; the history of it is not.
        assert session.get(InvestigationFinding, finding["id"]) is None
    finally:
        session.close()


def test_monitoring_counts_open_findings_on_the_movement(tenant) -> None:
    """The dashboard says which flagged movements somebody is already working on."""

    finding = create_finding(tenant["admin"], tenant["base"])
    body = tenant["admin"].get(f"{tenant['base']}/monitoring").json()
    entry = next(
        item for item in body["recent_abnormal"] if item["kpi_key"] == "revenue"
    )
    assert entry["open_findings"] >= 1
    assert body["findings_open"] >= 1
    assert body["recent_findings"], "the dashboard shows the notes it counted"

    tenant["admin"].patch(
        f"{tenant['base']}/investigation/findings/{finding['id']}",
        json={"status": "RESOLVED"},
    )
    after = tenant["admin"].get(f"{tenant['base']}/monitoring").json()
    resolved_entry = next(
        item for item in after["recent_abnormal"] if item["kpi_key"] == "revenue"
    )
    assert resolved_entry["open_findings"] == 0, (
        "a resolved finding is no longer open work on the movement"
    )
    assert after["findings_resolved"] >= 1

    tenant["admin"].delete(f"{tenant['base']}/investigation/findings/{finding['id']}")


def test_audit_scrub_keeps_identifiers_and_still_hides_secrets() -> None:
    """The trail must name the KPI it is about, and never name a credential.

    Regression guard for a real defect: ``key`` is a credential hint, so the
    substring scrub redacted ``kpi_key`` and the audit screen reported every
    governance action as touching ``[redacted]``. The identifier allowlist fixes
    that -- and this asserts it fixed only that.
    """

    from app.services.audit import _scrub

    cleaned = _scrub(
        {
            "kpi_key": "revenue",
            "config_key": "aurora-weekly",
            "role_key": "ANALYST",
            "password": "hunter2",
            "api_key": "sk-live-abc",
            "secret_key": "s3cr3t",
            "access_token": "ey...",
            "connection_uri": "postgres://u:p@h/db",
            "nested": {"kpi_key": "orders", "db_password": "nope"},
        }
    )
    assert cleaned["kpi_key"] == "revenue"
    assert cleaned["config_key"] == "aurora-weekly"
    assert cleaned["role_key"] == "ANALYST"
    assert cleaned["nested"]["kpi_key"] == "orders"
    for field in ("password", "api_key", "secret_key", "access_token", "connection_uri"):
        assert cleaned[field] == "[redacted]", f"{field} reached the audit trail"
    assert cleaned["nested"]["db_password"] == "[redacted]"
