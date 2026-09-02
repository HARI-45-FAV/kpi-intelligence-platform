"""Proof that one detection engine serves any company and any KPI.

The claim under test is architectural, so the tests are built to be able to
falsify it rather than to illustrate it. Two tenants are provisioned end to end
through the real API -- connect a source, approve a scope, profile it, register a
KPI, validate it, approve it, approve a comparison policy, run detection -- and
they are made to disagree about everything the engine touches:

* different table names (``orders`` vs ``sales_transactions``);
* different measure columns (``net_revenue`` vs ``amount``);
* different time fields, and different *types* of time field (a DATE vs a
  TIMESTAMP);
* different busiest weekdays (Friday vs Tuesday);
* within Company B, a second KPI on a different table with a third time field.

Then the same endpoint is called for both. If any company- or KPI-specific
knowledge had leaked into the algorithm, one of the two tenants would produce the
wrong number, and every figure asserted below is exact rather than approximate --
see :mod:`tests.fixture_generalization` for how the seeded daily totals are made
exact, and note that the expected median and MAD are recomputed here with the
standard library instead of by calling the engine's own statistics.

Two further tests attack the claim structurally rather than behaviourally:
:func:`test_the_algorithm_names_no_company_table_column_weekday_or_event` reads
the engine's own source and fails if a company-specific literal appears in
executable code, and :func:`test_no_model_can_reach_the_arithmetic` fails if the
numeric path acquires a dependency on the model layer.
"""

from __future__ import annotations

import ast
import json
import statistics
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.core.config import settings
from app.llm.provider import LLMProvider, LLMResponse, LLMUsage
from tests.conftest import API, ApiActor, register
from tests.fixture_generalization import (
    A_TARGET_ORDERS,
    A_TARGET_REVENUE,
    B_TARGET_AMOUNT,
    B_TARGET_REFUND,
    BUDGET,
    COMPANY_A_TARGET,
    COMPANY_B_TARGET,
    a_order_count_for,
    a_revenue_total_for,
    b_amount_total_for,
    b_refund_total_for,
    build_company_a_source,
    build_company_b_source,
)

MODIFIED_Z_CONSTANT = 0.6745


# ---------------------------------------------------------------------------
# Independent expectations. Deliberately not imported from the engine: a test
# that reuses the implementation cannot detect a wrong implementation.
# ---------------------------------------------------------------------------
def comparable_weekdays(target: date, iso_weekday: int, count: int = BUDGET) -> list[date]:
    """The ``count`` most recent dates before ``target`` on the same weekday."""
    found: list[date] = []
    offset = 1
    while len(found) < count:
        day = target - timedelta(days=offset)
        if day.isoweekday() == iso_weekday:
            found.append(day)
        offset += 1
    return found


def expected_statistics(actual: float, values: list[float]) -> dict:
    """Median, MAD and modified z-score, computed with the standard library."""
    median = statistics.median(values)
    mad = statistics.median([abs(value - median) for value in values])
    return {
        "median": median,
        "mad": mad,
        "z": MODIFIED_Z_CONSTANT * (actual - median) / mad,
        "deviation_pct": (actual - median) / abs(median) * 100.0,
    }


