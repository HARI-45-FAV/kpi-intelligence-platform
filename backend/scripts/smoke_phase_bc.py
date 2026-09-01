"""Phase B/C smoke: the endpoints the new screens call, over real HTTP.

The pytest suite proves the contracts on a purpose-built tenant. This proves the
same routes serve the *dev database this prototype actually has* -- 4 KPIs, 75
stored runs, 4 legacy WATCH rows -- through the real ASGI stack, so response_model
serialisation, permission gating and JSON shape are all exercised the way the
browser exercises them.

Run from ``backend/`` with ``PYTHONPATH=.``.
"""

from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import create_access_token
from app.main import app
from app.models.detection import DetectionRun
from app.models.tenant import Company, CompanyUser, Role, User


def rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    session = SessionLocal()
    company = session.scalars(select(Company)).first()
    link = session.scalars(
        select(CompanyUser).where(
            CompanyUser.company_id == company.id, CompanyUser.status == "ACTIVE"
        )
    ).first()
    user = session.get(User, link.user_id)
    role = session.get(Role, link.role_id)
    token, _ = create_access_token(user.id, user.email, company_id=company.id)
    print(f"company={company.company_name} user={user.email} role={role.role_key}")

    base = f"/api/v1/companies/{company.id}"
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  OK   {label}")
        else:
            print(f"  FAIL {label} {detail}")
            failures.append(label)

    # ---------------------------------------------------------------- monitoring
    rule("GET /monitoring  (the Monitoring overview)")
    response = client.get(f"{base}/monitoring", params={"window_days": 730}, headers=headers)
    check("200", response.status_code == 200, response.text[:300])
    body = response.json()
    counts = body["counts"]
    print(f"  counts: {json.dumps(counts)}")
    check(
        "verdict tiles sum to the evaluated total",
        counts["normal"] + counts["abnormal"] + counts["low_confidence"] + counts["unrecognised"]
        == counts["evaluated"],
    )
    check("legacy statuses are named, not folded", counts["unrecognised_statuses"] == ["WATCH"])
    check("an admin sees the investigation layer", body["findings_open"] is not None)
    check("movements carry the run id the Result page needs", all(
        row["detection_run_id"] for row in body["biggest_movements"]
    ))
    check("has_contribution disclosed to an entitled reader", all(
        row["has_contribution"] is not None for row in body["biggest_movements"]
    ))

    # ------------------------------------------------------------ run detail
    rule("GET /detection-runs/{id}  (OVERVIEW + WHY FLAGGED + EVIDENCE)")
    run_id = body["recent_abnormal"][0]["detection_run_id"]
    response = client.get(f"{base}/detection-runs/{run_id}", headers=headers)
    check("200", response.status_code == 200, response.text[:300])
    detail = response.json()
    result = detail["result"]
    check("run_id echoed", detail.get("run_id") == run_id)
    check("executed_at present", bool(detail.get("executed_at")))
    check("evidence attached for a kpi.read holder", "evidence" in detail)
    evidence = detail.get("evidence") or {}
    stats = evidence.get("statistics", {})
    tolerance = evidence.get("tolerance", {})
    print(
        f"  {result['kpi_key']} {result['target_date']} {result['status']}"
        f" actual={result['actual']} expected={result['expected']}"
    )
    print(
        f"  median={stats.get('median')} mad={stats.get('mad')}"
        f" basis={stats.get('dispersion_basis')} z={stats.get('modified_z_score')}"
        f" threshold={stats.get('z_threshold')} significant={stats.get('statistically_significant')}"
    )
    print(
        f"  tolerance pct={tolerance.get('relative_pct')} breached={tolerance.get('breached')}"
        f" material={tolerance.get('movement_is_material')}"
        f" reference={evidence.get('reference', {}).get('count')} points"
    )
    for field in ("median", "mad", "dispersion_basis", "modified_z_score", "z_threshold"):
        check(f"WHY FLAGGED has {field}", stats.get(field) is not None)
    check(
        "comparable periods are listed with values",
        len(evidence.get("reference", {}).get("points", [])) > 0,
    )
    # The page reads these; it never recomputes them.
    check(
        "the verdict is consistent with the two stored tests",
        (result["status"] == "ABNORMAL")
        == bool(stats.get("statistically_significant") or tolerance.get("breached")),
    )

    # ----------------------------------------------------------- explain result
    rule("POST /results/explain  (the Result page's Explain action)")
    response = client.post(
        f"{base}/results/explain",
        json={"kpi_id": result["kpi_key"], "target_date": result["target_date"]},
        headers=headers,
    )
    check("200", response.status_code == 200, response.text[:300])
    explanation = response.json()["explanation"]
    headings = [section["heading"] for section in explanation["sections"]]
    print(f"  sections: {headings}")
    check(
        "the six sections arrive in the specified order",
        headings
        == [
            "WHAT HAPPENED",
            "WHY IT WAS FLAGGED",
            "TOP CONTRIBUTORS",
            "SUPPORTING BUSINESS CONTEXT",
            "EVIDENCE LIMITATIONS",
            "CONFIDENCE LEVEL",
        ],
        str(headings),
    )
    check("confidence carries its reasons", bool(explanation["confidence"]["reasons"]))
    check("limitations are stated", bool(explanation["limitations"]))
    check("provenance of the prose is labelled", "model_written" in explanation)
    joined = " ".join(section["body"] for section in explanation["sections"]).lower()
    for word in ("caused by", "because of the", "due to the"):
        check(f"no causal claim: {word!r}", word not in joined)
    # Figures in the prose must be the stored ones.
    check(
        "the explanation's figures are the stored figures",
        explanation["facts"]["statistics"]["modified_z_score"] == stats["modified_z_score"],
    )

    # ------------------------------------------------------------ 409 run gate
    rule("The run gate: no stored evaluation means no answer invented")
    missing = client.post(
        f"{base}/results/explain",
        json={"kpi_id": result["kpi_key"], "target_date": "2019-01-01"},
        headers=headers,
    )
    check("409 Conflict, not a fabricated explanation", missing.status_code == 409, missing.text[:200])

    # --------------------------------------------------------------- contributors
    rule("POST /investigation/contribution  (the CONTRIBUTORS section)")
    response = client.post(
        f"{base}/investigation/contribution",
        json={
            "kpi_id": result["kpi_key"],
            "target_date": result["target_date"],
            "path": [],
            "top_k": 8,
        },
        headers=headers,
    )
    check("200", response.status_code == 200, response.text[:300])
    contribution = response.json()["result"]
    print(
        f"  dimension={contribution['dimension']} ranked={contribution['ranked_count']}"
        f" explained={contribution['explained_pct']} shares_available={contribution['shares_available']}"
    )
    for row in contribution["contributors"][:4]:
        print(
            f"    {row['label']}: change={row['change']}"
            f" share={row['share_pct']} abs_share={row['absolute_share_pct']}"
        )
    check("contributors are ranked", len(contribution["contributors"]) > 0)

    # ------------------------------------------------------------------ findings
    rule("GET /investigation/findings  (the notes the Result page shows)")
    response = client.get(
        f"{base}/investigation/findings",
        params={"kpi_id": result["kpi_key"], "target_date": result["target_date"]},
        headers=headers,
    )
    check("200", response.status_code == 200, response.text[:300])
    payload = response.json()
    check("the server states the allowed statuses", bool(payload.get("statuses")))
    print(f"  statuses={payload.get('statuses')} counts={payload.get('counts')}")

    # ------------------------------------------------------- a result list row id
    rule("GET /results  (each row's id must open the Result page)")
    response = client.get(f"{base}/results", headers=headers)
    check("200", response.status_code == 200, response.text[:200])
    items = response.json()["items"]
    check("rows returned", len(items) > 0)
    if items:
        row_id = items[0]["id"]
        stored = session.get(DetectionRun, row_id)
        check(
            "a results-list row id is a detection run id",
            stored is not None,
            f"id {row_id} is not a DetectionRun",
        )
        opened = client.get(f"{base}/detection-runs/{row_id}", headers=headers)
        check("that id opens the Result page endpoint", opened.status_code == 200)

    print("\n" + "-" * 78)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
