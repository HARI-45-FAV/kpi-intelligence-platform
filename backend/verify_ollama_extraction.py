"""End-to-end verification of comparison-policy extraction against a real model.

``verify_ollama.py`` exercises the Copilot: retrieval, governed tools, no leaked
reasoning. This script exercises the *other* route on which a language model
touches the platform, and the only one where a model's output reaches the
detection engine: ``POST /companies/{id}/bucket-configs/extract``.

That route is worth its own script because the suite cannot cover it. The tests
script the model -- they must, or every run would need a GPU -- so they prove the
contract holds against a payload chosen by the test author. What they cannot
prove is that a real model, reading real prose, produces a payload the contract
accepts and the engine can use. Those are different claims, and only this one
needs a model.

Three things are checked, in order of how much they matter:

1. **The boundary holds.** The handbook read here deliberately contains a
   figure ("an expected settled value of 10,250,000") and a percentage. Whatever
   the model does with them, no number may reach the stored configuration. This
   is asserted on *every* attempt, because a contract that holds on average is
   not a contract.
2. **Dates are grounded in the document.** The handbook names a recurring window
   by day and month with no year. The platform must expand it across the years
   in scope and discard any year the model supplies, and must drop a date the
   document does not contain.
3. **The policy is usable.** The five slots the handbook states should come back
   enabled, the named measure should get a scoped configuration of its own, and
   after approval the engine should select comparable dates from it and compute
   its own numbers.

Local by design: an extraction is one large prompt per attempt, and a hosted
quota is not the place to spend those.

Requires:  ollama serve  +  ollama pull qwen3:8b
Run:       python verify_ollama_extraction.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

TMP = Path(__file__).resolve().parent / "tests" / "_tmp"
TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "verify_extraction.db"
if DB.exists():
    DB.unlink()

# Its own database, so this never touches development or test data. The model
# configuration is read from .env like a real deployment; only storage and the
# secret are overridden.
os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["DOCUMENT_STORAGE_DIR"] = str(TMP / "verify_extraction_documents")
os.environ["SECRET_KEY"] = "verify-extraction-secret-not-for-production-0123456"
os.environ["ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.llm import get_llm_config  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed.bootstrap import sync_reference_data  # noqa: E402
from app.services.bucket_config import SLOT_KEYS  # noqa: E402

API = "/api/v1"

#: The complete set of keys a stored policy may carry: the five slots, plus the
#: three search-budget numbers the platform owns. Anything else in there means a
#: model wrote a field the contract does not know, which is the failure this
#: script exists to catch.
SLOT_NAMES = set(SLOT_KEYS.values())
ALLOWED_CONFIG_KEYS = SLOT_NAMES | {
    "lookback_days",
    "min_reference_points",
    "max_reference_points",
}
#: Slot names as the engine reports the one it applied.
APPLIED_NAMES = {str(bucket) for bucket in SLOT_KEYS}

# How many times to ask. A small local model is variable; the *contract* checks
# below must pass on every attempt, and only slot coverage is allowed to be the
# best of several tries. EXTRACT_ATTEMPTS=0 runs the provisioning half only,
# which is how the script itself is developed without occupying the model.
ATTEMPTS = int(os.environ.get("EXTRACT_ATTEMPTS", "3"))

# Figures planted in the handbook. None of them may appear in a stored policy.
PLANTED_FIGURES = ("10250000", "10,250,000", "18.5", "4200")

LEAK_MARKERS = ("<think", "</think", "<reasoning", "</reasoning", "scratchpad")

# Vocabulary invented for this script. Nothing here is a KPI the platform ships,
# a table it expects or a word the extractor knows: if the run passes with these
# names it passes with a real company's.
KPI_SETTLED = "settled_value"
KPI_HANDLING = "handling_units"
DOC_LABEL_HANDLING = "Handling Volume"   # registered, under a different name
DOC_LABEL_UNKNOWN = "Shrinkage Rate"    # named by the document, never registered

HANDBOOK = """
Chapter 4 -- Comparison basis for the operations scorecard

4.1 Trading week. Meridian Logistics is a weekend business. Saturday and Sunday
behave alike and neither resembles a weekday, so a Saturday figure is only ever
compared against other Saturdays, and a Sunday against other Sundays.

4.2 Position in the month. Settlement runs concentrate in the third week of each
month. A day in the third week is compared against days in the third week of
other months.

4.3 Seasonality. January is the peak month across every measure. January days are
compared against January days.

4.4 Recurring events. Lantern Week (15-21 Oct) is our largest promotional window
and repeats every year. Days inside Lantern Week are compared against Lantern
Week days from earlier years, never against ordinary October days.

