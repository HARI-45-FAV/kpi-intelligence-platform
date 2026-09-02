# BusinessIntelligence.ai — KPI Intelligence-to-Action Engine

**A KPI moved. Where did it move? How much of it can we account for? What should
someone actually do on Monday — and who?**

This platform answers that chain end to end, and every number in it is computed by
deterministic code: SQL, robust statistics and governed business rules. The LLM is
optional, off by default, and never the source of a figure.

```
   DETECT              EXPLAIN                LOCATE                  RECOMMEND
   ───────             ───────                ──────                  ─────────
   Is this KPI     →   What does the      →   Which part of the   →   What should the
   outside its         evidence say           business accounts       business consider
   comparable          about it?              for the largest         doing next, and
   history?                                   share?                  who owns it?

   robust median       stored findings        deterministic           evidence-derived
   MAD · modified      + confidence          apportionment            levers · owners
   z-score +           calibration           over the KPI's own       impact bands
   business                                  approved dimensions     monitoring plan
   materiality
```

**Nothing in that chain claims a cause.** Contribution measures *where* a movement
sits; every recommendation says so on its own card and is framed as a suggested
action for review, not a finding of fact. That distinction is enforced by tests
that fail if a causal verb or a promised outcome appears anywhere in a payload.

---

## 90-second demo path

```bash
# Terminal 1
cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload

# Terminal 2
cd frontend && npm run dev            # → http://localhost:5173
```

1. **Sign in** → **Monitoring**: verdicts, not a wall of numbers. `ABNORMAL`,
   with actual, expected, deviation and the comparison basis in words.
2. **Open the abnormal result** → the **Explanation** and **Findings** panels read
   the stored evidence back: comparable periods, deviation, confidence and why.
3. **Recommended next actions** (same page) → target area, the business lever to
   review, a specific action, a qualitative impact band, an owner, confidence, and
   what to monitor next. Toggle **Executive / Analyst** to change the depth.
4. **Investigate** → break the movement down by region, drill into a channel; come
   back and the recommendation has **re-aimed itself** at the deeper area, with a
   different owner.
5. **A normal day** → "No corrective action is currently recommended."
   **A sparse KPI** → "Evidence insufficient for targeted action," with the
   collection steps instead of advice. The engine abstains on purpose.
6. **Sign in as a viewer** → the same advice without the entity detail, and
   feedback is refused with a `403`. Entitlement is applied before reading.

To provision that exact demo tenant on a live server in one command:

```bash
cd backend && .venv/Scripts/python scripts/verify_recommendations_live.py \
    --email demo@your-run.example.com
```

It walks all seven scenarios over real HTTP, prints what the panel renders, and
asserts no causal claim and no guaranteed outcome anywhere in the served JSON.

---

## Round 2 objectives → where each one lives

| # | Objective | Implementation |
|---|---|---|
| 1 | Detect and prioritise material movements | Robust median / MAD / modified z-score **paired with** the KPI's own registered materiality, so a threshold follows the KPI's level and never the size of its numbers → [docs/DETECTION.md](docs/DETECTION.md) |
| 2 | Reconcile data and context across sources | Discovery, grain detection, join safety, freshness, cross-source reconciliation, one governed calendar per company. Supabase / PostgreSQL / SQLite / CSV-Excel upload behind one connector interface |
| 3 | Identify and rank explanatory drivers | Deterministic apportionment across approved dimensions with a stated unattributed gap; declared driver relationships per KPI → [docs/INVESTIGATION.md](docs/INVESTIGATION.md) |
| 4 | Persona-specific narratives with traceable evidence | Executive / Analyst views on the recommendation panel; a hard `business_view()` vs `evidence()` split where statistics are returned only to `kpi.read` holders; Copilot answers carry `[E1]`-style citations |
| 5 | Communicate uncertainty and abstain | `LOW_CONFIDENCE` is a first-class verdict, and the action layer answers it with an `EVIDENCE_FIRST` stance: no lever, no owner, no action → [docs/RECOMMENDATIONS.md](docs/RECOMMENDATIONS.md) |
| 6 | Actions grounded in levers, constraints, decision rights | Evidence → lever → action → impact band → owner → confidence → monitoring plan, on every card, derived from stored rows |
| 7 | Learn from analyst and business-user feedback | Per-recommendation usefulness and action status, recorded per reader, structurally unable to move a verdict |
| 8 | Realistic security, cost, latency, scalability | Row / column / domain entitlements, Fernet-sealed credentials, aggregate-only pushdown (a 26-point window is 27 scalar reads, not 27 days of rows), and `execution_logs` carrying latency, model calls, tokens and estimated cost |

**LLM vs non-LLM, stated plainly.** Every actual, expected value, deviation,
z-score, verdict, share, confidence level and recommendation is deterministic
Python and SQL. A model is used for exactly two things, both optional and both
gated: reading a company's handbook into a *proposed* comparison policy that a
human must approve, and explaining governed material in the Copilot. With
`LLM_ENABLED=false` the platform makes **zero model calls** — verifiable at
`GET /api/v1/companies/{id}/telemetry/summary` and by
`cd backend && python verify_no_llm.py`, which boots the app with the provider SDK
poisoned in `sys.modules` so an accidental import fails loudly.

---

## Run it

