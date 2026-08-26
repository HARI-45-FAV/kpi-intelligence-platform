"""The company-defined KPI path, end to end through the real HTTP API.

The premise this pins down: the company is the authority on what its KPIs mean.
The platform's job is to *find* the company's own KPI-definition table, read it
verbatim, bind each formula to real columns, and prove the definition works
against the connected data. Discovery proposals are a separate, optional extra.

Every assertion here is about behaviour the demo depends on:

* the definition table is located by column roles alone -- no table name is
  hardcoded anywhere in the platform, so a differently-named registry still works;
* rows that bind cleanly become governed contracts in PROPOSED, never ACTIVE;
* rows the governed grammar cannot express, or that name a column which does not
  exist, are still *listed* -- the company defined them -- but flagged with the
  precise reason rather than silently dropped or rewritten;
* import is idempotent, so the button stays usable after a partial import;
* an imported contract passes the deterministic validation suite and can then be
  approved, which is the whole point of starting from the company's definition.
"""

from __future__ import annotations

import pytest

from tests.conftest import API, ApiActor, login, register

# The analytical scope for this test. kpi_contracts is deliberately *not* in it:
# a KPI registry is governance metadata about the business, not a table anyone
# wants profiled, and the platform must still find it from discovery alone.
SCOPE = ("orders", "order_items", "marketing_daily", "product_master")


