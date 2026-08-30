"""Proof that a movement is split into parts, and that a part is not a verdict.

Detection answers *"did this KPI move more than it usually does?"* for every
registered KPI, every day. Investigation answers a different question, on request:
*"which part of the business accounts for that movement?"* This module tests the
second one, and it is built around the distinction that is easiest to lose:

**A share is arithmetic. A status is a judgement.** The KPI carries one status,
produced by the detection engine from the company's approved comparison policy. No
contributor carries one, because nothing has been run on any contributor -- entity
anomaly detection is on demand and selective, and nothing on this platform sweeps
every entity. So the assertions below check not only that the arithmetic is right
but that the response has nowhere to *put* a verdict about a region.

The figures are exact, not approximate. Company A's seeded ``orders`` table lays
each day's rows out so the day's total lands on the intended figure to the rupee
(see :mod:`tests.fixture_generalization`), and because the region and channel of
each row follow from its position, every *per-region* figure is exact too. Each
one is recomputed here from the seeded truth with the standard library -- never by
calling the contribution engine's own arithmetic, because a test that reuses the
implementation cannot detect a wrong implementation.

What is asserted, in order:

* the breakdown reconciles to the movement the detection engine measured, and to
  no other movement;
* Top-K trims the ranking without re-basing the shares onto the rows that survive;
* a drill-down follows the KPI's own registered hierarchy, and its shares are
  still measured against the whole KPI movement;
* an unapproved dimension, a missing detection run and a reader without the
  permission are each refused;
* a regionally scoped reader sees their own region and nothing else -- typed by
  hand or clicked, the same gate;
* the result is persisted and audited, so a breakdown someone acted on can be
  produced again later;
* the business surface carries no SQL and no statistics;
* running an investigation does not change what detection said.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import SessionLocal
from app.main import create_app
from app.models.detection import ContributionRun, DetectionRun
from app.models.kpi import KpiVersion
from app.services import contribution as contribution_service
from tests.conftest import login
from tests.fixture_generalization import (
    A_CHANNELS,
    A_REGIONS,
    A_TARGET_REVENUE,
    COMPANY_A_TARGET,
    a_order_count_for,
    a_revenue_total_for,
    build_company_a_source,
)

# Provisioning a tenant end to end through the API is already solved once, for the
# detection suite. Reusing those helpers is deliberate: an investigation must run
# on a KPI registered, validated and approved exactly the way a real one is, and a
# second private path to "approved KPI" would be a way for this suite to pass
# against a KPI the platform would not accept.
from tests.test_detection_generalization import (
    TENANT_VOCABULARY,
    _code_strings,
    approve_bucket_config,
    provision,
    run_detection,
)

#: How many comparable dates a breakdown will use, from the platform's own cap.
#: The stored run compared against more; a per-entity history is capped lower
#: because it costs one grouped read per date.
BREAKDOWN_REFERENCE_DATES = settings.contribution_max_reference_dates

#: Keys that must never appear on a contributor. Each one would turn "this part
#: accounts for most of the movement" into "this part has a problem", which is a
#: finding no computation on this platform has made.
FORBIDDEN_CONTRIBUTOR_KEYS = (
    "status",
    "verdict",
    "anomaly",
    "is_anomalous",
    "severity",
    "modified_z_score",
    "z_score",
    "confidence",
)


def test_demo_kpi_uses_fallback_dimensions_when_not_registered(module_client, tmp_path_factory) -> None:
    """A demo KPI without explicit dimensions must still resolve to the demo breakdowns."""
    seeded = build_company_a_source(
        tmp_path_factory.mktemp("fallback_resolution") / "aurora_fallback.db"
    )
    admin, base, tables = provision(
        module_client,
        email="admin@aurora-fallback.example.com",
        company_name="Aurora Retail Fallback",
        source_name="Aurora Commerce",
        source_path=seeded["path"],
        scope={"orders": "order_date"},
    )
    definition = register_kpi_without_dimensions(
        admin,
        base,
        source_table_id=tables["orders"]["id"],
        kpi_key="net_revenue",
    )
    version_id = definition["versions"][0]["id"]

    response = admin.get(f"{base}/investigation/dimensions", params={"kpi_id": "net_revenue"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dimensions"][0]["name"] == "region"
    assert payload["dimensions"][0]["is_default"] is True


# ---------------------------------------------------------------------------
# Independent expectations, from the seeded truth
# ---------------------------------------------------------------------------
def seeded_rows(day: date) -> list[tuple[str, str, float]]:
    """``(region, channel, net_revenue)`` for every order seeded on ``day``.

    Re-derived from the fixture's two public totals rather than imported from its
    private row builder, so this stays a statement of what the database contains
    rather than a copy of how it was filled.
    """

    count = a_order_count_for(day)
    total = a_revenue_total_for(day)
    # One row carries the remainder, the rest carry 1.0 each -- which is what makes
    # each region's daily figure exact rather than a floating-point residue.
    amounts = [float(total - (count - 1))] + [1.0] * (count - 1)
    return [
        (
            A_REGIONS[index % len(A_REGIONS)],
            A_CHANNELS[index % len(A_CHANNELS)],
            amount,
        )
        for index, amount in enumerate(amounts)
    ]


def seeded_totals(day: date, *, by: str, region: str | None = None) -> dict[str, float]:
    """``SUM(net_revenue)`` for ``day``, grouped by region or channel."""

    index = 0 if by == "region" else 1
    totals: dict[str, float] = {}
    for row in seeded_rows(day):
        if region is not None and row[0] != region:
            continue
        key = row[index]
        totals[key] = totals.get(key, 0.0) + row[2]
    return totals


def seeded_expectations(
    days: list[date], *, by: str, region: str | None = None
) -> dict[str, float]:
    """The robust median of each part across ``days``, absent days counted as zero.

    Counting an absent day as zero is what the engine does for a total, and it
    matters: giving an entity a history made only of the dates it appeared on
    would quietly raise its expectation and inflate the movement attributed to it.
    """

    per_day = [seeded_totals(day, by=by, region=region) for day in days]
    universe: set[str] = set()
    for totals in per_day:
        universe |= set(totals)
    return {
        entity: statistics.median([totals.get(entity, 0.0) for totals in per_day])
        for entity in universe
    }


def comparable_fridays(target: date, count: int) -> list[date]:
    """The ``count`` most recent Fridays before ``target``, oldest first."""

    found: list[date] = []
    offset = 1
    while len(found) < count:
        day = target - timedelta(days=offset)
        if day.isoweekday() == 5:
            found.append(day)
        offset += 1
    return sorted(found)


# ---------------------------------------------------------------------------
# One tenant, registered the long way round
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def module_client() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


def register_kpi_with_dimensions(admin, base: str, *, source_table_id: str) -> str:
    """Register, validate and approve Revenue *with its approved breakdowns*.

    The dimensions are the whole point of this fixture. A breakdown is only ever
    performed along a dimension the KPI declared and the company allowed, with the
    hierarchy the company declared -- ``region`` then ``channel`` here, and
    something else entirely for the next tenant. The engine learns all of it from
    this registration.
    """

    created = admin.post(
        f"{base}/kpis",
        json={
            "kpi_key": "revenue",
            "name": "Revenue",
            "business_definition": "Net revenue recognised on the order date.",
            "formula_expression": "SUM(orders.net_revenue)",
            "source_table_id": source_table_id,
            "time_field": "order_date",
            "time_grain": "DAY",
            "unit": "currency",
            "currency": "INR",
            "dimensions": [
                {
                    "dimension_name": "region",
                    "source_column": "region",
                    "is_default_breakdown": True,
                    "hierarchy": ["channel"],
                },
                {"dimension_name": "channel", "source_column": "channel"},
            ],
            "materiality": {
                "relative_threshold_pct": 8.0,
                "business_criticality": "HIGH",
            },
        },
    )
    assert created.status_code == 201, created.text
    definition = created.json()
    version_id = definition["versions"][0]["id"]

    validated = admin.post(f"{base}/kpi-versions/{version_id}/validate")
    assert validated.status_code == 200, validated.text
    report = validated.json()
    assert report["ready_for_approval"] is True, report["summary"]

    submitted = admin.post(f"{base}/kpi-versions/{version_id}/submit", json={})
    assert submitted.status_code == 200, submitted.text
    approved = admin.post(
        f"{base}/kpi-versions/{version_id}/approve",
        json={"reason": "Definition and breakdowns signed off for investigation."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "ACTIVE"
    return definition["id"]


def register_kpi_without_dimensions(admin, base: str, *, source_table_id: str, kpi_key: str) -> dict:
    """Register a demo KPI that does not have any approved dimensions yet."""
    created = admin.post(
        f"{base}/kpis",
        json={
            "kpi_key": kpi_key,
            "name": kpi_key.replace("_", " ").title(),
            "business_definition": "Demo KPI used to validate the fallback investigation route.",
            "formula_expression": "SUM(orders.net_revenue)",
            "source_table_id": source_table_id,
            "time_field": "order_date",
            "time_grain": "DAY",
            "unit": "currency",
            "currency": "INR",
            "dimensions": [],
            "materiality": {
                "relative_threshold_pct": 8.0,
                "business_criticality": "HIGH",
            },
        },
    )
    assert created.status_code == 201, created.text
    response = created.json()
    version_id = response["versions"][0]["id"]
    validated = admin.post(f"{base}/kpi-versions/{version_id}/validate")
    assert validated.status_code == 200, validated.text
    submitted = admin.post(f"{base}/kpi-versions/{version_id}/submit", json={})
    assert submitted.status_code == 200, submitted.text
    approved = admin.post(
        f"{base}/kpi-versions/{version_id}/approve",
        json={"reason": "Approved for fallback investigation resolution."},
    )
    assert approved.status_code == 200, approved.text
    return response


@pytest.fixture(scope="module")
def tenant(module_client, tmp_path_factory) -> dict:
    """A company with a registered KPI, an approved policy and a stored run.

    Everything an investigation needs, produced the way the platform produces it:
    the movement being split below was measured by the detection engine and stored,
    not stated by this test.
    """

    seeded = build_company_a_source(
        tmp_path_factory.mktemp("investigation") / "aurora_investigation.db"
    )
    admin, base, tables = provision(
        module_client,
        email="admin@aurora-investigation.example.com",
        company_name="Aurora Retail Investigation",
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
        config_key="aurora-investigation-weekly",
        name="Aurora weekly trading pattern",
        buckets={
            "same_day_of_week": {"enabled": True, "days": ["FRI"]},
            "yoy_period": {"enabled": True},
        },
    )
    detection = run_detection(admin, base, revenue_id, COMPANY_A_TARGET)
    assert detection["result"]["status"] == "ABNORMAL", (
        "the investigation suite depends on the seeded Friday collapse"
    )
    assert detection["evidence"]["reference"]["count"] > BREAKDOWN_REFERENCE_DATES, (
        "the KPI compared against more dates than a breakdown will, so the "
        "shorter-history disclosure below is exercised"
    )

    company_id = base.rsplit("/", 1)[-1]

    # A regionally scoped reader and a reader with no investigation permission at
    # all. Both are needed: an authorisation model is only proven by the requests
    # it refuses.
    scoped_created = admin.post(
        f"{base}/members",
        json={
            "email": "south@aurora-investigation.example.com",
            "full_name": "Sana South",
            "password": "Investigation-Tests-2026",
            "role_key": "REGIONAL_MANAGER",
            "row_scope": {"region": ["South"]},
        },
    )
    assert scoped_created.status_code == 201, scoped_created.text
    assert scoped_created.json()["row_scope"] == {"region": ["South"]}

    viewer_created = admin.post(
        f"{base}/members",
        json={
            "email": "viewer@aurora-investigation.example.com",
            "full_name": "Vik Viewer",
            "password": "Investigation-Tests-2026",
            "role_key": "VIEWER",
        },
    )
    assert viewer_created.status_code == 201, viewer_created.text

    return {
        "admin": admin,
        "base": base,
        "company_id": company_id,
        "revenue_id": revenue_id,
        "detection": detection,
        "scoped": login(
            module_client,
            "south@aurora-investigation.example.com",
            "Investigation-Tests-2026",
            company_id,
        ),
        "viewer": login(
            module_client,
            "viewer@aurora-investigation.example.com",
            "Investigation-Tests-2026",
            company_id,
        ),
    }


def contribution(actor, base: str, **body) -> dict:
    payload = {"kpi_id": "revenue", "target_date": COMPANY_A_TARGET.isoformat(), **body}
    response = actor.post(f"{base}/investigation/contribution", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The central proof
# ---------------------------------------------------------------------------
def test_contribution_splits_the_movement_the_engine_measured(tenant):
    """Every part's figure is exact, and together they reconcile to the whole.

    This is the property that makes a breakdown defensible: the movement being
    apportioned is the one the business already saw on the detection surface, and
    the parts add back up to it. A breakdown that reconciled to a *different*
    whole would be arithmetic about a KPI nobody was shown.
    """

    body = contribution(tenant["admin"], tenant["base"])
    result = body["result"]
    evidence = body["evidence"]

    # ---- the whole, carried through from the stored run, not recomputed -------
    stored = tenant["detection"]["result"]
    assert result["actual"] == pytest.approx(A_TARGET_REVENUE)
    assert result["actual"] == pytest.approx(stored["actual"])
    assert result["expected"] == pytest.approx(stored["expected"])
    assert result["expected"] == pytest.approx(10_250_000.0), "the seeded Friday median"
    assert result["movement"] == pytest.approx(-4_250_000.0)
    assert result["status"] == "ABNORMAL"
    assert result["dimension"] == "region", "the KPI's own default breakdown"
    assert result["path"] == [], "nothing has been drilled into yet"
    assert result["currency"] == "INR"

    # ---- the comparable dates are the stored run's, capped ------------------
    references = [date.fromisoformat(day) for day in evidence["reference_dates"]]
    assert references == comparable_fridays(COMPANY_A_TARGET, BREAKDOWN_REFERENCE_DATES)

    # ---- each part, recomputed from the seeded rows --------------------------
    actuals = seeded_totals(COMPANY_A_TARGET, by="region")
    expectations = seeded_expectations(references, by="region")
    assert actuals == {"North": 5_999_996.0, "South": 2.0, "East": 1.0, "West": 1.0}
    assert expectations["North"] == pytest.approx(10_249_996.0)

    rows = {row["label"]: row for row in result["contributors"]}
    assert set(rows) == set(A_REGIONS)
    for region, row in rows.items():
        change = actuals[region] - expectations[region]
        assert row["actual"] == pytest.approx(actuals[region]), region
        assert row["expected"] == pytest.approx(expectations[region]), region
        assert row["change"] == pytest.approx(change), region
        assert row["share_pct"] == pytest.approx(
            change / result["movement"] * 100.0
        ), region
        assert row["reference_count"] == BREAKDOWN_REFERENCE_DATES, region

    # ---- ranked by how much of the movement each accounts for ---------------
    # East before South on a tied absolute change, alphabetically -- a tie broken
    # by a stable rule rather than by whatever order the source returned.
    assert [row["label"] for row in result["contributors"]] == [
        "North",
        "East",
        "South",
        "West",
    ]
    assert rows["North"]["share_pct"] == pytest.approx(100.0)
    assert result["ranked_count"] == 4
    assert result["top_k"] == settings.contribution_top_k, "the platform default"

    # ---- and it reconciles -------------------------------------------------
    total_change = sum(row["change"] for row in result["contributors"])
    assert total_change == pytest.approx(result["movement"])
    assert result["explained_pct"] == pytest.approx(100.0)
    assert result["shares_available"] is True

    # ---- one part explains it, which is a reason to stop, not a finding -----
    assert result["leader_is_sufficient"] is True
    assert result["sufficiency_pct"] == pytest.approx(settings.contribution_sufficiency_pct)

    # ---- and where a drill-down may go next, from the KPI's own hierarchy ---
    assert result["next_dimensions"] == ["channel"]


def test_a_share_is_not_a_verdict(tenant):
    """The response has one status, and it belongs to the KPI.

    Contribution ranks parts of a business by how much of a movement they account
    for. That is not anomaly detection, and no anomaly detection has been run on
    any of them. So there must be nowhere in the payload -- and nowhere in the
    stored table -- to record a judgement about a region, because a field like
    that is how "North is 60% of the movement" becomes "North has a problem".
    """

    result = contribution(tenant["admin"], tenant["base"])["result"]

    for row in result["contributors"]:
        for key in FORBIDDEN_CONTRIBUTOR_KEYS:
            assert key not in row, f"a contributor carries {key!r}: {row['label']}"

    # Exactly one status in the whole business view, at the KPI level.
    assert result["status"] == "ABNORMAL"
    assert json.dumps(result).count('"status"') == 1

    # The same absence in the table, where a well-meaning migration could add one.
    columns = {column.name for column in ContributionRun.__table__.columns}
    assert "kpi_status" in columns, "the KPI's verdict is carried through"
    for name in ("entity_status", "contributor_status", "leader_status", "leader_verdict"):
        assert name not in columns, f"contribution_runs stores a verdict: {name}"


def test_top_k_limits_the_ranking_without_rebasing_the_shares(tenant):
    """Trimming what is displayed must not change what a share means.

    A share is a fraction of the whole KPI movement. Recomputing it over only the
    rows that survived Top-K would make every breakdown add to 100% and quietly
    delete the part of the movement nobody has accounted for -- which is exactly
    the number a reader needs in order to distrust a short list.
    """

    full = contribution(tenant["admin"], tenant["base"])["result"]
    trimmed = contribution(tenant["admin"], tenant["base"], top_k=2)["result"]

    assert len(trimmed["contributors"]) == 2
    assert trimmed["top_k"] == 2
    assert trimmed["ranked_count"] == 4, "four parts were ranked, two are shown"

    shown = {row["label"]: row for row in trimmed["contributors"]}
    everything = {row["label"]: row for row in full["contributors"]}
    for label, row in shown.items():
        assert row["share_pct"] == pytest.approx(everything[label]["share_pct"]), label
        assert row["change"] == pytest.approx(everything[label]["change"]), label

    # The two shown parts happen to account for the movement here, but the number
    # is computed against the whole and reported as such rather than forced to 100.
    assert trimmed["explained_pct"] == pytest.approx(100.0, abs=1e-3)


def test_drilling_into_a_contributor_follows_the_registered_hierarchy(tenant):
    """A drill-down narrows to a chosen part and breaks it down by the next level.

    Two things are asserted, and the second is the load-bearing one: the next
    dimension comes from the KPI's own declared hierarchy, and the shares inside
    the narrowed view are still measured against the *whole* KPI movement. A
    reader who has drilled twice must still be able to tell how much of the
    original movement they are looking at.
    """

    body = contribution(
        tenant["admin"],
        tenant["base"],
        dimension="channel",
        path=[{"dimension": "region", "value": "North"}],
    )
    result = body["result"]
    references = [date.fromisoformat(day) for day in body["evidence"]["reference_dates"]]

    assert result["dimension"] == "channel"
    assert result["path"] == [{"dimension": "region", "value": "North"}]
    assert result["movement"] == pytest.approx(-4_250_000.0), (
        "the KPI's movement, not the selected region's"
    )
    assert result["next_dimensions"] == [], "channel declares nothing below it"

    actuals = seeded_totals(COMPANY_A_TARGET, by="channel", region="North")
    expectations = seeded_expectations(references, by="channel", region="North")
    rows = {row["label"]: row for row in result["contributors"]}
    assert set(rows) == set(actuals), "only channels North actually traded in"

    for label, row in rows.items():
        change = actuals[label] - expectations[label]
        assert row["actual"] == pytest.approx(actuals[label]), label
        assert row["expected"] == pytest.approx(expectations[label]), label
        assert row["share_pct"] == pytest.approx(
            change / result["movement"] * 100.0
        ), label

    # North traded through one channel on this date, so that channel accounts for
    # all of North -- and North accounted for all of the movement.
    assert rows["STORE"]["share_pct"] == pytest.approx(100.0)


def test_only_approved_dimensions_can_be_broken_down(tenant):
    """A dimension is a governed contract, not a column the caller picks.

    The refusal names what *is* approved, so the caller learns the company's own
    vocabulary instead of guessing at column names -- and learns it without the
    platform ever running a query against the column they asked for.
    """

    listed = tenant["admin"].get(
        f"{tenant['base']}/investigation/dimensions", params={"kpi_id": "revenue"}
    )
    assert listed.status_code == 200, listed.text
    dimensions = {row["name"]: row for row in listed.json()["dimensions"]}
    assert set(dimensions) == {"region", "channel"}
    assert dimensions["region"]["is_default"] is True
    assert dimensions["region"]["hierarchy"] == ["channel"]

    refused = tenant["admin"].post(
        f"{tenant['base']}/investigation/contribution",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "dimension": "product_line",
        },
    )
    assert refused.status_code == 404, refused.text
    assert sorted(refused.json()["details"]["approved"]) == ["channel", "region"]


def test_investigation_requires_a_stored_detection_result(tenant):
    """No measured movement, no breakdown -- and no expectation invented here.

    The alternative is worse than a refusal: an investigation that computed its
    own expectation would apportion a movement the business was never shown, and
    the two surfaces would disagree about the same KPI on the same date.
    """

    never_run = COMPANY_A_TARGET - timedelta(days=1)
    refused = tenant["admin"].post(
        f"{tenant['base']}/investigation/contribution",
        json={"kpi_id": "revenue", "target_date": never_run.isoformat()},
    )
    assert refused.status_code == 409, refused.text
    assert "no stored detection result" in refused.json()["message"]
    assert refused.json()["details"]["target_date"] == never_run.isoformat()


def test_investigation_needs_its_own_permission(tenant):
    """Reading a KPI is not the same entitlement as investigating one."""

    refused = tenant["viewer"].post(
        f"{tenant['base']}/investigation/contribution",
        json={"kpi_id": "revenue", "target_date": COMPANY_A_TARGET.isoformat()},
    )
    assert refused.status_code == 403, refused.text


# ---------------------------------------------------------------------------
# The manual entry point
# ---------------------------------------------------------------------------
def test_manual_analysis_without_an_entity_ranks_contributors(tenant):
    """Typing a dimension gives the same answer as arriving from detection.

    The answer is not different for having been typed. What differs is only the
    recorded entry point, because how someone arrived at a question is worth
    keeping.
    """

    response = tenant["admin"].post(
        f"{tenant['base']}/investigation/analysis",
        json={
            "kpi_id": "revenue",
            "dimension": "region",
            "target_date": COMPANY_A_TARGET.isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "contribution"

    automatic = contribution(tenant["admin"], tenant["base"])["result"]
    assert [row["label"] for row in body["result"]["contributors"]] == [
        row["label"] for row in automatic["contributors"]
    ]
    assert body["result"]["movement"] == pytest.approx(automatic["movement"])

    with SessionLocal() as session:
        row = session.get(ContributionRun, body["evidence"]["contribution_run_id"])
        assert row is not None
        assert row.entry_point == "MANUAL"


def test_manual_analysis_with_an_entity_analyses_only_that_entity(tenant):
    """One entity, on request. Nothing else is read, and nothing else is judged.

    This is the permanent rule made observable: KPI anomaly detection is
    continuous, entity anomaly detection is selective. Asking about one region must
    not trigger an analysis of every other region, so the response is checked for
    the absence of any region but the one asked about.

    The one asked about *is* judged -- that is what asking for it means -- and by
    the platform's own engine, so the status here is the same status the dashboard
    uses. What must not appear is a second scoring vocabulary invented for this
    screen, so the response is checked for that too.
    """

    lookback = 7
    response = tenant["admin"].post(
        f"{tenant['base']}/investigation/analysis",
        json={
            "kpi_id": "revenue",
            "dimension": "region",
            "entity": "South",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "lookback_days": lookback,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    result = body["result"]

    assert body["mode"] == "entity"
    assert result["entity"] == "South"
    assert result["dimension"] == "region"
    assert result["observed_days"] == lookback

    days = [COMPANY_A_TARGET - timedelta(days=offset) for offset in range(lookback - 1, -1, -1)]
    assert [point["date"] for point in result["points"]] == [
        day.isoformat() for day in days
    ]
    for point, day in zip(result["points"], days, strict=True):
        assert point["value"] == pytest.approx(
            seeded_totals(day, by="region")["South"]
        ), day.isoformat()

    # Two on the target Friday against one on every ordinary day before it.
    assert result["latest"] == pytest.approx(2.0)
    assert result["typical"] == pytest.approx(1.0)
    assert result["change_vs_typical"] == pytest.approx(1.0)
    assert result["change_pct_vs_typical"] == pytest.approx(100.0)

    # A verdict about South, from the engine that judges the KPI -- and only from
    # it. The status is the KPI's own vocabulary; nothing here invents a scale.
    assert result["status"] in {"NORMAL", "ABNORMAL", "LOW_CONFIDENCE"}
    assert result["direction"] == "UP"
    assert result["variance"] == pytest.approx(result["actual"] - result["expected"])
    for invented in ("severity", "score", "confidence", "risk", "z_score", "is_anomalous"):
        assert invented not in result, (
            f"an entity analysis carries {invented!r}, which would be a second "
            "classification system"
        )

    # And no other region was touched, in the result or in the queries.
    rendered = json.dumps(body)
    for region in A_REGIONS:
        if region != "South":
            assert region not in rendered, f"{region} was read while analysing South"


# ---------------------------------------------------------------------------
# Security: the same gate whether a value was clicked or typed
# ---------------------------------------------------------------------------
def test_a_scoped_reader_sees_only_their_own_part(tenant):
    """Row scope survives the investigation surface, in both directions.

    The scoped reader is entitled to South. So the breakdown lists South, says how
    many values it withheld, and -- crucially -- still measures South's share
    against the whole KPI movement. Re-basing onto the visible rows would tell a
    regional manager their own region explains 100% of a company-wide collapse
    they cannot see.
    """

    body = contribution(tenant["scoped"], tenant["base"])
    result = body["result"]

    assert [row["label"] for row in result["contributors"]] == ["South"]
    assert result["movement"] == pytest.approx(-4_250_000.0), (
        "the KPI's movement is not narrowed to what the reader may see"
    )

    south = result["contributors"][0]
    assert south["change"] == pytest.approx(0.5)
    assert south["share_pct"] == pytest.approx(0.5 / result["movement"] * 100.0)
    assert abs(south["share_pct"]) < 1.0, "South accounts for almost none of it"
    assert result["leader_is_sufficient"] is False

    assert body["evidence"]["withheld_by_scope"] == len(A_REGIONS) - 1
    assert any("data scope" in note for note in result["notes"]), result["notes"]

    # And a value typed by hand goes through the very same gate as one clicked.
    refused = tenant["scoped"].post(
        f"{tenant['base']}/investigation/contribution",
        json={
            "kpi_id": "revenue",
            "target_date": COMPANY_A_TARGET.isoformat(),
            "dimension": "channel",
            "path": [{"dimension": "region", "value": "North"}],
        },
    )
    assert refused.status_code == 404, refused.text
    assert "available to you" in refused.json()["message"]

    profiled = tenant["scoped"].post(
        f"{tenant['base']}/investigation/analysis",
        json={
            "kpi_id": "revenue",
            "dimension": "region",
            "entity": "North",
            "target_date": COMPANY_A_TARGET.isoformat(),
        },
    )
    assert profiled.status_code == 404, profiled.text


# ---------------------------------------------------------------------------
# Persistence, audit and what the business surface shows
# ---------------------------------------------------------------------------
def test_the_breakdown_is_persisted_and_audited(tenant):
    """A breakdown someone acted on has to remain readable afterwards.

    Stored for the same reason a detection run is stored -- so the parts can be
    produced again as they were measured -- and pointed at from the audit trail,
    so "who split this movement, when, and what did it say" is one lookup rather
    than a re-run against data that has since changed.
    """

    body = contribution(tenant["admin"], tenant["base"])
    run_id = body["evidence"]["contribution_run_id"]
    assert run_id

    with SessionLocal() as session:
        row = session.get(ContributionRun, run_id)
        assert row is not None
        assert row.entry_point == "AUTOMATIC"
        assert row.target_date == COMPANY_A_TARGET
        assert row.dimension == "region"
        assert row.kpi_status == "ABNORMAL"
        assert row.kpi_movement == pytest.approx(-4_250_000.0)
        assert row.currency == "INR"
        assert row.leader_entity == "North"
        assert row.leader_share_pct == pytest.approx(100.0)
        assert row.ranked_count == 4
        assert [item["label"] for item in row.contributors] == [
            item["label"] for item in body["result"]["contributors"]
        ]

        # It points at the detection run it split. Checked through the run rather
        # than against a remembered id, so the link is asserted and not the
        # accident of which run happened to be the newest.
        split = session.get(DetectionRun, row.detection_run_id)
        assert split is not None
        assert split.target_date == COMPANY_A_TARGET
        assert split.kpi_key == "revenue"
        assert split.status == "ABNORMAL"
        assert split.actual_value == pytest.approx(row.kpi_actual)
        assert split.expected_value == pytest.approx(row.kpi_expected)
        assert body["evidence"]["detection_run_id"] == row.detection_run_id
        row_detection_run_id = row.detection_run_id

    logged = tenant["admin"].get(
        f"{tenant['base']}/audit",
        params={"action": "investigation.contribution_analysed"},
    )
    assert logged.status_code == 200, logged.text
    entries = logged.json()
    assert entries, "the breakdown left no audit trail"

    # Found by the run it points at rather than by position: several breakdowns are
    # performed across this module, and an assertion on "the newest row" would be
    # an assertion about test order.
    entry = next(
        (item for item in entries if item["details"].get("contribution_run_id") == run_id),
        None,
    )
    assert entry is not None, "the stored breakdown is not named in the audit trail"
    assert entry["resource_type"] == "detection_run"
    assert entry["resource_id"] == row_detection_run_id
    assert entry["details"]["dimension"] == "region"
    assert entry["details"]["kpi_status"] == "ABNORMAL"
    assert entry["details"]["detection_run_id"] == row_detection_run_id


def test_the_business_view_shows_no_sql_and_no_statistics(tenant):
    """What a business reader sees, and what only an entitled reader sees.

    The split is not cosmetic. A movement broken into parts is a business answer;
    the queries that produced it, the comparable dates and how many values a scope
    withheld are the method, and the method is returned separately to callers
    already entitled to read KPI definitions.
    """

    body = contribution(tenant["admin"], tenant["base"])
    rendered = json.dumps(body["result"]).lower()

    # Word boundaries, not substrings: "mad" hides inside ordinary prose, and a
    # test that fails on the word "made" would get deleted rather than fixed.
    for leak in ("select", "group by", "modified_z", "mad", "z_score", "queries", "sql"):
        assert not re.search(rf"\b{re.escape(leak)}\b", rendered), (
            f"the business view exposes {leak!r}"
        )

    evidence = body["evidence"]
    assert evidence["queries"], "the method is available to an entitled caller"
    assert any("select" in query.lower() for query in evidence["queries"])
    assert evidence["additive"] is True
    assert evidence["detection_run_id"], "the run being split is named in the method"


def test_investigating_does_not_change_what_detection_said(tenant):
    """The detection engine is upstream of all of this, and untouched by it.

    An investigation reads a stored run and reads the KPI's source. If it ever
    wrote back -- a recomputed expectation, a softened status -- the number on the
    detection surface would move because somebody opened a breakdown.
    """

    before = tenant["detection"]["result"]
    contribution(tenant["admin"], tenant["base"])
    contribution(tenant["admin"], tenant["base"], dimension="channel")
    after = run_detection(
        tenant["admin"], tenant["base"], tenant["revenue_id"], COMPANY_A_TARGET
    )["result"]

    assert after["actual"] == pytest.approx(before["actual"])
    assert after["expected"] == pytest.approx(before["expected"])
    assert after["deviation_pct"] == pytest.approx(before["deviation_pct"])
    assert after["status"] == before["status"]


def test_the_contribution_engine_names_no_company_vocabulary():
    """The engine's source is the evidence, because a branch is easy to hide.

    A breakdown engine that knows the word ``region`` is one company's script. The
    dimension, its column and its hierarchy all arrive from the KPI's registration,
    so this suite's own vocabulary must not appear in executable code -- and the
    tenant's table and column names must not appear even in prose, because a table
    name in a comment is a sign the module was written against one schema.
    """

    module = Path("app/services/contribution.py")
    assert module.is_file(), f"{module} not found -- run pytest from backend/"

    text = module.read_text(encoding="utf-8").lower()
    for word in TENANT_VOCABULARY:
        assert word not in text, f"{module} names tenant-specific vocabulary: {word}"

    # Dimension names may appear in a docstring as illustration ("Region -> Product
    # for one company, Country -> Category for another"), which is documentation of
    # the generality rather than a breach of it. In executable code they would be a
    # hard-coded breakdown.
    literals = " | ".join(_code_strings(module)).lower()
    for word in ("region", "product", "channel", "north", "south", "territory", "branch"):
        assert word not in literals, (
            f"{module} has the dimension literal {word!r} in executable code; "
            "dimensions come from the KPI's registration"
        )