```bash
# Backend  (Python 3.11+)
cd backend
.venv/Scripts/python -m alembic upgrade head       # schema
.venv/Scripts/python -m app.seed.bootstrap         # roles + permissions
.venv/Scripts/python -m uvicorn app.main:app --reload
# API on http://127.0.0.1:8000 · interactive docs at /docs

# Frontend
cd frontend && npm run dev
# UI on http://localhost:5173 · /api is proxied, so no CORS setup in development
```

First run from empty: create an account → create the company (timezone, currency,
fiscal year — this becomes the governed calendar) → **KPI Setup** (re-enter your
password; the governance area is re-authenticated, not just hidden) → add a source
or upload a spreadsheet → tick the tables in scope and name a time column → run
analysis → register a KPI → validate → approve → approve a comparison policy →
run detection.

## Verified state

```
backend    python -m pytest tests/       →  298 passed, no warnings
frontend   npx tsc -b --noEmit           →  clean
frontend   npx vitest run                →  12 files, 99 tests passed
frontend   npm run build                 →  clean
backend    python verify_no_llm.py       →  48 checks, passes with the provider SDK poisoned
backend    scripts/verify_recommendations_live.py  →  7 scenarios over real HTTP
backend    python verify_ollama_extraction.py      →  all checks, against a real local model
```

The tests worth reading are the ones written to *falsify* a claim rather than
confirm it: `test_detection_generalization.py` provisions two tenants that share no
table, column, time-field type or busiest weekday and shows one engine getting both
exactly right (two of its tests read the algorithm's own source and fail if a
company name, weekday or event literal appears in it); `test_investigation_grain.py`
recomputes apportionment independently in plain Python and compares to `rel=1e-9`;
`test_recommendations.py` scans every served string for causal verbs and guarantees;
and `monitoring-business-view.test.tsx` feeds the frontend the **full** statistical
payload so that its absence from the DOM is a real result and not a thin fixture.

---

## Where the depth is

| Document | What it covers |
|---|---|
| [APPLICATION_WALKTHROUGH.md](APPLICATION_WALKTHROUGH.md) | Everything: plain-language overview, architecture, the full user journey, page-by-page, feature-by-feature, data flows, the wiring map, and a timed demo script |
| [docs/DETECTION.md](docs/DETECTION.md) | The generalized detection engine, its statistics, and the generalization proof |
| [docs/INVESTIGATION.md](docs/INVESTIGATION.md) | Contribution analysis, deterministic apportionment, and the grain problem |
| [docs/RECOMMENDATIONS.md](docs/RECOMMENDATIONS.md) | The evidence-to-action layer: how a card is derived and what it refuses to say |
| [docs/COPILOT.md](docs/COPILOT.md) | The governed, optional, provider-independent AI layer |

## Design rules that hold everywhere

**Entitlement is applied before reading, not after.** Profiling asks
`can_read_column` per column and *skips* the ones the caller may not see, recording
them as withheld with a reason. Nothing sensitive is read and then filtered out of
a response.

**KPI formulas are structured contracts, not free-text SQL.** A strict parser
accepts `AGG(col)`, `AGG(DISTINCT col)` and `A / B`, compiling to a machine-readable
spec. That is what makes validation enforceable, gives lineage that cannot drift
from the calculation, and leaves no injection surface.

**Bad data is reported, never repaired.** A stale source stays recorded as stale; a
malformed uploaded row is skipped and counted, not padded; a measure column with
any nulls gets a note even at `GOOD` status, because those rows silently shrink a
`SUM`. When a breakdown cannot account for all of a movement, the gap is stated.

**Two databases, kept physically apart.** `DATABASE_URL` is platform metadata
(companies, contracts, catalog, audit). Tenant business data is never configured
there — it is registered at runtime and reached only through a connector.

**Reads only.** `execute_query` accepts exactly one `SELECT` / `WITH`. Identifiers
are validated against a strict pattern and dialect-quoted, since they cannot be
bound as parameters.

## Configuration

Environment-driven; `backend/.env` is read if present. Defaults run the whole
platform with no model and no cloud account.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/platform.db` | Platform **metadata** only. Postgres via `postgresql+psycopg://…` |
| `SECRET_KEY` | dev value | Signs access tokens. Rotatable on demand — the cost is one round of sign-ins |
| `CREDENTIAL_ENCRYPTION_KEY` | unset → `SECRET_KEY` | Seals stored source credentials. Set it and stored credentials are re-sealed under it once, at boot, and the count is logged. Back it up separately |
| `ENVIRONMENT` | `development` | Anything else refuses to boot on the shipped `SECRET_KEY` and stops serving `/docs` |
| `LLM_ENABLED` | `false` | Master switch for the optional Copilot. Everything else works without it |
| `CONNECTOR_MAX_ROWS_RETURNED` | `5000` | Hard cap on any connector read |
| `CONNECTOR_QUERY_TIMEOUT_SECONDS` | `30` | |
| `ACCESS_TOKEN_TTL_MINUTES` | `720` | |
| `CORS_ORIGINS` | `localhost:5173` | Comma-separated |

Full list, including the seventeen `LLM_*` / `COPILOT_*` settings:
[backend/.env.example](backend/.env.example) and [docs/COPILOT.md](docs/COPILOT.md).

## Stack

FastAPI · SQLAlchemy 2.0 · Alembic · 40 tables · 7 migrations · 119 endpoints · pytest
React 18 · TypeScript · Vite · Tailwind · Vitest · no component library
Optional model layer, two transports: any OpenAI-compatible endpoint (vLLM, Ollama,
Groq, OpenAI) or Gemini — one module and one branch per additional provider
