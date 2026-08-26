"""The golden end-to-end test: Sprint 1's definition of done, executable.

One test walks the entire administrator journey through the real HTTP API:

    register -> login -> create company -> add member with a scoped role
    -> register data source -> test connection -> discover tables
    -> select analytical scope -> profile (access-aware) -> detect grain
    -> detect relationships -> analyse join safety -> check freshness
    -> reconcile sources -> upload a versioned reference document
    -> get a discovery proposal -> accept it -> validate (9 checks)
    -> approve and activate -> verify lineage, contract and audit trail
    -> publish an immutable catalog version

Then the tenant boundary: a second company must not be able to reach the first
by any route.

This is deliberately one long test rather than thirty small ones. The thing that
has to work is the *journey* â€” every step consuming what the previous step
produced. Thirty isolated tests can all pass while the journey is broken.
"""

from __future__ import annotations

import json

from tests.conftest import API, login, register

ADMIN_EMAIL = "asha.admin@novamart-hq.com"
ADMIN_PASSWORD = "GoldenFlow-Admin-2026"
ANALYST_EMAIL = "ravi.analyst@novamart-hq.com"
ANALYST_PASSWORD = "GoldenFlow-Analyst-2026"
REGIONAL_EMAIL = "sana.south@novamart-hq.com"
REGIONAL_PASSWORD = "GoldenFlow-Regional-2026"

# The frozen NovaMart analytical scope. customer_master is deliberately excluded
# (it holds personal data) and campaigns_archive is excluded as historical.
SCOPE = {
    "orders": "order_date",
    "order_items": None,
    "marketing_daily": "spend_date",
    "product_master": None,
    "region_targets": None,
}


