"""Proof that a breakdown across levels of detail does not multiply the KPI.

The KPI most companies care about is measured once per record, and the parts of the
business they want it split by are often recorded once per *line* of that record.
Those are two different grains, and the obvious way to bridge them is wrong: join
the record's total to its lines and sum, and the total is repeated once per line.
A record worth 1,000 with three lines reports 3,000. Percentages still add to
100%, every figure looks plausible, and the KPI on the screen above is now a
different number from the KPI the business signed off.

So this module asserts the property that failure mode breaks, and asserts it
against arithmetic done here in plain Python rather than by the engine:

* a breakdown along the finer table **sums to the KPI's own measured total**, not
  a multiple of it, and each part matches an independently computed figure;
* what the breakdown cannot attribute is a *shortfall*, never an excess, and it is
  disclosed rather than absorbed;
* a drill-down stays inside the ancestors already chosen, so a product figure is
  that product within that category within that area -- not the product's total;
* a part that was there on the comparable dates and is not there on this one is
  reported as a measured zero rather than dropped, because going to nothing is
  the clearest movement a breakdown can show;
* a KPI that is a distinct count is **refused** the finer levels rather than
  approximated, because there is no weighting that makes a fraction of a record
  true;
* a date the detection engine never analysed offers no investigation and reads
  nothing, so no figure can appear that the platform did not measure.

The fixture is the shape this problem actually occurs in: ``orders`` at one row per
record, ``order_items`` at one row per item, and an ``item_value`` that is
deliberately null on a small fraction of rows -- because the real question is not
whether the arithmetic works on clean data but what the platform says when it
cannot divide a record at all.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# Provisioning a company through the real API is a long sequence -- account,
# company, source, connection test, discovery, scope, profiling, KPI, validation,
# approval, comparison policy -- and it is the same sequence here as for detection.
# Reusing those helpers keeps this module about the arithmetic, and means an
# investigation is only ever tested on a KPI that was registered and approved the
# ordinary way.
from tests.test_detection_generalization import (
    approve_bucket_config,
    provision,
    register_kpi,
    run_detection,
)

#: A record whose lines carry no size at all cannot be divided, so its value sits
#: inside the KPI without belonging to any part of the breakdown. The fixture nulls
#: a small share of ``item_value`` on purpose, so a little unattributed movement is
#: expected -- and a lot would mean something else is wrong.
MIN_ATTRIBUTABLE_SHARE = 0.85


# ---------------------------------------------------------------------------
# The same arithmetic, done independently
# ---------------------------------------------------------------------------
def _rows(db_path: str, day: date) -> tuple[dict[str, tuple[str, float]], list[tuple[str, str, str, float]]]:
    """``{record: (area, value)}`` for one day, and every line of every record."""

    connection = sqlite3.connect(db_path)
    try:
        records = {
            str(row[0]): (str(row[1]), float(row[2]))
            for row in connection.execute(
                "SELECT order_id, region, order_value, order_date FROM orders"
            )
            if str(row[3])[:10] == day.isoformat()
        }
        lines = [
            (str(row[0]), str(row[1]), str(row[2]), float(row[3] or 0.0))
            for row in connection.execute(
                "SELECT order_id, sector, product_id, item_value FROM order_items"
            )
        ]
    finally:
        connection.close()
    return records, lines


def apportioned(
    db_path: str,
    day: date,
    *,
    by: str,
    area: str | None = None,
    category: str | None = None,
) -> dict[str, float]:
    """Divide each record's value between its own lines, then total by ``by``.

    Plain Python over the raw rows. The weight denominator is the record's total
    across *all* of its lines, never a filtered subset -- which is the whole reason
    the parts reconcile: filtering the numerator narrows what is counted without
    changing what each line is worth.
    """

    records, lines = _rows(db_path, day)

    weights: dict[str, float] = {}
    for record, _category, _line, size in lines:
        weights[record] = weights.get(record, 0.0) + size

    totals: dict[str, float] = {}
    for record, line_category, line, size in lines:
        if record not in records:
            continue
        denominator = weights.get(record, 0.0)
        if denominator <= 0:
            continue
        record_area, value = records[record]
        if area is not None and record_area != area:
            continue
        if category is not None and line_category != category:
            continue
        key = line_category if by == "category" else line
        totals[key] = totals.get(key, 0.0) + value * size / denominator
    return totals


def attributable_total(db_path: str, day: date) -> float:
    """The KPI's own total, restricted to records that can be divided at all."""

    records, lines = _rows(db_path, day)
    weights: dict[str, float] = {}
    for record, _category, _line, size in lines:
        weights[record] = weights.get(record, 0.0) + size
    return sum(
        value for record, (_area, value) in records.items() if weights.get(record, 0.0) > 0
    )


