"""Proof that a recommendation is a reading of stored evidence, framed as a suggestion.

The recommendation layer is the only surface in this platform that can put words in a
manager's mouth, so this suite is mostly about what it *refuses* to say. Four claims:

**It never upgrades a share to a cause.** Contribution measures where a movement sits.
Every recommendation carries the causation note, and no sentence anywhere in the
payload reaches for a causal verb. This suite fails if one does.

**It invents no figures.** Every number in the recommendation prose is a column of the
stored detection run or the stored breakdown, re-rendered. Potential impact is a band
with a stated basis, never an amount — because nothing in this platform measures a
counterfactual, so any money attached to "what this action is worth" would be made up.

**It will not recommend an intervention on a result the platform could not judge.** A
LOW CONFIDENCE verdict produces evidence-collection steps and no lever, no owner and no
action. A NORMAL verdict produces no corrective action at all. Only an ABNORMAL result
the confidence logic rates above LOW gets a suggested action.

**It degrades rather than guesses.** With no stored breakdown it scopes its advice to
the KPI and says a breakdown would sharpen it; it does not pick a plausible region.
Once a breakdown exists it names the area the stored ranking actually put first, and
drilling deeper re-aims it at the deeper area. A reader without investigation access
gets the KPI-level shape and is told why.

And one thing it does record: whether a human found the advice useful. That is the only
signal here nothing else can derive — and nothing about it can touch a verdict.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.recommendation_config import (
    CAUSATION_NOTE,
    LOW_CONFIDENCE_HEADLINE,
    LOW_CONFIDENCE_NEXT_STEPS,
    NORMAL_HEADLINE,
)
from tests.conftest import login
from tests.fixture_generalization import (
    A_REGIONS,
    COMPANY_A_TARGET,
    HISTORY_DAYS,
    build_company_a_source,
)
from tests.test_detection_generalization import (
    approve_bucket_config,
    provision,
    run_detection,
)
from tests.test_explainability_findings import CAUSAL_WORDS

PASSWORD = "Recommendation-Tests-2026"

#: A comparable Friday that came in ordinary. Same KPI, same engine, seven days
#: earlier -- so the NORMAL branch is proven on a real verdict rather than a stub.
NORMAL_TARGET = COMPANY_A_TARGET - timedelta(days=7)


def _sparse_friday() -> date:
    """The second Friday in the seeded history: one comparable date behind it.

    Below the engine's minimum reference points, so its verdict is LOW_CONFIDENCE
    for the reason the engine states rather than because this test asked for it.
    """

    earliest = COMPANY_A_TARGET - timedelta(days=HISTORY_DAYS)
    first_friday = earliest + timedelta(days=(4 - earliest.weekday()) % 7)
    return first_friday + timedelta(days=7)


SPARSE_TARGET = _sparse_friday()

#: Phrases that would promise the business an outcome. A recommendation suggests a
#: review; it cannot know what a review will find, and nothing here measures what an
#: action recovered.
GUARANTEE_WORDS = (
    "will recover",
    "will increase",
    "will restore",
    "guaranteed",
    "will improve by",
    "expected savings",
    "will result in",
)


def numbers_in(text: str) -> set[str]:
    """Every formatted figure in a block of prose, commas stripped."""

    return {match.replace(",", "") for match in re.findall(r"-?\d[\d,]*\.?\d*", text)}


def prose_of(payload: dict) -> str:
    """Every sentence the payload would put in front of a reader, concatenated."""

    return json.dumps(payload)


# ---------------------------------------------------------------------------
# One tenant, provisioned the way the platform provisions one
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def module_client() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


def register_revenue_with_drivers(admin, base: str, *, source_table_id: str) -> str:
    """Revenue, with its approved breakdowns *and* its registered drivers.

    The drivers are what this fixture adds over the investigation suite's version,
    and they are the point: a lever is only a lever because the company registered
    a driver and marked it controllable. ``Competitor pricing`` is registered and
    deliberately *not* controllable -- a candidate explanation the business cannot
    pull, which must never surface as a recommended action.
    """

    created = admin.post(
        f"{base}/kpis",
        json={
            "kpi_key": "revenue",
            "name": "net_revenue",
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
            "drivers": [
                {
                    "driver_name": "Order volume",
                    "driver_type": "VOLUME",
                    "controllable": True,
                },
                {
                    "driver_name": "Promotions",
                    "driver_type": "MARKETING",
                    "controllable": True,
                },
                {
                    "driver_name": "Competitor pricing",
                    "driver_type": "EXTERNAL",
                    "controllable": False,
                },
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
    assert validated.json()["ready_for_approval"] is True

    submitted = admin.post(f"{base}/kpi-versions/{version_id}/submit", json={})
    assert submitted.status_code == 200, submitted.text
    approved = admin.post(
        f"{base}/kpi-versions/{version_id}/approve",
        json={"reason": "Definition, breakdowns and drivers signed off."},
    )
    assert approved.status_code == 200, approved.text
    return definition["id"]


@pytest.fixture(scope="module")
def tenant(module_client, tmp_path_factory) -> dict:
    """A company with three stored verdicts on one KPI: abnormal, normal, unjudgeable.

    All three measured by the same engine on the same registration. The recommendation
    layer's three shapes are then proven against real verdicts rather than against
    statuses this test wrote into a row.
    """

    seeded = build_company_a_source(
        tmp_path_factory.mktemp("recommendations") / "aurora_recommend.db"
    )
    admin, base, tables = provision(
        module_client,
        email="admin@aurora-recommend.example.com",
        company_name="Aurora Retail Recommend",
        source_name="Aurora Commerce",
        source_path=seeded["path"],
        scope={"orders": "order_date"},
    )
    revenue_id = register_revenue_with_drivers(
        admin, base, source_table_id=tables["orders"]["id"]
    )
    approve_bucket_config(
        admin,
        base,
        config_key="aurora-recommend-weekly",
        name="Aurora weekly trading pattern",
        buckets={
            "same_day_of_week": {"enabled": True, "days": ["FRI"]},
            "yoy_period": {"enabled": True},
        },
    )

    abnormal = run_detection(admin, base, revenue_id, COMPANY_A_TARGET)
    assert abnormal["result"]["status"] == "ABNORMAL", (
        "this suite reads recommendations for a flagged movement, so the seeded "
        "Friday collapse must still flag"
    )
    normal = run_detection(admin, base, revenue_id, NORMAL_TARGET)
    assert normal["result"]["status"] == "NORMAL", (
        "the NORMAL branch is proven on a real ordinary Friday"
    )
    sparse = run_detection(admin, base, revenue_id, SPARSE_TARGET)
    assert sparse["result"]["status"] == "LOW_CONFIDENCE", (
        "a date with too little comparable history behind it must be unjudgeable"
    )

    company_id = base.rsplit("/", 1)[-1]

    viewer_created = admin.post(
        f"{base}/members",
        json={
            "email": "viewer@aurora-recommend.example.com",
            "full_name": "Vik Viewer",
            "password": PASSWORD,
            "role_key": "VIEWER",
        },
    )
    assert viewer_created.status_code == 201, viewer_created.text

    return {
        "admin": admin,
        "base": base,
        "company_id": company_id,
        "revenue_id": revenue_id,
        "source_path": seeded["path"],
        "abnormal": abnormal,
        "normal": normal,
        "sparse": sparse,
        "viewer": login(
            module_client, "viewer@aurora-recommend.example.com", PASSWORD, company_id
        ),
    }


def recommendations(actor, base: str, run_id: str) -> dict:
    response = actor.get(f"{base}/detection-runs/{run_id}/recommendations")
    assert response.status_code == 200, response.text
    return response.json()


def breakdown(actor, base: str, **body) -> dict:
    payload = {"kpi_id": "revenue", "target_date": COMPANY_A_TARGET.isoformat(), **body}
    response = actor.post(f"{base}/investigation/contribution", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["result"]


# ---------------------------------------------------------------------------
# Before a breakdown: advice that admits it has no area yet
# ---------------------------------------------------------------------------
def test_without_a_stored_breakdown_the_advice_asks_for_one_and_names_no_area(tenant):
    """No breakdown, no target area. The layer asks rather than guessing.

    This is the failure mode the whole design is arranged against: four seeded
    regions exist, and it would be trivially easy to name the largest one. But
    nothing has apportioned this movement yet, so naming a region here would be a
    claim the platform has not measured.
    """

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    result = body["result"]

    assert result["stance"] == "ACTION"
    assert result["verdict"] == "ABNORMAL"
    assert result["target_area"] is None
    assert result["awaiting_breakdown"] is True, (
        "the page needs to know a breakdown would sharpen this"
    )
    assert result["recommendations"], "an abnormal, judgeable result still gets advice"

    prose = prose_of(result)
    for region in A_REGIONS:
        assert region not in prose, f"{region} was never apportioned this movement"

    top = result["recommendations"][0]
    assert "break this movement down" in top["action"].lower()
    assert top["target_area"] is None
    assert "no stored breakdown" in result["body"].lower()


# ---------------------------------------------------------------------------
# After a breakdown: the same advice, aimed
# ---------------------------------------------------------------------------
def test_a_stored_breakdown_aims_the_advice_at_the_area_the_ranking_put_first(tenant):
    """The target area is the stored breakdown's own leader, with its own share.

    Not recomputed here and not chosen by the recommendation layer: the engine
    ranked the parts, stored the ranking, and the recommendation quotes it. The
    share in the evidence sentence is the share in the stored row, to a decimal.
    """

    split = breakdown(tenant["admin"], tenant["base"], dimension="region", top_k=8)
    leader = split["contributors"][0]

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    result = body["result"]
    area = result["target_area"]

    assert area is not None, "a stored breakdown must produce a target area"
    assert area["entity"] == leader["label"]
    assert area["entity"] in A_REGIONS
    assert area["dimension"] == "region"
    assert area["entity_type"] == "Region", "the entity type is derived from the dimension"
    assert area["chain"] == [leader["label"]]
    assert area["chain_label"] == leader["label"]
    assert area["share_pct"] == pytest.approx(leader["share_pct"])
    assert result["awaiting_breakdown"] is False

    # The company's own declared hierarchy, and nothing invented beside it.
    assert area["drill_next"] == ["channel"]

    top = result["recommendations"][0]
    assert f"{abs(leader['share_pct']):.1f}%" in top["finding"]
    assert "accounts for" in top["finding"].lower()
    assert leader["label"] in top["action"]
    assert "channel" in top["action"].lower(), (
        "the action should point at the next level the hierarchy allows"
    )


def test_the_recommendation_carries_its_owner_impact_confidence_and_monitoring(tenant):
    """All eight parts present, or explicitly absent. An action alone is an order."""

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    top = body["result"]["recommendations"][0]

    assert top["finding"]
    assert top["target_area"] is not None
    assert top["lever"]["label"]
    assert top["action"]
    assert top["impact"]["level"] in {"HIGH", "MEDIUM", "LOW"}
    assert top["owner"]
    assert top["confidence"]["level"] in {"HIGH", "MEDIUM", "LOW"}
    assert top["monitoring"]["metrics"], "an action with nothing to watch cannot be reviewed"
    assert top["monitoring"]["window"] == "Next 3 comparable periods"
    assert top["priority"] in {"HIGH_PRIORITY", "MEDIUM_PRIORITY", "PREVENTIVE_ACTION"}
    assert top["priority_label"]

    # The expandable trail: verdict, deviation, comparison, contributor, share,
    # confidence, lever provenance. A reader who disagrees needs all of it.
    why = " ".join(top["why"]).lower()
    for expected in ("kpi verdict", "deviation", "comparison basis", "top contributor",
                     "contribution", "confidence", "lever"):
        assert expected in why, f"the why-trail is missing {expected!r}"

    # And exactly one preventive card, ranked as its own thing rather than as a
    # weaker corrective action.
    priorities = [item["priority"] for item in body["result"]["recommendations"]]
    assert priorities.count("PREVENTIVE_ACTION") == 1
    assert priorities[-1] == "PREVENTIVE_ACTION"


def test_the_lever_is_a_registered_controllable_driver_and_never_an_uncontrollable_one(tenant):
    """A lever is a lever because the company said the business can pull it.

    ``controllable`` exists in the KPI registration precisely to record that, and a
    recommendation to review something nobody can change is noise. Competitor
    pricing is registered here and marked uncontrollable: it may explain a movement,
    but it can never be an action.
    """

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    result = body["result"]

    corrective = [
        item for item in result["recommendations"] if item["priority"] != "PREVENTIVE_ACTION"
    ]
    assert corrective
    for item in corrective:
        assert item["lever"]["source"] == "KPI_DRIVER", (
            "this KPI registered controllable drivers, so the defaults must not be used"
        )
        assert item["lever"]["driver_name"] in {"Order volume", "Promotions"}
        assert "registered as a controllable driver" in item["lever"]["note"].lower()

    prose = prose_of(result).lower()
    assert "competitor pricing" not in prose, (
        "an uncontrollable driver must never surface as a recommended action"
    )


def test_the_owner_follows_the_lever_and_the_area(tenant):
    """Who to hand it to is derived, not typed into a component.

    Order volume in a region is a regional sales question; promotions are a
    marketing question wherever they happen. Both are decided by the mapping layer
    from the lever and the dimension, which is why neither string appears in the UI.
    """

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    owners = {item["lever"]["key"]: item["owner"] for item in body["result"]["recommendations"]}

    assert owners.get("order_volume") == "Regional Sales Manager"
    assert owners.get("promotions") == "Marketing Manager"


# ---------------------------------------------------------------------------
# The two things this layer may never produce
# ---------------------------------------------------------------------------
def test_no_recommendation_claims_a_cause(tenant):
    """The whole payload, checked for the verbs that would make it a causal claim."""

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    prose = prose_of(body["result"]).lower()

    for word in CAUSAL_WORDS:
        assert word not in prose, f"a recommendation claimed cause with {word!r}"

    # And the note is not hidden behind a disclosure: every card carries it.
    assert body["result"]["causation_note"] == CAUSATION_NOTE
    for item in body["result"]["recommendations"]:
        assert item["causation_note"] == CAUSATION_NOTE
    assert CAUSATION_NOTE in body["result"]["limitations"]


def test_impact_is_a_qualitative_band_with_a_stated_basis_and_never_a_figure(tenant):
    """No money, no percentage of recovery, no promised outcome.

    The platform measures no counterfactual, so the honest answer to "what is this
    worth" is a band and the reason for it. The basis names the KPI's registered
    criticality and how concentrated the movement is -- both stored facts.
    """

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    result = body["result"]

    for item in result["recommendations"]:
        impact = item["impact"]
        assert impact["level"] in {"HIGH", "MEDIUM", "LOW"}
        assert "potential impact" in impact["label"].lower()
        assert "criticality" in impact["basis"].lower()
        assert "INR" not in impact["label"]

    prose = prose_of(result).lower()
    for phrase in GUARANTEE_WORDS:
        assert phrase not in prose, f"a recommendation promised an outcome with {phrase!r}"

    assert any("no counterfactual" in line.lower() for line in result["limitations"])


def test_every_figure_in_the_prose_is_a_stored_figure(tenant):
    """Take the numbers out of the advice and find each one in the evidence.

    This is the same discipline the explainability suite applies to explanations,
    and it is what makes a recommendation auditable: the layer re-renders stored
    columns and computes nothing, so a figure that is not in the run or the
    breakdown is a figure somebody made up.
    """

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    result = body["result"]
    summary = result["evidence_summary"]
    area = result["target_area"]

    allowed: set[str] = {"0", "1", "2", "3"}  # ordinals, counts of listed steps
    for value in (
        summary["actual"],
        summary["expected"],
        summary["deviation_absolute"],
        summary["deviation_pct"],
        summary["reference_count"],
        None if area is None else area["share_pct"],
        None if area is None else area["change"],
    ):
        if value is None:
            continue
        allowed |= {
            f"{abs(value):,.0f}".replace(",", ""),
            f"{abs(value):.1f}",
            f"{abs(value):.3f}",
            str(abs(value)),
        }
    allowed |= numbers_in(result["target_date"])

    for item in result["recommendations"]:
        text = " ".join([item["finding"], item["action"], *item["why"], item["impact"]["basis"]])
        for number in numbers_in(text):
            assert number.lstrip("-") in allowed, (
                f"{number!r} appears in a recommendation but in no stored figure: {text}"
            )


# ---------------------------------------------------------------------------
# The verdicts that must not produce an intervention
# ---------------------------------------------------------------------------
def test_a_normal_result_recommends_no_corrective_action(tenant):
    """Nothing moved materially, so there is nothing to act on -- and no card offered.

    An empty list rather than a softened action: offering a "low priority review" of
    a KPI that is behaving would train a reader to ignore this panel entirely.
    """

    body = recommendations(tenant["admin"], tenant["base"], tenant["normal"]["run_id"])
    result = body["result"]

    assert result["verdict"] == "NORMAL"
    assert result["stance"] == "NO_ACTION"
    assert result["recommendations"] == []
    assert result["headline"] == NORMAL_HEADLINE
    assert "routine monitoring" in result["body"].lower()
    assert result["target_area"] is None
    assert result["awaiting_breakdown"] is False
    # Still says what to keep an eye on -- monitoring is not an intervention.
    assert result["monitoring"]["metrics"]


def test_a_low_confidence_result_withholds_intervention_and_asks_for_evidence(tenant):
    """The platform could not judge this, so it does not tell anyone to act.

    Acting hard on an unjudgeable number is the specific failure this platform
    exists to prevent, and a recommendation layer that produced a confident action
    here would undo the verdict's whole point. No lever, no owner, no action --
    four evidence steps instead.
    """

    body = recommendations(tenant["admin"], tenant["base"], tenant["sparse"]["run_id"])
    result = body["result"]

    assert result["verdict"] == "LOW_CONFIDENCE"
    assert result["stance"] == "EVIDENCE_FIRST"
    assert result["recommendations"] == []
    assert result["headline"] == LOW_CONFIDENCE_HEADLINE
    assert "no direct intervention is recommended" in result["body"].lower()
    assert "recommended next step" in result["body"].lower()
    assert result["next_steps"] == list(LOW_CONFIDENCE_NEXT_STEPS)
    assert len(result["next_steps"]) == 4

    prose = prose_of(result).lower()
    for word in CAUSAL_WORDS:
        assert word not in prose


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------
def test_a_reader_without_investigation_access_is_named_no_area_and_told_why(tenant):
    """A breakdown names parts of the business. The permission travels with it.

    The same stored breakdown that sharpens an analyst's advice is invisible here,
    so this reader gets the KPI-level shape -- and a limitation saying so, rather
    than an unexplained absence they might read as "nothing was found".
    """

    body = recommendations(tenant["viewer"], tenant["base"], tenant["abnormal"]["run_id"])
    result = body["result"]

    assert result["target_area"] is None
    assert result["awaiting_breakdown"] is False, (
        "offering a breakdown button to somebody who may not run one is a dead end"
    )
    assert body["may_submit_feedback"] is False
    # This role does hold kpi.read, so provenance is returned -- and it must show
    # that no breakdown was read, rather than quietly omitting the fields.
    assert body["evidence"]["contribution_run_id"] is None
    assert body["evidence"]["contribution_dimension"] is None

    prose = prose_of(result)
    for region in A_REGIONS:
        assert region not in prose
    assert any("investigation access" in line.lower() for line in result["limitations"])


def test_one_company_never_sees_another_companys_result(tenant, module_client):
    """Scope comes from the resolved access context, never from the path."""

    other_admin, other_base, _ = provision(
        module_client,
        email="admin@borealis-recommend.example.com",
        company_name="Borealis Recommend",
        source_name="Borealis Ledger",
        source_path=tenant["source_path"],
        scope={"orders": "order_date"},
    )
    response = other_admin.get(
        f"{other_base}/detection-runs/{tenant['abnormal']['run_id']}/recommendations"
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Drilling deeper re-aims the advice
# ---------------------------------------------------------------------------
def test_drilling_deeper_re_aims_the_advice_at_the_deeper_area(tenant):
    """Region → Channel. The chain is the drill-down that actually happened.

    Read back from the stored breakdown's own ``path`` and ``depth`` rather than
    assembled by the browser, so the most specific evidence stored is the most
    specific area the advice may name -- and the entity type, review scope and
    owner all move with it.
    """

    region_split = breakdown(tenant["admin"], tenant["base"], dimension="region", top_k=8)
    leader_region = region_split["contributors"][0]["label"]

    deeper = breakdown(
        tenant["admin"],
        tenant["base"],
        dimension="channel",
        path=[{"dimension": "region", "value": leader_region}],
        top_k=8,
    )
    deeper_leader = deeper["contributors"][0]["label"]

    body = recommendations(tenant["admin"], tenant["base"], tenant["abnormal"]["run_id"])
    area = body["result"]["target_area"]

    assert area is not None
    assert area["depth"] == 1
    assert area["dimension"] == "channel"
    assert area["chain"] == [leader_region, deeper_leader]
    assert area["chain_label"] == f"{leader_region} → {deeper_leader}"
    assert area["entity_type"] == "Channel"
    assert area["drill_next"] == [], "channel declares no level beneath it"

    top = body["result"]["recommendations"][0]
    assert area["chain_label"] in top["finding"]
    assert area["chain_label"] in top["action"]
    # Nothing further to drill into, so the action must not invite one.
    assert "starting with the" not in top["action"]


# ---------------------------------------------------------------------------
# Feedback: the one thing here a human contributes
# ---------------------------------------------------------------------------
def test_feedback_is_recorded_upserted_and_cannot_touch_a_verdict(tenant):
    """A reader responds, corrects themselves, and the KPI's verdict is untouched."""

    run_id = tenant["abnormal"]["run_id"]
    body = recommendations(tenant["admin"], tenant["base"], run_id)
    key = body["result"]["recommendations"][0]["key"]

    first = tenant["admin"].post(
        f"{tenant['base']}/detection-runs/{run_id}/recommendation-feedback",
        json={
            "recommendation_key": key,
            "usefulness": "USEFUL",
            "action_status": "IN_REVIEW",
            "comment": "Handing this to the regional team this week.",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["feedback"]["usefulness"] == "USEFUL"
    assert first.json()["feedback"]["action_status"] == "IN_REVIEW"

    second = tenant["admin"].post(
        f"{tenant['base']}/detection-runs/{run_id}/recommendation-feedback",
        json={
            "recommendation_key": key,
            "usefulness": "NEEDS_REVIEW",
            "action_status": "ACTION_TAKEN",
        },
    )
    assert second.status_code == 200, second.text

    reread = recommendations(tenant["admin"], tenant["base"], run_id)
    entries = [row for row in reread["feedback"] if row["recommendation_key"] == key]
    assert len(entries) == 1, "a second submission corrects the first rather than stacking"
    assert entries[0]["usefulness"] == "NEEDS_REVIEW"
    assert entries[0]["action_status"] == "ACTION_TAKEN"
    assert entries[0]["comment"] is None, "an omitted comment clears the earlier one"

    # And the measurement is exactly where it was.
    detail = tenant["admin"].get(f"{tenant['base']}/detection-runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["result"]["status"] == "ABNORMAL"
    assert reread["result"]["verdict"] == "ABNORMAL"


def test_feedback_on_a_recommendation_this_result_never_produced_is_refused(tenant):
    """An orphan row no screen could show is a bug, not a datum."""

    run_id = tenant["abnormal"]["run_id"]
    response = tenant["admin"].post(
        f"{tenant['base']}/detection-runs/{run_id}/recommendation-feedback",
        json={"recommendation_key": "pricing|atlantis", "usefulness": "USEFUL"},
    )
    assert response.status_code == 422, response.text
    assert "not part of this result" in response.text


def test_an_unrecognised_response_is_refused(tenant):
    """The three responses are the platform's, and the client cannot invent a fourth."""

    run_id = tenant["abnormal"]["run_id"]
    body = recommendations(tenant["admin"], tenant["base"], run_id)
    key = body["result"]["recommendations"][0]["key"]

    response = tenant["admin"].post(
        f"{tenant['base']}/detection-runs/{run_id}/recommendation-feedback",
        json={"recommendation_key": key, "usefulness": "BRILLIANT"},
    )
    assert response.status_code == 422, response.text

    # The options a screen may offer come from the server, so it cannot offer one
    # the writer would reject.
    assert set(body["feedback_options"]["usefulness"]) == {
        "USEFUL",
        "NOT_USEFUL",
        "NEEDS_REVIEW",
    }
    assert set(body["feedback_options"]["action_status"]) == {
        "NOT_STARTED",
        "IN_REVIEW",
        "ACTION_TAKEN",
    }


def test_a_viewer_may_read_recommendations_but_not_respond_to_one(tenant):
    """Writing feedback puts a person's name to a conclusion. Same gate as a finding."""

    run_id = tenant["abnormal"]["run_id"]
    body = recommendations(tenant["admin"], tenant["base"], run_id)
    key = body["result"]["recommendations"][0]["key"]

    response = tenant["viewer"].post(
        f"{tenant['base']}/detection-runs/{run_id}/recommendation-feedback",
        json={"recommendation_key": key, "usefulness": "USEFUL"},
    )
    assert response.status_code == 403, response.text
