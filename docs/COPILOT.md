# The Governed AI Copilot

An optional retrieval-and-explanation layer over the governed knowledge Sprint 1
already produces. It is **off by default**, **provider-independent**, and cannot
compute, change, or approve anything.

> The Copilot explains the platform. It is not a second, softer path into the data.

---

## 1. Architecture

```
                     Browser
  CopilotProvider ── inherits company · page · KPI · version · date
        │            (context is read from where the user stands, never typed)
        ▼
  POST /api/v1/companies/{company_id}/copilot/chat
        │
        ▼
  require_permissions("analytics.read")        ← app/core/deps.py, UNCHANGED
  JWT → User → CompanyMembership → Role → Permissions → AccessContext
        │        (the path company_id is a claim, already checked here)
        ▼
  build_context()                              ← app/copilot/context.py
  re-resolves every client hint inside access.company.id
        │
        ▼
  retrieve()                                   ← app/copilot/retrieval.py
  assembles a per-request corpus from rows this caller can already read,
  ranks lexically, returns scored passages
        │
        ├── empty?  → deterministic "nothing found" answer, model never called
        │
        ▼
  EvidenceBundle                               ← app/copilot/evidence.py
  every item stamped with the company; a foreign company_id raises ValueError
        │
        ▼
  answer_question()                            ← app/copilot/orchestrator.py
  ├─ system prompt (rules only — no retrieved text, ever)
  ├─ tools = REGISTRY.available_for(access)    ← filtered by this caller's perms
  ├─ bounded loop, ≤ LLM_MAX_TOOL_ITERATIONS
  │    each call: registry re-checks permission, validates args, rejects
  │    forbidden params, runs an existing service, returns structured data
  └─ strip_reasoning() on the way out
        │
        ▼
  build_provider(config)                       ← app/llm/provider.py
  the ONLY provider dispatch in the codebase; NullProvider when unconfigured
        │
        ▼
  LlmUsage → execution_logs                    ← app/core/telemetry.py
  model name, call count, tokens, cost. No prompt. No completion. No key.
```

**Two data domains, unchanged.** The Copilot reads only the platform metadata
database (`DATABASE_URL`). It never opens a connector, never issues a query
against tenant business data, and no business row is copied anywhere. The closest
it gets to data is a stored column *profile* — and `sample_values` are stripped
before a profile leaves a tool, so a passage can state that a column is 4% null
but never what is in it.

**No second store.** There is no vector database, no embedding index, no Copilot
cache. Retrieval builds its corpus per request from the same tables the REST API
reads. That is why cross-tenant retrieval is not a filter that could be
forgotten: there is no shared index to leak from.

---

## 2. Files added

| File | Lines | What it is |
|---|---|---|
| `backend/app/llm/provider.py` | 254 | `LLMProvider` ABC, message/tool/response types, `NullProvider`, `strip_reasoning`, `build_provider` — the single dispatch site |
| `backend/app/llm/config.py` | 171 | `LLMConfig`, `unavailable_reason`, `describe()` (never includes the key), cost estimation |
| `backend/app/llm/openai_compatible.py` | 364 | One transport. Imported lazily, so `httpx` never enters the import graph on a deployment without a model |
| `backend/app/llm/gemini.py` | 489 | The second transport, added the same way: one module behind the same interface, reached by one branch in `build_provider` |
| `backend/app/llm/__init__.py` | 47 | Public surface |
| `backend/app/copilot/context.py` | 352 | `CopilotContext` — the security boundary. Re-resolves every client hint inside the caller's company |
| `backend/app/copilot/retrieval.py` | 873 | Company-scoped lexical retrieval over 14 kinds of governed knowledge |
| `backend/app/copilot/evidence.py` | 478 | `EvidenceBundle`, `EvidenceItem`, `[E1]` citation ids, `placeholder_notice()` |
| `backend/app/copilot/orchestrator.py` | 658 | The bounded turn: retrieve → offer tools → ask → return with evidence |
| `backend/app/copilot/prompts.py` | 482 | System rules, per-panel notes, user prompt assembly, and the deterministic answers used when no model or no evidence exists |
| `backend/app/copilot/schemas.py` | 158 | Request/response contracts. The request cannot carry a company, SQL, a filter, a tool choice, or a prompt override |
| `backend/app/copilot/text.py` | 438 | Document text extraction and chunking |
| `backend/app/copilot/tools/base.py` | 298 | `ToolSpec`, `ToolRegistry`, `FORBIDDEN_PARAMETERS`, argument validation, `refuse()` |
| `backend/app/copilot/tools/kpi_tools.py` | 741 | 7 KPI governance tools |
| `backend/app/copilot/tools/data_tools.py` | 580 | 6 profiling / relationship / source tools |
| `backend/app/copilot/tools/document_tools.py` | 286 | 1 document-passage tool |
| `backend/app/copilot/tools/observability_tools.py` | 284 | 2 telemetry / capability tools |
| `backend/app/copilot/tools/contribution_tools.py` | 326 | 2 stored-breakdown tools, `investigation.read` |
| `backend/app/copilot/tools/__init__.py` | 108 | Registry assembly (18 tools) + `PLANNED_TOOLS` (named, never stubbed) |
| `backend/app/copilot/__init__.py` | 30 | Package doc |
| `backend/app/api/v1/copilot.py` | 186 | `POST …/copilot/chat`, `GET …/copilot/status` |
| `backend/app/services/analysis_views.py` | 297 | Payload shapers extracted from `analysis.py` so tools and screens answer from one code path |
| `backend/tests/test_copilot.py` | 1433 | 34 tests, 44 cases |
| `backend/verify_no_llm.py` | 214 | Out-of-band verification that the platform runs fully with `LLM_ENABLED=false` |
| `frontend/src/copilot/CopilotProvider.tsx` | 214 | Context inheritance — company, page, KPI, version, date |
| `frontend/src/copilot/CopilotPanel.tsx` | 354 | The panel: answer, evidence with citations, tool trail, honest disabled state |