# ---------------------------------------------------------------------------
# One tenant, at two grains
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def module_client() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(scope="module")
def grained(module_client, source_fixture) -> dict:
    """A company whose KPI is measured per record over a source that has lines.

    Only ``orders`` is in the analytical scope, exactly as it would be for a KPI
    measured at that grain. ``order_items`` is registered by discovery and is what
    the finer breakdowns are read from -- which is the condition the investigation
    map checks before offering them.
    """

    target = date.fromisoformat(source_fixture["reference_date"])
    admin, base, tables = provision(
        module_client,
        email="admin@grain-investigation.example.com",
        company_name="Grain Investigation",
        source_name="Commerce Warehouse",
        source_path=source_fixture["path"],
        scope={"orders": "order_date"},
    )
    assert "order_items" in tables, "the finer-grained table was not discovered"

    total_id = register_kpi(
        admin,
        base,
        kpi_key="recorded_value",
        name="Recorded Value",
        formula="SUM(orders.order_value)",
        source_table_id=tables["orders"]["id"],
        time_field="order_date",
        tolerance_pct=8.0,
    )
    counted_id = register_kpi(
        admin,
        base,
        kpi_key="recorded_count",
        name="Recorded Count",
        formula="COUNT(DISTINCT orders.order_id)",
        source_table_id=tables["orders"]["id"],
        time_field="order_date",
        tolerance_pct=8.0,
        unit="count",
        currency=None,
    )
    approve_bucket_config(
        admin,
        base,
        config_key="grain-weekly",
        name="Weekly trading pattern",
        buckets={"same_day_of_week": {"enabled": True}},
    )
    detection = run_detection(admin, base, total_id, target)
    run_detection(admin, base, counted_id, target)

    return {
        "admin": admin,
        "base": base,
        "path": source_fixture["path"],
        "target": target,
        "total_id": total_id,
        "counted_id": counted_id,
        "kpi_actual": detection["result"]["actual"],
    }


