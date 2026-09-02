"""Read the recommendation layer off a *live* server, the way the browser does.

The pytest suite proves the same properties against ``TestClient``, which calls the
ASGI app in-process. This script is the other half of that: it provisions a tenant
over real HTTP against a running uvicorn, stores three real verdicts, and prints
exactly what the Result page's "Recommended next actions" panel would render for
each of them — including the sharpening step, the deeper drill, one recorded
response, and a viewer who may not break a movement down.

Run it against a server started with::

    .venv/Scripts/python -m uvicorn app.main:app --port 8000

    .venv/Scripts/python scripts/verify_recommendations_live.py [--url http://127.0.0.1:8000]

It asserts the two rules that matter most and cannot be checked by eye at speed —
no causal claim and no guaranteed outcome anywhere in the served prose — and
otherwise prints, so the wording can be read as a reviewer would read it.

The tenant it creates is a real company in whatever database the server is using.
That is the point (a demo needs one), and it is additive: nothing existing is read,
changed or removed. Each run provisions a *new* tenant, so ``--email`` must be one
the server has not registered before.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import httpx

from app.services.recommendation_config import CAUSATION_NOTE
from tests.conftest import ApiActor, login
from tests.fixture_generalization import COMPANY_A_TARGET, build_company_a_source
from tests.test_detection_generalization import (
    approve_bucket_config,
    provision,
    run_detection,
)
from tests.test_explainability_findings import CAUSAL_WORDS
from tests.test_recommendations import (
    GUARANTEE_WORDS,
    NORMAL_TARGET,
    SPARSE_TARGET,
    register_revenue_with_drivers,
)

#: ``provision`` registers its admin with this password, so it is also the
#: password of the company this script leaves behind.
PASSWORD = "Detection-Tests-2026"
VIEWER_PASSWORD = "Recommendation-Demo-2026"

RULE = "─" * 78


def say(text: str = "") -> None:
    print(text, flush=True)


def use_utf8() -> None:
    """Print the same text to a console and to a redirected file.

    On Windows a redirected stdout defaults to the OS codepage, which cannot encode
    the rules and check marks below — and a verifier that dies on its own formatting
    is worse than useless.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def head(text: str) -> None:
    say()
    say(RULE)
    say(text)
    say(RULE)


def prose_of(payload: dict) -> str:
    return json.dumps(payload)


def assert_no_unsupported_claims(label: str, payload: dict) -> None:
    """The final rule, checked on what the wire actually carried."""

    prose = prose_of(payload).lower()
    for word in CAUSAL_WORDS:
        assert word not in prose, f"{label}: served prose claims a cause — {word!r}"
    for word in GUARANTEE_WORDS:
        assert word not in prose, f"{label}: served prose promises an outcome — {word!r}"
    say(f"  ✓ no causal claim, no guaranteed outcome in the served payload ({label})")


def render_card(card: dict, index: int) -> None:
    """One card, in the reading order the panel puts it in."""

    target = card.get("target_area")
    say(f"  ── {index}. {card['priority_label']} · {card['impact']['label']} "
        f"· {card['confidence']['level']} CONFIDENCE")
    say(f"     FINDING        {card['finding']}")
    if target:
        chain = " › ".join(target["chain"]) or "—"
        shares = target.get("share_pct")
        share_text = "" if shares is None else f"  ({abs(shares):.1f}% of the movement)"
        say(f"     TARGET AREA    {chain}   [{target['entity_type']}]{share_text}")
        if target.get("drill_next"):
            say(f"                    can drill into: {', '.join(target['drill_next'])}")
    else:
        say("     TARGET AREA    none named — no stored breakdown")
    lever = card["lever"]
    say(f"     LEVER          {lever['label']}  [{lever['source']}]")
    say(f"                    {lever['note']}")
    say(f"     ACTION         {card['action']}")
    say(f"     IMPACT         {card['impact']['label']} — {card['impact']['basis']}")
    say(f"     OWNER          {card['owner']}")
    say(f"     CONFIDENCE     {card['confidence']['level']} — {card['confidence']['meaning']}")
    say(f"     MONITOR        {'; '.join(card['monitoring']['metrics'])}")
    say(f"                    review window · {card['monitoring']['window']}")
    say("     WHY            " + "\n                    ".join(card["why"]))
    say(f"     CAUSATION      {card['causation_note']}")


