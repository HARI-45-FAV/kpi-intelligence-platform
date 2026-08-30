"""§21 for the company-governance and data-source-foundation stage.

Five things are proved here, in the order the specification asks for them:

1. **Company security.** A frontend-supplied company id buys nothing. Every
   company-scoped route re-derives the caller's entitlement from the database,
   and a permission — not a role name — decides what they may do.
2. **Source registry.** A source belongs to exactly one company, and a source
   type with no driver is registered honestly rather than pretending to connect.
3. **Profiling.** Row counts, null shares, distinct counts, numeric ranges and
   date coverage come from the source by aggregate query, against controlled
   fixture data whose answers are known in advance.
4. **Source health.** HEALTHY, STALE, DEGRADED and UNKNOWN each reached
   deliberately, with no model anywhere in the path.
5. **Governed metadata.** CONFIRMED is reachable only through a review call, and
   a confirmation survives every later automated pass.

Plus a regression guard on the detection engine this stage was told not to touch.

Everything is driven through the same HTTP API the frontend uses, with one
deliberate exception: the DEGRADED case needs a quality score the clean fixture
cannot produce, so that one measurement is written directly and the *read* path
is then asserted. That is the honest way round — it proves the classification
arithmetic and, at the same time, proves a read projects stored measurements
instead of quietly re-measuring.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import create_app
from app.models.base import (
    DetectionStatus,
    FreshnessStatus,
    MetadataStatus,
    QualityStatus,
    SourceHealthStatus,
)
from app.models.detection import AgentRun, DetectionRun
from app.models.profiling import TableProfile
from app.models.source import DataSource, SourceHealth, SourceTable
from tests.conftest import API, ApiActor, login, register

PASSWORD = "Source-Governance-2026"

# The NovaMart scope this module governs, chosen so that all three freshness
# states occur at once: orders is current, marketing_daily lags three days behind
# a DAILY cadence, and order_items is the fixture's only table with no temporal
# column at all — so its freshness is genuinely unmeasurable rather than merely
# bad. product_master is deliberately left out: it carries launch_date, which the
# time-column resolver would pick up, making it a measured table rather than an
# unmeasurable one.
FULL_SCOPE = {
    "orders": "order_date",
    "order_items": None,
    "marketing_daily": "spend_date",
}


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------
def _company(admin: ApiActor, name: str) -> str:
    created = admin.post(
        f"{API}/companies",
        json={"company_name": name, "currency": "INR", "timezone": "Asia/Kolkata"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _register_source(admin: ApiActor, base: str, path: str, name: str = "NovaMart Commerce") -> str:
    created = admin.post(
        f"{base}/data-sources",
        json={
            "name": name,
            "source_type": "SQLITE",
            "path": path,
            "refresh_frequency": "DAILY",
            "timezone": "Asia/Kolkata",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _discover(admin: ApiActor, base: str, source_id: str) -> dict[str, dict]:
    assert admin.post(f"{base}/data-sources/{source_id}/test").status_code == 200
    assert admin.post(f"{base}/data-sources/{source_id}/discover").status_code == 200
    return {row["table_name"]: row for row in admin.get(f"{base}/tables").json()}


def _set_scope(admin: ApiActor, base: str, tables: dict, scope: dict[str, str | None]) -> None:
    response = admin.put(
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
    assert response.status_code == 200, response.text


def _table_profile(actor: ApiActor, base: str, table_id: str) -> dict:
    """The stored profile for one table, read back over the API.

    Per-column statistics are served by the existing per-table profile route, so
    the assertions below go through the same path the screens use rather than
    reaching into the ORM. The response is access-aware: a column the caller may
    not read arrives with ``profile: None`` and a stated reason.
    """
    response = actor.get(f"{base}/tables/{table_id}/profile")
    assert response.status_code == 200, response.text
    return response.json()


def _column_measurements(actor: ApiActor, base: str, table_id: str) -> dict[str, dict]:
    """Per-column statistics keyed by column name."""
    view = _table_profile(actor, base, table_id)
    return {column["column_name"]: column for column in view["columns"]}


def _instant(value: str) -> datetime:
    """Parse a timestamp from the API into an aware datetime.

    Ordering assertions go through this rather than comparing the strings, because
    string order and instant order are not the same relation: the fractional part
    is omitted when microseconds are zero, and depending on how the serialiser
    spells UTC the separator that follows sorts either side of ``.`` — so
    ``…:00Z`` compares greater than ``…:00.5Z`` while denoting the earlier moment.
    Asserting on the instant also keeps these tests from pinning a JSON spelling
    they have no opinion about.
    """
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f"{value!r} carries no timezone"
    return parsed


@pytest.fixture(scope="module")
def module_client() -> TestClient:
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(scope="module")
def governed(module_client, source_fixture) -> dict:
    """One company with a profiled NovaMart source, plus a member of each role.

    Module-scoped because profiling issues real aggregate queries against the
    fixture database; the claims below are about the governed metadata that
    results, not about how quickly it can be rebuilt.
    """
    admin = register(module_client, "asha@novamart-gov.example.com", PASSWORD, "Asha Admin")
    company_id = _company(admin, "NovaMart Governance")
    base = f"{API}/companies/{company_id}"

    source_id = _register_source(admin, base, source_fixture["path"])
    tables = _discover(admin, base, source_id)
    _set_scope(admin, base, tables, FULL_SCOPE)

    # Grain detection has to run before a grain can be reviewed, and profiling
    # before health can be judged. Both are explicit operations.
    assert admin.post(f"{base}/analysis/run").status_code == 200
    profiled = admin.post(f"{base}/data-sources/{source_id}/profile")
    assert profiled.status_code == 200, profiled.text

    for email, role_key in (
        ("ravi@novamart-gov.example.com", "ANALYST"),
        ("meera@novamart-gov.example.com", "MANAGER"),
    ):
        member = admin.post(
            f"{base}/members",
            json={
                "email": email,
                "full_name": email.split("@")[0].title(),
                "password": PASSWORD,
                "role_key": role_key,
            },
        )
        assert member.status_code == 201, member.text

    return {
        "client": module_client,
        "admin": admin,
        "company_id": company_id,
        "base": base,
        "source_id": source_id,
        "tables": {row["table_name"]: row for row in admin.get(f"{base}/tables").json()},
        "profile": profiled.json(),
        "fixture": source_fixture,
    }


# ---------------------------------------------------------------------------
# 1. Company security
# ---------------------------------------------------------------------------
def test_a_frontend_supplied_company_id_grants_nothing(client, source_fixture):
    """The id in the URL is a request, not a claim the backend accepts.

    Three separate ways of asserting the same boundary, because the specification
    names all three: a member of another company, a member of no company, and a
    caller holding a token minted for a company they *are* in, aimed at a company
    they are not.
    """
    alice = register(client, "alice@alpha-gov.example.com", PASSWORD, "Alice Alpha")
    alpha_id = _company(alice, "Alpha Governance")
    alpha_base = f"{API}/companies/{alpha_id}"
    alpha_source = _register_source(alice, alpha_base, source_fixture["path"], "Alpha DB")
    alpha_tables = _discover(alice, alpha_base, alpha_source)
    orders_id = alpha_tables["orders"]["id"]

    bob = register(client, "bob@beta-gov.example.com", PASSWORD, "Bob Beta")
    beta_id = _company(bob, "Beta Governance")

    # A token scoped to Beta, pointed at Alpha. Every governance route refuses,
    # and refuses identically: even confirming Alpha exists would leak.
    beta_token = login(client, "bob@beta-gov.example.com", PASSWORD, beta_id)
    for method, path in (
        ("get", f"{alpha_base}/data-sources"),
        ("get", f"{alpha_base}/data-sources/{alpha_source}"),
        ("get", f"{alpha_base}/data-sources/{alpha_source}/health"),
        ("get", f"{alpha_base}/tables"),
        ("get", f"{alpha_base}/tables/{orders_id}"),
        ("post", f"{alpha_base}/data-sources/{alpha_source}/profile"),
        ("post", f"{alpha_base}/data-sources/{alpha_source}/health"),
        ("patch", f"{alpha_base}/tables/{orders_id}"),
    ):
        response = getattr(beta_token, method)(path, **({} if method == "get" else {"json": {}}))
        assert response.status_code == 403, f"{method.upper()} {path} crossed the tenant boundary"
        assert response.json()["code"] == "tenant_isolation"

    # Nor can Alpha's resources be reached through Bob's *own* authorised
    # company. A foreign id must not resolve, and the refusal is a plain 404 —
    # the same answer a genuinely missing row gives, so probing learns nothing.
    beta_base = f"{API}/companies/{beta_id}"
    assert beta_token.get(f"{beta_base}/data-sources/{alpha_source}").status_code == 404
    assert beta_token.get(f"{beta_base}/tables/{orders_id}").status_code == 404
    assert beta_token.patch(f"{beta_base}/tables/{orders_id}", json={}).status_code == 404

    # A user who is a member of nothing is refused before any resource lookup.
    carol = register(client, "carol@gamma-gov.example.com", PASSWORD, "Carol Gamma")
    assert carol.get(f"{alpha_base}/data-sources").status_code == 403
    assert carol.get(f"{beta_base}/data-sources").status_code == 403

    # And Alpha's own administrator is unaffected by any of it.
    assert alice.get(f"{alpha_base}/data-sources/{alpha_source}").status_code == 200


def test_permissions_decide_not_role_names(governed):
    """A role is a bundle of permissions; the endpoints check the permissions.

    The ANALYST here holds ``source.read`` and ``profiling.run`` but not
    ``source.manage``, and the split in what it may do follows exactly that — not
    the string "ANALYST" appearing anywhere in a handler. The MANAGER holds
    neither write permission, which is why the two diverge on measurement.
    """
    client, base = governed["client"], governed["base"]
    orders_id = governed["tables"]["orders"]["id"]
    source_id = governed["source_id"]

    analyst = login(client, "ravi@novamart-gov.example.com", PASSWORD, governed["company_id"])
    manager = login(client, "meera@novamart-gov.example.com", PASSWORD, governed["company_id"])

    # source.read: both may read the registry and the governed metadata.
    for actor in (analyst, manager):
        assert actor.get(f"{base}/data-sources").status_code == 200
        assert actor.get(f"{base}/data-sources/{source_id}").status_code == 200
        assert actor.get(f"{base}/data-sources/{source_id}/health").status_code == 200
        assert actor.get(f"{base}/tables/{orders_id}").status_code == 200

    # source.manage: neither may write it. Both payloads are valid, so a 403 here
    # cannot be mistaken for a validation failure — and the refusal is a
    # permission one, not a tenant one: they are legitimately in this company.
    for actor in (analyst, manager):
        denied_review = actor.patch(f"{base}/tables/{orders_id}", json={"description": "nope"})
        assert denied_review.status_code == 403, "reviewing metadata needs source.manage"
        assert denied_review.json()["code"] == "permission_denied"

        denied_create = actor.post(
            f"{base}/data-sources",
            json={"name": "Unauthorised", "source_type": "SQLITE", "path": "/tmp/nope.db"},
        )
        assert denied_create.status_code == 403, "registering a source needs source.manage"
        assert denied_create.json()["code"] == "permission_denied"

    # profiling.run is held by the analyst and not by the manager, so the same
    # two callers diverge on the measurement endpoints.
    assert analyst.post(f"{base}/data-sources/{source_id}/health").status_code == 200
    manager_health = manager.post(f"{base}/data-sources/{source_id}/health")
    assert manager_health.status_code == 403
    assert manager_health.json()["code"] == "permission_denied"
    assert manager.post(f"{base}/data-sources/{source_id}/profile").status_code == 403


def test_an_unauthenticated_caller_reaches_nothing(client, governed):
    """No token, no data — checked on the bare client rather than an actor."""
    base = governed["base"]
    for path in (
        f"{base}/data-sources",
        f"{base}/data-sources/{governed['source_id']}",
        f"{base}/tables",
        f"{base}/tables/{governed['tables']['orders']['id']}",
    ):
        assert client.get(path).status_code == 401, f"{path} answered without a token"


# ---------------------------------------------------------------------------
# 2. Source registry
# ---------------------------------------------------------------------------
def test_a_source_belongs_to_one_company_only(client, source_fixture):
    alice = register(client, "alice@alpha-reg.example.com", PASSWORD, "Alice Alpha")
    alpha_id = _company(alice, "Alpha Registry")
    alpha_base = f"{API}/companies/{alpha_id}"
    alpha_source = _register_source(alice, alpha_base, source_fixture["path"], "Alpha Commerce")

    bob = register(client, "bob@beta-reg.example.com", PASSWORD, "Bob Beta")
    beta_id = _company(bob, "Beta Registry")
    beta_base = f"{API}/companies/{beta_id}"
    beta_source = _register_source(bob, beta_base, source_fixture["path"], "Beta Commerce")

    assert alpha_source != beta_source
    assert [row["id"] for row in alice.get(f"{alpha_base}/data-sources").json()] == [alpha_source]
    assert [row["id"] for row in bob.get(f"{beta_base}/data-sources").json()] == [beta_source]

    # Two companies may point at the same physical database without becoming one
    # registry entry: ownership is a platform fact, not a property of the data.
    with SessionLocal() as session:
        owners = {
            row.id: row.company_id
            for row in session.scalars(
                select(DataSource).where(DataSource.id.in_([alpha_source, beta_source]))
            )
        }
    assert owners == {alpha_source: alpha_id, beta_source: beta_id}

    # Discovered tables inherit that ownership, so neither company's catalog can
    # see the other's rows even though the underlying file is shared.
    _discover(alice, alpha_base, alpha_source)
    _discover(bob, beta_base, beta_source)
    alpha_table_ids = {row["id"] for row in alice.get(f"{alpha_base}/tables").json()}
    beta_table_ids = {row["id"] for row in bob.get(f"{beta_base}/tables").json()}
    assert alpha_table_ids and beta_table_ids
    assert alpha_table_ids.isdisjoint(beta_table_ids)


def test_a_driverless_source_type_is_governed_not_faked(client):
    """CSV, FILE and API sources are registered for governance, not connection.

    §11 asks for the wider type list without building connectors for it. The
    honest result is a source that records cadence, coverage and known
    limitations, and refuses connection and discovery with a reason rather than
    returning an empty profile that a caller could read as evidence.
    """
    admin = register(client, "dana@files-gov.example.com", PASSWORD, "Dana Data")
    company_id = _company(admin, "File Governance")
    base = f"{API}/companies/{company_id}"

    catalog = {
        row["source_type"]: row for row in client.get(f"{API}/connectors").json()["connectors"]
    }
    for source_type in ("CSV", "FILE", "API"):
        assert source_type in catalog, f"{source_type} must appear in the connector catalog"
        assert catalog[source_type]["implemented"] is False
        assert catalog[source_type]["supports_profiling"] is False
        assert "connection_reference" in {f["name"] for f in catalog[source_type]["fields"]}

    # A location reference is required: a source nobody can find is not governed.
    unlocatable = admin.post(
        f"{base}/data-sources",
        json={"name": "Nightly finance extract", "source_type": "CSV"},
    )
    assert unlocatable.status_code == 422
    assert "location reference" in unlocatable.json()["message"]

    # And it must not be a credential smuggled into a metadata column. The
    # validator's own words arrive under details[].problem, hence the raw text.
    with_secret = admin.post(
        f"{base}/data-sources",
        json={
            "name": "Nightly finance extract",
            "source_type": "CSV",
            "connection_reference": "s3://finance/extract.csv?password=hunter2",
        },
    )
    assert with_secret.status_code == 422
    assert "must not contain credentials" in with_secret.text

    created = admin.post(
        f"{base}/data-sources",
        json={
            "name": "Nightly finance extract",
            "source_type": "CSV",
            "connection_reference": "s3://novamart-finance/exports/daily/",
            "refresh_frequency": "DAILY",
            "known_limitations": "Hand-uploaded; refunds are excluded before 2025-04.",
        },
    )
    assert created.status_code == 201, created.text
    source = created.json()
    source_id = source["id"]
    assert source["connection_reference"] == "s3://novamart-finance/exports/daily/"
    assert source["known_limitations"].startswith("Hand-uploaded")
    assert source["has_credentials"] is False, "no credential was supplied, and none is invented"
    assert source["health_status"] == SourceHealthStatus.UNKNOWN, "nothing measured yet"
    assert source["health_checked_at"] is None

    # Connecting and discovering refuse, and name the situation so the answer is
    # actionable rather than a bare "unsupported".
    for path in ("test", "discover"):
        refused = admin.post(f"{base}/data-sources/{source_id}/{path}")
        assert refused.status_code == 502, f"{path} should refuse a driverless source"
        assert "governed metadata only" in refused.text

    # Profiling refuses earlier still: with no discovered tables there is nothing
    # in analytical scope, which is the more useful thing to say.
    unprofilable = admin.post(f"{base}/data-sources/{source_id}/profile")
    assert unprofilable.status_code == 422
    assert "approved data scope" in unprofilable.json()["message"]

    # Health answers, because an honest answer exists: nothing is measurable. It
    # must not claim HEALTHY on the strength of having measured nothing.
    health = admin.post(f"{base}/data-sources/{source_id}/health")
    assert health.status_code == 200, health.text
    verdict = health.json()
    assert verdict["status"] == SourceHealthStatus.UNKNOWN
    assert "No tables are in analytical scope" in verdict["reason"]


def test_registry_metadata_persists_across_requests(governed):
    """Source, table and column metadata is stored, not recomputed per response."""
    admin, base = governed["admin"], governed["base"]
    orders = governed["tables"]["orders"]

    detail = admin.get(f"{base}/tables/{orders['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["schema_name"] and body["table_name"] == "orders"
    assert body["qualified_name"].endswith("orders")
    assert body["columns"], "columns must have been registered by discovery"

    names = {column["column_name"] for column in body["columns"]}
    assert names == {"order_id", "order_date", "customer_id", "region", "channel", "order_value"}

    # Every governed field arrives with the status that produced it, which is what
    # lets a screen show a proposal as a proposal.
    statuses = {status.value for status in MetadataStatus}
    assert body["candidates_status"] in statuses
    assert body["grain_status"] in {"PROPOSED", "DECLARED", "CONFIRMED"}
    for column in body["columns"]:
        assert column["role_status"] in statuses
        assert column["effective_role"] == (column["confirmed_role"] or column["candidate_role"])

    # A second identical read returns the same stored answer.
    again = admin.get(f"{base}/tables/{orders['id']}").json()
    assert again["columns"] == body["columns"]
    assert again["grain_status"] == body["grain_status"]
    assert again["row_count"] == body["row_count"]


# ---------------------------------------------------------------------------
# 3. Profiling, against known fixture data
# ---------------------------------------------------------------------------
def test_profiling_measures_the_source_it_was_pointed_at(governed):
    """Row counts, nulls, distincts, ranges and coverage, all from the source.

    The fixture's answers are known in advance, so these assert arithmetic rather
    than shape: ``orders`` holds one row per order across a seeded history, and
    ``order_items.item_value`` was given gaps on purpose.
    """
    profile = governed["profile"]
    fixture = governed["fixture"]
    admin, base = governed["admin"], governed["base"]
    assert profile["profiled_table_count"] == len(FULL_SCOPE)

    by_table = {entry["table"].split(".")[-1]: entry for entry in profile["tables"]}
    assert set(by_table) == set(FULL_SCOPE)

    orders = by_table["orders"]
    assert orders["row_count"] == fixture["row_counts"]["orders"]
    assert orders["row_count"] > 300, "the fixture seeds a real history, not a handful of rows"
    assert orders["profiled_columns"] == 6
    assert orders["withheld_columns"] == 0, "an administrator is entitled to every column"
    assert orders["quality_status"] in {status.value for status in QualityStatus}
    assert 0 < orders["completeness_pct"] <= 100

    measured = _column_measurements(admin, base, governed["tables"]["orders"]["id"])
    assert set(measured) == {
        "order_id",
        "order_date",
        "customer_id",
        "region",
        "channel",
        "order_value",
    }
    assert all(column["readable"] is True for column in measured.values())

    # A primary key: every row present, every value distinct.
    order_id = measured["order_id"]["profile"]
    assert order_id["row_count"] == orders["row_count"]
    assert order_id["null_count"] == 0
    assert order_id["distinct_count"] == orders["row_count"]
    assert order_id["is_unique"] is True
    assert order_id["is_candidate_key"] is True

    # A money column: a numeric range the platform measured rather than assumed.
    # Min and max are stored as text so one profile shape covers numeric, date and
    # categorical columns, hence the explicit float().
    order_value = measured["order_value"]["profile"]
    low, high = float(order_value["min"]), float(order_value["max"])
    assert 0 < low <= order_value["mean"] <= high

    # A date column: the measured window spans the seeded history, so coverage is
    # read off the data instead of defaulting to today.
    order_date = measured["order_date"]["profile"]
    first = date.fromisoformat(order_date["min"][:10])
    last = date.fromisoformat(order_date["max"][:10])
    assert (last - first).days >= fixture["history_days"] - 1
    assert order_date["null_count"] == 0

    # A repeat customer base and a handful of regions, so both sit strictly below
    # the row count: cardinality is measured, not assumed from the column name.
    assert 1 < measured["customer_id"]["profile"]["distinct_count"] < orders["row_count"]
    assert 1 < measured["region"]["profile"]["distinct_count"] < orders["row_count"]

    # Nulls are reported, never repaired — and a 2% gap is recorded without being
    # inflated into a failing grade, because a threshold nobody trusts is worse
    # than no threshold.
    items = _column_measurements(admin, base, governed["tables"]["order_items"]["id"])
    item_value = items["item_value"]["profile"]
    assert item_value["null_count"] > 0
    assert 0 < item_value["null_pct"] < 10
    assert item_value["quality_status"] == QualityStatus.GOOD
    assert items["order_id"]["profile"]["null_count"] == 0

    # The source-level rollup is the intersection of what the tables can support,
    # and it was recorded by the same run.
    health = profile["health"]
    assert health["coverage_start"] and health["coverage_end"]
    assert health["selected_table_count"] == len(FULL_SCOPE)


def test_profiling_is_explicit_and_a_read_never_measures(governed):
    """§18: profiling is an operation. A read replays what it recorded.

    Asserted through the two timestamps: ``measured_at`` says how old the
    underlying evidence is, and a GET must not move it. If a read re-measured,
    the second read would report newer evidence than the first.
    """
    admin, base, source_id = governed["admin"], governed["base"], governed["source_id"]

    first = admin.get(f"{base}/data-sources/{source_id}/health").json()
    second = admin.get(f"{base}/data-sources/{source_id}/health").json()
    assert first["measured_at"] is not None
    assert first["measured_at"] == second["measured_at"], "a read re-measured the source"
    # The verdict itself is replayed per read — cheap arithmetic over stored rows
    # — so checked_at advances while the evidence behind it does not.
    assert _instant(second["checked_at"]) >= _instant(first["checked_at"])

    # The stored rollup on the source is likewise untouched by reads.
    before = admin.get(f"{base}/data-sources/{source_id}").json()
    admin.get(f"{base}/data-sources/{source_id}/health")
    admin.get(f"{base}/tables")
    admin.get(f"{base}/tables/{governed['tables']['orders']['id']}")
    after = admin.get(f"{base}/data-sources/{source_id}").json()
    assert after["health_checked_at"] == before["health_checked_at"]
    assert after["quality_score"] == before["quality_score"]
    assert after["coverage_end"] == before["coverage_end"]

    # An explicit check is what advances it.
    assert admin.post(f"{base}/data-sources/{source_id}/health").status_code == 200
    refreshed = admin.get(f"{base}/data-sources/{source_id}").json()
    assert refreshed["health_checked_at"] is not None
    assert refreshed["health_checked_at"] != before["health_checked_at"]


# ---------------------------------------------------------------------------
# 4. Source health — every status reached deliberately
# ---------------------------------------------------------------------------
def test_health_is_stale_when_a_table_lags_its_declared_cadence(governed):
    """marketing_daily is three days behind a DAILY cadence, so the source is STALE.

    And STALE outranks a quality verdict on purpose: figures computed before the
    lag are themselves out of date, so leading with quality would understate it.
    """
    admin, base, source_id = governed["admin"], governed["base"], governed["source_id"]
    report = admin.post(f"{base}/data-sources/{source_id}/health")
    assert report.status_code == 200, report.text
    body = report.json()

    assert body["status"] == SourceHealthStatus.STALE
    assert body["stale_tables"] >= 1
    assert "behind the declared daily cadence" in body["reason"]

    lines = {line["table"].split(".")[-1]: line for line in body["tables"]}
    marketing = lines["marketing_daily"]
    assert marketing["freshness_status"] == FreshnessStatus.STALE
    assert marketing["time_column"] == "spend_date"
    # Three days of lag against a one-day cadence, and the tolerance is 2x — so
    # this crosses the line by design rather than by a rounding accident.
    assert marketing["lag_seconds"] > 2 * 24 * 60 * 60

    assert lines["orders"]["freshness_status"] == FreshnessStatus.FRESH
    # order_items has no temporal column at all, so it is excluded from the
    # freshness verdict rather than guessed at.
    assert lines["order_items"]["time_column"] is None
    assert lines["order_items"]["freshness_status"] == FreshnessStatus.UNKNOWN
    assert body["unknown_tables"] >= 1

    # Coverage is the intersection across tables, never the union: the window
    # every table in scope can actually support.
    starts = [_instant(line["coverage_start"]) for line in body["tables"] if line["coverage_start"]]
    ends = [_instant(line["coverage_end"]) for line in body["tables"] if line["coverage_end"]]
    assert starts and ends
    assert _instant(body["coverage_start"]) == max(starts)
    assert _instant(body["coverage_end"]) == min(ends)


def test_health_is_healthy_when_every_measured_table_is_current(module_client, source_fixture):
    """Scope only the current table and the verdict follows the measurements."""
    admin = register(module_client, "healthy@novamart-gov.example.com", PASSWORD, "Hana Health")
    company_id = _company(admin, "NovaMart Fresh")
    base = f"{API}/companies/{company_id}"
    source_id = _register_source(admin, base, source_fixture["path"], "Fresh Commerce")
    tables = _discover(admin, base, source_id)
    _set_scope(admin, base, tables, {"orders": "order_date"})

    assert admin.post(f"{base}/data-sources/{source_id}/profile").status_code == 200
    body = admin.post(f"{base}/data-sources/{source_id}/health").json()

    assert body["status"] == SourceHealthStatus.HEALTHY, body["reason"]
    assert body["fresh_tables"] == 1
    assert body["stale_tables"] == 0
    assert "within the declared daily cadence" in body["reason"]
    assert body["quality_score"] is not None
    assert body["coverage_start"] and body["coverage_end"]

    # The verdict is written onto the source, so a list screen needs no recompute.
    stored = admin.get(f"{base}/data-sources/{source_id}").json()
    assert stored["health_status"] == SourceHealthStatus.HEALTHY
    assert stored["health_reason"] == body["reason"]
    assert stored["health_checked_at"] == body["checked_at"]


def test_health_is_unknown_without_a_declared_cadence(module_client, source_fixture):
    """No cadence means lag cannot be judged, and saying so beats saying HEALTHY."""
    admin = register(module_client, "unknown@novamart-gov.example.com", PASSWORD, "Umar Unknown")
    company_id = _company(admin, "NovaMart Uncadenced")
    base = f"{API}/companies/{company_id}"
    source_id = _register_source(admin, base, source_fixture["path"], "Uncadenced Commerce")
    tables = _discover(admin, base, source_id)
    _set_scope(admin, base, tables, {"orders": "order_date"})
    assert admin.post(f"{base}/data-sources/{source_id}/profile").status_code == 200

    cleared = admin.patch(
        f"{base}/data-sources/{source_id}", json={"refresh_frequency": "UNKNOWN"}
    )
    assert cleared.status_code == 200, cleared.text
    # Changing the cadence invalidates a verdict measured against the old one, so
    # the stored status is cleared rather than recomputed inside an edit.
    assert cleared.json()["health_status"] == SourceHealthStatus.UNKNOWN
    assert cleared.json()["health_checked_at"] is None

    body = admin.post(f"{base}/data-sources/{source_id}/health").json()
    assert body["status"] == SourceHealthStatus.UNKNOWN
    assert "cadence is not declared" in body["reason"]

    # UNKNOWN for the other reason: nothing in scope has a measurable time
    # column, so there is no lag to compute at all. order_items is the fixture's
    # only table with no date anywhere in it.
    _set_scope(admin, base, tables, {"order_items": None})
    restored = admin.patch(
        f"{base}/data-sources/{source_id}", json={"refresh_frequency": "DAILY"}
    )
    assert restored.status_code == 200, restored.text
    assert admin.post(f"{base}/data-sources/{source_id}/profile").status_code == 200

    no_axis = admin.post(f"{base}/data-sources/{source_id}/health").json()
    assert no_axis["status"] == SourceHealthStatus.UNKNOWN
    assert "measurable time column" in no_axis["reason"]
    assert no_axis["fresh_tables"] == 0
    assert no_axis["stale_tables"] == 0
    assert no_axis["unknown_tables"] == 1


def test_health_is_degraded_when_current_data_fails_quality(module_client, source_fixture):
    """On time but not trustworthy is its own verdict, and it is not HEALTHY.

    The fixture is clean enough to pass, so the stored quality score is set to a
    controlled value and the *read* path is asserted. That is the honest way
    round: it proves the classification arithmetic and simultaneously proves a
    read projects stored measurements rather than re-measuring.
    """
    admin = register(module_client, "degraded@novamart-gov.example.com", PASSWORD, "Devi Degraded")
    company_id = _company(admin, "NovaMart Degraded")
    base = f"{API}/companies/{company_id}"
    source_id = _register_source(admin, base, source_fixture["path"], "Degraded Commerce")
    tables = _discover(admin, base, source_id)
    _set_scope(admin, base, tables, {"orders": "order_date"})
    assert admin.post(f"{base}/data-sources/{source_id}/profile").status_code == 200
    healthy = admin.post(f"{base}/data-sources/{source_id}/health").json()
    assert healthy["status"] == SourceHealthStatus.HEALTHY, healthy["reason"]

    with SessionLocal() as session:
        table = session.scalar(
            select(SourceTable).where(
                SourceTable.data_source_id == source_id, SourceTable.table_name == "orders"
            )
        )
        stored = session.scalar(
            select(TableProfile).where(TableProfile.source_table_id == table.id)
        )
        assert stored is not None, "profiling should have recorded a table profile"
        stored.quality_score = 41.0
        session.commit()

    body = admin.get(f"{base}/data-sources/{source_id}/health").json()
    assert body["status"] == SourceHealthStatus.DEGRADED, body["reason"]
    assert body["quality_score"] == 41.0
    assert "arriving on time" in body["reason"]
    assert "70" in body["reason"], "the threshold that was applied should be stated"
    # Freshness is untouched — the source is current, just not trustworthy.
    assert body["stale_tables"] == 0
    assert body["fresh_tables"] == 1


def test_health_timestamps_are_utc_aware_whatever_their_provenance(governed):
    """A loaded timestamp and a freshly written one have to be comparable.

    SQLite has no timezone type, so a row read back used to arrive naive while a
    row still sitting in the session stayed aware — and a profile run does both in
    one unit of work, then rolls the two up. That raised "can't compare
    offset-naive and offset-aware datetimes" from whichever comparison happened to
    see both first, which made it look intermittent.

    The rollup arithmetic itself is exercised by every health test above. This one
    pins the two invariants underneath it.
    """
    admin, base, source_id = governed["admin"], governed["base"], governed["source_id"]

    # 1. Every timestamp read back from the platform database is UTC-aware,
    #    because the column type normalises on the way out.
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(SourceHealth).where(SourceHealth.data_source_id == source_id)
            )
        )
    assert rows, "profiling should have recorded freshness observations"
    for row in rows:
        assert row.checked_at.tzinfo is not None
        assert row.checked_at.utcoffset() == timedelta(0)
        for value in (row.coverage_start, row.coverage_end, row.last_refresh_at):
            assert value is None or value.tzinfo is not None

    # 2. The wire format carries the offset too, so a caller comparing two reads
    #    compares like with like instead of one qualified instant against a bare
    #    local-looking string. Which spelling of UTC the serialiser picks is not
    #    this test's business; that the offset is there and is zero is.
    measured = admin.post(f"{base}/data-sources/{source_id}/health").json()
    replayed = admin.get(f"{base}/data-sources/{source_id}/health").json()
    for payload in (measured, replayed):
        assert _instant(payload["checked_at"]).utcoffset() == timedelta(0)
        assert payload["measured_at"] is not None
        assert _instant(payload["measured_at"]).utcoffset() == timedelta(0)
    assert _instant(replayed["measured_at"]) == _instant(measured["measured_at"])


# ---------------------------------------------------------------------------
# 5. Governed metadata: PROPOSED versus CONFIRMED
# ---------------------------------------------------------------------------
def test_only_a_review_reaches_confirmed_and_it_survives_reprofiling(governed):
    """The whole point of confirming something is that a machine cannot undo it."""
    admin, base, source_id = governed["admin"], governed["base"], governed["source_id"]
    orders_id = governed["tables"]["orders"]["id"]

    before = admin.get(f"{base}/tables/{orders_id}").json()
    assert before["candidates_status"] == MetadataStatus.PROPOSED
    assert before["primary_identifier_candidates"], "discovery should propose an identifier"
    assert before["time_field_candidates"], "orders has a date column to propose"
    assert before["grain_status"] != "CONFIRMED", "nothing automated may write CONFIRMED"

    confirmed = admin.patch(
        f"{base}/tables/{orders_id}",
        json={
            "display_name": "Customer orders",
            "description": "One row per placed order.",
            "primary_identifier_candidates": ["order_id"],
            "time_field_candidates": ["order_date"],
            "confirm_candidates": True,
            "confirm_grain": True,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["candidates_status"] == MetadataStatus.CONFIRMED
    assert body["primary_identifier_candidates"] == ["order_id"]
    assert body["time_field_candidates"] == ["order_date"]
    assert body["grain_status"] == "CONFIRMED"
    assert body["confirmed_grain"], "confirming a grain must record what was confirmed"
    assert body["grain_confirmed_by"] and body["grain_confirmed_at"]
    assert body["display_name"] == "Customer orders"

    # Re-profile and re-analyse: the automated passes must leave the decision be.
    assert admin.post(f"{base}/data-sources/{source_id}/profile").status_code == 200
    assert admin.post(f"{base}/analysis/run").status_code == 200
    after = admin.get(f"{base}/tables/{orders_id}").json()
    assert after["candidates_status"] == MetadataStatus.CONFIRMED
    assert after["primary_identifier_candidates"] == ["order_id"]
    assert after["grain_status"] == "CONFIRMED"
    assert after["confirmed_grain"] == body["confirmed_grain"]

    # Withdrawing hands the field back to whatever authority the evidence carries
    # on its own — never silently back to CONFIRMED.
    withdrawn = admin.patch(
        f"{base}/tables/{orders_id}", json={"confirm_candidates": False, "confirm_grain": False}
    ).json()
    assert withdrawn["candidates_status"] == MetadataStatus.PROPOSED
    assert withdrawn["grain_status"] in {"PROPOSED", "DECLARED"}
    assert withdrawn["confirmed_grain"] is None
    assert withdrawn["grain_confirmed_by"] is None


def test_a_candidate_must_name_a_column_that_exists(governed):
    """A governed pointer to a renamed-away column fails far from here, later."""
    admin, base = governed["admin"], governed["base"]
    orders_id = governed["tables"]["orders"]["id"]

    rejected = admin.patch(
        f"{base}/tables/{orders_id}",
        json={"time_field_candidates": ["order_date", "shipped_at"]},
    )
    assert rejected.status_code == 422, rejected.text
    assert "no column 'shipped_at'" in rejected.json()["message"]

    # The rejection is total: nothing from the payload was applied.
    unchanged = admin.get(f"{base}/tables/{orders_id}").json()
    assert "shipped_at" not in unchanged["time_field_candidates"]


def test_column_role_review_is_separate_from_classification(governed):
    """Role says what a column means; classification says who may read it.

    Two endpoints and two payloads, so a meaning correction can never quietly
    widen access.
    """
    admin, base, source_id = governed["admin"], governed["base"], governed["source_id"]
    orders_id = governed["tables"]["orders"]["id"]

    detail = admin.get(f"{base}/tables/{orders_id}").json()
    order_value = next(c for c in detail["columns"] if c["column_name"] == "order_value")
    assert order_value["role_status"] == MetadataStatus.PROPOSED
    assert order_value["confirmed_role"] is None
    original_classification = order_value["classification"]

    reviewed = admin.patch(
        f"{base}/columns/{order_value['id']}/role",
        json={"confirmed_role": "CURRENCY", "description": "Order gross value in INR."},
    )
    assert reviewed.status_code == 200, reviewed.text
    body = reviewed.json()
    assert body["confirmed_role"] == "CURRENCY"
    assert body["role_status"] == MetadataStatus.CONFIRMED
    assert body["effective_role"] == "CURRENCY"
    assert body["description"] == "Order gross value in INR."
    # Sensitivity is untouched by a meaning review.
    assert body["classification"] == original_classification
    assert body["is_pii"] is False

    # A confirmed role survives the next profiling pass.
    assert admin.post(f"{base}/data-sources/{source_id}/profile").status_code == 200
    after = admin.get(f"{base}/tables/{orders_id}").json()
    persisted = next(c for c in after["columns"] if c["column_name"] == "order_value")
    assert persisted["confirmed_role"] == "CURRENCY"
    assert persisted["role_status"] == MetadataStatus.CONFIRMED

    # Clearing hands the column back to the proposer.
    cleared = admin.patch(
        f"{base}/columns/{order_value['id']}/role", json={"clear_confirmed_role": True}
    ).json()
    assert cleared["confirmed_role"] is None
    assert cleared["role_status"] == MetadataStatus.PROPOSED
    assert cleared["effective_role"] == cleared["candidate_role"]

    # The role endpoint refuses a value outside the governed vocabulary rather
    # than storing free text a later engine cannot interpret.
    assert (
        admin.patch(
            f"{base}/columns/{order_value['id']}/role", json={"confirmed_role": "MONEY"}
        ).status_code
        == 422
    )


def test_governance_changes_land_in_the_existing_audit_trail(governed):
    """§8: reuse the audit system, do not start a second one."""
    admin, base = governed["admin"], governed["base"]
    orders_id = governed["tables"]["orders"]["id"]

    assert (
        admin.patch(
            f"{base}/tables/{orders_id}", json={"description": "Audited description."}
        ).status_code
        == 200
    )

    # The whole trail, not the default first page: registration happened at the
    # very start of this module's setup, and everything since would otherwise
    # push it off the end of a newest-first page.
    trail = admin.get(f"{base}/audit", params={"resource_type": "source_table", "limit": 500})
    assert trail.status_code == 200, trail.text
    reviews = trail.json()
    assert any(
        row["resource_id"] == orders_id and "governed metadata" in (row["summary"] or "")
        for row in reviews
    ), "a governed metadata review must be recorded in the existing audit trail"
    assert all(row["resource_type"] == "source_table" for row in reviews)

    # Registration, profiling and the health check are in that same trail rather
    # than in a second, competing one — and each names its actor.
    everything = admin.get(f"{base}/audit", params={"limit": 500}).json()
    assert {"source.created", "profiling.executed", "profiling.freshness_checked"} <= {
        row["action"] for row in everything
    }
    assert all(row["actor_email"] for row in everything)


# ---------------------------------------------------------------------------
# 6. Regression: the detection engine this stage was told not to touch
# ---------------------------------------------------------------------------
def test_the_three_state_classification_is_unchanged():
    """NORMAL / ABNORMAL / LOW_CONFIDENCE, and WATCH stays retired."""
    assert {status.value for status in DetectionStatus} == {
        "NORMAL",
        "ABNORMAL",
        "LOW_CONFIDENCE",
    }
    assert not hasattr(DetectionStatus, "WATCH")


def test_agent_run_persistence_and_linking_are_intact():
    """The AgentRun contract and the DetectionRun → AgentRun link still stand.

    Asserted against the mapped columns rather than a live run, so a regression
    surfaces as the missing field it is instead of as a failure somewhere
    downstream in a detection test.
    """
    columns = set(AgentRun.__table__.columns.keys())
    assert {
        "kpi_count",
        "processed_count",
        "normal_count",
        "abnormal_count",
        "low_confidence_count",
        "error_count",
        "status",
        "started_at",
        "completed_at",
        "duration_ms",
        "executed_by_user_id",
    } <= columns

    link = DetectionRun.__table__.columns["agent_run_id"]
    assert link.nullable is True, "a single-KPI run has no batch, so the link is optional"
    assert {fk.column.table.name for fk in link.foreign_keys} == {"agent_runs"}


def test_this_stage_added_no_new_migration_head():
    """§20: additive migrations, one linear chain, nothing rewritten.

    A second head is how two developers' revisions silently stop being applied
    together, and this stage added one revision on purpose.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_root = Path(__file__).resolve().parent.parent
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"the migration chain has branched: {heads}"