def contribution(grained: dict, *, dimension: str, path: list[dict] | None = None, top_k: int = 50) -> dict:
    response = grained["admin"].post(
        f"{grained['base']}/investigation/contribution",
        json={
            "kpi_id": "recorded_value",
            "target_date": grained["target"].isoformat(),
            "dimension": dimension,
            "path": path or [],
            "top_k": top_k,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def assert_parts_match_raw_rows(
    contributors: list[dict],
    expected: dict[str, float],
    *,
    key: str = "label",
) -> float:
    """Every part equals the independently computed figure. Returns their total.

    A breakdown also lists what was there on the comparable dates and is *not*
    there on this one -- a part that went to nothing is movement, and dropping it
    would hide the clearest thing a breakdown can show. Those entries carry no
    activity on the date, so they are checked to be a measured zero standing on a
    real history rather than a value from somewhere else.
    """

    produced = {row[key]: row for row in contributors}
    assert not set(expected) - set(produced), (
        f"the breakdown omitted {sorted(set(expected) - set(produced))}"
    )
    for entity, value in expected.items():
        assert produced[entity]["actual"] == pytest.approx(value, rel=1e-9), (
            f"the apportioned figure for {entity} does not match the raw rows"
        )
    for entity, row in produced.items():
        if entity in expected:
            continue
        assert not row["actual"], (
            f"{entity} has no activity on this date, yet a figure was reported for it"
        )
        assert row["reference_count"] > 0, (
            f"{entity} was listed with neither activity on the date nor any history"
        )
    return sum(row["actual"] or 0.0 for row in contributors)


# ---------------------------------------------------------------------------
# What the KPI may be broken down by
# ---------------------------------------------------------------------------
def test_a_total_is_offered_the_finer_levels_and_a_distinct_count_is_not(grained):
    """The measure decides how far a breakdown can go, and says so up front.

    A total can be divided between the lines of a record. A distinct count of
    records cannot -- one record spans several lines, so a division would either
    count it once per line or hand back a fraction of a record. Both KPIs sit on
    the same table with the same lines beneath it, so the only thing separating the
    two answers is the measure itself.
    """

    admin, base = grained["admin"], grained["base"]

    offered = admin.get(f"{base}/investigation/dimensions", params={"kpi_id": "recorded_value"})
    assert offered.status_code == 200, offered.text
    by_name = {row["name"]: row for row in offered.json()["dimensions"]}
    assert set(by_name) == {"region", "sector", "product", "channel"}
    assert by_name["region"]["is_default"] is True
    # The hierarchy is what makes a drill-down guided rather than a free jump
    # between dimensions: one step is offered from each level, and the last is a
    # leaf.
    assert by_name["region"]["hierarchy"] == ["sector"]
    assert by_name["sector"]["hierarchy"] == ["product"]
    assert by_name["product"]["hierarchy"] == []
    # A dimension outside the hierarchy is still offered, and still leads nowhere:
    # it is there to be chosen by name in a manual analysis, not descended into.
    assert by_name["channel"]["hierarchy"] == []

    counted = admin.get(f"{base}/investigation/dimensions", params={"kpi_id": "recorded_count"})
    assert counted.status_code == 200, counted.text
    assert [row["name"] for row in counted.json()["dimensions"]] == ["region", "channel"], (
        "a distinct count was offered a breakdown it cannot be divided into"
    )

    refused = admin.post(
        f"{base}/investigation/contribution",
        json={
            "kpi_id": "recorded_count",
            "target_date": grained["target"].isoformat(),
            "dimension": "sector",
        },
    )
    assert refused.status_code == 404, refused.text
    assert "sector" in refused.text


# ---------------------------------------------------------------------------
# The property that a double count breaks
# ---------------------------------------------------------------------------
def test_a_breakdown_along_the_finer_table_still_sums_to_the_kpi(grained):
    """The parts add up to the whole, and to the same whole detection measured.

    This is the assertion a naive join fails, and it fails it loudly: repeating a
    record's value once per line would return several times the KPI here, because
    the fixture's records carry more than one line each. Every part is compared
    against a figure computed in this module from the raw rows, so a wrong
    apportionment cannot agree with it by construction.
    """

    body = contribution(grained, dimension="sector")
    result = body["result"]

    expected = apportioned(grained["path"], grained["target"], by="category")
    assert expected, "the fixture has no lines on the target date"

    total = assert_parts_match_raw_rows(result["contributors"], expected)

    # The sum of the parts is the KPI's own total over the records that can be
    # divided -- not a multiple of it, which is the failure this test exists for.
    divisible = attributable_total(grained["path"], grained["target"])
    assert total == pytest.approx(divisible, rel=1e-9)

    whole = grained["kpi_actual"]
    assert total <= whole + 1e-6, (
        "the breakdown reports more than the KPI itself -- the record value was "
        "counted once per line"
    )
    assert total >= whole * MIN_ATTRIBUTABLE_SHARE

    # And the shortfall is disclosed rather than absorbed into a part.
    assert any("divided" in note for note in result["notes"]), (
        "a breakdown that had to apportion the KPI did not say so"
    )


def test_the_same_breakdown_is_read_the_same_way_at_every_level(grained):
    """A drill-down narrows the question; it does not change the arithmetic.

    Each step adds an ancestor to the filter, and every level is still measured by
    dividing a record's value between its own lines. The independent figures below
    are computed with exactly the ancestors the request carried, so a step that
    silently dropped or widened a filter -- a product's company-wide total shown
    under one area, say -- would not match.
    """

    areas = contribution(grained, dimension="region")["result"]["contributors"]
    assert areas, "the default breakdown returned nothing"
    area = areas[0]["label"]

    step = [{"dimension": "region", "value": area}]
    categories = contribution(grained, dimension="sector", path=step)["result"]
    within_area = apportioned(grained["path"], grained["target"], by="category", area=area)
    inside = assert_parts_match_raw_rows(categories["contributors"], within_area)

    # Narrowing is real: no category inside one area exceeds that category across
    # the company, and the area as a whole is strictly smaller than the company.
    company_wide = apportioned(grained["path"], grained["target"], by="category")
    assert all(within_area[label] <= company_wide[label] + 1e-9 for label in within_area)
    assert inside < sum(company_wide.values()) - 1e-6

    category = max(within_area, key=lambda key: within_area[key])
    deeper = [*step, {"dimension": "sector", "value": category}]
    leaves = contribution(grained, dimension="product", path=deeper)["result"]
    within_both = apportioned(
        grained["path"], grained["target"], by="line", area=area, category=category
    )
    assert within_both, "the chosen area and category have no items on this date"
    assert_parts_match_raw_rows(leaves["contributors"], within_both, key="entity")

    # The leaf is an identifier in the data and a name on the screen. The identifier
    # stays the thing that was filtered on; the name is only how it is read.
    labelled = {row["entity"]: row["label"] for row in leaves["contributors"]}
    assert any(label != entity for entity, label in labelled.items()), (
        "no display name was resolved for an identifier-valued dimension"
    )
    assert leaves["path"] == [
        {"dimension": "region", "value": area},
        {"dimension": "sector", "value": category},
    ]
    assert leaves["next_dimensions"] == []


# ---------------------------------------------------------------------------
# The gate, and the picker behind it
# ---------------------------------------------------------------------------
def test_an_unanalysed_date_offers_no_investigation_and_no_figures(grained):
    """No stored run, no investigation -- and nothing computed to fill the gap.

    The movement an investigation splits is the one detection measured. For a date
    the engine never analysed there is no such movement, so the surface is told to
    run the analysis first and is given an empty list rather than a breakdown of a
    number nobody has seen.
    """

    admin, base = grained["admin"], grained["base"]
    unanalysed = grained["target"] + timedelta(days=1)

    blocked = admin.get(
        f"{base}/investigation/entities",
        params={"kpi_id": "recorded_value", "target_date": unanalysed.isoformat()},
    )
    assert blocked.status_code == 200, blocked.text
    payload = blocked.json()
    assert payload["run_available"] is False
    assert payload["entities"] == []
    assert payload["kpi_status"] is None
    assert "run the kpi analysis first" in payload["message"].lower()

    refused = admin.post(
        f"{base}/investigation/contribution",
        json={"kpi_id": "recorded_value", "target_date": unanalysed.isoformat()},
    )
    assert refused.status_code == 409, refused.text


def test_the_entities_offered_come_from_the_data(grained):
    """The picker is a measurement, not a list someone typed into the platform.

    Every entry is read from the company's own source for the KPI and date in
    question, which is why it can be trusted to be the current vocabulary -- and
    why none of it carries a verdict. Choosing one is what starts an analysis of
    it; appearing on the list is not an analysis.
    """

    admin, base = grained["admin"], grained["base"]
    response = admin.get(
        f"{base}/investigation/entities",
        params={
            "kpi_id": "recorded_value",
            "target_date": grained["target"].isoformat(),
            "dimension": "sector",
            "limit": 10,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["run_available"] is True
    assert payload["run_state"] == "COMPLETED"
    assert payload["kpi_status"] in {"NORMAL", "ABNORMAL", "LOW_CONFIDENCE"}
    assert payload["dimension"] == "sector"
    assert payload["next_dimensions"] == ["product"]

    expected = apportioned(grained["path"], grained["target"], by="category")
    assert {row["entity"] for row in payload["entities"]} == set(expected)
    for row in payload["entities"]:
        assert row["value"] == pytest.approx(expected[row["entity"]], rel=1e-9)
        for forbidden in ("status", "verdict", "anomaly", "severity"):
            assert forbidden not in row, (
                f"the entity picker carries {forbidden!r}, which is a judgement "
                "nothing has computed"
            )

    # Largest first, so the list is useful without being ranked by anything the
    # platform has decided.
    values = [row["value"] for row in payload["entities"]]
    assert values == sorted(values, reverse=True)
    assert sum(row["share_of_total_pct"] for row in payload["entities"]) == pytest.approx(
        100.0, abs=1e-6
    )


# ---------------------------------------------------------------------------
# One entity, judged by the engine that judges the KPI
# ---------------------------------------------------------------------------
def test_a_named_entity_is_judged_at_its_own_grain_by_the_one_engine(grained):
    """An entity recorded finer than the KPI can still be analysed, and is judged
    by the platform's own detection engine rather than by a second one.

    Two things are being asserted at once, and the second is the harder one.

    The entity here lives on the *lines*, not on the records the KPI is measured
    on, so classifying it means the engine has to read a divided figure for the
    target date and for every comparable date -- the same apportionment a
    breakdown uses, otherwise the entity's own history would be measured
    differently from its present.

    And the verdict has to be the platform's single classification. So the status
    is checked to come from the KPI's own vocabulary, the expectation is checked to
    rest on comparable dates rather than on a trailing average, and the response is
    checked for the absence of any *second* scoring vocabulary -- a severity, a
    confidence, a score of its own.
    """

    admin, base = grained["admin"], grained["base"]

    before = admin.get(
        f"{base}/investigation/entities",
        params={"kpi_id": "recorded_value", "target_date": grained["target"].isoformat()},
    )
    assert before.status_code == 200, before.text
    kpi_status_before = before.json()["kpi_status"]

    ranked = contribution(grained, dimension="sector")["result"]["contributors"]
    chosen = next(row["entity"] for row in ranked if row["actual"])

    response = admin.post(
        f"{base}/investigation/analysis",
        json={
            "kpi_id": "recorded_value",
            "dimension": "sector",
            "entity": chosen,
            "target_date": grained["target"].isoformat(),
            "lookback_days": 7,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    result = body["result"]

    assert body["mode"] == "entity"
    assert result["entity"] == chosen
    assert result["target_date"] == grained["target"].isoformat()

    # One classification, the KPI's own. Not a scale of this screen's invention.
    assert result["status"] in {"NORMAL", "ABNORMAL", "LOW_CONFIDENCE"}
    assert result["direction"] in {"UP", "DOWN", "FLAT"}
    assert result["headline"], "a verdict was reported without anything a person can read"
    assert result["status_reason"], "a verdict was reported without its reasoning"
    for invented in ("severity", "score", "confidence", "risk", "z_score"):
        assert invented not in result, (
            f"the entity analysis carries {invented!r}, which is a second "
            "classification system"
        )

    # The actual is the entity's own divided figure -- the same one the breakdown
    # attributed to it, so the two screens cannot disagree about what it did.
    attributed = next(row["actual"] for row in ranked if row["entity"] == chosen)
    assert result["actual"] == pytest.approx(attributed, rel=1e-9)

    # The expectation rests on comparable dates chosen by the approved policy, and
    # the variance is the difference between the two. Nothing is rounded into
    # agreement here: the arithmetic is checked.
    assert body["evidence"]["reference_dates"], (
        "the entity was judged without any comparable history being named"
    )
    assert grained["target"].isoformat() not in body["evidence"]["reference_dates"], (
        "the date being judged was used as part of its own expectation"
    )
    if result["expected"] is not None:
        assert result["variance"] == pytest.approx(
            result["actual"] - result["expected"], rel=1e-9
        )

    # A share, so a reader knows how much of the business this verdict is about --
    # and it is a share of the KPI, not of a total recomputed for this screen.
    assert result["share_of_kpi_pct"] == pytest.approx(
        attributed / abs(grained["kpi_actual"]) * 100.0, rel=1e-9
    )
    assert 0.0 < result["share_of_kpi_pct"] <= 100.0

    # The KPI's own stored verdict is untouched by any of this: an entity was
    # judged, and the number the business signed off did not move.
    after = admin.get(
        f"{base}/investigation/entities",
        params={"kpi_id": "recorded_value", "target_date": grained["target"].isoformat()},
    )
    assert after.status_code == 200, after.text
    assert after.json()["kpi_status"] == kpi_status_before


def test_an_unanalysed_date_refuses_an_entity_analysis_too(grained):
    """The gate does not have a back door.

    A named entity is investigated against the movement the platform measured, so
    the date it is named on has to be a date the engine analysed -- exactly as for
    a breakdown. Otherwise the strictest path on the screen would be reachable by
    typing around the loosest.
    """

    admin, base = grained["admin"], grained["base"]
    unanalysed = grained["target"] + timedelta(days=1)

    refused = admin.post(
        f"{base}/investigation/analysis",
        json={
            "kpi_id": "recorded_value",
            "dimension": "sector",
            "entity": "anything",
            "target_date": unanalysed.isoformat(),
        },
    )
    assert refused.status_code == 409, refused.text
    assert "run the kpi analysis first" in refused.text.lower()