# ---------------------------------------------------------------------------
# Provisioning. One helper, used for both companies, parameterised by nothing
# but the tenant's own data -- which is the point being tested.
# ---------------------------------------------------------------------------
def provision(
    client: TestClient,
    *,
    email: str,
    company_name: str,
    source_name: str,
    source_path: str,
    scope: dict[str, str],
) -> tuple[ApiActor, str, dict]:
    """Take a company from "no account" to "profiled source", through the API."""
    admin = register(client, email, "Detection-Tests-2026", f"Admin of {company_name}")
    created = admin.post(f"{API}/companies", json={"company_name": company_name})
    assert created.status_code == 201, created.text
    base = f"{API}/companies/{created.json()['id']}"

    source = admin.post(
        f"{base}/data-sources",
        json={
            "name": source_name,
            "source_type": "SQLITE",
            "path": source_path,
            "refresh_frequency": "DAILY",
            "timezone": "Asia/Kolkata",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    tested = admin.post(f"{base}/data-sources/{source_id}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True

    discovered = admin.post(f"{base}/data-sources/{source_id}/discover")
    assert discovered.status_code == 200, discovered.text

    tables = {row["table_name"]: row for row in admin.get(f"{base}/tables").json()}
    for name in scope:
        assert name in tables, f"{name} was not discovered in {source_name}"

    scoped = admin.put(
        f"{base}/data-scope",
        json={
            "replace": True,
            "tables": [
                {
                    "source_table_id": tables[name]["id"],
                    "enabled": True,
                    "primary_time_column": time_column,
                }
                for name, time_column in scope.items()
            ],
        },
    )
    assert scoped.status_code == 200, scoped.text

    analysed = admin.post(f"{base}/analysis/run")
    assert analysed.status_code == 200, analysed.text

    tables = {row["table_name"]: row for row in admin.get(f"{base}/tables").json()}
    return admin, base, tables


def register_kpi(
    admin: ApiActor,
    base: str,
    *,
    kpi_key: str,
    name: str,
    formula: str,
    source_table_id: str,
    time_field: str,
    tolerance_pct: float,
    unit: str = "currency",
    currency: str | None = "INR",
) -> str:
    """Register, validate and approve one KPI. Returns its definition id.

    The engine reads ``source_table_id``, ``formula`` and ``time_field`` back out
    of this registration -- they are the only place it learns what to measure and
    where.
    """
    created = admin.post(
        f"{base}/kpis",
        json={
            "kpi_key": kpi_key,
            "name": name,
            "business_definition": f"{name}, as defined by the company's finance team.",
            "formula_expression": formula,
            "source_table_id": source_table_id,
            "time_field": time_field,
            "time_grain": "DAY",
            "unit": unit,
            "currency": currency,
            "materiality": {
                "relative_threshold_pct": tolerance_pct,
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
        json={"reason": "Definition signed off for the detection generalisation suite."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "ACTIVE"
    return definition["id"]


def approve_bucket_config(
    admin: ApiActor,
    base: str,
    *,
    config_key: str,
    name: str,
    buckets: dict,
    kpi_key: str | None = None,
) -> dict:
    """Draft, propose and approve a comparison policy.

    Approval is a separate step by design: an unreviewed comparison basis
    silently changes every number computed after it, so the engine only reads
    APPROVED rows.
    """
    created = admin.post(
        f"{base}/bucket-configs",
        json={
            "config_key": config_key,
            "name": name,
            "kpi_key": kpi_key,
            "buckets": buckets,
        },
    )
    assert created.status_code == 201, created.text
    config_id = created.json()["id"]

    proposed = admin.post(f"{base}/bucket-configs/{config_id}/propose", json={})
    assert proposed.status_code == 200, proposed.text

    approved = admin.post(
        f"{base}/bucket-configs/{config_id}/approve",
        json={"reason": "Comparison basis reviewed against the company's trading calendar."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    return approved.json()


def run_detection(admin: ApiActor, base: str, kpi_id: str, target: date) -> dict:
    response = admin.post(
        f"{base}/run-detection",
        json={"kpi_id": kpi_id, "target_date": target.isoformat()},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The two tenants. Module-scoped: provisioning profiles a real source, and the
# claim is about one engine serving both, not about setup speed.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def module_client() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(scope="module")
def company_a(module_client, tmp_path_factory) -> dict:
    """Aurora Retail: ``orders`` / ``SUM(net_revenue)`` / ``order_date`` / Friday."""
    seeded = build_company_a_source(tmp_path_factory.mktemp("aurora") / "aurora_retail.db")
    admin, base, tables = provision(
        module_client,
        email="admin@aurora-retail.example.com",
        company_name="Aurora Retail",
        source_name="Aurora Commerce",
        source_path=seeded["path"],
        scope={"orders": "order_date"},
    )
    revenue_id = register_kpi(
        admin,
        base,
        kpi_key="revenue",
        name="Revenue",
        formula="SUM(orders.net_revenue)",
        source_table_id=tables["orders"]["id"],
        time_field="order_date",
        tolerance_pct=8.0,
    )
    order_count_id = register_kpi(
        admin,
        base,
        kpi_key="order_count",
        name="Order Count",
        formula="COUNT(DISTINCT orders.order_id)",
        source_table_id=tables["orders"]["id"],
        time_field="order_date",
        tolerance_pct=15.0,
        unit="count",
        currency=None,
    )
    # Friday is this company's trading peak, and it has two full years of
    # history, so year-over-year is available as a stability reference.
    approve_bucket_config(
        admin,
        base,
        config_key="aurora-weekly",
        name="Aurora weekly trading pattern",
        buckets={
            "same_day_of_week": {"enabled": True, "days": ["FRI"]},
            "yoy_period": {"enabled": True},
        },
    )
    return {
        "admin": admin,
        "base": base,
        "tables": tables,
        "revenue_id": revenue_id,
        "order_count_id": order_count_id,
        "target": COMPANY_A_TARGET,
    }


@pytest.fixture(scope="module")
def company_b(module_client, tmp_path_factory) -> dict:
    """Borealis Foods: ``sales_transactions`` / ``SUM(amount)`` / a TIMESTAMP / Tuesday."""
    seeded = build_company_b_source(tmp_path_factory.mktemp("borealis") / "borealis_foods.db")
    admin, base, tables = provision(
        module_client,
        email="admin@borealis-foods.example.com",
        company_name="Borealis Foods",
        source_name="Borealis Sales",
        source_path=seeded["path"],
        scope={"sales_transactions": "transaction_date", "refunds": "refund_date"},
    )
    revenue_id = register_kpi(
        admin,
        base,
        kpi_key="revenue",
        name="Revenue",
        formula="SUM(sales_transactions.amount)",
        source_table_id=tables["sales_transactions"]["id"],
        time_field="transaction_date",
        tolerance_pct=10.0,
    )
    refunds_id = register_kpi(
        admin,
        base,
        kpi_key="refund_value",
        name="Refund Value",
        formula="SUM(refunds.refund_amount)",
        source_table_id=tables["refunds"]["id"],
        time_field="refund_date",
        tolerance_pct=25.0,
    )
    # A different weekday, and no year-over-year. Same five slots, different values.
    approve_bucket_config(
        admin,
        base,
        config_key="borealis-weekly",
        name="Borealis weekly trading pattern",
        buckets={
            "same_day_of_week": {"enabled": True, "days": ["TUE"]},
            "yoy_period": {"enabled": False},
        },
    )
    return {
        "admin": admin,
        "base": base,
        "tables": tables,
        "revenue_id": revenue_id,
        "refunds_id": refunds_id,
        "target": COMPANY_B_TARGET,
    }


# ---------------------------------------------------------------------------
# The central proof
# ---------------------------------------------------------------------------
def test_one_engine_detects_for_two_companies_that_share_no_schema(company_a, company_b):
    """The same endpoint, the same code path, two unlike tenants, both correct.

    Company A's Friday collapsed; Company B's Tuesday was ordinary. Both numbers
    are exact, and both are derived from the tenant's own registered source and
    its own approved comparison policy.
    """
    a = run_detection(company_a["admin"], company_a["base"], company_a["revenue_id"], COMPANY_A_TARGET)
    b = run_detection(company_b["admin"], company_b["base"], company_b["revenue_id"], COMPANY_B_TARGET)

    # ---- Company A: orders / SUM(net_revenue) / order_date / Friday ----------
    a_values = [float(a_revenue_total_for(day)) for day in comparable_weekdays(COMPANY_A_TARGET, 5)]
    a_stats = expected_statistics(A_TARGET_REVENUE, a_values)

    assert a["result"]["actual"] == pytest.approx(A_TARGET_REVENUE)
    assert a["result"]["expected"] == pytest.approx(a_stats["median"])
    assert a["result"]["expected"] == pytest.approx(10_250_000.0), "the seeded Friday median"
    assert a["result"]["deviation_pct"] == pytest.approx(a_stats["deviation_pct"], abs=1e-6)
    assert a["result"]["deviation_pct"] == pytest.approx(-41.4634, abs=1e-3)
    assert a["result"]["status"] == "ABNORMAL"
    assert "friday" in a["result"]["comparison"].lower()

    assert a["evidence"]["statistics"]["median"] == pytest.approx(a_stats["median"])
    assert a["evidence"]["statistics"]["mad"] == pytest.approx(a_stats["mad"])
    assert a["evidence"]["statistics"]["modified_z_score"] == pytest.approx(a_stats["z"], abs=1e-6)
    assert a["evidence"]["source"]["table"] == "orders"
    assert a["evidence"]["source"]["time_field"] == "order_date"
    assert "net_revenue" in a["evidence"]["source"]["formula"]

    # ---- Company B: sales_transactions / SUM(amount) / transaction_date / Tuesday
    b_values = [float(b_amount_total_for(day)) for day in comparable_weekdays(COMPANY_B_TARGET, 2)]
    b_stats = expected_statistics(B_TARGET_AMOUNT, b_values)

    assert b["result"]["actual"] == pytest.approx(B_TARGET_AMOUNT)
    assert b["result"]["expected"] == pytest.approx(b_stats["median"])
    assert b["result"]["expected"] == pytest.approx(825_000.0), "the seeded Tuesday median"
    assert b["result"]["deviation_pct"] == pytest.approx(b_stats["deviation_pct"], abs=1e-6)
    assert b["result"]["status"] == "NORMAL"
    assert "tuesday" in b["result"]["comparison"].lower()

    assert b["evidence"]["statistics"]["median"] == pytest.approx(b_stats["median"])
    assert b["evidence"]["statistics"]["mad"] == pytest.approx(b_stats["mad"])
    assert b["evidence"]["statistics"]["modified_z_score"] == pytest.approx(b_stats["z"], abs=1e-6)
    assert b["evidence"]["source"]["table"] == "sales_transactions"
    assert b["evidence"]["source"]["time_field"] == "transaction_date"
    assert "amount" in b["evidence"]["source"]["formula"]

    # ---- And the two tenants really are different all the way down -----------
    assert a["evidence"]["source"]["table"] != b["evidence"]["source"]["table"]
    assert a["evidence"]["source"]["time_field"] != b["evidence"]["source"]["time_field"]
    assert a["evidence"]["bucket"]["config_key"] != b["evidence"]["bucket"]["config_key"]
    assert a["evidence"]["bucket"]["applied"] == b["evidence"]["bucket"]["applied"] == (
        "SAME_DAY_OF_WEEK"
    ), "the same fixed bucket slot, filled with different company values"


def test_the_reference_set_is_the_configured_weekday_and_never_the_target(company_a, company_b):
    """Comparable history is selected by the calendar, not by a trailing window.

    A "last 7 days" baseline would put Monday through Thursday into a Friday's
    expectation. Every reference date here is the target's own weekday, and the
    target is never compared against itself.
    """
    a = run_detection(company_a["admin"], company_a["base"], company_a["revenue_id"], COMPANY_A_TARGET)
    b = run_detection(company_b["admin"], company_b["base"], company_b["revenue_id"], COMPANY_B_TARGET)

    a_dates = [date.fromisoformat(point["date"]) for point in a["evidence"]["reference"]["points"]]
    assert a_dates, "Company A must have comparable history"
    assert all(day.isoweekday() == 5 for day in a_dates), "every reference must be a Friday"
    assert COMPANY_A_TARGET not in a_dates

    b_dates = [date.fromisoformat(point["date"]) for point in b["evidence"]["reference"]["points"]]
    assert b_dates, "Company B must have comparable history"
    assert all(day.isoweekday() == 2 for day in b_dates), "every reference must be a Tuesday"
    assert COMPANY_B_TARGET not in b_dates

    # Company B enabled no year-over-year, so its reference set is one budget.
    assert len(b_dates) == BUDGET
    # Company A did, so the prior year is carried as a growth reference and the
    # expectation still comes from the most recent year alone.
    assert len(a_dates) == 2 * BUDGET
    assert a["evidence"]["year_over_year"]["applied"] is True
    assert a["evidence"]["year_over_year"]["factor"] == pytest.approx(1.0)
    assert b["evidence"]["year_over_year"]["applied"] is False


def test_multiple_kpis_on_one_date_and_one_policy_reach_different_verdicts(company_a, company_b):
    """Different KPIs, same company, same date, same configuration.

    Company A's order count held steady on the day its revenue collapsed;
    Company B's refunds doubled on the day its revenue looked ordinary. Nothing
    about the engine changed between any of these four runs -- only the
    registered source, formula and time field it was pointed at.
    """
    orders = run_detection(
        company_a["admin"], company_a["base"], company_a["order_count_id"], COMPANY_A_TARGET
    )
    refunds = run_detection(
        company_b["admin"], company_b["base"], company_b["refunds_id"], COMPANY_B_TARGET
    )

    # A different formula against the *same* table as Company A's revenue KPI.
    count_values = [float(a_order_count_for(day)) for day in comparable_weekdays(COMPANY_A_TARGET, 5)]
    count_stats = expected_statistics(A_TARGET_ORDERS, count_values)
    assert orders["result"]["actual"] == pytest.approx(A_TARGET_ORDERS)
    assert orders["result"]["expected"] == pytest.approx(count_stats["median"])
    assert orders["result"]["status"] == "NORMAL"
    assert orders["evidence"]["source"]["table"] == "orders"
    assert "COUNT" in orders["evidence"]["source"]["formula"].upper()

    # A different table *and* a different time field within Company B.
    refund_values = [float(b_refund_total_for(day)) for day in comparable_weekdays(COMPANY_B_TARGET, 2)]
    refund_stats = expected_statistics(B_TARGET_REFUND, refund_values)
    assert refunds["result"]["actual"] == pytest.approx(B_TARGET_REFUND)
    assert refunds["result"]["expected"] == pytest.approx(refund_stats["median"])
    assert refunds["result"]["deviation_pct"] == pytest.approx(100.0, abs=1e-6)
    assert refunds["result"]["status"] == "ABNORMAL"
    assert refunds["evidence"]["source"]["table"] == "refunds"
    assert refunds["evidence"]["source"]["time_field"] == "refund_date"

    # Same company, same date, same approved policy -- two different tables.
    revenue = run_detection(
        company_b["admin"], company_b["base"], company_b["revenue_id"], COMPANY_B_TARGET
    )
    assert revenue["evidence"]["bucket"]["config_key"] == refunds["evidence"]["bucket"]["config_key"]
    assert revenue["evidence"]["source"]["table"] != refunds["evidence"]["source"]["table"]
    assert revenue["result"]["status"] == "NORMAL"
    assert refunds["result"]["status"] == "ABNORMAL"


def test_a_kpis_threshold_follows_its_own_level_and_never_the_size_of_its_numbers(company_a):
    """Two KPIs six orders of magnitude apart, judged on their own terms.

    Company A's revenue is seeded in millions and its order count in single
    digits -- on the same table, the same date and the same approved policy. If any
    universal magnitude were wired into the engine (a currency amount, a unit
    count, an absolute deviation floor) the small KPI would be unjudgeable and the
    large one would look abnormal for being large. Instead each KPI's materiality
    is a percentage of its *own* expected level, read from its own registration,
    so the two carry different floors and neither floor is a size.
    """
    revenue = run_detection(
        company_a["admin"], company_a["base"], company_a["revenue_id"], COMPANY_A_TARGET
    )
    orders = run_detection(
        company_a["admin"], company_a["base"], company_a["order_count_id"], COMPANY_A_TARGET
    )

    # Six orders of magnitude apart, and nothing else differs.
    assert revenue["result"]["actual"] == pytest.approx(A_TARGET_REVENUE)
    assert orders["result"]["actual"] == pytest.approx(A_TARGET_ORDERS)
    assert revenue["evidence"]["bucket"]["config_key"] == orders["evidence"]["bucket"]["config_key"]
    assert revenue["evidence"]["source"]["table"] == orders["evidence"]["source"]["table"]

    # The floor each one is held to is its own registered tolerance, not a shared
    # constant -- 8% for revenue, 15% for the order count.
    assert revenue["evidence"]["tolerance"]["relative_floor_pct"] == pytest.approx(8.0)
    assert orders["evidence"]["tolerance"]["relative_floor_pct"] == pytest.approx(15.0)

    for result in (revenue, orders):
        tolerance = result["evidence"]["tolerance"]
        # A percentage, never an amount: nothing in the decision is denominated in
        # currency or in orders, so neither KPI can inherit the other's scale.
        assert tolerance["absolute"] is None
        # Materiality is that percentage applied to this KPI's own movement.
        assert tolerance["movement_is_material"] == (
            abs(result["result"]["deviation_pct"]) >= tolerance["relative_floor_pct"]
        )
        # And significance is measured against this KPI's own history, so the two
        # have their own centre and their own spread rather than a shared one.
        assert result["evidence"]["statistics"]["median"] == pytest.approx(
            result["result"]["expected"]
        )

    assert revenue["evidence"]["statistics"]["median"] != orders["evidence"]["statistics"]["median"]

    # The verdicts differ, and not in the direction a magnitude rule would give:
    # the KPI held to the *looser* 15% floor is the small one.
    assert revenue["result"]["status"] == "ABNORMAL"
    assert orders["result"]["status"] == "NORMAL"


def test_the_same_kpi_history_and_date_reproduce_the_same_verdict(company_a):
    """Determinism, checked by repetition rather than by reading the code.

    Every stored result is meant to be reproducible: a historical run reopened
    months later has to show what it showed on the day. So nothing in the path may
    vary between two runs -- no sampling, no clock, no set or dict ordering leaking
    into the median, the dispersion or the bucket precedence.
    """
    first = run_detection(
        company_a["admin"], company_a["base"], company_a["revenue_id"], COMPANY_A_TARGET
    )
    second = run_detection(
        company_a["admin"], company_a["base"], company_a["revenue_id"], COMPANY_A_TARGET
    )

    for field in ("actual", "expected", "deviation_absolute", "deviation_pct", "status"):
        assert first["result"][field] == second["result"][field], f"{field} is not reproducible"

    # The same statistics from the same reference set, chosen the same way.
    assert first["evidence"]["statistics"] == second["evidence"]["statistics"]
    assert [point["date"] for point in first["evidence"]["reference"]["points"]] == [
        point["date"] for point in second["evidence"]["reference"]["points"]
    ]
    assert first["evidence"]["bucket"]["signature"] == second["evidence"]["bucket"]["signature"]


def test_a_timestamp_time_field_covers_the_whole_trading_day(company_b):
    """Company B's revenue is only correct if the day's last instant is included.

    ``transaction_date`` is a TIMESTAMP and the seeded day runs from 00:00:01 to
    23:59:59. Bounding it by the bare date -- or by midnight to midnight -- drops
    transactions and understates every value the engine computes. The engine reads
    the column's profiled type; it does not guess from the column's name.
    """
    result = run_detection(
        company_b["admin"], company_b["base"], company_b["revenue_id"], COMPANY_B_TARGET
    )
    # The seeded total is only reachable if all five times of day are counted.
    assert result["result"]["actual"] == pytest.approx(B_TARGET_AMOUNT)

    columns = company_b["admin"].get(
        f"{company_b['base']}/tables/{company_b['tables']['sales_transactions']['id']}/columns"
    ).json()
    transaction_date = next(c for c in columns if c["column_name"] == "transaction_date")
    assert transaction_date["semantic_type"] == "TIMESTAMP", (
        "the whole-day bound is chosen from the profiled type, so the profile must say TIMESTAMP"
    )

    # Every reference value is likewise a complete day.
    for point in result["evidence"]["reference"]["points"]:
        day = date.fromisoformat(point["date"])
        assert point["value"] == pytest.approx(float(b_amount_total_for(day)))


def test_changing_only_the_configuration_changes_the_verdict(company_a):
    """The comparison basis is an input, and it is what makes a number abnormal.

    Six orders on a Friday is unremarkable when Fridays are the comparison, and
    clearly abnormal when the whole of August is -- because Aurora's Fridays run
    at five to seven orders and its ordinary days at four. Nothing here touches
    the source, the formula, the date, the measurement or a line of code: a second
    approved policy is swapped in for this KPI alone, and the verdict inverts.

    This is the concrete reason the expectation cannot be a fixed trailing window.
    A "last N days" baseline would have given every Friday the August answer, and
    would have escalated the company's best trading day every week.
    """
    admin, base = company_a["admin"], company_a["base"]
    weekly = run_detection(admin, base, company_a["order_count_id"], COMPANY_A_TARGET)
    assert weekly["evidence"]["bucket"]["applied"] == "SAME_DAY_OF_WEEK"
    assert weekly["result"]["expected"] == pytest.approx(6.0)
    assert weekly["result"]["status"] == "NORMAL"

    # A KPI-scoped policy overrides the company-wide one, for this KPI alone. It
    # claims a monthly season rather than a weekly rhythm -- a legitimate policy,
    # and the wrong one for this KPI, which is the point.
    approve_bucket_config(
        admin,
        base,
        config_key="aurora-order-count-monthly",
        name="Aurora order count: compared across the month instead",
        kpi_key="order_count",
        buckets={"same_month_or_season": {"enabled": True, "months": [8]}},
    )

    monthly = run_detection(admin, base, company_a["order_count_id"], COMPANY_A_TARGET)
    assert monthly["evidence"]["bucket"]["applied"] == "SAME_MONTH_OR_SEASON"
    assert monthly["evidence"]["bucket"]["config_key"] == "aurora-order-count-monthly"

    monthly_dates = [
        date.fromisoformat(p["date"]) for p in monthly["evidence"]["reference"]["points"]
    ]
    assert all(day.month == 8 for day in monthly_dates)
    assert len({day.isoweekday() for day in monthly_dates}) > 1, (
        "a monthly comparison mixes weekdays, which is exactly why it moves the "
        "expectation for a KPI with a weekly rhythm"
    )

    # The actual is a measurement, so it cannot depend on the comparison policy.
    assert monthly["result"]["actual"] == pytest.approx(weekly["result"]["actual"])
    # The expectation and the verdict can, and do.
    assert monthly["result"]["expected"] == pytest.approx(4.0), "the median ordinary day"
    assert monthly["result"]["deviation_pct"] == pytest.approx(50.0)
    assert monthly["result"]["status"] == "ABNORMAL"
    assert "august" in monthly["result"]["comparison"].lower()
    assert "friday" not in monthly["result"]["comparison"].lower()

    # The company-wide policy still governs the KPI that has no override, so one
    # KPI's configuration cannot silently re-base another's.
    revenue = run_detection(admin, base, company_a["revenue_id"], COMPANY_A_TARGET)
    assert revenue["evidence"]["bucket"]["config_key"] == "aurora-weekly"
    assert revenue["evidence"]["bucket"]["applied"] == "SAME_DAY_OF_WEEK"


def test_a_zero_spread_reference_window_is_never_given_a_fabricated_score(company_a):
    """The zero-MAD guard, reached through the API rather than in a unit test.

    Aurora's order count sits at exactly four on every ordinary day, so a
    weekday-blind comparison has a median absolute deviation of zero. Dividing by
    it would make any difference at all infinitely abnormal, so the engine returns
    no z-score and lets the KPI's business tolerance decide -- and says which
    happened, which is why the caveat is asserted alongside the number.
    """
    admin, base = company_a["admin"], company_a["base"]
    # Mondays only: 26 comparable Mondays, all seeded at the same order count.
    approve_bucket_config(
        admin,
        base,
        config_key="aurora-order-count-monday",
        name="Aurora order count: compared against Mondays",
        kpi_key="order_count",
        buckets={"same_day_of_week": {"enabled": True, "days": ["MON"]}},
    )
    monday = date(2026, 8, 24)
    assert monday.isoweekday() == 1

    result = run_detection(admin, base, company_a["order_count_id"], monday)
    stats = result["evidence"]["statistics"]

    assert result["result"]["expected"] == pytest.approx(4.0)
    assert result["result"]["actual"] == pytest.approx(4.0)
    assert stats["mad"] == pytest.approx(0.0)
    assert stats["dispersion_basis"] == "NONE"
    # The actual matches the repeated value exactly, so zero is a fact rather
    # than a fabrication -- and it is reported as zero, not as None.
    assert stats["modified_z_score"] == pytest.approx(0.0)
    assert result["result"]["status"] == "NORMAL"
    assert any("identical" in note.lower() for note in result["evidence"]["notes"]), (
        "a result computed without measurable spread has to disclose that"
    )


def test_the_business_view_hides_the_statistics_it_was_computed_from(company_a):
    """Requirement 11's contract, enforced at the API rather than in the browser.

    ``result`` is what a business surface may render. Bucket types, reference
    dates, median, MAD, z-scores and the generated SQL binding live in
    ``evidence``, for the governance surface.
    """
    payload = run_detection(
        company_a["admin"], company_a["base"], company_a["revenue_id"], COMPANY_A_TARGET
    )
    result = payload["result"]

    assert set(result) == {
        "kpi",
        "kpi_key",
        "target_date",
        "actual",
        "expected",
        "deviation_pct",
        "deviation_absolute",
        "status",
        "comparison",
        "headline",
        "unit",
        "currency",
    }
    # A business reader gets the comparison basis in words, and nothing technical.
    assert "friday" in result["comparison"].lower()
    for leaked in ("median", "mad", "z_score", "modified", "sql", "SAME_DAY_OF_WEEK"):
        assert leaked not in str(result), f"{leaked} must not reach the business view"


# ---------------------------------------------------------------------------
# Structural proofs: read the engine's own source
# ---------------------------------------------------------------------------
#: The modules that make up the detection algorithm proper. Everything a company
#: differs by must arrive as an argument to these, never be written inside them.
ALGORITHM_MODULES = (
    Path("app/services/detection.py"),
    Path("app/services/bucket_config.py"),
    Path("app/services/robust_stats.py"),
)

#: Vocabulary from this suite's two tenants. None of it may appear in the engine
#: -- not in code, and not in prose either, because a table name in a comment is
#: a sign the algorithm was written against one company's schema.
TENANT_VOCABULARY = (
    "aurora",
    "borealis",
    "novamart",
    "orders",
    "sales_transactions",
    "refunds",
    "net_revenue",
    "order_date",
    "transaction_date",
    "refund_amount",
)

#: Calendar and event literals. These may legitimately appear in a docstring as
#: illustration ("two Fridays in week 3 of December"), so they are only forbidden
#: in executable code, where they would encode one company's trading pattern.
CALENDAR_LITERALS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "december",
    "diwali",
    "christmas",
    "ramadan",
    "black friday",
)


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Node ids of every docstring, so prose can be excluded from a code scan."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _code_strings(path: Path) -> list[str]:
    """Every string literal in executable code, docstrings excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def test_the_algorithm_names_no_company_table_column_weekday_or_event():
    """The engine's source is the evidence, because a branch is easy to hide.

    A single ``if company == ...`` or ``if weekday == "FRI"`` outside the
    configuration is what turns a reusable engine into one company's script, and
    it would still pass every behavioural test above as long as this suite's two
    tenants happened to agree with it.
    """
    for module in ALGORITHM_MODULES:
        assert module.is_file(), f"{module} not found -- run pytest from backend/"
        text = module.read_text(encoding="utf-8").lower()
        for word in TENANT_VOCABULARY:
            assert word not in text, f"{module} names tenant-specific vocabulary: {word}"

        literals = " | ".join(_code_strings(module)).lower()
        for word in CALENDAR_LITERALS:
            assert word not in literals, (
                f"{module} has the calendar literal {word!r} in executable code; "
                "weekday and month names must be derived from the calendar module "
                "and matched against the company's configuration"
            )


def test_no_model_can_reach_the_arithmetic():
    """Every number is arithmetic on values the database returned.

    A language model may draft a bucket configuration, and that draft has to be
    approved by a human before the engine will read it. The numeric path itself
    must have no route to a model at all -- asserted over the import graph,
    because a single convenience import is how that boundary erodes.
    """
    for module in ALGORITHM_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            assert not name.startswith("app.llm"), f"{module} imports the model layer: {name}"
            assert not name.startswith("app.copilot"), f"{module} imports the Copilot: {name}"
            assert not name.startswith("app.services.bucket_extraction"), (
                f"{module} imports the extraction service: {name}"
            )


def test_an_unapproved_configuration_is_invisible_to_detection(company_b):
    """A drafted or proposed policy must not change any number.

    This is the boundary that keeps a model's draft out of the arithmetic: the
    extraction endpoint can only ever produce a PROPOSED row, and the engine
    reads APPROVED rows only.
    """
    admin, base = company_b["admin"], company_b["base"]
    before = run_detection(admin, base, company_b["revenue_id"], COMPANY_B_TARGET)

    # A draft that claims a completely different weekday, left unapproved.
    drafted = admin.post(
        f"{base}/bucket-configs",
        json={
            "config_key": "borealis-unapproved",
            "name": "Unapproved draft claiming a different pattern",
            "kpi_key": "revenue",
            "buckets": {"same_day_of_week": {"enabled": True, "days": ["SAT"]}},
        },
    )
    assert drafted.status_code == 201, drafted.text
    proposed = admin.post(f"{base}/bucket-configs/{drafted.json()['id']}/propose", json={})
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["status"] == "PROPOSED"

    after = run_detection(admin, base, company_b["revenue_id"], COMPANY_B_TARGET)
    assert after["evidence"]["bucket"]["config_key"] == before["evidence"]["bucket"]["config_key"]
    assert after["result"]["expected"] == pytest.approx(before["result"]["expected"])
    assert after["result"]["status"] == before["result"]["status"]


def test_batch_run_persists_agent_aggregate_and_linked_results(company_b):
    admin, base = company_b["admin"], company_b["base"]
    response = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": company_b["target"].isoformat(),
            "kpi_ids": [company_b["revenue_id"], company_b["refunds_id"]],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    agent = body["agent_run"]
    assert body["agent_run_id"] == agent["id"]
    assert agent["status"] == "COMPLETED"
    assert agent["kpi_count"] == 2
    assert agent["processed_count"] == 2
    assert agent["error_count"] == 0
    assert agent["normal_count"] + agent["abnormal_count"] + agent["low_confidence_count"] == 2
    assert all(item["agent_run_id"] == agent["id"] for item in body["results"])

    history = admin.get(f"{base}/agent-runs")
    assert history.status_code == 200, history.text
    assert any(item["id"] == agent["id"] for item in history.json())

    stored = admin.get(f"{base}/agent-runs/{agent['id']}")
    assert stored.status_code == 200, stored.text
    assert {item["result"]["kpi_key"] for item in stored.json()["results"]} == {
        "revenue",
        "refund_value",
    }


def test_results_history_carries_its_unit_and_states_no_explanation_it_lacks(company_b):
    """The Results screen's contract, on both counts the screen used to get wrong.

    * The row carries ``unit`` and ``currency``, so the browser renders the KPI's
      own money. Without them the screen inferred "money" from the substring
      ``revenue`` in the KPI key and printed it as USD -- wrong symbol for this
      tenant's INR books, and no symbol at all for ``refund_value``.

    * Nothing in the platform writes ``agent_run_explanations`` -- explanation
      generation is the Copilot's, and it is off by default -- so a row reports
      ``NOT_GENERATED``/``NOT_SENT`` and a null ``ai_explanation`` rather than
      defaulting to ``READY``/``EMAIL_SENT``, which claimed a finished
      explanation and a delivered email for every historical row of a platform
      that has no email engine. ``top_driver`` still carries the engine's own
      deterministic headline, which is what the screen shows instead.
    """

    admin, base = company_b["admin"], company_b["base"]
    ran = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": company_b["target"].isoformat(),
            "kpi_ids": [company_b["revenue_id"], company_b["refunds_id"]],
        },
    )
    assert ran.status_code == 200, ran.text

    response = admin.get(f"{base}/results")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["summary"]["total_runs"] == len(body["items"])
    rows = {item["kpi_key"]: item for item in body["items"]}
    assert {"revenue", "refund_value"} <= set(rows)

    for kpi_key, item in rows.items():
        assert item["unit"] == "currency", kpi_key
        assert item["currency"] == "INR", kpi_key
        # Always present: the engine stores a headline for every run, so the
        # summary column never has to render an empty cell.
        assert item["top_driver"], kpi_key
        assert item["ai_explanation"] is None, kpi_key
        assert item["explanation_status"] == "NOT_GENERATED", kpi_key
        assert item["email_status"] == "NOT_SENT", kpi_key


def test_the_results_list_offers_only_filters_that_would_return_something(company_b):
    """The Results screen's filter contract.

    The stored list is capped, so a screen that filtered the page it already held
    would leave an older date unreachable -- the reader would have no control for
    the very row they came for. The narrowing therefore happens here, and the
    values on offer are read from the company's own stored runs rather than written
    into the client, so no control can be offered that returns an empty table.

    ``total_stored`` is what keeps a narrowed page honest: the tile above the table
    reads "N of M stored" rather than presenting the filtered count as the whole.
    """

    admin, base = company_b["admin"], company_b["base"]
    target = company_b["target"]
    ran = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": target.isoformat(),
            "kpi_ids": [company_b["revenue_id"], company_b["refunds_id"]],
        },
    )
    assert ran.status_code == 200, ran.text

    unfiltered = admin.get(f"{base}/results")
    assert unfiltered.status_code == 200, unfiltered.text
    body = unfiltered.json()

    options = body["options"]
    stored_keys = {item["kpi_key"] for item in body["items"]}
    offered_keys = {row["kpi_key"] for row in options["kpis"]}
    assert stored_keys <= offered_keys
    assert target.isoformat() in options["dates"]
    assert {item["status"] for item in body["items"]} <= set(options["statuses"])
    assert body["total_stored"] == len(body["items"])
    # Echoed, so the screen never claims a narrowing the server did not apply.
    assert body["filters"] == {
        "status": None,
        "kpi_key": None,
        "target_date": None,
        "dimension": None,
    }

    one_kpi = admin.get(f"{base}/results", params={"kpi_key": "revenue"})
    assert one_kpi.status_code == 200, one_kpi.text
    narrowed = one_kpi.json()
    assert {item["kpi_key"] for item in narrowed["items"]} == {"revenue"}
    assert narrowed["filters"]["kpi_key"] == "revenue"
    # The summary describes what the reader is looking at; the company's own total
    # stays alongside it rather than being replaced by it.
    assert narrowed["summary"]["total_runs"] == len(narrowed["items"])
    assert narrowed["total_stored"] == body["total_stored"]
    assert narrowed["total_stored"] > narrowed["summary"]["total_runs"]

    one_date = admin.get(f"{base}/results", params={"target_date": target.isoformat()})
    assert one_date.status_code == 200, one_date.text
    assert {item["target_date"] for item in one_date.json()["items"]} == {target.isoformat()}

    empty_date = admin.get(f"{base}/results", params={"target_date": "1999-01-01"})
    assert empty_date.status_code == 200, empty_date.text
    drained = empty_date.json()
    assert drained["items"] == []
    assert drained["summary"]["total_runs"] == 0
    # The options survive an empty page, so the reader can filter their way back.
    assert drained["options"]["kpis"]
    assert drained["total_stored"] == body["total_stored"]


def test_kpi_handbook_extraction_persists_and_drives_real_detection(company_b, monkeypatch):
    """The handbook JSON is validated, approved, and consumed by the real engine."""

    class HandbookModel(LLMProvider):
        async def generate(self, messages, tools=None, stream=False):
            return LLMResponse(
                text=(
                    '{"same_day_of_week":{"enabled":true,"days":["TUE"]},'
                    '"yoy_period":{"enabled":false}}'
                ),
                model="test/handbook-model",
                usage=LLMUsage(prompt_tokens=20, completion_tokens=12),
            )

    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_model", "test/handbook-model")
    monkeypatch.setattr(
        "app.services.bucket_extraction.build_provider",
        lambda config=None: HandbookModel(config),
    )

    admin, base = company_b["admin"], company_b["base"]
    document = admin.post(
        f"{base}/documents",
        data={
            "metadata": __import__("json").dumps(
                {
                    "title": "Borealis KPI Handbook",
                    "document_type": "KPI Handbook",
                    "inline_content": (
                        "For comparable trading history, use the same weekday. "
                        "Borealis trading patterns are compared on Tuesday."
                    ),
                }
            )
        },
    )
    assert document.status_code == 201, document.text
    assert document.json()["document_type"] == "KPI_HANDBOOK"
    assert document.json()["document_class"] == "REFERENCE"

    extracted = admin.post(
        f"{base}/bucket-configs/extract",
        json={
            "config_key": "borealis-handbook-policy",
            "name": "Borealis handbook policy",
            "document_id": document.json()["id"],
        },
    )
    assert extracted.status_code == 201, extracted.text
    extracted_body = extracted.json()
    assert extracted_body["status"] == "PROPOSED"
    assert extracted_body["source_document_id"] == document.json()["id"]
    assert extracted_body["extraction"]["model"] == "test/handbook-model"
    assert extracted_body["buckets"]["same_day_of_week"]["days"] == [2]
    assert extracted_body["extraction"]["retrieval"]["passages_selected"] >= 1

    approved = admin.post(
        f"{base}/bucket-configs/{extracted_body['id']}/approve",
        json={"reason": "Reviewed against the Borealis KPI Handbook."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    result = run_detection(admin, base, company_b["revenue_id"], company_b["target"])
    assert result["result"]["actual"] is not None
    assert result["result"]["expected"] is not None
    assert result["result"]["deviation_absolute"] is not None
    assert result["result"]["status"] in {"NORMAL", "ABNORMAL", "LOW_CONFIDENCE"}
    assert result["evidence"]["source"]["table"] == "sales_transactions"
    assert result["evidence"]["source"]["formula"] == "SUM(sales_transactions.amount)"
    assert result["evidence"]["source"]["time_field"] == "transaction_date"
    assert result["evidence"]["bucket"]["config_key"] == "borealis-handbook-policy"


def test_a_handbook_that_singles_out_one_measure_scopes_a_policy_to_it(
    company_b, monkeypatch
):
    """One company-wide policy, plus a policy for the measure the document excepts.

    A handbook states how the business generally behaves and then names its
    exceptions. Both readings have to survive: the general rule for every measure,
    and the exception for the one the document actually singles out.

    This runs *after* the handbook test above and deliberately approves only the
    KPI-scoped row, so the company-wide policy in force is left exactly as that
    test left it. The assertion is therefore a real precedence check rather than a
    restatement of the setup: the same endpoint, on the same date, resolves a
    different comparison basis for the two KPIs -- ``refund_value`` reads the row
    scoped to it, ``revenue`` still reads the company-wide one.
    """

    class HandbookModel(LLMProvider):
        async def generate(self, messages, tools=None, stream=False):
            return LLMResponse(
                text=(
                    '{"same_day_of_week":{"enabled":true,"days":["TUE"]},'
                    '"kpi_overrides":['
                    '{"kpi":"Refund Value",'
                    '"same_week_of_month":{"enabled":true,"weeks":[1,2,3,4,5]}},'
                    '{"kpi":"Shrinkage Rate",'
                    '"same_week_of_month":{"enabled":true,"weeks":[2]}}]}'
                ),
                model="test/handbook-model",
                usage=LLMUsage(prompt_tokens=30, completion_tokens=40),
            )

    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_model", "test/handbook-model")
    monkeypatch.setattr(
        "app.services.bucket_extraction.build_provider",
        lambda config=None: HandbookModel(config),
    )

    admin, base = company_b["admin"], company_b["base"]
    document = admin.post(
        f"{base}/documents",
        data={
            "metadata": __import__("json").dumps(
                {
                    "title": "Borealis KPI Handbook v2",
                    "document_type": "KPI Handbook",
                    "inline_content": (
                        "For comparable trading history, use the same weekday; Borealis "
                        "trading is compared on Tuesday. Refund Value is the exception: "
                        "it follows the position of the week within the month rather "
                        "than the weekday. Shrinkage Rate settles in the second week."
                    ),
                }
            )
        },
    )
    assert document.status_code == 201, document.text

    extracted = admin.post(
        f"{base}/bucket-configs/extract",
        json={
            "config_key": "borealis-handbook-v2",
            "name": "Borealis handbook policy v2",
            "document_id": document.json()["id"],
        },
    )
    assert extracted.status_code == 201, extracted.text
    body = extracted.json()

    # The company-wide row is the general rule, unchanged by the exception.
    assert body["kpi_key"] is None
    assert body["buckets"]["same_day_of_week"]["days"] == [2]
    assert body["buckets"]["same_week_of_month"]["enabled"] is False

    # "Refund Value" is registered here as refund_value: the same measure written
    # the way a document writes it, matched without the model being told the key.
    (override,) = body["kpi_overrides"]
    assert override["kpi_key"] == "refund_value"
    assert override["status"] == "PROPOSED"
    assert override["buckets"]["same_week_of_month"]["enabled"] is True
    assert override["config_key"] != body["config_key"]

    # "Shrinkage Rate" is not a measure this company registered. That is reported
    # rather than stored or invented, and it changes nothing else.
    assert any("Shrinkage Rate" in reason for reason in body["kpi_overrides_skipped"])
    assert len(body["kpi_overrides"]) == 1

    approved = admin.post(
        f"{base}/bucket-configs/{override['id']}/approve",
        json={"reason": "The handbook states a different basis for this measure."},
    )
    assert approved.status_code == 200, approved.text

    # The precedence, proved through the real engine rather than asserted.
    refunds = run_detection(admin, base, company_b["refunds_id"], company_b["target"])
    revenue = run_detection(admin, base, company_b["revenue_id"], company_b["target"])

    assert refunds["evidence"]["bucket"]["config_key"] == override["config_key"]
    assert revenue["evidence"]["bucket"]["config_key"] == "borealis-handbook-policy"
    # Both still produce a real number from the company's own source.
    for outcome in (refunds, revenue):
        assert outcome["result"]["actual"] is not None
        assert outcome["result"]["status"] in {"NORMAL", "ABNORMAL", "LOW_CONFIDENCE"}


# ---------------------------------------------------------------------------
# The post-run summary mail
# ---------------------------------------------------------------------------
class _Recorder:
    """A transport that keeps what it was handed instead of sending it."""

    name = "recorder"

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message):
        from app.notifications.provider import SendResult

        self.sent.append(message)
        return SendResult(
            sent=True, provider=self.name, recipient_count=len(message.recipients)
        )

    def describe(self) -> dict:
        return {"provider": self.name}


def _configured_email():
    from app.notifications.config import EmailConfig

    return EmailConfig(
        enabled=True,
        provider="smtp",
        host="mail.internal",
        port=587,
        username="",
        password="",
        use_tls=True,
        timeout_seconds=5,
        sender="agent@borealis.example.com",
        recipients=("ops@borealis.example.com",),
        subject_prefix="[KPI Intelligence]",
    )


def test_no_mail_host_configured_is_a_state_not_a_failed_run(company_b):
    """The default deployment sends nothing and still completes.

    A summary is a notification about work that already finished and is already
    stored. So an unconfigured mail host is reported in ``email`` with the reason,
    the run's own results are returned unchanged, and the request succeeds -- the
    alternative would let a mail setting decide whether a business gets its KPI
    results.
    """

    admin, base = company_b["admin"], company_b["base"]
    response = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": company_b["target"].isoformat(),
            "kpi_ids": [company_b["revenue_id"]],
            "force_rerun": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"], "the run must still return its results"
    assert body["email"]["sent"] is False
    assert "EMAIL_ENABLED" in (body["email"]["reason"] or "")


def test_the_summary_mail_reprints_the_stored_result_and_is_sent_once(
    company_b, monkeypatch
):
    """One mail per Agent Run, composed from the rows the run stored.

    Three properties, each of which is invisible when it breaks:

    * **The figures are the stored ones.** The mail's actual and expected are the
      values the API returns for the same run, so the mail and the Results screen
      cannot drift into two different analyses of one movement.
    * **Reopening a completed date sends nothing.** The second request is answered
      from storage, so no work happened and there is nothing to announce.
    * **An authorised re-run sends its own.** It is a new Agent Run and a new
      reading, and suppressing that would hide the fact that the day was measured
      again.

    And throughout: no causal language. The mail carries the same non-causal prose
    the explanation service assembles.
    """

    from app.services import run_email

    recorder = _Recorder()
    monkeypatch.setattr(run_email, "load_email_config", _configured_email)
    monkeypatch.setattr(run_email, "build_email_provider", lambda *a, **k: recorder)

    admin, base = company_b["admin"], company_b["base"]
    target = company_b["target"]

    first = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": target.isoformat(),
            "kpi_ids": [company_b["revenue_id"]],
            "force_rerun": True,
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["email"]["sent"] is True
    assert len(recorder.sent) == 1

    message = recorder.sent[0]
    # Addressed to the company's own registered user, not to the deployment's
    # fallback list. Membership, not equality, because a later test in this module
    # adds members to this company; the exact set is asserted there.
    assert "admin@borealis-foods.example.com" in message.recipients
    assert "ops@borealis.example.com" not in message.recipients
    assert first.json()["email"]["recipient_source"] == "REGISTERED_USERS"
    assert target.isoformat() in message.subject
    assert "[KPI Intelligence]" in message.subject
    body = message.body
    # The chain requirement asks for: actual vs expected, status, contributors,
    # the explanation, the recommendation, and the confidence in all of it.
    for heading in ("Actual", "Expected", "Deviation", "Status"):
        assert heading in body, heading
    assert "Top Contributors" in body
    assert "What Happened" in body
    assert "Confidence Level" in body
    assert "Recommended Next Step" in body
    assert "Contribution is not causation" in body
    # The prose is the platform's own assembly of stored figures, and the mail says
    # so: a summary that looked model-written would invite the reader to discount it.
    assert "no language model produced or adjusted any number" in body
    for word in ("caused", "drove", "driven by", "led to", "resulted in", "root cause"):
        assert word not in body.lower(), f"the summary claims causation: {word!r}"

    # The same figures the API reports for this run, rendered the same way.
    result = first.json()["results"][0]["result"]
    assert f"{abs(result['actual']):,.0f}" in body or f"{abs(result['actual']):,.1f}" in body

    # Reopening the date: answered from storage, so nothing is announced.
    replay = admin.post(
        f"{base}/run-detection/batch",
        json={"target_date": target.isoformat(), "kpi_ids": [company_b["revenue_id"]]},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["already_completed"] is True
    assert "email" not in replay.json()
    assert len(recorder.sent) == 1, "a reopened date must not re-send its summary"

    # An authorised re-run is a new reading, and it announces itself.
    again = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": target.isoformat(),
            "kpi_ids": [company_b["revenue_id"]],
            "force_rerun": True,
        },
    )
    assert again.status_code == 200, again.text
    assert again.json()["email"]["sent"] is True
    assert len(recorder.sent) == 2


def test_a_refused_mail_server_is_recorded_and_does_not_fail_the_run(
    company_b, monkeypatch
):
    """A transport failure is the summary's state, never the run's."""

    from app.notifications.provider import SendResult
    from app.services import run_email

    class _Refuses:
        name = "smtp"

        def send(self, message):
            return SendResult(
                sent=False, provider=self.name, reason="The mail server refused the summary."
            )

    monkeypatch.setattr(run_email, "load_email_config", _configured_email)
    monkeypatch.setattr(run_email, "build_email_provider", lambda *a, **k: _Refuses())

    admin, base = company_b["admin"], company_b["base"]
    response = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": company_b["target"].isoformat(),
            "kpi_ids": [company_b["revenue_id"]],
            "force_rerun": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"], "the run's results survive a mail failure"
    assert response.json()["email"]["sent"] is False
    assert "refused" in (response.json()["email"]["reason"] or "").lower()


def test_the_summary_addresses_the_companys_own_entitled_members(company_b, monkeypatch):
    """Recipients come from the membership table, and only from entitled members.

    Two members are added to a company that already has an administrator, and the
    mail must reach exactly the two who could have opened the same figures in the
    application:

    * the **analyst** holds ``analytics.read`` and ``investigation.read``, and the
      summary carries both a stored verdict and its contribution shares;
    * the **viewer** holds ``analytics.read`` alone, and apportionment is not theirs
      to read -- so a body containing TOP CONTRIBUTORS must not be addressed to them.

    And the configured ``EMAIL_RECIPIENTS`` list stays out of it entirely. It is the
    fallback for a company with no entitled member, not a standing copy-list; a
    deployment address appearing here would mean every company's results reached one
    inbox regardless of who was registered.
    """

    from app.services import run_email

    admin, base = company_b["admin"], company_b["base"]

    for email, role_key in (
        ("analyst@borealis-foods.example.com", "ANALYST"),
        ("viewer@borealis-foods.example.com", "VIEWER"),
    ):
        added = admin.post(
            f"{base}/members",
            json={
                "email": email,
                "full_name": email.split("@")[0].title(),
                "password": "Detection-Tests-2026",
                "role_key": role_key,
            },
        )
        assert added.status_code == 201, added.text

    recorder = _Recorder()
    monkeypatch.setattr(run_email, "load_email_config", _configured_email)
    monkeypatch.setattr(run_email, "build_email_provider", lambda *a, **k: recorder)

    response = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": company_b["target"].isoformat(),
            "kpi_ids": [company_b["revenue_id"]],
            "force_rerun": True,
        },
    )
    assert response.status_code == 200, response.text
    email = response.json()["email"]
    assert email["sent"] is True
    assert email["recipient_source"] == "REGISTERED_USERS"

    message = recorder.sent[0]
    assert message.recipients == (
        "admin@borealis-foods.example.com",
        "analyst@borealis-foods.example.com",
    ), message.recipients
    assert "viewer@borealis-foods.example.com" not in message.recipients
    assert "ops@borealis.example.com" not in message.recipients
    # The body says whose reading it is, so a recipient can tell a registered-user
    # summary from a fallback one without asking an operator.
    assert "registered users of this company" in message.body

    # No address is written into the audit trail -- a mailing list is personal data --
    # but where the list came from is, because that is what answers "why these people".
    trail = admin.get(f"{base}/audit", params={"action": "detection.run_summary_emailed"})
    assert trail.status_code == 200, trail.text
    entry = trail.json()[0]
    assert entry["details"]["recipient_source"] == "REGISTERED_USERS"
    assert "analyst@borealis-foods.example.com" not in json.dumps(entry)


def test_a_company_with_no_entitled_member_falls_back_to_the_configured_list(
    company_b, monkeypatch
):
    """The deployment's list is the answer only when the membership table has none.

    A company can exist before anyone is entitled to read its results -- mid-setup,
    or after its last analyst is deactivated -- and a run that finishes then still
    produced something an operator should see. The fallback exists for that, and the
    mail says which of the two it was so nobody mistakes an operator list for the
    business's own distribution.
    """

    from app.services import run_email

    recorder = _Recorder()
    monkeypatch.setattr(run_email, "load_email_config", _configured_email)
    monkeypatch.setattr(run_email, "build_email_provider", lambda *a, **k: recorder)
    # Patched rather than deactivating the real administrator: this fixture is shared
    # with every later test in this module, and taking away its only member would
    # leave them running against a company nobody may read.
    monkeypatch.setattr(run_email, "_entitled_member_emails", lambda *a, **k: ())

    admin, base = company_b["admin"], company_b["base"]
    response = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": company_b["target"].isoformat(),
            "kpi_ids": [company_b["revenue_id"]],
            "force_rerun": True,
        },
    )
    assert response.status_code == 200, response.text
    email = response.json()["email"]
    assert email["sent"] is True
    assert email["recipient_source"] == "CONFIGURED_FALLBACK"

    message = recorder.sent[0]
    assert message.recipients == ("ops@borealis.example.com",)
    assert "no member of this company currently holds both entitlements" in message.body


def test_nobody_to_address_is_reported_as_its_own_state_with_both_reasons(
    company_b, monkeypatch
):
    """No members and no fallback: a state, and a message that names every cause.

    The run is already finished and stored, so this can only be reported, never
    raised. And when a deployment is *both* switched off and unaddressed, both facts
    are in the reason -- an operator who fixes ``EMAIL_ENABLED`` and then discovers
    there was also nobody to send to has been made to debug the same problem twice.
    """

    from app.services import run_email

    configured = _configured_email()
    monkeypatch.setattr(
        run_email,
        "load_email_config",
        lambda *a, **k: replace(configured, enabled=False, recipients=()),
    )
    monkeypatch.setattr(run_email, "_entitled_member_emails", lambda *a, **k: ())

    admin, base = company_b["admin"], company_b["base"]
    response = admin.post(
        f"{base}/run-detection/batch",
        json={
            "target_date": company_b["target"].isoformat(),
            "kpi_ids": [company_b["revenue_id"]],
            "force_rerun": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"], "the run's results survive having no recipient"
    email = response.json()["email"]
    assert email["sent"] is False
    assert email["recipient_source"] == "NONE"
    reason = email["reason"] or ""
    assert "EMAIL_ENABLED" in reason
    assert "EMAIL_RECIPIENTS" in reason
    assert "analytics.read" in reason and "investigation.read" in reason