## 3. Files modified

| File | Change |
|---|---|
| `backend/app/core/config.py` | 17 settings (`LLM_*`, `COPILOT_*`). Defaults keep the Copilot off |
| `backend/app/main.py` | Copilot description; `/api/v1/meta` now reports `copilot.enabled/available/model/unavailable_reason` and `vector_embeddings` as absent |
| `backend/app/api/router.py` | Mounts `copilot.router` |
| `backend/app/core/telemetry.py` | Added `LlmUsage` and `llm_usage_of()`. **Also converted `TelemetryMiddleware` from `BaseHTTPMiddleware` to a plain ASGI middleware** — see §13 |
| `backend/app/models/observability.py` | `ExecutionLog` gains `llm_model`, `llm_calls`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd`. **No new migration needed** — these columns already existed in `37f11cf88d4d` |
| `backend/app/api/v1/observability.py` | Telemetry summary reports recorded model usage |
| `backend/app/api/v1/analysis.py` | Payload shapers moved to `analysis_views.py` and imported back under their original names. Behaviour identical |
| `backend/app/api/v1/kpis.py` | Same extraction pattern; no behavioural change |
| `backend/app/services/kpi_validation.py` | Validation-state summarisation reused by `get_kpi_validation_summary` |
| `backend/.env.example` | All 17, documented and defaulted off |
| `frontend/src/api/types.ts` | Copilot request/response/status types |
| `frontend/src/pages/AppShell.tsx` | Wraps routes in `CopilotProvider`, mounts the panel and launcher |
| `frontend/src/pages/Dashboard.tsx` | Publishes KPI/date context; "Ask" affordance on tiles |
| `frontend/src/pages/kpi-setup/KpiRegistryPanel.tsx` | Publishes the selected KPI and version; honest copy about placeholders |

Nothing in `core/deps.py`, `core/security.py`, `connectors/`, `services/kpi_formula.py`,
`services/kpi_governance.py`, or the KPI lifecycle was touched.

---

## 4. Configuring a provider

Set these in `backend/.env`. The Copilot stays off until `LLM_ENABLED=true`.

```ini
LLM_ENABLED=true
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:8000/v1
LLM_API_KEY=EMPTY
LLM_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
```

Any OpenAI-compatible chat-completions endpoint works with no code change:
vLLM, SGLang, llama.cpp's server, Ollama's `/v1`, Together, Groq, OpenAI itself.
Point `LLM_BASE_URL` at it and name the model.

**Adding a genuinely different protocol** (Anthropic Messages, Bedrock, Vertex):

1. Write `app/llm/<name>.py` with a class implementing `LLMProvider.generate`.
2. Add its name to `SUPPORTED_PROVIDERS` in `app/llm/config.py`.
3. Add one branch to `build_provider` in `app/llm/provider.py`.

That is the whole change. `build_provider` is the only place in the codebase that
branches on provider identity — there is no `if model == "Qwen"` anywhere, and a
test asserts it (`test_the_orchestration_layer_never_names_a_model_or_provider`).

### Enabling local Qwen later

Nothing in this codebase needs to change — Qwen is served behind the same
OpenAI-compatible API the transport already speaks.

```bash
pip install vllm
vllm serve Qwen/Qwen3-30B-A3B-Instruct-2507 \
  --host 0.0.0.0 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