def test_golden_sprint_one_flow(client, source_fixture):
    # ---------------------------------------------------------------- 1. Identity
    admin = register(client, ADMIN_EMAIL, ADMIN_PASSWORD, "Asha Admin")

    session = admin.get(f"{API}/auth/session")
    assert session.status_code == 200
    assert session.json()["memberships"] == [], "a new user belongs to no company yet"

    # ---------------------------------------------------------------- 2. Company
    created = admin.post(
        f"{API}/companies",
        json={
            "company_name": "NovaMart",
            "industry": "E-commerce",
            "country": "India",
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "fiscal_year_start_month": 4,
            "week_start_day": 1,
        },
    )
    assert created.status_code == 201, created.text
    company = created.json()
    company_id = company["id"]
    assert company["status"] == "DRAFT", "a company is not active until it is configured"
    base = f"{API}/companies/{company_id}"

    # The creator is ADMIN, and a default calendar exists so "month" has meaning.
    calendars = admin.get(f"{base}/calendars").json()
    assert len(calendars) == 1 and calendars[0]["is_default"] is True
    assert calendars[0]["fiscal_year_start_month"] == 4

    # ------------------------------------------------------- 3. Members and roles
    roles = {role["role_key"]: role for role in admin.get(f"{base}/roles").json()}
    assert "ADMIN" in roles and roles["ADMIN"]["is_admin_role"] is True
    assert "data.read_pii" not in roles["ANALYST"]["permissions"], (
        "an analyst must not read personal data by default"
    )

    analyst_created = admin.post(
        f"{base}/members",
        json={
            "email": ANALYST_EMAIL,
            "full_name": "Ravi Analyst",
            "password": ANALYST_PASSWORD,
            "role_key": "ANALYST",
        },
    )
    assert analyst_created.status_code == 201, analyst_created.text

    regional_created = admin.post(
        f"{base}/members",
        json={
            "email": REGIONAL_EMAIL,
            "full_name": "Sana South",
            "password": REGIONAL_PASSWORD,
            "role_key": "REGIONAL_MANAGER",
            "row_scope": {"region": ["South"]},
        },
    )
    assert regional_created.status_code == 201, regional_created.text
    assert regional_created.json()["row_scope"] == {"region": ["South"]}

    # The last administrator cannot be removed: that would strand the workspace.
    admin_membership = next(
        m for m in admin.get(f"{base}/members").json() if m["role_key"] == "ADMIN"
    )
    orphaned = admin.delete(f"{base}/members/{admin_membership['membership_id']}")
    assert orphaned.status_code == 403
    assert "only active administrator" in orphaned.json()["message"]

    # -------------------------------------------------- 4. Protected admin unlock
    unlock = client.post(
        f"{API}/auth/admin-unlock",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "company_id": company_id},
    )
    assert unlock.status_code == 200, unlock.text
    assert "kpi.approve" in unlock.json()["permissions"]

    # An analyst cannot enter the governance workspace even with valid credentials.
    denied = client.post(
        f"{API}/auth/admin-unlock",
        json={"email": ANALYST_EMAIL, "password": ANALYST_PASSWORD, "company_id": company_id},
    )
    assert denied.status_code == 403

    # -------------------------------------------------------- 5. Data source
    connectors = {c["source_type"]: c for c in client.get(f"{API}/connectors").json()["connectors"]}
    assert connectors["SUPABASE"]["implemented"] is True
    assert connectors["SNOWFLAKE"]["implemented"] is False, "warehouses are interface-only"
    # Supabase onboarding asks for exactly two things: the project URL and the
    # secret key. No database password, because a secret key is a REST
    # credential and cannot open a Postgres session.
    supabase_fields = {f["name"] for f in connectors["SUPABASE"]["fields"]}
    assert supabase_fields == {"supabase_url", "secret_key"}
    assert all(f["required"] for f in connectors["SUPABASE"]["fields"])
    assert connectors["SUPABASE"]["accepts_connection_uri"] is False

    source_created = admin.post(
        f"{base}/data-sources",
        json={
            "name": "NovaMart Commerce",
            "source_type": "SQLITE",
            "path": source_fixture["path"],
            "refresh_frequency": "DAILY",
            "timezone": "Asia/Kolkata",
        },
    )
    assert source_created.status_code == 201, source_created.text
    source = source_created.json()
    source_id = source["id"]
    assert source["connection_status"] == "UNTESTED"
    # A credential must never come back out of the API.
    assert "password" not in source_created.text.lower() or source["has_credentials"] is False

    tested = admin.post(f"{base}/data-sources/{source_id}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    assert tested.json()["connection_status"] == "CONNECTED"
    assert any("tables detected" in check["check"] for check in tested.json()["checks"])

    # ------------------------------------------------------------ 6. Discovery
    discovered = admin.post(f"{base}/data-sources/{source_id}/discover")
    assert discovered.status_code == 200, discovered.text
    assert discovered.json()["tables_found"] >= 7
    assert "metadata only" in discovered.json()["note"]

    tables = {t["table_name"]: t for t in admin.get(f"{base}/tables").json()}
    for expected in (*SCOPE, "customer_master", "campaigns_archive"):
        assert expected in tables, f"{expected} should have been discovered"
    assert all(not t["selected"] for t in tables.values()), (
        "discovery must not grant analytical access"
    )

    # Personal data is classified on first sight, before any profiling.
    customer_columns = {
        c["column_name"]: c
        for c in admin.get(f"{base}/tables/{tables['customer_master']['id']}/columns").json()
    }
    assert customer_columns["email"]["is_pii"] is True
    assert customer_columns["email"]["classification"] == "RESTRICTED"
    assert customer_columns["phone"]["is_pii"] is True
    assert customer_columns["region"]["is_pii"] is False, "region is not personal data"

    # --------------------------------------------------------- 7. Analytical scope
    scope_response = admin.put(
        f"{base}/data-scope",
        json={
            "replace": True,
            "tables": [
                {
                    "source_table_id": tables[name]["id"],
                    "enabled": True,
                    "primary_time_column": time_column,
                }
                for name, time_column in SCOPE.items()
            ],
        },
    )
    assert scope_response.status_code == 200, scope_response.text
    assert scope_response.json()["enabled_count"] == len(SCOPE)

    # Profiling an unselected table is refused, not silently allowed.
    out_of_scope = admin.post(f"{base}/tables/{tables['campaigns_archive']['id']}/profile")
    assert out_of_scope.status_code == 403
    assert "approved data scope" in out_of_scope.json()["message"]

    # ------------------------------------ 8. Profiling, grain, relationships, etc.
    analysis = admin.post(f"{base}/analysis/run")
    assert analysis.status_code == 200, analysis.text
    steps = analysis.json()["steps"]
    assert analysis.json()["tables_analysed"] == len(SCOPE)
    assert analysis.json()["connector_queries"] > 0, "profiling must push work to the source"

    profiles = {p["table"]: p for p in steps["profiling"]["tables"]}
    orders_profile = next(p for name, p in profiles.items() if name.endswith("orders"))
    assert orders_profile["row_count"] > 300
    assert orders_profile["quality_status"] in {"GOOD", "WARNING"}

    # Quality defects are reported, never repaired.
    items_profile = next(p for name, p in profiles.items() if name.endswith("order_items"))
    assert any("null" in warning for warning in items_profile["warnings"]), (
        "the nulls seeded into item_value must surface as a warning"
    )

    # Grain is inferred from the data, not assumed.
    grains = {g["table"].split(".")[-1]: g for g in steps["grain"]["tables"]}
    assert grains["orders"]["is_unique"] is True
    assert grains["orders"]["grain_columns"], "orders must have a detected grain"
    assert grains["orders"]["method"] in {"uniqueness_scan", "declared_primary_key"}
    assert set(grains["order_items"]["grain_columns"]) >= {"order_id", "product_id"}, (
        "order_items grain should be (order_id, product_id)"
    )
    assert grains["marketing_daily"]["time_grain"] == "DAY"

    # Both relationship discovery paths must have fired.
    relationships = admin.get(f"{base}/analysis/relationships").json()["relationships"]
    declared = [r for r in relationships if r["is_declared"]]
    inferred = [r for r in relationships if not r["is_declared"]]
    assert declared, "the declared order_items.product_id foreign key must be found"
    assert inferred, "orders.customer_id has no declared FK and must be inferred"
    assert all(r["confidence"] is not None for r in relationships)

    product_edge = next(
        r for r in declared if r["from_table"] == "order_items" and r["from_column"] == "product_id"
    )
    assert product_edge["to_table"] == "product_master"
    assert product_edge["confidence"] == 1.0
    assert product_edge["join_safety"]["level"] == "SAFE"

    # The fan-out trap must be caught: region_targets repeats each region monthly.
    risky = [
        r
        for r in relationships
        if r["join_safety"] and r["join_safety"]["level"] in {"RISKY", "SAFE_WITH_AGGREGATION"}
    ]
    assert risky, "joining orders to a per-region-per-month table must be flagged"
    assert any("region_targets" in (r["to_table"] or "") for r in risky)
    assert all(r["join_safety"]["guidance"] for r in risky), "a warning needs actionable guidance"

    # Freshness is measured against the declared cadence and reported honestly.
    freshness = {f["table"].split(".")[-1]: f for f in steps["freshness"]["tables"]}
    assert freshness["orders"]["status"] == "FRESH"
    assert freshness["marketing_daily"]["status"] == "STALE", (
        "marketing lags three days behind a daily cadence"
    )
    assert freshness["marketing_daily"]["note"]

    # Reconciliation records how sources may cooperate, without combining them.
    reconciliation = admin.get(f"{base}/analysis/reconciliation").json()["pairs"]
    orders_vs_marketing = next(
        p
        for p in reconciliation
        if {p["left_table"], p["right_table"]} == {"orders", "marketing_daily"}
    )
    # They share region and channel, but marketing_daily also carries sector,
    # which lives in order_items rather than orders. Alignment on the full
    # granularity therefore needs an explicit dimension mapping, not just a
    # roll-up — which is the more precise of the two verdicts.
    assert orders_vs_marketing["status"] == "REQUIRES_DIMENSION_MAPPING"
    assert "region" in orders_vs_marketing["shared_dimensions"]
    assert "channel" in orders_vs_marketing["shared_dimensions"]
    assert "sector" in orders_vs_marketing["unmapped_dimensions"]
    assert orders_vs_marketing["guidance"]
    # A dimension lookup with no time axis is not a reconciliation question.
    assert not any(
        "product_master" in {p["left_table"], p["right_table"]} for p in reconciliation
    ), "tables without a time axis should be excluded, not reported as UNKNOWN"

    # ----------------------------------------------- 9. Access-aware profiling
    # The analyst may profile, but must not read personal data. Bring
    # customer_master into scope to prove profiling withholds rather than redacts.
    widened = [
        {"source_table_id": tables[name]["id"], "enabled": True, "primary_time_column": tc}
        for name, tc in SCOPE.items()
    ]
    widened.append({"source_table_id": tables["customer_master"]["id"], "enabled": True})
    assert admin.put(f"{base}/data-scope", json={"replace": True, "tables": widened}).status_code == 200

    analyst = login(client, ANALYST_EMAIL, ANALYST_PASSWORD, company_id)
    analyst_profile = analyst.post(f"{base}/tables/{tables['customer_master']['id']}/profile")
    assert analyst_profile.status_code == 200, analyst_profile.text
    body = analyst_profile.json()
    assert body["withheld_columns"] >= 2, "email and phone must be withheld from an analyst"
    withheld_names = {w["column"] for w in body["warnings"] and body.get("withheld", [])} or {
        c["column_name"] for c in body["columns"] if not c["readable"]
    }
    assert {"email", "phone"} <= withheld_names
    for column in body["columns"]:
        if column["column_name"] in {"email", "phone"}:
            assert column["profile"] is None, "a withheld column must carry no statistics"
            assert column["withheld_reason"]

    # An admin, holding data.read_pii, sees the same columns profiled.
    admin_profile = admin.post(f"{base}/tables/{tables['customer_master']['id']}/profile")
    assert admin_profile.status_code == 200
    admin_columns = {c["column_name"]: c for c in admin_profile.json()["columns"]}
    assert admin_columns["email"]["profile"] is not None
    assert admin_columns["email"]["readable"] is True

    # ------------------------------------------------------- 10. Reference document
    document = admin.post(
        f"{base}/documents",
        data={
            "metadata": json.dumps(
                {
                    "title": "NovaMart KPI Handbook",
                    "document_type": "KPI_HANDBOOK",
                    "description": "Finance-approved KPI definitions.",
                    "access_scope": ["ADMIN", "ANALYST", "EXECUTIVE"],
                    "effective_from": "2026-04-01",
                    "inline_content": (
                        "Revenue is the sum of order_value across all orders in the period.\n"
                        "Orders is the count of distinct order_id.\n"
                        "Unique Customers is the count of distinct customer_id.\n"
                        "AOV is Revenue divided by Orders."
                    ),
                }
            )
        },
    )
    assert document.status_code == 201, document.text
    document_id = document.json()["id"]
    assert document.json()["current_version"] == 1
    assert document.json()["document_class"] == "REFERENCE"
    assert document.json()["retrieval_ready"] is False, "no embeddings in Sprint 1"

    # A revision creates v2 and leaves v1 resolvable.
    revised = admin.post(
        f"{base}/documents/{document_id}/versions",
        data={
            "metadata": json.dumps(
                {
                    "title": "NovaMart KPI Handbook",
                    "document_type": "KPI_HANDBOOK",
                    "change_note": "Clarified that AOV is not additive across periods.",
                    "inline_content": "Revenue = SUM(order_value). AOV must be recomputed, never averaged.",
                }
            )
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["current_version"] == 2
    assert len(revised.json()["versions"]) == 2
    v1 = admin.get(f"{base}/documents/{document_id}/content", params={"version": 1})
    assert v1.status_code == 200 and b"Unique Customers" in v1.content

    # ------------------------------------------------- 11. KPI discovery proposals
    proposals = admin.get(f"{base}/kpi-proposals").json()
    assert "deterministically" in proposals["note"]
    by_key = {p["kpi_key"]: p for p in proposals["proposals"]}

    assert "revenue" in by_key, f"expected a Revenue proposal, got {sorted(by_key)}"
    revenue_proposal = by_key["revenue"]
    assert revenue_proposal["formula_expression"] == "SUM(orders.order_value)"
    assert revenue_proposal["evidence"]["method"] == "deterministic profile scan"
    assert {d["dimension_name"] for d in revenue_proposal["dimensions"]} >= {"region", "channel"}
    assert revenue_proposal["drivers"], "a KPI proposal should carry candidate drivers"

    assert "orders" in by_key and by_key["orders"]["formula_expression"] == (
        "COUNT(DISTINCT orders.order_id)"
    )
    assert "unique_customers" in by_key
    assert by_key["unique_customers"]["formula_expression"] == (
        "COUNT(DISTINCT orders.customer_id)"
    )
    assert "aov" in by_key and by_key["aov"]["kind"] == "RATIO"

    # --------------------------------------------------- 12. Accept, validate, approve
    accepted = admin.post(
        f"{base}/kpi-proposals/accept",
        json={
            "kpi_key": "revenue",
            "overrides": {
                "business_definition": "Total recognised sales revenue across all orders.",
                "definition_document_id": document_id,
                "definition_document_version": 2,
                "definition_source": "NovaMart KPI Handbook v2, approved by Finance.",
            },
        },
    )
    assert accepted.status_code == 201, accepted.text
    revenue = accepted.json()
    revenue_id = revenue["id"]
    assert revenue["status"] == "PROPOSED", "a proposal is never activated automatically"
    revenue_version_id = revenue["versions"][0]["id"]
    assert revenue["versions"][0]["proposal_origin"] == "DISCOVERY"

    # Approval is refused before validation has run.
    premature = admin.post(f"{base}/kpi-versions/{revenue_version_id}/approve", json={})
    assert premature.status_code == 422
    assert "not passed validation" in premature.json()["message"]

    validation = admin.post(f"{base}/kpi-versions/{revenue_version_id}/validate")
    assert validation.status_code == 200, validation.text
    report = validation.json()
    checks = {c["test_type"]: c for c in report["checks"]}
    assert len(checks) == 9, f"all nine governance checks must run, got {sorted(checks)}"
    assert report["ready_for_approval"] is True, report["summary"]

    for required in (
        "FORMULA_PARSES",
        "COLUMNS_EXIST",
        "TIME_FIELD_VALID",
        "AGGREGATION_VALID",
        "DUPLICATE_COUNTING",
        "GRAIN_COMPATIBLE",
        "ACCESS_POLICY_VALID",
        "RECONCILES_TO_SOURCE",
    ):
        assert checks[required]["status"] in {"PASS", "WARN"}, (
            f"{required} failed: {checks[required]['actual']}"
        )

    # Check 9 executes the KPI: the number is real, produced by SQL.
    reconciles = checks["RECONCILES_TO_SOURCE"]
    assert reconciles["evidence"]["value"] > 0
    assert "SUM" in reconciles["evidence"]["sql"]

    approved = admin.post(
        f"{base}/kpi-versions/{revenue_version_id}/approve",
        json={"reason": "Definition matches KPI Handbook v2; Finance signed off."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "ACTIVE"

    # ------------------------------------------- 13. Register the remaining KPIs
    orders_table_id = tables["orders"]["id"]
    for key in ("orders", "unique_customers", "aov"):
        created_kpi = admin.post(f"{base}/kpi-proposals/accept", json={"kpi_key": key})
        assert created_kpi.status_code == 201, created_kpi.text
        version_id = created_kpi.json()["versions"][0]["id"]
        result = admin.post(f"{base}/kpi-versions/{version_id}/validate").json()
        assert result["ready_for_approval"] is True, f"{key}: {result['summary']}"
        activated = admin.post(
            f"{base}/kpi-versions/{version_id}/approve",
            json={"reason": "Reviewed against the KPI Handbook."},
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "ACTIVE"

    registry = admin.get(f"{base}/kpis").json()
    assert len(registry) == 4
    assert all(kpi["status"] == "ACTIVE" for kpi in registry)
    assert {kpi["kpi_key"] for kpi in registry} == {
        "revenue",
        "orders",
        "unique_customers",
        "aov",
    }

    # ------------------------------------------------- 14. Contract, lineage, values
    detail = admin.get(f"{base}/kpis/{revenue_id}").json()
    contract = detail["version"]
    assert contract["status"] == "ACTIVE" and contract["version"] == 1
    assert contract["formula"] == "SUM(orders.order_value)"
    assert contract["time_grain"] == "DAY"
    assert contract["calendar"]["fiscal_year_start_month"] == 4
    assert contract["materiality"]["relative_threshold_pct"] == 5.0
    assert contract["governance"]["approved_by"]
    assert contract["governance"]["definition_document_version"] == 2

    # Lineage is derived from the contract, so it cannot drift from the formula.
    lineage = {(item["role"], item["column"]) for item in contract["lineage"]}
    assert ("NUMERATOR", "order_value") in lineage
    assert ("TIME", "order_date") in lineage
    assert any(role == "DIMENSION" for role, _column in lineage)
    assert all(item["table"] == "orders" for item in contract["lineage"] if item["column"])

    # Declaring a dimension authorises a breakdown; it does not schedule work.
    assert all(
        "not a per-entity monitoring instruction" in d["monitoring_note"]
        for d in contract["dimensions"]
    )

    # A ratio KPI must be marked non-additive: summing an AOV across periods
    # produces a plausible, wrong number.
    aov = next(k for k in registry if k["kpi_key"] == "aov")
    aov_contract = admin.get(f"{base}/kpis/{aov['id']}").json()["version"]
    assert aov_contract["kind"] == "RATIO"
    assert aov_contract["denominator"]["effective_aggregation"] == "COUNT_DISTINCT"
    assert aov_contract["is_additive"] is False
    assert "Never sum" in aov_contract["additivity_note"]
    # Revenue, by contrast, is additive.
    assert contract["is_additive"] is True

    # Unique Customers is a genuinely different number from Orders.
    unique_customers = next(k for k in registry if k["kpi_key"] == "unique_customers")
    uc_version_id = unique_customers["versions"][0]["id"]
    orders_kpi = next(k for k in registry if k["kpi_key"] == "orders")
    orders_version_id = orders_kpi["versions"][0]["id"]
    uc_value = admin.post(f"{base}/kpi-versions/{uc_version_id}/preview", json={}).json()
    orders_value = admin.post(f"{base}/kpi-versions/{orders_version_id}/preview", json={}).json()
    assert uc_value["rows"][0]["value"] < orders_value["rows"][0]["value"], (
        "customers place repeat orders, so distinct customers must be fewer than orders"
    )

    # The value comes from SQL, and the SQL is shown.
    preview = admin.post(
        f"{base}/kpi-versions/{revenue_version_id}/preview",
        json={"group_by": ["region"], "limit": 10},
    )
    assert preview.status_code == 200, preview.text
    assert "No model involved" in preview.json()["method"]
    rows = preview.json()["rows"]
    assert len(rows) == 4, "four regions"
    assert all(row["value"] > 0 for row in rows)
    assert rows == sorted(rows, key=lambda r: -r["numerator"]), "largest contributor first"

    # An ungoverned breakdown is refused.
    bad_breakdown = admin.post(
        f"{base}/kpi-versions/{revenue_version_id}/preview",
        json={"group_by": ["customer_id"]},
    )
    assert bad_breakdown.status_code == 422

    # ------------------------------------------------- 15. Editing creates a version
    revision = admin.post(
        f"{base}/kpis/{revenue_id}/versions",
        json={
            "name": "Revenue",
            "business_definition": "Total recognised sales revenue, excluding cancelled orders.",
            "formula_expression": "SUM(orders.order_value)",
            "source_table_id": orders_table_id,
            "time_field": "order_date",
            "time_grain": "DAY",
            "unit": "currency",
            "dimensions": [
                {"dimension_name": "region", "source_column": "region"},
                {"dimension_name": "channel", "source_column": "channel"},
            ],
        },
    )
    assert revision.status_code == 200, revision.text
    assert revision.json()["version"]["version"] == 2
    assert revision.json()["version"]["status"] == "DRAFT"

    # v1 keeps serving until v2 is approved.
    still_live = admin.get(f"{base}/kpis/{revenue_id}").json()["version"]
    assert still_live["version"] == 1 and still_live["status"] == "ACTIVE"

    # -------------------------------------------------------- 16. Catalog version
    catalog = admin.get(f"{base}/catalog").json()
    assert catalog["counts"]["active_kpis"] == 4
    assert catalog["counts"]["selected_tables"] == len(SCOPE) + 1
    assert catalog["kpi_registry"], "the catalog must carry the KPI registry"
    assert "anomaly detection" in " ".join(catalog["boundaries"]["not_in_sprint_1"])

    published = admin.post(f"{base}/catalog/publish", json={"note": "Sprint 1 baseline."})
    assert published.status_code == 201, published.text
    assert published.json()["version"] == 1
    assert published.json()["active_kpi_count"] == 4
    assert published.json()["checksum_sha256"]

    snapshot = admin.get(f"{base}/catalog/versions/1").json()
    assert snapshot["snapshot"]["company"]["name"] == "NovaMart"

    # --------------------------------------------------------- 17. Company activation
    activated_company = admin.post(f"{base}/activate")
    assert activated_company.status_code == 200, activated_company.text
    assert activated_company.json()["status"] == "ACTIVE"

    # ------------------------------------------------------- 18. Audit and telemetry
    audit_entries = admin.get(f"{base}/audit", params={"limit": 500}).json()
    actions = {entry["action"] for entry in audit_entries}
    for expected in (
        "company.created",
        "member.added",
        "source.created",
        "source.tested",
        "source.tables_discovered",
        "source.scope_updated",
        "profiling.executed",
        "document.created",
        "document.version_added",
        "kpi.proposed",
        "kpi.validated",
        "kpi.approved",
        "kpi.activated",
        "catalog.published",
        "company.activated",
    ):
        assert expected in actions, f"{expected} missing from the audit trail"

    approval = next(e for e in audit_entries if e["action"] == "kpi.approved")
    assert approval["actor_email"] == ADMIN_EMAIL
    assert approval["new_version"] == "1"
    # Credentials must never reach the audit trail.
    assert "GoldenFlow" not in json.dumps(audit_entries)

    telemetry = admin.get(f"{base}/telemetry/summary").json()
    assert telemetry["requests"] > 0
    assert telemetry["connector"]["queries"] > 0
    assert telemetry["llm"]["calls"] == 0, "Sprint 1 makes no model calls"
    assert telemetry["processing_split"]["llm"] == []

    # ----------------------------------------------------------- 19. Dashboard
    dashboard = admin.get(f"{base}/dashboard").json()
    assert dashboard["system_status"]["kpis"]["active"] == 4
    assert dashboard["system_status"]["data_sources"]["connected"] == 1
    assert dashboard["system_status"]["catalog_version"] == 1
    assert len(dashboard["kpi_summary"]) == 4
    assert all(item["value"] is None for item in dashboard["kpi_summary"]), (
        "Sprint 1 has no monitoring engine, so it must not invent KPI values"
    )
    assert dashboard["system_status"]["freshness"]["stale_tables"], (
        "the stale marketing source must be visible on the dashboard"
    )
    assert dashboard["recent_activity"]


def test_tenant_isolation(client, source_fixture):
    """Company B must not reach Company A by changing an id in the URL."""
    alice = register(client, "alice@alpha-industries.com", "Alpha-Company-Pass-1", "Alice Alpha")
    alpha_id = alice.post(
        f"{API}/companies", json={"company_name": "Alpha Industries", "currency": "USD"}
    ).json()["id"]

    bob = register(client, "bob@beta-traders.com", "Beta-Company-Pass-1", "Bob Beta")
    beta_id = bob.post(
        f"{API}/companies", json={"company_name": "Beta Traders", "currency": "EUR"}
    ).json()["id"]

    assert alpha_id != beta_id

    alpha_source = alice.post(
        f"{API}/companies/{alpha_id}/data-sources",
        json={"name": "Alpha DB", "source_type": "SQLITE", "path": source_fixture["path"]},
    )
    assert alpha_source.status_code == 201
    alpha_source_id = alpha_source.json()["id"]

    # Every route on Alpha is closed to Bob, and the refusal is generic: even
    # confirming that Alpha exists would leak across the tenant boundary.
    for method, path in (
        ("get", f"{API}/companies/{alpha_id}"),
        ("get", f"{API}/companies/{alpha_id}/members"),
        ("get", f"{API}/companies/{alpha_id}/data-sources"),
        ("get", f"{API}/companies/{alpha_id}/tables"),
        ("get", f"{API}/companies/{alpha_id}/kpis"),
        ("get", f"{API}/companies/{alpha_id}/catalog"),
        ("get", f"{API}/companies/{alpha_id}/audit"),
        ("get", f"{API}/companies/{alpha_id}/dashboard"),
        ("post", f"{API}/companies/{alpha_id}/analysis/run"),
        ("post", f"{API}/companies/{alpha_id}/catalog/publish"),
    ):
        response = getattr(bob, method)(path, **({"json": {}} if method == "post" else {}))
        assert response.status_code == 403, f"{method.upper()} {path} leaked to another tenant"
        assert response.json()["code"] == "tenant_isolation"

    # Nor can Bob reach Alpha's resource through his own authorised company.
    cross = bob.post(f"{API}/companies/{beta_id}/data-sources/{alpha_source_id}/test")
    assert cross.status_code == 404, "a foreign resource id must not resolve"

    # Alpha's own administrator is unaffected.
    assert alice.get(f"{API}/companies/{alpha_id}").status_code == 200

    # And a member of neither company is refused as well.
    carol = register(client, "carol@gamma-labs.com", "Gamma-Company-Pass-1", "Carol Gamma")
    assert carol.get(f"{API}/companies/{alpha_id}").status_code == 403
    assert carol.get(f"{API}/companies/{beta_id}").status_code == 403
