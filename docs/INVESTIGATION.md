# The Investigation Center

A KPI moved. **Which part of the business accounts for it?**

That question is not anomaly detection, and this document is careful about the
difference throughout:

> **KPI anomaly detection is continuous.** Every registered KPI, every day, a
> stored verdict.
> **Entity anomaly detection is on-demand and selective.** One named entity, once,
> because a person asked for it.
> **Contribution analysis identifies where the movement occurred.** It does not
> claim causality, and a share is never a verdict.

Nothing in this feature changes a KPI's number, a KPI's status, a bucket
configuration, a summary mail or a dashboard card. See §8.

Its output does, however, get read: a stored breakdown is what lets
[the recommendation layer](RECOMMENDATIONS.md) name a **target area** and an owning
role instead of only a KPI. That direction is one-way — §8 has the details — and
naming an area is still not naming a cause.

---

## 1. Architecture

```
        THE MOVEMENT ALREADY MEASURED              WHAT THE KPI MAY BE SPLIT BY
        detection_runs row for (KPI, date)         approved KpiDimension rows,
        actual · expected · deviation · status     else investigation_map (demo)
                    │                                        │
                    └────────────────┬───────────────────────┘
                                     ▼
                      ┌──────── THE RUN GATE ────────┐        ← §2
                      │ no stored run for that date  │
                      │      → 409, no query runs    │
                      └──────────────┬───────────────┘
                                     ▼
                          resolve_binding()                    ← detection.py, UNCHANGED
                          table · formula · time field · scope
                                     ▼
                    kpi_breakdown.execute_grouped()            ← §3
                    same table?  → execute_kpi_any() verbatim
                    finer table? → deterministic apportionment
                                     ▼
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
      RANK THE PARTS                              ONE NAMED ENTITY          ← §5
      contribution.build()                        contribution.classify_entity()
      share % · direction · Top-K                 detection.detect(read=…)
      no status, ever                             the KPI's own verdict, at entity grain
              │                                             │
              └──────────────────────┬──────────────────────┘
                                     ▼
                      business_view()   |   evidence()
                      what a reader sees | kpi.read holders only
```

Files:

| Concern | Where |
|---|---|
| Routes, gate, audit | [investigation.py](../backend/app/api/v1/investigation.py) |
| Ranking, entity profile, entity verdict | [contribution.py](../backend/app/services/contribution.py) |
| Grain-aware SQL and apportionment | [kpi_breakdown.py](../backend/app/services/kpi_breakdown.py) |
| Demo dimension metadata (deletable) | [investigation_map.py](../backend/app/services/investigation_map.py) |
| The screen | [Investigation.tsx](../frontend/src/pages/Investigation.tsx) |

---

## 2. The run gate

**Selected KPI + selected date → does an agent/KPI run exist?**

```
YES → investigation available, and the movement it splits is that run's movement
NO  → blocked:  "Investigation unavailable — no KPI agent run was recorded for
                 this date. Run the KPI analysis first to investigate this
                 movement."
```

No path is exempt. `GET /investigation/entities` answers `run_available` and reads
no entities when it is false; `POST /investigation/contribution` and `POST
/investigation/analysis` — including the named-entity branch — raise `409` with the
same sentence. The screen asks the gate *before* offering the buttons, so the
common case is a disabled action with a reason rather than an error after a click.

Why so strict: the movement being split is the one the business already saw. A
breakdown of a date the platform never analysed would be a number nobody has
measured, apportioned into parts nobody can reconcile. **No investigation result
is calculated for a date without an official KPI run.**

---

## 3. Grain: the part that is easy to get wrong

The dimensions of a business are not all recorded at the KPI's level of detail.

```
orders                                    order_items
  order_id      ← the KPI's grain           order_id   ← several rows per order
  order_date    ← the time field            product_id
  region        ← a dimension               sector     ← a dimension
  order_value   ← the measure               item_value ← the weight
```

`Revenue = SUM(orders.order_value)` is measured **once per order**. `sector` is
recorded **once per item**. There is no single-table answer.

**The wrong answer is a join and a `SUM`.** Joining an order-level total to a
line-level table repeats that total once per line; summing it multiplies the KPI.
The percentages then add up to something nobody can explain, and the total
disagrees with the dashboard.

**What this platform does: deterministic apportionment.**

```
    part(order, line) = order_value(order) × item_value(line) / total_item_value(order)
```

`total_item_value(order)` is summed over **all** of that order's lines — never a
filtered subset. So:

* for any one order, the parts sum to exactly that order's own measure;
* summed over orders, **the KPI's total is unchanged**.

Three consequences, all deliberate:

1. **A missing weight is a zero, not a guess.** A line with no `item_value`
   receives none of the order's value; its siblings receive all of it.