Then in `backend/.env`:

```ini
LLM_ENABLED=true
LLM_BASE_URL=http://localhost:8000/v1
LLM_API_KEY=EMPTY
LLM_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
```

Restart the API and check `GET /api/v1/companies/{id}/copilot/status` —
`available` should be `true` and `endpoint_host` should read `localhost:8000`.

Two notes specific to this model family. `--enable-auto-tool-choice` with a tool
parser is required or the governed tools will never be called. And Qwen3 emits
`<think>…</think>` blocks: `strip_reasoning()` removes them before the answer
reaches a response, so hidden reasoning is never returned to a user
(`test_no_hidden_reasoning_reaches_the_response`).

---

## 5. Environment variables

| Variable | Default | Notes |
|---|---|---|
| `LLM_ENABLED` | `false` | The master switch. Everything else works when false |
| `LLM_PROVIDER` | `openai_compatible` | Must be in `SUPPORTED_PROVIDERS` |
| `LLM_BASE_URL` | `http://localhost:8000/v1` | Endpoint root. Only its **host** is ever surfaced |
| `LLM_API_KEY` | `EMPTY` | Never in a response, log, audit entry, or `describe()` |
| `LLM_MODEL` | `Qwen/Qwen3-30B-A3B-Instruct-2507` | Recorded in `execution_logs.llm_model` |
| `LLM_TEMPERATURE` | `0.1` | Low: the task is explaining records, not writing prose |
| `LLM_MAX_OUTPUT_TOKENS` | `1200` | |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `90` | A timeout returns evidence, not a 500 |
| `LLM_MAX_TOOL_ITERATIONS` | `4` | Hard ceiling on the tool loop |
| `LLM_INPUT_COST_PER_1K_USD` | `0.0` | Zero-rated: honest for a self-hosted model |
| `LLM_OUTPUT_COST_PER_1K_USD` | `0.0` | |
| `COPILOT_RETRIEVAL_TOP_K` | `8` | Passages carried into evidence |
| `COPILOT_CHUNK_CHARS` | `1200` | Document chunk target |
| `COPILOT_MAX_DOCUMENT_BYTES_SCANNED` | `2097152` | Per-document read cap |

---

## 6. Governed tools

Eighteen, each one a narrow read over the platform metadata database, gated by the
permission that already governs the same material in the REST API. Every call
re-checks that permission, validates its arguments, and is scoped to
`context.company_id`. **No tool takes a company argument.**

| Tool | Permission | Returns |
|---|---|---|
| `get_active_kpis` | `kpi.read` | Active KPI contracts in this company |
| `get_kpi_definition` | `kpi.read` | Business definition, formula contract, grain, additivity |
| `get_kpi_version` | `kpi.read` | One version's contract and lifecycle status |
| `get_kpi_validation_summary` | `kpi.read` | The recorded result of the nine checks |
| `get_kpi_lineage` | `kpi.read` | Column-level lineage from the parsed formula |
| `get_kpi_dimensions` | `kpi.read` | Declared dimensions, with the "declaring ≠ scheduling" note |
| `get_kpi_drivers` | `kpi.read` | Declared driver relationships |
| `get_contribution_breakdown` | `investigation.read` | A **stored** breakdown: each part's actual, expected, change and signed share, ranked, with how much of the movement the listed parts account for. Computes nothing |
| `list_stored_contribution_analyses` | `investigation.read` | Which breakdowns exist for the KPI in view — date, dimension, drill depth, when run |
| `get_data_source_summary` | `source.read` | Source type, status, connector limitations. **Never credentials** |
| `get_table_profile` | `analytics.read` | Row counts, quality status, grain, freshness |
| `get_column_profile` | `analytics.read` | Null rate, cardinality, type. **`sample_values` stripped** |
| `get_relationship_summary` | `analytics.read` | Detected and declared relationships |
| `get_join_safety_summary` | `analytics.read` | Fan-out, duplicate-key rate, SAFE/RISKY verdict |
| `get_reconciliation_result` | `analytics.read` | Cross-source alignment verdicts |
| `get_document_context` | `document.read` | Passages from documents this caller may read, by version |
| `get_execution_summary` | `telemetry.read` | Aggregate latency, connector and model usage |
| `get_platform_capabilities` | — | What exists and what does not, so the model can decline honestly |