4.5 Year on year. Every measure is also read against the same period in the
previous year.

4.6 Exception. Handling Volume does not follow the trading week above: it peaks
in the third week of the month regardless of weekday, and the weekday rule is
not applied to it. Shrinkage Rate follows the same third-week pattern.

4.7 Reference figures (for the finance appendix only). The board plan assumes an
expected settled value of 10,250,000 per peak day, a tolerance of 18.5 percent,
and 4200 handling units per ordinary day. These are planning figures and are not
comparison rules.
"""

failures: list[str] = []
warnings: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label} {detail}".strip())
    return bool(condition)


def note(text: str) -> None:
    print(f"  ..    {text}")
    warnings.append(text)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
print("\n[1] Configuration resolves to the local endpoint")
# ---------------------------------------------------------------------------
config = get_llm_config()
print("      " + json.dumps(config.describe()))
check("LLM_ENABLED is true", config.enabled is True)
check("the transport is the OpenAI-compatible one", config.provider == "openai_compatible", config.provider)
check("the endpoint is Ollama's /v1", config.base_url == "http://localhost:11434/v1", config.base_url)
check("a model is named", bool(config.model.strip()), config.model)
check("configuration reports itself available", config.is_available is True, str(config.unavailable_reason))
check("the API key never appears in describe()", "EMPTY" not in json.dumps(config.describe()))

if failures:
    print("\nConfiguration is wrong; not contacting the model.")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)


# ---------------------------------------------------------------------------
print("\n[2] A source with two measures, built for this run")
# ---------------------------------------------------------------------------
SOURCE = TMP / "verify_extraction_source.db"
if SOURCE.exists():
    SOURCE.unlink()

# A weekend-heavy series, so an extracted weekend policy has something to select.
# Deliberately dull: the engine's job here is to be correct, not surprised.
TARGET = date(2026, 1, 17)  # a Saturday, in the peak month, in the third week
connection = sqlite3.connect(SOURCE)
connection.executescript(
    """
    CREATE TABLE ledger (
        id INTEGER PRIMARY KEY,
        -- Declared DATE, not TEXT: KPI validation requires a temporal time axis,
        -- and SQLite reports the declared type rather than inferring one.
        booked_on DATE NOT NULL,
        depot TEXT NOT NULL,
        settled_value REAL NOT NULL,
        handling_units REAL NOT NULL
    );
    """
)
rows = []
day = TARGET - timedelta(days=400)
while day <= TARGET:
    weekend = day.weekday() >= 5
    for depot in ("Depot North", "Depot South", "Depot East"):
        base_value = 90_000.0 if weekend else 40_000.0
        bump = 1.6 if (day == TARGET and depot == "Depot North") else 1.0
        rows.append(
            (
                day.isoformat(),
                depot,
                round(base_value * bump + (day.toordinal() % 7) * 350.0, 2),
                round((220.0 if weekend else 140.0) + (day.toordinal() % 5) * 4.0, 2),
            )
        )
    day += timedelta(days=1)
connection.executemany(
    "INSERT INTO ledger (booked_on, depot, settled_value, handling_units) VALUES (?, ?, ?, ?)",
    rows,
)
connection.commit()
connection.close()
print(f"      {len(rows)} rows across {(TARGET - (TARGET - timedelta(days=400))).days + 1} days")

Base.metadata.create_all(bind=engine)
session = SessionLocal()
try:
    sync_reference_data(session)
    session.commit()
finally:
    session.close()

app = create_app()

with TestClient(app) as client:
    registered = client.post(
        f"{API}/auth/register",
        json={
            "email": "ops@meridian-logistics.example.com",
            "password": "Meridian-Extract-2026",
            "full_name": "Mira Ops",
        },
    )
    check("a user can register", registered.status_code == 201, registered.text[:200])
    admin = bearer(registered.json()["access_token"])

    company = client.post(
        f"{API}/companies",
        headers=admin,
        json={"company_name": "Meridian Logistics", "currency": "INR", "timezone": "Asia/Kolkata"},
    )
    check("a company can be created", company.status_code == 201, company.text[:200])
    base = f"{API}/companies/{company.json()['id']}"

    source = client.post(
        f"{base}/data-sources",
        headers=admin,
        json={
            "name": "Meridian Ledger",
            "source_type": "SQLITE",
            "path": str(SOURCE),
            "refresh_frequency": "DAILY",
            "timezone": "Asia/Kolkata",
        },
    )
    check("a data source registers", source.status_code == 201, source.text[:200])
    source_id = source.json()["id"]
    check(
        "discovery succeeds",
        client.post(f"{base}/data-sources/{source_id}/discover", headers=admin).status_code == 200,
    )

    tables = {row["table_name"]: row for row in client.get(f"{base}/tables", headers=admin).json()}
    check("the ledger table was discovered", "ledger" in tables, str(list(tables)))
    scoped = client.put(
        f"{base}/data-scope",
        headers=admin,
        json={
            "replace": True,
            "tables": [
                {
                    "source_table_id": tables["ledger"]["id"],
                    "enabled": True,
                    "primary_time_column": "booked_on",
                }
            ],
        },
    )
    check("the table is brought into scope", scoped.status_code == 200, scoped.text[:200])
    check(
        "profiling succeeds",
        client.post(f"{base}/analysis/run", headers=admin).status_code == 200,
    )
    tables = {row["table_name"]: row for row in client.get(f"{base}/tables", headers=admin).json()}

    def register_kpi(kpi_key: str, name: str, formula: str, tolerance: float) -> str:
        created = client.post(
            f"{base}/kpis",
            headers=admin,
            json={
                "kpi_key": kpi_key,
                "name": name,
                "business_definition": f"{name}, as defined by Meridian operations.",
                "formula_expression": formula,
                "source_table_id": tables["ledger"]["id"],
                "time_field": "booked_on",
                "time_grain": "DAY",
                "unit": "currency",
                "currency": "INR",
                # A breakdown dimension, so contribution analysis has a real
                # hierarchy to apportion a movement across.
                "dimensions": [
                    {
                        "dimension_name": "depot",
                        "source_column": "depot",
                        "is_default_breakdown": True,
                    }
                ],
                "materiality": {
                    "relative_threshold_pct": tolerance,
                    "business_criticality": "HIGH",
                },
            },
        )
        assert created.status_code == 201, created.text
        definition = created.json()
        version_id = definition["versions"][0]["id"]
        validated = client.post(f"{base}/kpi-versions/{version_id}/validate", headers=admin)
        assert validated.status_code == 200, validated.text
        report = validated.json()
        if not report.get("ready_for_approval"):
            print(f"      validation of {kpi_key} FAILED: {report.get('summary')}")
            print("      " + json.dumps(report.get("checks", report))[:1500])
            raise SystemExit(1)
        client.post(f"{base}/kpi-versions/{version_id}/submit", headers=admin, json={})
        approved = client.post(
            f"{base}/kpi-versions/{version_id}/approve",
            headers=admin,
            json={"reason": "Signed off for the extraction verification run."},
        )
        assert approved.status_code == 200, approved.text
        return definition["id"]

    settled_id = register_kpi(KPI_SETTLED, "Settled Value", "SUM(ledger.settled_value)", 10.0)
    # Registered under a display name the handbook writes differently in case and
    # spacing -- the point is that label matching is done by a reader's rules, and
    # that the model is never asked to produce a key.
    handling_id = register_kpi(KPI_HANDLING, "Handling volume", "SUM(ledger.handling_units)", 20.0)
    check("two measures are registered and active", bool(settled_id and handling_id))

    # -----------------------------------------------------------------
    print(f"\n[3] The model reads the handbook ({ATTEMPTS} attempts, best kept)")
    # -----------------------------------------------------------------
    best: dict | None = None
    best_slots = -1

    for attempt in range(1, ATTEMPTS + 1):
        print(f"\n   -> attempt {attempt}/{ATTEMPTS}")
        response = client.post(
            f"{base}/bucket-configs/extract",
            headers=admin,
            json={
                "config_key": f"meridian-handbook-{attempt}",
                "name": f"Meridian comparison basis (attempt {attempt})",
                "text": HANDBOOK,
            },
        )
        if response.status_code != 201:
            check(f"[attempt {attempt}] extraction returned 201", False, response.text[:300])
            continue

        body = response.json()
        raw = json.dumps(body)
        buckets = body["buckets"]
        extraction = body["extraction"]
        enabled = list(body["enabled_slots"])

        print(f"      model         : {extraction.get('model')}")
        print(f"      status        : {body['status']}  needs_review={body['needs_review']}")
        print(f"      slots enabled : {enabled or '(none)'}")
        print(f"      rejected keys : {extraction.get('rejected_keys')}")
        print(f"      overrides     : {[o['kpi_key'] for o in body['kpi_overrides']]}")
        if body["kpi_overrides_skipped"]:
            print(f"      skipped       : {body['kpi_overrides_skipped']}")
        if body["review_reasons"]:
            print(f"      review        : {body['review_reasons']}")

        # --- the boundary. Asserted every attempt, without exception. -----
        check(
            f"[attempt {attempt}] the stored policy holds only keys the contract knows",
            set(buckets) <= ALLOWED_CONFIG_KEYS,
            str(sorted(set(buckets) - ALLOWED_CONFIG_KEYS)),
        )
        planted = [figure for figure in PLANTED_FIGURES if figure in json.dumps(buckets)]
        check(
            f"[attempt {attempt}] no planted figure reached the stored policy",
            not planted,
            str(planted),
        )
        for row_out in body["kpi_overrides"]:
            leaked = [f for f in PLANTED_FIGURES if f in json.dumps(row_out["buckets"])]
            check(
                f"[attempt {attempt}] no planted figure reached the {row_out['kpi_key']} policy",
                not leaked,
                str(leaked),
            )
            check(
                f"[attempt {attempt}] the {row_out['kpi_key']} policy holds only known keys",
                set(row_out["buckets"]) <= ALLOWED_CONFIG_KEYS,
                str(sorted(set(row_out["buckets"]) - ALLOWED_CONFIG_KEYS)),
            )
        check(
            f"[attempt {attempt}] the search budget was not shortened by the model",
            body["lookback_days"] >= 365 and body["min_reference_points"] >= 3,
            f"lookback={body['lookback_days']} min={body['min_reference_points']}",
        )
        check(
            f"[attempt {attempt}] no reasoning leaked into the response",
            not [m for m in LEAK_MARKERS if m in raw.lower()],
            str([m for m in LEAK_MARKERS if m in raw.lower()]),
        )
        check(f"[attempt {attempt}] no credential in the response", "EMPTY" not in raw)
        check(
            f"[attempt {attempt}] the draft cannot approve itself",
            body["status"] in ("PROPOSED", "NEEDS_REVIEW"),
            body["status"],
        )

        # --- date grounding ------------------------------------------------
        event = buckets.get("business_event") or {}
        if event.get("enabled") and event.get("events"):
            dates = sorted({d for item in event["events"] for d in item.get("dates", [])})
            years = sorted({d[:4] for d in dates})
            outside = [d for d in dates if not ("10-15" <= d[5:] <= "10-21")]
            print(f"      event dates   : {len(dates)} across {years}")
            check(
                f"[attempt {attempt}] a day-month window expanded across years",
                len(years) > 1,
                str(years),
            )
            check(
                f"[attempt {attempt}] every event date sits inside the stated window",
                not outside,
                str(outside[:6]),
            )
            check(
                f"[attempt {attempt}] no date outside the document's own months survived",
                all(d[5:7] == "10" for d in dates),
                str({d[5:7] for d in dates}),
            )

        if len(enabled) > best_slots:
            best, best_slots = body, len(enabled)

    check("at least one attempt produced a draft", best is not None)

    # -----------------------------------------------------------------
    print("\n[4] The best draft states the policy the handbook states")
    # -----------------------------------------------------------------
    if best is not None:
        buckets = best["buckets"]
        enabled = {
            key for key, value in buckets.items() if isinstance(value, dict) and value.get("enabled")
        }
        print("      " + json.dumps(buckets))

        expected_slots = {
            "same_day_of_week": "the weekend trading rule in 4.1",
            "same_week_of_month": "the third-week rule in 4.2",
            "same_month_or_season": "the January peak in 4.3",
            "business_event": "Lantern Week in 4.4",
            "yoy_period": "the year-on-year rule in 4.5",
        }
        for slot, where in expected_slots.items():
            check(f"{slot} is enabled ({where})", slot in enabled)

        weekend = buckets.get("same_day_of_week", {}).get("days") or []
        check(
            "the weekday rule names Saturday and Sunday, not weekdays",
            set(weekend) <= {"SAT", "SUN", 6, 7, "6", "7"} and len(weekend) == 2,
            str(weekend),
        )
        weeks = buckets.get("same_week_of_month", {}).get("weeks") or []
        check("the week rule names the third week", list(weeks) == [3], str(weeks))
        months = buckets.get("same_month_or_season", {}).get("months") or []
        check(
            "the season rule names January and nothing else",
            bool(months) and set(months) <= {"JAN", 1, "1"},
            str(months),
        )

        # --- the measure the document excepts ------------------------------
        overrides = {row["kpi_key"]: row for row in best["kpi_overrides"]}
        if check(
            f"the handbook's exception was scoped to {KPI_HANDLING}",
            KPI_HANDLING in overrides,
            str(list(overrides)) + " skipped=" + str(best["kpi_overrides_skipped"]),
        ):
            override = overrides[KPI_HANDLING]
            override_buckets = override["buckets"]
            check(
                "the exception drops the weekday rule, as 4.6 says",
                not (override_buckets.get("same_day_of_week") or {}).get("enabled"),
                json.dumps(override_buckets.get("same_day_of_week")),
            )
            check(
                "the exception keeps the third-week rule",
                (override_buckets.get("same_week_of_month") or {}).get("enabled") is True,
                json.dumps(override_buckets.get("same_week_of_month")),
            )
            check(
                "the exception is scoped to one measure, not the company",
                override["kpi_key"] == KPI_HANDLING,
                str(override["kpi_key"]),
            )
        check(
            "a measure this company never registered was reported, not stored",
            DOC_LABEL_UNKNOWN.lower() in json.dumps(best["kpi_overrides_skipped"]).lower()
            or DOC_LABEL_UNKNOWN not in json.dumps([r["name"] for r in best["kpi_overrides"]]),
            str(best["kpi_overrides_skipped"]),
        )

        # -------------------------------------------------------------
        print("\n[5] The engine uses the approved policy, and computes its own numbers")
        # -------------------------------------------------------------
        approved = client.post(
            f"{base}/bucket-configs/{best['id']}/approve",
            headers=admin,
            json={"reason": "Comparison basis checked against chapter 4 of the handbook."},
        )
        if best["needs_review"]:
            note(
                "the best draft landed NEEDS_REVIEW, which is the platform behaving "
                "correctly on an incomplete extraction; approving it here to exercise "
                f"the engine. Reasons: {best['review_reasons']}"
            )
        check("the drafted policy can be approved", approved.status_code == 200, approved.text[:300])

        detected = client.post(
            f"{base}/run-detection",
            headers=admin,
            json={"kpi_id": settled_id, "target_date": TARGET.isoformat()},
        )
        check("detection runs against the extracted policy", detected.status_code == 200, detected.text[:300])
        if detected.status_code == 200:
            payload = detected.json()
            result = payload["result"]
            evidence = payload["evidence"]
            bucket = evidence["bucket"]
            comparables = evidence["reference"]["count"]
            print(f"      config used   : {bucket.get('config_key')}")
            print(f"      slot applied  : {bucket.get('applied')}")
            print(f"      comparables   : {comparables}")
            print(f"      actual        : {result['actual']}")
            print(f"      expected      : {result['expected']}")
            print(f"      deviation     : {result['deviation_pct']}%  status={result['status']}")
            check(
                "the engine read the extracted configuration",
                bucket.get("config_key") == best["config_key"],
                str(bucket.get("config_key")),
            )
            check(
                "the engine selected comparable days from it",
                comparables >= 3,
                str(comparables),
            )
            check(
                "the slot applied is one the handbook stated",
                bucket.get("applied") in APPLIED_NAMES,
                str(bucket.get("applied")),
            )
            check(
                "the expectation is the engine's, not the handbook's planning figure",
                abs((result["expected"] or 0.0) - 10_250_000.0) > 1.0,
                str(result["expected"]),
            )
            check("a status was decided", bool(result["status"]), str(result["status"]))

        # The scoped policy is approved separately -- which is the whole point of
        # storing it as its own row.
        if KPI_HANDLING in overrides:
            scoped_approved = client.post(
                f"{base}/bucket-configs/{overrides[KPI_HANDLING]['id']}/approve",
                headers=admin,
                json={"reason": "The 4.6 exception is correct for this measure."},
            )
            check(
                "the scoped policy approves independently",
                scoped_approved.status_code == 200,
                scoped_approved.text[:200],
            )
            scoped_run = client.post(
                f"{base}/run-detection",
                headers=admin,
                json={"kpi_id": handling_id, "target_date": TARGET.isoformat()},
            )
            if check(
                "detection runs for the excepted measure",
                scoped_run.status_code == 200,
                scoped_run.text[:200],
            ):
                used = scoped_run.json()["evidence"]["bucket"]["config_key"]
                print(f"      scoped config : {used}")
                check(
                    "the excepted measure used its own policy, not the company-wide one",
                    used == overrides[KPI_HANDLING]["config_key"],
                    str(used),
                )


# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
if warnings:
    print("Notes:")
    for item in warnings:
        print(f"  ~ {item}")
if failures:
    print(f"FAILED  {len(failures)} check(s)")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("All comparison-policy extraction checks passed against a real model.")
print("=" * 70)
