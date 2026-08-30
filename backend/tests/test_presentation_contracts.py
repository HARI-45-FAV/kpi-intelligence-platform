"""The presentation contracts the refined KPI Setup screens are driven by.

Both screens were simplified by *deriving* a business-level view from the data the
platform already computes, rather than by hiding data or restating it in prose.
That only holds if the derivation lives in one place, so these tests pin the two
API contracts the UI reads:

* the roles endpoint exposes which roles the access model is explained with, and
  the four headline access boundaries -- each derived from the permissions the
  role actually holds, so the concise view cannot drift from what is enforced;
* the relationships endpoint returns decision-level totals over the same
  deterministic join-safety results the detailed table shows, so the summary and
  the per-row verdicts can never disagree.

Neither addition changes authorisation or the analysis itself.
"""

from __future__ import annotations

import pytest

from app.core.permissions import CORE_ROLE_KEYS, PERMISSION_KEYS, ROLES
from tests.conftest import API, ApiActor, register


@pytest.fixture
def company(request, client) -> tuple[ApiActor, str]:
    slug = request.node.name.replace("test_", "")[:30]
    admin = register(client, f"presenter.{slug}@novamart-hq.com", "Presentation-2026", "Pia Admin")
    created = admin.post(f"{API}/companies", json={"company_name": f"Presentation {slug}"})
    assert created.status_code == 201, created.text
    return (admin, f"{API}/companies/{created.json()['id']}")


def test_core_roles_are_the_three_the_access_model_is_explained_with():
    # Administrator / Analyst / Viewer carry the demo. The others remain defined
    # and enforced -- simplifying the screen must not shrink the model.
    assert CORE_ROLE_KEYS == ("ADMIN", "ANALYST", "VIEWER")
    assert {role.key for role in ROLES} == {
        "ADMIN",
        "ANALYST",
        "EXECUTIVE",
        "MANAGER",
        "REGIONAL_MANAGER",
        "VIEWER",
    }
    # Authorisation is unchanged: the full permission catalogue is intact and
    # ADMIN still holds all of it. The count is a tripwire against a permission
    # being dropped while simplifying a screen -- it moves only when a capability
    # is deliberately added. Detection contributed the two keys named below.
    assert {"detection.run", "detection.configure"} <= set(PERMISSION_KEYS)
    assert len(PERMISSION_KEYS) == 25
    admin_role = next(role for role in ROLES if role.key == "ADMIN")
    assert set(admin_role.permissions) == set(PERMISSION_KEYS)


def test_roles_endpoint_derives_access_areas_from_real_permissions(company):
    admin, base = company
    response = admin.get(f"{base}/roles")
    assert response.status_code == 200, response.text
    roles = {role["role_key"]: role for role in response.json()}

    # Every role is still returned, so nothing disappears from the role picker.
    assert set(roles) == {
        "ADMIN",
        "ANALYST",
        "EXECUTIVE",
        "MANAGER",
        "REGIONAL_MANAGER",
        "VIEWER",
    }
    assert {key for key, role in roles.items() if role["is_core"]} == {
        "ADMIN",
        "ANALYST",
        "VIEWER",
    }

    areas = ("workspace_configuration", "kpi_definitions", "sensitive_data", "documents")
    for role in roles.values():
        assert set(role["access_areas"]) == set(areas), role["role_key"]
        assert role["access_summary"], f"{role['role_key']} has no business summary"

    # Administrator reaches everything; Viewer reaches none of the four; Analyst
    # authors KPIs and reads confidential columns but cannot configure the
    # workspace. Each flag is derived, so it matches the permission list beside it.
    assert all(roles["ADMIN"]["access_areas"][area] for area in areas)
    assert not any(roles["VIEWER"]["access_areas"][area] for area in areas)
    assert roles["ANALYST"]["access_areas"]["kpi_definitions"] is True
    assert roles["ANALYST"]["access_areas"]["sensitive_data"] is True
    assert roles["ANALYST"]["access_areas"]["workspace_configuration"] is False
    assert "company.manage" not in roles["ANALYST"]["permissions"]


def test_relationship_summary_agrees_with_the_detailed_results(client, source_fixture):
    admin = register(client, "joins@novamart-hq.com", "Join-Safety-2026", "Jo Analyst")
    created = admin.post(f"{API}/companies", json={"company_name": "Join Safety Co"})
    assert created.status_code == 201, created.text
    base = f"{API}/companies/{created.json()['id']}"

    source = admin.post(
        f"{base}/data-sources",
        json={"name": "Warehouse", "source_type": "SQLITE", "path": source_fixture["path"]},
    )
    assert source.status_code == 201, source.text
    assert admin.post(f"{base}/data-sources/{source.json()['id']}/discover").status_code == 200

    tables = {t["table_name"]: t for t in admin.get(f"{base}/tables").json()}
    scope = ("orders", "order_items", "product_master", "region_targets")
    assert admin.put(
        f"{base}/data-scope",
        json={
            "replace": True,
            "tables": [{"source_table_id": tables[n]["id"], "enabled": True} for n in scope],
        },
    ).status_code == 200
    for name in scope:
        assert admin.post(f"{base}/tables/{tables[name]['id']}/profile").status_code == 200
    assert admin.post(f"{base}/analysis/relationships").status_code == 200

    response = admin.get(f"{base}/analysis/relationships")
    assert response.status_code == 200, response.text
    payload = response.json()
    relationships = payload["relationships"]
    summary = payload["summary"]

    assert relationships, "the fixture is built so relationships are found"
    assert summary["checked"] == len(relationships)
    # The buckets partition the set exactly -- no relationship is counted twice or
    # dropped, which is what lets the business view replace the table safely.
    assert (
        summary["safe"] + summary["needs_attention"] + summary["unsafe"] + summary["unrated"]
        == summary["checked"]
    )

    # "Material" means it can change a KPI number: not rated SAFE, or it drops
    # rows through orphan keys.
    expected_material = {
        rel["id"]
        for rel in relationships
        if (rel.get("join_safety") or {}).get("level") != "SAFE" or rel.get("orphan_count")
    }
    assert set(summary["material_relationship_ids"]) == expected_material
    assert summary["material_count"] == len(expected_material)

    # region_targets holds one row per region per month, so joining orders to it on
    # region alone multiplies rows. That trap must not be summarised away.
    assert summary["material_count"] > 0
    assert summary["safe"] > 0, "the declared product_master FK is a safe join"
