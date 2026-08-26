# BusinessIntelligence.ai — Sprint 1

**Foundation + KPI Governance.** At the end of Sprint 1 the platform answers one
question per company:

> *What does this KPI mean, exactly where does its data come from, who is allowed
> to see it, and can we reliably calculate it?*

Sprint 1 makes **zero LLM calls**. That is verifiable at
`GET /api/v1/companies/{id}/telemetry/summary`.

---

## Running it

Two terminals. Backend first.

### Backend

```bash
cd backend

# The virtual environment already exists. If you need to recreate it:
#   py -3.13 -m venv .venv
#   .venv/Scripts/python -m pip install -r requirements.txt

.venv/Scripts/python -m alembic upgrade head       # create/upgrade schema
.venv/Scripts/python -m app.seed.bootstrap         # seed roles + permissions
.venv/Scripts/python -m uvicorn app.main:app --reload
```

API on <http://127.0.0.1:8000>, interactive docs at `/docs`.

### Frontend

```bash
cd frontend
npm run dev
```

UI on <http://localhost:5173>. `/api` is proxied to the backend, so no CORS
configuration is needed in development.

### Tests

```bash
cd backend
.venv/Scripts/python -m pytest tests/ -v
```

30 tests. The important one is `tests/test_golden_flow.py`, which walks the
entire Sprint 1 journey through the real HTTP API and then proves the tenant
boundary holds.

---

## First run, end to end

1. **Create an account** → you land on company creation.
2. **Create the company** — name, timezone, currency, fiscal year. This becomes
   the governed calendar, which is what gives "monthly revenue" a reproducible
   meaning later.
3. **Click `KPI Setup`** in the top nav and **re-enter your password**. The
   governance area is re-authenticated, not merely hidden behind a route.
4. **Sources → + Add source → Supabase.** Paste your project URL and database
   password, or tick *paste a connection string* and use the URI straight from
   Supabase → Project Settings → Database. Then **Test connection** →
   **Discover tables**.
5. **Data scope** — tick only the tables the platform may analyse, and set a
   primary time column on each time series (`orders.order_date`,
   `marketing_daily.spend_date`). Leave PII tables like `customer_master`
   unticked. **Save.**
6. **Run full analysis** — profiling, quality, grain, relationships, join safety,
   freshness and cross-source reconciliation, all as aggregate SQL pushed into
   Supabase.
7. **Documents** — upload the KPI handbook so a KPI can cite it as its definition
   source.
8. **KPIs → Suggest from data.** Review each candidate, edit the business
   definition if it is not how your business defines it, then **Use candidate**.
9. **Validate** (nine checks, including executing the KPI) → **Approve &
   activate**.
10. **History → Publish catalog version** to freeze an immutable snapshot.

---

## Configuration

Everything is environment-driven; `backend/.env` is read if present.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/platform.db` | The **platform metadata** DB. Point at Postgres with `postgresql+psycopg://…` |
| `SECRET_KEY` | dev value | Signs JWTs **and** derives the key that encrypts data-source credentials. Change it in production; rotating it invalidates stored credentials. |
| `ACCESS_TOKEN_TTL_MINUTES` | `720` | |
| `DOCUMENT_STORAGE_DIR` | `./data/documents` | |
| `CONNECTOR_QUERY_TIMEOUT_SECONDS` | `30` | |
| `CONNECTOR_MAX_ROWS_RETURNED` | `5000` | Hard cap on any connector read. |
| `CORS_ORIGINS` | `localhost:5173` | Comma-separated. |

**Two databases, kept physically apart.** `DATABASE_URL` is *our* metadata
(companies, users, KPI contracts, catalog, audit). Tenant business data is never
configured here — it is registered at runtime through the Data Source Registry
and reached only through a connector. Conflating the two is the standard way a
multi-tenant BI platform leaks across companies.

---

## Layout