2. **An order whose lines carry no weight at all cannot be apportioned.** It is
   excluded from the parts rather than divided by zero or spread evenly — and
   because its value is still inside the KPI, it surfaces as movement the
   breakdown does not account for. Reported, not hidden.
3. **Only a plain `SUM` can be apportioned.** `COUNT`, `COUNT(DISTINCT …)` and
   ratios are refused with a business-readable reason: there is no weighting that
   makes a fraction of an order, or a ratio of parts, true. Those KPIs keep the
   dimensions on their own table and are offered nothing else — a smaller answer,
   not a wrong one.

**The KPI number is never adjusted to make a breakdown work.** When the parts
cannot account for all of it, the gap is stated.

Proven, not asserted: [test_investigation_grain.py](../backend/tests/test_investigation_grain.py)
recomputes the apportionment independently in plain Python from raw `sqlite3`
rows and compares to `rel=1e-9`, including `Σ parts == attributable total` and
`Σ parts ≤ KPI actual`. A naive join-and-sum fails it loudly.

When no finer table is involved, `execute_grouped()` delegates to
`execute_kpi_any()` — **the query an investigation runs is the query detection
runs.**

---

## 4. The hierarchy

```
KPI movement  →  Region  →  Sector  →  Product
```

Each dimension declares its own next level, and the drill path is that hierarchy —
not the reader's choice of column. A `Sector` breakdown is filtered to the chosen
region; a `Product` breakdown is filtered through `order_id → orders → region` and
the chosen sector. The breadcrumb (`KPI → Region → Sector`) is how a reader goes
back up, and the next level is only offered when it is actually available, so no
click leads to a dead end.

The manual entry point is separate and flatter — KPI → date → gate → dimension →
optional entity → run — because it exists for the question that does not start
from a movement.

---

## 5. One named entity

When, and only when, a person selects an entity by name:

`contribution.classify_entity()` runs **the existing detection engine** over that
entity's own comparable history. It does this by passing a reader to
`detection.detect(read=…)`; everything after the read — comparable dates,
expected as the robust median, MAD, the modified z-score, the registered business
tolerance, the wording — is the engine's, untouched.

There is **no second classification system**. The result carries:

| | |
|---|---|
| Entity, KPI, date | what was asked about |
| Actual | the same figure the breakdown attributed to it |
| Expected / baseline | the engine's expectation from *this entity's* comparable dates |
| Variance, Variance % | `actual − expected` |
| Direction | `UP` / `DOWN` / `FLAT` |
| Entity status | `NORMAL` / `ABNORMAL` / `LOW_CONFIDENCE` — the KPI's vocabulary |
| Share of the KPI | how much of the day this entity accounts for. A size, not a cause |
| Recent trend | the lookback window, as measured |
| Evidence | comparable dates and queries, to `kpi.read` holders only |

Two safety properties:

* **The tolerance travels unchanged and is conservative at entity scale.** An
  absolute threshold applied to a part, whose deviation is smaller than the whole's,
  fires *less* often than at KPI level, never more. The statistical route is
  scale-free.
* **The verdict belongs to the entity, not the KPI.** It is not persisted as a
  detection run, it does not change the KPI's status, and no dashboard card or
  alert reads it. The audit trail records it, because "who asked, and what were
  they told" is what an audit trail is for.

**Nothing sweeps entities.** No schedule, no background pass, no "detect anomalies
across all regions". An entity is judged because it was named.

---

## 6. Dimensions are declared, not discovered

Only **Region, Sector, Product** are exposed, and the platform never scans
arbitrary columns and calls them dimensions.

The preferred source is governance: approved `KpiDimension` rows on the KPI
version, each with its column, its hierarchy and its own `allowed` switch. When a
KPI has none, [investigation_map.py](../backend/app/services/investigation_map.py)
supplies the demo metadata — in one file, shaped so it can be **deleted rather than
unwound**, and holding exactly:

```
KPI's own table → allowed dimensions → hierarchy → where each one lives
```

`MappedDimension` answers to the same attributes the engine reads off a governed
row, so the engine receives one or the other and branches on neither.

Three things that file must never contain, and does not:

* **A measured value.** Every figure on the surface comes from a query against the
  company's own data.
* **An entity name.** The values a dimension takes are read from the source, per
  KPI, per date — `GET /investigation/entities` ranks the top 10 by SQL. Nothing is
  enumerated in code, on the server or in the browser.
* **A judgement.** No status, no threshold, no expectation.

A dimension is only offered when a drill-down would actually work: the finer table
must be registered on the same data source, and the KPI's measure must be
apportionable (§3).

### Replacing it later