### What is deliberately absent

There is no `execute_sql`, `run_query`, `run_arbitrary_query`,
`database_connection`, or `get_connector_credentials`. This is enforced, not
documented: `ToolRegistry.register` raises at **import time** if a tool declares
any parameter in `FORBIDDEN_PARAMETERS` (`sql`, `query`, `raw_sql`, `company_id`,
`credentials`, `password`, `api_key`, `secret`, …). A tool that would let the
model choose a tenant or supply SQL cannot be registered, so the application
would fail to start rather than ship the hole.

### Extension points

Named in `PLANNED_TOOLS`, and **not implemented** — a stub returning a plausible
forecast or a plausible cause would be exactly the failure this platform exists to
prevent: `get_forecast`, `get_causal_attribution`, `get_alert_history`,
`get_anomaly_feedback_state`.

The Copilot reads this list so it can say *"that will be answerable when
forecasting ships"* instead of inventing an answer. A name leaves the list on the
same commit that lands the real computation, because the system prompt is generated
from it: a stale entry would have the Copilot deny a capability whose result is
sitting in its own evidence.

**`get_recommended_action` has left this list.** [`services/recommendation.py`](../backend/app/services/recommendation.py)
derives a governed next action from a stored result — target area, business lever,
action to review, a qualitative impact band, an owning role and a monitoring window
— and the result screen renders it (see [RECOMMENDATIONS.md](RECOMMENDATIONS.md)).
What the Copilot lacks is a *tool* for it, not the capability, so the honest
position is neither "planned" nor "here is my advice": the `future_action` panel
note points the reader at the recommended actions on the result and forbids the
model composing its own, alongside the standing bans on a claimed cause, a currency
figure and a guaranteed outcome. Registering a read-only tool over the same derived
payload is the obvious next step and needs no new computation.

**Detection and contribution are not on this list either.** A KPI's actual,
expected value, deviation and `NORMAL`/`ABNORMAL`/`LOW_CONFIDENCE` status are
computed and persisted by `services.detection`, and a movement's breakdown by
`services.contribution`. Both reach the Copilot as *evidence* as well as through the
two contribution tools above: `orchestrator._FIGURE_PANELS` loads the stored
`DetectionRun` for the panel's KPI and date before the model is asked anything, and
the investigation panel additionally loads the stored `ContributionRun` when the
caller holds `investigation.read`. When no run exists for the date,
`no_detection_run_notice` says so — the absence is stated, never filled in.

---

## 7. RAG knowledge sources

Governed metadata only. Fourteen passage types, all company-filtered in SQL:

**KPI governance** — contracts, versions, lineage, dimensions, drivers, recorded
validation runs.
**Documents** — `document.read` + `assert_readable`, by version, extracted and
chunked.
**Data understanding** — table profiles (with grain and freshness), column
profiles (sample values stripped), relationships, join-safety verdicts.
**Registry** — data source type, status and connector limitations.
**Catalog** — published semantic catalog snapshots.

Never indexed: tenant business rows, connector credentials, Supabase keys, other
companies' anything.

**Ranking** is IDF-weighted term overlap with prefix matching (floored at 4
characters on both sides, so a nonsense question matches nothing) and a title
boost. No embedding model, no vector store. The corpus is a few hundred short,
proper-noun-dense passages where exact matching on `gross margin` or
`orders.customer_id` beats semantic similarity. Swapping in embeddings later is a
change to `_score` and nothing else.

---

## 8. Company isolation

Five independent layers. Any one of them alone would be sufficient; they are all
present because the cost of the failure is a cross-tenant leak.

1. **Authorisation is unchanged and reused.** `require_permissions("analytics.read")`
   walks JWT → user → membership row → role → permissions against the database.
   The `company_id` in the path is a claim that has already been checked; handlers
   read `access.company.id`, never the path parameter.