```
backend/
  app/
    core/          config, database, security, deps (authz), telemetry, permissions
    models/        33 tables: tenant, source, profiling, document, catalog, kpi, observability
    connectors/    base interface → sql → {supabase, postgres, sqlite} + warehouse stubs
    services/      discovery, profiling, grain, relationships, join_safety, freshness,
                   reconciliation, catalog, documents, kpi_{formula,sql,discovery,
                   validation,governance}, classification, audit
    api/v1/        auth, companies, sources, analysis, documents, catalog, kpis, observability
    seed/          bootstrap (roles + permissions)
  alembic/         schema migrations
  tests/           golden end-to-end flow, tenant isolation, formula parser
frontend/
  src/
    api/           typed client + response shapes
    auth/          session + elevated admin context
    components/    UI primitives, formatting, data hooks
    pages/         Dashboard, Monitoring, Investigation, Insights, Activity
    pages/kpi-setup/  Company, Sources, Documents, KPIs, Security, History
```

---

## Decisions worth knowing

**KPI formulas are structured contracts, not free-text SQL.** A strict parser
accepts only `AGG(col)`, `AGG(DISTINCT col)` and `A / B`, compiling to a
machine-readable spec. This is what makes validation checks 2, 5, 6 and 7
enforceable, gives column-level lineage that cannot drift from the calculation,
and leaves no SQL-injection surface. Free-text SQL would make those checks
impossible.

**Entitlement is applied before reading, not after.** Profiling asks
`can_read_column` for each column and *skips* the ones the caller may not see,
recording them as withheld with a reason. Nothing sensitive is read and then
filtered out of a response. An analyst profiling `customer_master` sees
`email` and `phone` marked withheld with no statistics attached; an
administrator holding `data.read_pii` sees them profiled.

**Declaring a KPI dimension is not scheduling work.** `kpi_dimensions` says
"region is a valid way to slice Revenue". It does *not* mean anomaly detection
runs per region. Monitoring happens at the KPI level; entity analysis stays
selective. Every dimension in the API response carries this note explicitly.

**Non-additivity is stated, not implied.** Each contract carries `is_additive`.
A ratio KPI like AOV is flagged `false` with instructions to recompute from
numerator and denominator at each level. Summing a ratio across periods produces
a plausible, wrong number — the kind of error nothing downstream would catch.

**Bad data is reported, never repaired.** Quality warnings are stored. A stale
source stays recorded as stale. Where a measure column has *any* nulls, a note
is recorded even at GOOD status, because those rows silently shrink a `SUM`.

**Join safety guards the most dangerous BI failure.** Fan-out factor,
duplicate-key rate and observed cardinality are measured per relationship and
classified `SAFE / SAFE_WITH_AGGREGATION / RISKY / UNKNOWN` with actionable
guidance. A correct-looking KPI inflated by a fan-out join is the error this
prevents.

**Reconciliation needs a declared time axis.** Only tables with an
administrator-designated primary time column participate.
`product_master.launch_date` is structurally indistinguishable from
`orders.order_date` to a schema reader, so the judgement is the
administrator's, consistent with explicit scope everywhere else.

**Credentials never leave the server.** Stored Fernet-encrypted under
`SECRET_KEY`, decrypted only inside a connector, absent from every response
schema, and scrubbed before any audit entry is written.

**Reads only.** `execute_query` accepts exactly one `SELECT`/`WITH` statement.
Identifiers are validated against a strict pattern and dialect-quoted, since
they cannot be bound as parameters. A BI platform that can write to a tenant's
production database is a liability.

---

## Sprint 1 scope

**Delivered.** Multi-tenant companies with row/column/domain entitlements ·
data source registry (Supabase / PostgreSQL working, Snowflake / BigQuery
interface-only) · discovery · explicit analytical scope · access-aware profiling
with SQL pushdown · quality · grain detection · relationship detection (declared
and inferred) · join safety · calendar governance · freshness · cross-source
reconciliation · sensitivity classification · versioned document store ·
versioned semantic catalog · governed KPI contracts with nine validation checks,
human approval, versioning, lineage and access policies · append-only audit
trail · runtime telemetry.

**Deliberately absent.** Anomaly detection · forecasting · expected-value
monitoring · contribution analysis · root-cause investigation · causal inference
· document embeddings and RAG · LLM reasoning and narratives · action
recommendations · automated alerts.

Sprint 2 consumes `GET /api/v1/companies/{id}/kpi-contracts` and should never
need to ask what Revenue means.