def render(label: str, payload: dict) -> None:
    """The panel, as text. Every line below came off the wire."""

    result = payload["result"]
    head(label)
    say(f"  STANCE        {result['stance']}   verdict {result['verdict']}   "
        f"movement {result['movement_direction']}   confidence {result['confidence']['level']}")
    say(f"  HEADLINE      {result['headline']}")
    say(f"  BODY          {result['body']}")
    summary = result["evidence_summary"]
    say(f"  EVIDENCE      measured {summary['actual']} against {summary['expected']} expected "
        f"({summary['deviation_pct']}%), {summary['reference_count']} comparable periods, "
        f"basis {summary['comparison']}")
    say(f"                largest contributing part: {summary['top_contributor'] or '—'}"
        + ("" if summary["top_contributor_share_pct"] is None
           else f" ({abs(summary['top_contributor_share_pct']):.1f}%)"))
    say(f"  AWAITING      awaiting_breakdown={result['awaiting_breakdown']}")
    if result["recommendations"]:
        say(f"  PREAMBLE      {result['action_preamble']}")
        for index, card in enumerate(result["recommendations"], start=1):
            say()
            render_card(card, index)
    else:
        say("  CARDS         none — this result offers no targeted action")
    if result["next_steps"]:
        say("  NEXT STEPS")
        for index, step in enumerate(result["next_steps"], start=1):
            say(f"     {index}. {step}")
    if not result["recommendations"]:
        say(f"  MONITOR       {'; '.join(result['monitoring']['metrics'])}")
        say(f"                review window · {result['monitoring']['window']}")
    say("  LIMITATIONS")
    for line in result["limitations"]:
        say(f"     · {line}")
    say(f"  EXECUTIVE     {json.dumps(result['executive'], indent=None)}")
    say(f"  MAY RESPOND   {payload['may_submit_feedback']}   "
        f"responses on file: {len(payload['feedback'])}")
    assert_no_unsupported_claims(label, payload)


def recommendations(actor: ApiActor, base: str, run_id: str) -> dict:
    response = actor.get(f"{base}/detection-runs/{run_id}/recommendations")
    assert response.status_code == 200, response.text
    return response.json()