2. **Client hints are re-resolved, never trusted.** A KPI id from another company
   goes through `load_scoped` and comes back as `NotFound` — indistinguishable
   from a deleted id, so a neighbouring tenant's existence is never confirmed.
   The context records a note and the answer says the reference was not found.
3. **Retrieval has no cross-tenant query.** Every corpus builder filters on
   `company_id == context.company_id` in the SQL. The corpus is assembled per
   request from rows the caller can already read. There is no shared index.
4. **Evidence is stamped.** `EvidenceBundle.add` raises `ValueError` if an item
   carries a different `company_id`. One line, not a convention.
5. **The model has no vocabulary for a company.** No tool accepts a company
   argument, and the registry refuses to register one that does. A model that
   tries gets `Unknown parameter: company_id` back as data.

The LLM never determines or overrides scope, because scope is resolved before it
is contacted and it has no parameter through which to express one.

---

## 9. API

### `GET /api/v1/companies/{company_id}/copilot/status`

Permission: `analytics.read`. Lets the UI render an honest state on load instead
of discovering it from a failed question.

```json
{
  "enabled": false,
  "available": false,
  "provider": "openai_compatible",
  "model": null,
  "endpoint_host": null,
  "unavailable_reason": "The AI Copilot is disabled on this deployment (LLM_ENABLED=false). All governed retrieval, KPI, validation and connector features continue to work without it.",
  "tools_available": ["get_active_kpis", "…"],
  "knowledge_sources": ["KPI contracts, versions, lineage, dimensions, drivers and validation state", "…"],
  "planned_capabilities": ["get_forecast", "…"]
}
```

`tools_available` and `knowledge_sources` are filtered by the caller's own
permissions — this answers *"what can I ask about"*, not *"what exists"*. No
credential and no endpoint URL appear, only the host.

### `POST /api/v1/companies/{company_id}/copilot/chat`

Permission: `analytics.read`.

```json
{
  "message": "What does Revenue actually measure?",
  "context": {
    "kpi_id": "…", "kpi_version": 3,
    "selected_date": "2026-08-27",
    "agent_run_id": null,
    "page": "dashboard"
  }
}
```

The body cannot carry a company, SQL, a filter, a tool choice, or a system-prompt
override — extra fields are rejected by the schema
(`test_the_request_body_cannot_carry_a_company_or_sql`).

```json
{
  "answer": "Revenue is defined as … [E1], validated on … [E3].",
  "evidence": [
    {
      "id": "E1", "source_type": "kpi_contract",
      "title": "Revenue — version 3 (ACTIVE)",
      "content": "SUM(orders.order_value)",
      "reference": {"kpi_definition_id": "…", "kpi_version": 3},
      "is_placeholder": false
    }
  ],
  "context": {"company_id": "…", "kpi_name": "Revenue", "notes": []},
  "llm_available": true,
  "model": "Qwen/Qwen3-30B-A3B-Instruct-2507",
  "tool_calls": [{"tool": "get_kpi_definition", "ok": true, "error": null}],
  "caveats": ["…"],
  "iterations": 2,
  "usage": {"calls": 2, "prompt_tokens": 112, "completion_tokens": 37},
  "unavailable_reason": null,
  "truncated": false
}
```

**Always 200 when the request is valid.** A deployment with `LLM_ENABLED=false`
is working as designed, so it returns the retrieved evidence with
`llm_available: false` and `unavailable_reason` set. Turning a supported
configuration into a 4xx would push the frontend into treating a normal state as
a fault. A model endpoint that is down or times out behaves the same way: the
evidence still comes back (`test_a_failing_model_endpoint_still_returns_governed_evidence`).

---

## 10. Missing-measurement honesty

Every figure on the surface is now a real measurement: the dashboard, stage
performance, detection detail and investigation all render values the deterministic
engines computed and persisted. Nothing is generated in the browser.

What remains is the harder honesty problem — a question about a figure that was
*never computed*. Detection runs when an agent run evaluates a KPI, and contribution
analysis runs when someone asks for a breakdown, so "no result exists for that date"
is a normal state rather than a fault, and the model must say it instead of reaching
for the nearest thing that looks like a number.

Two mechanisms bring the measurement into the turn:

* **Panel** — a question from `stage_performance`, `detection_detail`,
  `historical_run`, `investigation` or `future_action` fetches the stored
  `DetectionRun` for that KPI and date *without waiting for a trigger word*, because
  a figure is the whole point of those panels even when the question is "explain
  this". The definition and setup panels are deliberately excluded: attaching a
  detection run to *"what does this KPI mean"* would disclose an absence to an
  answer that never needed a number.