The four-part shape above is the shape the KPI Contract will carry once the
contract, the company's confirmed table metadata and its reference documents drive
it. When approved dimensions exist, `contribution.available_dimensions()` finds
them, this file is never read, and nothing downstream can tell the difference. The
client holds **no** fallback list at all — it asks the server for every KPI, because
which breakdowns exist is governance, not a property of whatever contract the
browser happens to have loaded.

The boundary the RAG layer does not cross: **the LLM explains analytical results;
it is not the numerical source of truth, and it does not name database columns.**

---

## 7. What the screen says, and does not

```
INVESTIGATION CENTER            KPI: Revenue   Date: 2026-08-14
                                Analysis: completed   KPI status: ABNORMAL
                                                        [ Explain the movement ]

KPI MOVEMENT
  Actual        Expected        Variance        Variance %

WHERE DID THE MOVEMENT COME FROM?                    By region · 4 of 4 shown
  North   ████████████████████   58%   +₹1.2L
  South   ███████                21%   +₹0.4L
  ...
  A share is not a verdict.  Click a region to drill down.

SELECTED: North → top sectors …          [Click a sector → investigate products]
```

* Ranked bars, contribution percentages, cards and drill-downs — not large tables.
* **The only status in a breakdown is the KPI's.** No contributor gets a badge, and
  no colour in a ranking means "bad": colour distinguishes moving *with* the KPI
  from moving *against* it. The single exception is §5's named entity, which
  carries the engine's own badge and the engine's own three words.
* Wording is "accounts for" and "associated with". Never "caused".
* **No raw SQL reaches a business reader.** Queries, comparable dates and
  additivity live in an optional *Technical details* area, returned only to holders
  of `kpi.read`.
* Loading, empty, blocked and error states are all explicit, and the arithmetic is
  entirely the server's — a share recomputed in a browser would be a second answer
  to a settled question.

---

## 8. What this feature changed, and what reads it afterwards

**Untouched.** KPI registration · KPI calculation · expected-vs-actual · anomaly
classification · bucket/RAG configuration · authentication · role-based access ·
the dashboard · the post-run summary mail · the existing Copilot.

Two behaviour-preserving changes were made inside detection, both additive:

1. `detect()` gained one optional `read` parameter. Its default is the engine's own
   reader, so a scheduled run is byte-identical; §5 substitutes a reader that
   apportions. This is what makes reusing the one engine possible instead of
   writing a second one.
2. `policy_for()`, `config_payload()` and `UNCONFIGURED_WARNING` moved from the
   detection *API* into the detection *engine*, where the lookup belongs, and the
   API now delegates. One accessor for "which approved comparison policy is in
   force", so a scheduled run and an on-demand analysis cannot drift apart.

The full backend suite passes, which is where the non-regression claim actually
lives — the detection, dashboard and mail tests were not modified.

### What now reads a stored breakdown

A `ContributionRun` is durable evidence, and three other surfaces read it. None of
them can write one, and none can create a verdict:

| Reader | What it does with the breakdown |
|---|---|
| [Recommendations](RECOMMENDATIONS.md) | Turns the top contributor into a **target area** with an owning role and a comparison basis, then aims a business lever and an action at it |
| Copilot | `get_contribution_breakdown` and `list_stored_contribution_analyses` read it under `investigation.read`; the investigation panel also loads it as evidence before the model is asked anything |
| Result detail | Renders the same ranked shares beside the verdict that produced them |

**The relationship with recommendation derivation, precisely.** A recommendation is
computed on every read from whatever is stored at that moment — there is no
`recommendations` table and nothing is generated ahead of time. So the breakdown is
an *input*, not a trigger:

* **No breakdown stored?** The advice is still produced, aimed at the KPI as a
  whole, and it says plainly that no part of the business has been named yet. It
  degrades; it does not fail or fall silent.
* **A breakdown stored?** The highest-contribution part becomes the target area,
  and the `EntityRole` for that dimension supplies the owning role and the
  comparison hint ("against peer regions", "against other channels serving the
  same customers").
* **Drilled a level deeper?** The next read sees the newer, deeper
  `ContributionRun` and the advice **re-aims itself** — new area, new owner, new
  comparison basis. Nothing is regenerated, because nothing was ever stored.

That is the whole reason recommendations are derived rather than persisted: a stored
recommendation written against a region-level breakdown would still be sitting there,
naming a region, after the reader drilled into channel — disagreeing with the
evidence printed directly above it. Deriving on read makes that state
unrepresentable. What *is* persisted is the reader's response to a recommendation,
in `recommendation_feedback`, and no detection or contribution path reads that table.

And the direction of the arrow never reverses: contribution measures **share**.
Naming a target area is not naming a cause, the recommendation cards carry that
sentence in the open, and a test scans the served payload for causal verbs.