@pytest.fixture
def workspace(request, client, source_fixture) -> tuple[ApiActor, str]:
    """An admin with a connected source, discovered schema and approved scope.

    A fresh company per test, keyed on the test name: the platform database is
    session-scoped, and several of these tests mutate the KPI registry, so
    sharing one workspace would make them order-dependent.
    """
    slug = request.node.name.replace("test_", "")[:30]
    email = f"kpiowner.{slug}@novamart-hq.com"
    admin = register(client, email, "Governed-KPIs-2026", "Kai Owner")
    company = admin.post(
        f"{API}/companies",
        json={"company_name": f"NovaMart {slug}", "currency": "INR", "timezone": "Asia/Kolkata"},
    )
    assert company.status_code == 201, company.text
    company_id = company.json()["id"]
    base = f"{API}/companies/{company_id}"

    source = admin.post(
        f"{base}/data-sources",
        json={
            "name": "NovaMart Warehouse",
            "source_type": "SQLITE",
            "path": source_fixture["path"],
            "refresh_frequency": "DAILY",
            "timezone": "Asia/Kolkata",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    assert admin.post(f"{base}/data-sources/{source_id}/discover").status_code == 200

    tables = {t["table_name"]: t for t in admin.get(f"{base}/tables").json()}
    assert "kpi_contracts" in tables, "the company KPI registry must be discovered"
    assert admin.put(
        f"{base}/data-scope",
        json={
            "replace": True,
            "tables": [
                {"source_table_id": tables[name]["id"], "enabled": True} for name in SCOPE
            ],
        },
    ).status_code == 200

    # Profiling is what gives the catalog its semantic types, which is how the
    # reader knows which column places a KPI in time.
    for name in SCOPE:
        assert admin.post(f"{base}/tables/{tables[name]['id']}/profile").status_code == 200

    return (admin, base)


def test_definition_table_is_found_by_column_roles(workspace):
    admin, base = workspace
    response = admin.get(f"{base}/kpi-source-definitions")
    assert response.status_code == 200, response.text
    payload = response.json()

    table = payload["definition_table"]
    assert table is not None, "the company KPI registry was not located"
    assert table["table"] == "kpi_contracts"
    # Located by role, not by name: the roles that matter must be bound to the
    # registry's own column names.
    assert table["role_columns"]["name"] == "kpi_name"
    assert table["role_columns"]["formula"] == "formula"
    assert table["role_columns"]["description"] == "description"
    assert table["role_columns"]["active"] == "is_active"
    assert "column-role scan" in table["detection_method"]

    # No table name is hardcoded anywhere in the platform's detection path.
    from pathlib import Path

    service = Path("app/services/kpi_source_definitions.py").read_text(encoding="utf-8")
    assert "kpi_contracts" not in service
    assert "kpi_semantic_contract" not in service


def test_company_definitions_are_read_and_bound_to_real_columns(workspace):
    admin, base = workspace
    payload = admin.get(f"{base}/kpi-source-definitions").json()
    by_key = {d["kpi_key"]: d for d in payload["definitions"]}

    # A qualified formula binds directly.
    revenue = by_key["revenue"]
    assert revenue["resolution_status"] == "RESOLVED"
    assert revenue["formula_expression"] == "SUM(orders.order_value)"
    assert revenue["source_table"] == "orders"
    assert revenue["time_field"] == "order_date"
    # The company's own wording is preserved, not regenerated.
    assert revenue["business_definition"] == "Total recognised sales revenue across all orders."
    assert revenue["source_formula"] == "SUM(orders.order_value)"
    assert revenue["owner"] == "Finance"

    # An *unqualified* column resolves against the declared source table.
    orders = by_key["orders"]
    assert orders["resolution_status"] == "RESOLVED"
    assert orders["formula_expression"] == "COUNT(DISTINCT orders.order_id)"

    # A ratio is recognised as one and flagged non-additive.
    aov = by_key["average_order_value"]
    assert aov["kind"] == "RATIO"
    assert aov["resolution_status"] == "RESOLVED"
    assert any("not additive" in issue for issue in aov["issues"])

    # The declared grain is honoured rather than defaulted.
    assert by_key["gross_margin_percent"]["time_grain"] == "MONTH"

    # A row inactive in the source stays listed and is marked inactive.
    legacy = by_key["legacy_basket_size"]
    assert legacy["is_active"] is False

    # A formula naming a column that does not exist is reported, not dropped.
    margin = by_key["gross_margin_percent"]
    assert margin["resolution_status"] == "NEEDS_MAPPING"
    assert margin["importable"] is False
    assert any("gross_margin" in issue for issue in margin["issues"]), margin["issues"]

    # A formula outside the governed grammar is reported with a pointed reason.
    repeat = by_key["repeat_purchase_rate"]
    assert repeat["resolution_status"] == "NEEDS_MAPPING"
    assert repeat["issues"], "an unparseable company formula must say why"

    counts = payload["counts"]
    assert counts["total"] == 7
    assert counts["active"] == 6
    assert counts["resolved"] == 5
    assert counts["needs_mapping"] == 2
    assert counts["registered"] == 0
    assert counts["importable"] == 5


def test_import_creates_governed_contracts_and_is_idempotent(workspace):
    admin, base = workspace

    first = admin.post(f"{base}/kpi-source-definitions/import", json={})
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["counts"]["imported"] == 5
    # The two unbound definitions are skipped with a reason, not failed silently.
    assert result["counts"]["skipped"] == 2
    assert all(item["reason"] for item in result["skipped"])

    # Nothing jumps to ACTIVE: the company's meaning still has to be proven
    # against the data before anyone relies on the number.
    for definition in result["imported"]:
        assert definition["status"] == "PROPOSED"

    registry = admin.get(f"{base}/kpis").json()
    assert len(registry) == 5
    origins = {v["proposal_origin"] for kpi in registry for v in kpi["versions"]}
    assert origins == {"COMPANY"}

    # Re-importing skips rather than raising, so the button stays usable.
    second = admin.post(f"{base}/kpi-source-definitions/import", json={})
    assert second.status_code == 201, second.text
    assert second.json()["counts"]["imported"] == 0
    assert second.json()["counts"]["skipped"] == 7
    assert len(admin.get(f"{base}/kpis").json()) == 5

    # The listing now reflects what is already governed.
    counts = admin.get(f"{base}/kpi-source-definitions").json()["counts"]
    assert counts["registered"] == 5
    assert counts["importable"] == 0


def test_imported_definition_validates_and_can_be_approved(workspace):
    admin, base = workspace
    imported = admin.post(
        f"{base}/kpi-source-definitions/import", json={"kpi_keys": ["revenue"]}
    )
    assert imported.status_code == 201, imported.text
    kpi = imported.json()["imported"][0]
    version_id = kpi["versions"][0]["id"]

    report = admin.post(f"{base}/kpi-versions/{version_id}/validate")
    assert report.status_code == 200, report.text
    validation = report.json()

    # The deterministic suite -- the company definition is the intent; this is the
    # proof it actually works against the connected data.
    assert validation["overall_status"] in {"PASS", "WARN"}, validation
    assert validation["ready_for_approval"] is True
    checks = {check["test_type"]: check for check in validation["checks"]}
    assert len(checks) == 9, sorted(checks)
    for required in ("FORMULA_PARSES", "COLUMNS_EXIST", "TIME_FIELD_VALID"):
        assert checks[required]["status"] == "PASS", checks[required]

    # Reconciliation actually executed the KPI and shows the SQL it generated.
    reconciles = next(
        check for key, check in checks.items() if "RECONCIL" in key or "EXECUT" in key
    )
    assert reconciles["evidence"], reconciles

    approved = admin.post(
        f"{base}/kpi-versions/{version_id}/approve",
        json={"reason": "Matches the company KPI registry."},
    )
    assert approved.status_code == 200, approved.text

    live = next(k for k in admin.get(f"{base}/kpis").json() if k["id"] == kpi["id"])
    assert live["status"] == "ACTIVE"

    # The import and the approval are both in the audit trail.
    trail = admin.get(f"{base}/audit").json()
    entries = trail["entries"] if isinstance(trail, dict) else trail
    actions = [entry["action"] for entry in entries]
    assert "kpi.imported_from_source" in actions
    assert "kpi.approved" in actions


def test_unknown_definition_key_is_rejected(workspace):
    admin, base = workspace
    response = admin.post(
        f"{base}/kpi-source-definitions/import", json={"kpi_keys": ["not_a_real_kpi"]}
    )
    assert response.status_code == 404
    assert "not_a_real_kpi" in response.text


def test_company_definitions_are_tenant_scoped(client, workspace):
    """Another company's admin cannot read this company's KPI definitions."""
    _admin, base = workspace
    intruder = register(client, "intruder@othercorp-hq.com", "Other-Company-2026", "Ivy Intruder")
    other = intruder.post(f"{API}/companies", json={"company_name": "Other Co"})
    assert other.status_code == 201, other.text
    intruder = login(client, "intruder@othercorp-hq.com", "Other-Company-2026", other.json()["id"])

    assert intruder.get(f"{base}/kpi-source-definitions").status_code == 403
    assert intruder.post(f"{base}/kpi-source-definitions/import", json={}).status_code == 403