* **Trigger word** — a question from anywhere else containing any of 25 words
  (`value`, `why`, `drop`, `anomaly`, `expected`, `baseline`, `forecast`,
  `deviation`, `higher`, …) does the same lookup.

When the lookup finds nothing, `no_detection_run_notice` or
`no_contribution_analysis_notice` enters the bundle with `is_placeholder=True`. That
flag is not decoration: it marks an item as **not a measurement**, the prompt rules
key off it, and `EvidenceBundle` orders those notices ahead of ranked passages so
they cannot fall off the end of a truncated bundle. The notice is evidence, not a
suffix, so an answer touching a displayed number cannot omit it. Each notice ends
with an explicit instruction — *"Do not state or estimate any figure for this
date"*, *"Do not state, rank or estimate any part's share of this movement"*.

The model is never asked to invent an actual, an expected value, a deviation, a
status, a share, or a forecast. `get_platform_capabilities` exists so it can say
what the platform does not compute rather than approximate it.

---

## 11. Audit and telemetry

**Audit** records `copilot.question_asked` with the page, the KPI and version in
context, the character count of the message, which tools ran, and how many
evidence items were used. **The question text is not stored** — it is user-authored
free text that may quote business figures, and the audit trail is not the place
for it (`test_the_audit_trail_records_the_question_without_its_text`).

**Telemetry** lands on the same `execution_logs` row as the request's latency and
connector accounting: `llm_model`, `llm_calls`, `prompt_tokens`,
`completion_tokens`, `estimated_cost_usd`. `LlmUsage` has no field a prompt,
completion, tool argument, or credential could occupy, so no code path can
persist one through it. When no model runs the columns are **NULL rather than
zero**, which is what makes `llm.calls = 0` a fact read from the database rather
than a claim in a docstring.

---

## 12. Tests

`backend/tests/test_copilot.py` — **34 tests, 44 cases** with parametrisation,
grouped by the invariant each one defends:

- **Honest disabled state (6)** — status reflects reality; reach is
  permission-filtered; planned tools are named but absent; chat answers from
  evidence with no model; no model usage is recorded; `build_provider` returns
  `NullProvider`.
- **Company isolation (9)** — auth required; another company's admin refused;
  body cannot carry a company or SQL; retrieval isolated in both directions;
  foreign KPI id resolves to nothing; a tool call naming another company's KPI is
  refused; the model cannot supply `company_id`; foreign evidence cannot enter a
  bundle.
- **The tool boundary (3)** — no tool exposes SQL, a connection, or a credential;
  a tool declaring a forbidden parameter cannot be registered; the model cannot
  invent a tool.
- **Provider independence (1)** — the orchestration layer never names a model or
  provider.
- **Governed explanation (3)** — KPI definitions and validation state explained
  from the recorded contract; the Copilot cannot change governance.
- **Honesty about limits (4)** — no evidence never reaches the model; placeholder
  disclosure, with and without a date; retrieved text never enters the system
  prompt.
- **Robustness (3)** — the tool loop is bounded; a failing endpoint still returns
  evidence; an unavailable model mid-turn is reported, not raised.
- **Non-disclosure (5)** — usage recorded on the execution log; audit without
  question text; the API key never leaves configuration; no hidden reasoning
  reaches the response.

### Results

```
backend:   260 passed, no warnings  (~2 min)
           ├──  44  copilot                    (34 tests, parametrised)
           ├──  40  explainability findings
           ├──  28  kpi formula
           ├──  20  source governance
           ├──  19  detection generalization
           ├──  18  upload onboarding
           ├──  17  recommendations
           ├──  15  investigation / contribution
           ├──  15  security keys
           ├──  14  gemini transport
           ├──  11  password policy
           ├──   7  company kpi definitions
           ├──   7  investigation grain
           ├──   3  presentation contracts
           └──   2  golden flow

frontend:  tsc -b --noEmit  clean
           npm run build    clean
           vitest           87 passed (12 files)

verify_no_llm.py:  ALL CHECKS PASSED
```

The deprecation warnings this section used to note — `fastapi.testclient` on
`httpx`, and Alembic on `path_separator` — no longer appear; the suite runs clean.

No pre-existing test was weakened, skipped, or removed. The tenant-isolation
tests are untouched.

### `verify_no_llm.py`