def main() -> int:
    use_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--company", default="Aurora Retail Actions")
    parser.add_argument("--email", default="demo@aurora-actions.example.com")
    args = parser.parse_args()

    demo_dir = BACKEND / "data" / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    seeded = build_company_a_source(demo_dir / "aurora_actions.db")

    with httpx.Client(base_url=args.url, timeout=120.0) as client:
        probe = client.get("/health")
        assert probe.status_code == 200, (
            f"no server answering at {args.url} — start uvicorn first ({probe.status_code})"
        )
        head(f"LIVE SERVER {args.url} — provisioning a tenant over HTTP")
        admin, base, tables = provision(
            client,
            email=args.email,
            company_name=args.company,
            source_name="Aurora Commerce",
            source_path=seeded["path"],
            scope={"orders": "order_date"},
        )
        company_id = base.rsplit("/", 1)[-1]
        say(f"  company {company_id} · admin {args.email} / {PASSWORD}")

        revenue_id = register_revenue_with_drivers(
            admin, base, source_table_id=tables["orders"]["id"]
        )
        approve_bucket_config(
            admin,
            base,
            config_key="aurora-actions-weekly",
            name="Aurora weekly trading pattern",
            buckets={
                "same_day_of_week": {"enabled": True, "days": ["FRI"]},
                "yoy_period": {"enabled": True},
            },
        )
        say("  revenue registered with region/channel breakdowns and three drivers")
        say("  (Order volume and Promotions controllable; Competitor pricing not)")

        runs: dict[str, dict] = {}
        for label, target in (
            ("abnormal", COMPANY_A_TARGET),
            ("normal", NORMAL_TARGET),
            ("sparse", SPARSE_TARGET),
        ):
            runs[label] = run_detection(admin, base, revenue_id, target)
            say(f"  detection {target.isoformat()} → {runs[label]['result']['status']}")

        abnormal_run = runs["abnormal"]["run_id"]

        # 1. Abnormal, no breakdown: advice that admits it has no area.
        render(
            "SCENARIO 1 · ABNORMAL, no stored breakdown (advice aimed at the KPI)",
            recommendations(admin, base, abnormal_run),
        )

        # 2. Break it down: the same result, re-aimed at the area the ranking names.
        contribution = admin.post(
            f"{base}/investigation/contribution",
            json={
                "kpi_id": "revenue",
                "target_date": COMPANY_A_TARGET.isoformat(),
                "dimension": None,
                "path": [],
                "top_k": 8,
            },
        )
        assert contribution.status_code == 200, contribution.text
        ranked = contribution.json()["result"]
        leader = ranked["contributors"][0]
        say()
        say(f"  breakdown by {ranked['dimension']}: {leader['label']} leads with "
            f"{abs(leader['absolute_share_pct'] or 0):.1f}% of the movement")
        render(
            "SCENARIO 2 · the same result after a breakdown (re-aimed at a named area)",
            recommendations(admin, base, abnormal_run),
        )

        # 3. Drill one level deeper: the entity type changes with the dimension.
        deeper = admin.post(
            f"{base}/investigation/contribution",
            json={
                "kpi_id": "revenue",
                "target_date": COMPANY_A_TARGET.isoformat(),
                "dimension": ranked["next_dimensions"][0] if ranked["next_dimensions"] else None,
                "path": [{"dimension": ranked["dimension"], "value": leader["entity"]}],
                "top_k": 8,
            },
        )
        assert deeper.status_code == 200, deeper.text
        deeper_result = deeper.json()["result"]
        say()
        say(f"  drilled {ranked['dimension']}={leader['entity']} → "
            f"{deeper_result['dimension']}: "
            f"{deeper_result['contributors'][0]['label'] if deeper_result['contributors'] else '—'}")
        render(
            "SCENARIO 3 · drilled a level deeper (the advice follows the deeper area)",
            recommendations(admin, base, abnormal_run),
        )

        # 4. A response, recorded and read back.
        first_card = recommendations(admin, base, abnormal_run)["result"]["recommendations"][0]
        recorded = admin.post(
            f"{base}/detection-runs/{abnormal_run}/recommendation-feedback",
            json={
                "recommendation_key": first_card["key"],
                "usefulness": "USEFUL",
                "action_status": "IN_REVIEW",
                "comment": "Regional review scheduled for the next trading week.",
            },
        )
        assert recorded.status_code in (200, 201), recorded.text
        after = recommendations(admin, base, abnormal_run)
        head("SCENARIO 4 · one recorded response, read back from the server")
        for row in after["feedback"]:
            say(f"  {row['recommendation_key']} → {row['usefulness']} / {row['action_status']}"
                f" by {row['submitted_by_email']}")
            say(f"    note: {row['comment']}")
        assert after["feedback"], "the response was not read back"
        say(f"  verdict still {after['result']['verdict']} — a response cannot move it")

        # 5 and 6. The two verdicts that must not produce an intervention.
        render("SCENARIO 5 · NORMAL (no corrective action)", recommendations(admin, base, runs["normal"]["run_id"]))
        render(
            "SCENARIO 6 · LOW_CONFIDENCE (evidence first, no intervention)",
            recommendations(admin, base, runs["sparse"]["run_id"]),
        )

        # 7. A viewer: may read the advice, may not break a movement down or respond.
        viewer_email = f"viewer.{args.email}"
        created_viewer = admin.post(
            f"{base}/members",
            json={
                "email": viewer_email,
                "full_name": "Vik Viewer",
                "password": VIEWER_PASSWORD,
                "role_key": "VIEWER",
            },
        )
        assert created_viewer.status_code == 201, created_viewer.text
        viewer = login(client, viewer_email, VIEWER_PASSWORD, company_id)
        render(
            "SCENARIO 7 · a viewer without investigation access",
            recommendations(viewer, base, abnormal_run),
        )
        refused = viewer.post(
            f"{base}/detection-runs/{abnormal_run}/recommendation-feedback",
            json={
                "recommendation_key": first_card["key"],
                "usefulness": "USEFUL",
                "action_status": "IN_REVIEW",
                "comment": None,
            },
        )
        say(f"  viewer response refused with {refused.status_code} (expected 403)")
        assert refused.status_code == 403, refused.text

        head("DONE")
        say(f"  company id      {company_id}")
        say(f"  admin login     {args.email} / {PASSWORD}")
        say(f"  viewer login    {viewer_email} / {VIEWER_PASSWORD}")
        say(f"  abnormal run    {abnormal_run}  → /results/{abnormal_run}")
        say(f"  normal run      {runs['normal']['run_id']}")
        say(f"  sparse run      {runs['sparse']['run_id']}")
        say(f"  causation note  {CAUSATION_NOTE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