Section 23's "starts with `LLM_ENABLED=false`" and "dashboard works with no LLM"
are claims about a *deployment*, not something the suite's environment can prove.
So this runs in its own process, on its own database, with the provider SDK
poisoned in `sys.modules` — if any import path reached for one, it would fail
rather than quietly succeed on a package installed for other reasons. Every
setting the claim depends on is pinned in `os.environ` rather than inherited from
a developer's `.env`, `LLM_TOOL_CALLING_ENABLED` included: the invariant is that
turning the *model* off leaves the governed tool layer intact, and the one other
setting that also empties that layer would otherwise fail the check for an
unrelated reason.

```bash
cd backend && python verify_no_llm.py
```

It checks that the config resolves unavailable and says why, that `build_provider`
returns the null provider, that the app boots, that **18 dashboard and KPI
endpoints all return 200**, that `/api/v1/meta` reports `llm_calls_made: 0`, that
status and chat degrade honestly, and that the chat request's `execution_logs`
row has `llm_model` and `llm_calls` **NULL**.

---

## 13. A defect found and fixed on the way

`TelemetryMiddleware` was a `BaseHTTPMiddleware`. That base class hands the
response back to its caller while the route's dependency stack is still
unwinding — so the request's own database transaction was still open when the
middleware tried to write the log. A second connection then asked SQLite for a
write lock the request itself still held: five seconds of waiting, then
`database is locked`, then a `except Exception` that swallowed it.

The effect was that **every write request cost ~5.5s and produced no telemetry
row at all**. That is also why §17's model accounting could not have worked:
`POST …/copilot/chat` is a write request, so its `llm_*` columns were never
persisted.

Converting it to a plain ASGI middleware puts the write after teardown, where the
lock is free. The `tenants` fixture went from 56.49s to 0.85s and the suite from
~40 minutes to 45s. `execution_logs` rows now exist for POSTs, `x-request-id` is
still returned, and `/health` is still skipped.

---

## 14. Limitations

**Retrieval is lexical.** A question phrased entirely in synonyms of the recorded
vocabulary may retrieve nothing — and when it retrieves nothing, the Copilot says
so rather than guessing. That is the intended failure direction, but it is a real
limitation.

**Two transports.** `openai_compatible` and `gemini`
([provider.py](../backend/app/llm/provider.py) holds the only dispatch in the
codebase). Anthropic, Bedrock and Vertex are the three-step addition in §4 — one
module, one branch, one settings value, and nothing above the provider interface
changes.

**Tool calling is required for the tool layer.** A model or server without
function-calling support still gets retrieval-grounded answers, but the 18 tools
will never be invoked.

**Documents are parsed by extension and content type.** Text, Markdown, CSV, and
JSON extract cleanly. A scanned PDF has no text layer, and there is no OCR.

**No conversation memory.** Each request is one turn. History is not stored — the
question text is deliberately not persisted, and reconstructing a thread from the
audit trail would defeat that.

**No streaming.** The transport accepts a `stream` flag but the API returns a
complete response. Streaming a partially-cited answer would let a claim appear
before its evidence.

**Cost is zero-rated by default.** Honest for a self-hosted model; set
`LLM_INPUT_COST_PER_1K_USD` / `LLM_OUTPUT_COST_PER_1K_USD` when using a metered
API or `estimated_cost_usd` will read 0.

**Explanation is not attribution.** The Copilot may say a movement is *associated
with* what the company's documents record, or that something *may explain* it. It
may not say what caused it, and no engine behind it computes causality —
contribution analysis measures shares of a movement, which is not a cause. A
question demanding a cause gets that distinction, not a guess.

**Recommendations are readable, not composable.** The platform derives a governed
next action for a stored result, and the Copilot has no tool over it. Asked what to
do, it points the reader at the recommended actions on that result rather than
writing advice of its own — which is the right answer while no tool exists, and the
smallest gap left on this list: the derived payload is already served, so a
read-only tool over it adds no computation. Until then the model stays inside the
same three bans the panel itself observes: no claimed cause, no currency figure, no
guaranteed outcome.

**Not computed anywhere in this version:** forecasts, causal attribution,
threshold-based alert rules and their history, and learned feedback state — reader
feedback is recorded and deliberately moves no threshold, so there is no learned
state to report. The Copilot names these as absent rather than approximating them,
and the list is generated from `PLANNED_TOOLS` so it cannot drift from the code.
