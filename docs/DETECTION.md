# The Generalized KPI Detection Engine

One deterministic algorithm, no company inside it. What varies between tenants —
the table, the column, the formula, the time field, the busiest weekday, the
season, the festival — arrives as **governed configuration** at runtime.

> KPI Registration defines **what** and **where**. Company configuration defines
> **when comparable**. The detection engine defines **how to detect**.

---

## 1. Architecture

```
  KPI REGISTRATION (governed, approved)        COMPANY BUCKET CONFIG (governed, approved)
  ├─ company_id                                ├─ same_day_of_week     → which weekdays
  ├─ kpi_id / kpi_version                      ├─ same_week_of_month   → which weeks
  ├─ source table  (orders | sales_…)          ├─ same_month_or_season → which months
  ├─ formula       (SUM(net_revenue) | …)      ├─ business_event       → which dates
  ├─ time field    (order_date | …)            ├─ yoy_period           → optional
  └─ business tolerance (materiality)          └─ lookback / min / max reference points
            │                                            │
            └──────────────────┬─────────────────────────┘
                               ▼
              POST /api/v1/companies/{company_id}/run-detection
                               │
                               ▼
              require_permissions("detection.run")     ← app/core/deps.py, UNCHANGED
              JWT → User → CompanyMembership → Role → Permissions → AccessContext
                               │
                               ▼
              resolve_binding()                        ← app/services/detection.py
              KpiVersion (ACTIVE | APPROVED only) → table + formula spec + time field
                               │
                               ▼
              load_bucket_config_row()                 ← APPROVED rows only
              KPI-scoped override, else company default, else trailing floor
                               │
                               ▼
              detect()                                 ← the algorithm, 7 steps
              ├─ 1. actual        = one pushed-down aggregate on the target date
              ├─ 2. comparable    = which historical dates the config makes comparable
              ├─ 3. references    = one pushed-down aggregate per comparable date
              ├─ 4. year-over-year re-basing (only if stable)
              ├─ 5. expected      = robust median of the comparable values
              ├─ 6. dispersion    = MAD → modified z-score
              └─ 7. classify      = z-score + business tolerance → status
                               │
                 ┌─────────────┴──────────────┐
                 ▼                            ▼
          business_view()                evidence()
          KPI · Actual · Expected        source · SQL binding · buckets ·
          Deviation · Status ·           reference dates · median · MAD ·
          comparison in words            z-score · tolerance · notes
                 │                            │
                 ▼                            ▼
          Monitoring screen              kpi.read holders only
                 │
                 ▼
          persist_run() → detection_runs + audit event
```

**The engine is pointed, never programmed.** `detect()` takes a `KpiBinding` and a
`BucketConfig` as arguments. There is no company table, no column name, no weekday
and no event anywhere in [detection.py](../backend/app/services/detection.py),
[bucket_config.py](../backend/app/services/bucket_config.py) or
[robust_stats.py](../backend/app/services/robust_stats.py) — a property asserted
against the source itself in §13 below, not merely believed.

---

## 2. Files

| File | Lines | What it is |
|---|---|---|
| `backend/app/services/detection.py` | 1013 | `KpiBinding`, `resolve_binding`, `load_bucket_config_row`, `DetectionOutcome`, `detect()`, `persist_run` — the algorithm |
| `backend/app/services/bucket_config.py` | 715 | The five slots, `validate_bucket_config`, `select_comparable_dates`, `describe_buckets` — the calendar reasoning |
| `backend/app/services/robust_stats.py` | 210 | `median`, `median_absolute_deviation`, `dispersion_of`, `modified_z_score`, `parse_z_threshold` — the statistics |
| `backend/app/services/bucket_extraction.py` | 272 | The one place a model is used: prose → bucket JSON → validation → PROPOSED |
| `backend/app/api/v1/detection.py` | 1146 | Run endpoints, run history, overview, bucket-config lifecycle, extraction |
| `backend/app/models/detection.py` | 197 | `CompanyBucketConfig`, `DetectionRun` |
| `backend/alembic/versions/495cfc3af89a_detection_engine.py` | 151 | `company_bucket_configs`, `detection_runs` |
| `frontend/src/pages/Monitoring.tsx` | 490 | The business surface: five figures, nothing else |
| `backend/tests/test_detection_generalization.py` | 822 | Ten tests that try to falsify the generalization claim |
| `backend/tests/fixture_generalization.py` | 282 | Two tenants seeded to exact daily totals |
| `frontend/src/monitoring-business-view.test.tsx` | 398 | Nine tests pinning the business surface, above all what it must **not** show |

---

## 3. Requirement 1 — the algorithm is fixed, the company is configuration

`detect()` has one signature and one code path. Everything a tenant differs by is
a parameter:

```python
def detect(
    connector: SqlConnector,
    binding: KpiBinding,        # source + formula + time field, from registration
    config: BucketConfig,       # when history is comparable, from company config
    target_date: date,
    *,
    materiality: KpiMaterialityRule | None = None,   # the business tolerance
    config_row: CompanyBucketConfig | None = None,   # provenance, for the evidence
) -> DetectionOutcome:
    """Run detection for one KPI on one date. Deterministic, top to bottom."""
```

There is no `company_id` branch, no `if table == …`, no weekday literal in
executable code. `BucketType` — the *slots* — is a closed enum
([models/base.py:265](../backend/app/models/base.py#L265)) precisely so the engine
can reason about precedence; what fills a slot is never in code.

---

## 4. Requirement 2 — KPI Registration is the source of truth

`resolve_binding(session, version)` reads the approved `KpiVersion` and produces:

| From registration | Used as |
|---|---|
| source table (+ schema) | the FROM clause |
| formula spec | the aggregate |
| time / date column | the day bound, typed from the column **profile** |
| unit, currency, direction | presentation and headline wording |
| materiality (relative %, absolute, statistical rule) | the business tolerance in step 7 |
| `min_history_days`, `min_reference_points` | when the engine declines to judge |

Only `DETECTABLE_STATUSES = {ACTIVE, APPROVED}` are detectable. A DRAFT definition
has no agreed meaning, so a number computed from it would be unattributable.

Two registrations the same engine serves with no code change:

```
Company A   Revenue → orders             → SUM(orders.net_revenue)        → order_date        (DATE)
Company B   Revenue → sales_transactions → SUM(sales_transactions.amount) → transaction_date  (TIMESTAMP)
```

---

## 5. Requirements 3 & 4 — actual and historical values

Both go through the same private function, so the actual and every reference are
computed by definitionally identical means:

```python
def _kpi_value(connector, binding, day):
    """One bounded aggregate for one day, pushed down to the source."""
    start, end = binding.window_for(day)
    result = execute_kpi(connector, binding.spec, schema=…, table=…,
                         time_column=binding.time_field, start=start, end=end)
    return result.scalar
```

**No SQL is composed from user text.** `execute_kpi` builds the statement from the
formula *spec* that was validated and approved during KPI registration — the same
path the KPI validation suite already exercises. The engine never sees a string a
user typed.

**No rows enter the platform.** Each day is one aggregate, computed in the source
database. A 26-point reference window is 27 scalar reads, not 27 days of rows;
`evidence.query_count` reports the count for every run.

**The day bound is typed, not guessed.** `binding.window_for(day)` returns a date
pair for a DATE column and an instant pair covering the whole trading day for a
TIMESTAMP column, read from the profiled `semantic_type` — never inferred from the
column's name. Company B's seeded day runs 00:00:01 → 23:59:59 and its total is
only reachable if the last instant is included; that is
`test_a_timestamp_time_field_covers_the_whole_trading_day`.

---

## 6. Requirement 5 — fixed slots, variable values

Five slots, closed. Precedence is part of the algorithm — an event day is unlike
any ordinary day; a weekday pattern is more specific than a week-of-month
pattern; year-over-year is a last resort because it yields the fewest dates:

```python
BUCKET_PRECEDENCE = (
    BucketType.BUSINESS_EVENT,        # events, with the dates they actually fell on
    BucketType.SAME_DAY_OF_WEEK,      # which weekdays this company treats as distinctive
    BucketType.SAME_WEEK_OF_MONTH,    # which weeks of the month
    BucketType.SAME_MONTH_OR_SEASON,  # which months behave alike
    BucketType.YOY_PERIOD,            # optional
)
```

Plus `TRAILING_PERIOD` — not configurable and not a company pattern, but the
documented floor used when a target date matches none of the configured slots, and
named so a result never claims a comparison basis it did not use.

`validate_bucket_config` accepts exactly these five keys plus `lookback_days`,
`min_reference_points`, `max_reference_points` (defaults 365 / 3 / 26) and rejects
every other key. A business event with a name but no dates is *kept* — so the
intent stays visible and reviewable — and reported as unusable rather than quietly
approximated, because no calendar in code can know when a given company observes a
given festival.

Configurations are governed like KPIs: `DRAFT → PROPOSED → APPROVED → ARCHIVED`,
and **the engine reads APPROVED rows only**. Resolution order is: an APPROVED
config scoped to this `kpi_key`, else the company-wide APPROVED default, else the
trailing floor with the note said out loud on the screen.

---

## 7. Requirement 6 — the LLM boundary

```
Company document → LLM → bucket JSON → validate_bucket_config → PROPOSED → human approval → engine
```

The model is asked for a *policy* in a fixed JSON shape: which weekdays are
distinctive, which weeks, which months form a season, which dates an event fell
on. That is reading comprehension over prose, and the one part of this feature a
model is better at than a rule.

The model never supplies an actual value, an expected value, a median, a MAD, a
modified z-score, a deviation, or a verdict — and **the enforcement is the return
type, not a prompt instruction**.
[bucket_extraction.py](../backend/app/services/bucket_extraction.py) returns a
`BucketDraft` whose payload has already passed `validate_bucket_config`, which
knows only the five slots. A model returning `{"expected_value": 10250000}` fails
with *"Unknown bucket configuration key(s)"*. Numbers cannot reach the engine
through that door.

Two further locks:

* **Import graph.** `test_no_model_can_reach_the_arithmetic` parses the three
  algorithm modules and fails if any imports `app.llm`, `app.copilot` or
  `app.services.bucket_extraction`. A single convenience import is how this
  boundary erodes.
* **Governance.** Extraction can only ever write a PROPOSED row.
  `test_an_unapproved_configuration_is_invisible_to_detection` drafts and proposes
  a policy claiming a completely different weekday, then re-runs detection: the
  config key, the expected value and the status are all unchanged.

### Dates the document states without a year

A handbook writes an annual window the way a person says it — *"the promotion runs
15–21 Oct"* — with no year, because the window recurs. Requiring a full
year-month-day rendering therefore discarded the most common way a document states
an event, which made `business_event` unfillable from a real handbook and, because
a dropped date marks the draft for review, held four correctly-read slots out of an
engine that reads APPROVED rows only.

A date now grounds two ways, and the difference is recorded on the draft:

| The document contains | Outcome |
| --- | --- |
| the whole date | kept exactly as stated |
| its day and month only | read as a recurring window: the platform expands it across the years it may compare (5 back, 1 forward) and **discards the model's year** |
| neither | discarded, and the draft is marked for review |

A range is expanded before matching, so *"15–21 Oct"* grounds all seven days and
not just the two that appear as numbers. No weekday, month or event literal is
involved: month spellings come from `calendar`, so every tenant's calendar is read
by the same code. `tests/test_bucket_grounding.py` covers the three outcomes and
asserts no weekday is privileged over another.

### One company-wide policy, plus the measures a document excepts

Documents state a general rule and then name its exceptions — *"orders peak in the
third week, unlike everything else"*. The model may return a `kpi_overrides`
section alongside the five slots, naming a measure **as the document writes it**;
the platform matches that name against the company's registered measures itself
(`kpi_key` or display name, normalised), so a model never names a key and an
unmatched name is reported rather than stored or guessed.

Each override becomes its own configuration row scoped to that `kpi_key`, approved
separately. That needs no engine change: resolution order already prefers a
KPI-scoped APPROVED row over the company-wide one, and every measure the document
does not single out keeps the company-wide policy unchanged.

An override reaches the engine through the same door, so it is put through the same
checks — unknown keys quarantined, event dates grounded in the same document,
search budget forced to the platform's, payload validated against the same
five-slot contract. An override that stated no pattern is *not* stored: that is the
model having noticed a measure rather than having read a policy for it, and storing
it would offer an approver a configuration that cannot select a single comparable
date while taking that measure off the policy currently serving it correctly.
`tests/test_bucket_overrides.py` covers the contract;
`test_a_handbook_that_singles_out_one_measure_scopes_a_policy_to_it` proves the
precedence through the real engine.

---

## 8. Requirement 7 — expected value from comparable dates

Not a trailing window. `_choose_buckets` picks the most specific applicable slot,
then *refines* with the next one only while enough reference points survive —
narrowing to two Fridays in week 3 of December is more precise and less
trustworthy, and the engine prefers trustworthy. `_budget_dates` then takes the
most recent `max_reference_points` dates, and the target date is never among them.

**Year-over-year re-basing** (`_year_over_year`) applies only when it is stable:
at least `YOY_MIN_POINTS_PER_ERA = 3` points on each side of the year boundary and
a factor inside `[0.5, 2.0]`. Outside that band the two eras describe different
businesses — a launch, a migration, a gap in history — rather than growth, and
scaling by it would fabricate an expectation. The expectation still comes from the
most recent year; the prior year is carried as a growth reference.

Expected value is then the **robust median** of the comparable values.

---

## 9. Requirement 8 — classification

```python
score = modified_z_score(actual, values, center=expected)   # 0.6745·(actual−median)/dispersion
z_threshold, z_note = parse_z_threshold(materiality.statistical_rule)   # default 3.5
significant = score is not None and |score| >= z_threshold

# Materiality: a share of THIS KPI's own expected level, never a magnitude.
relative_floor_pct   = tolerance_pct if registered else DEFAULT_RELATIVE_FLOOR_PCT   # 1.0
movement_is_material = |deviation_pct| >= relative_floor_pct   # True when deviation_pct is None

statistically_abnormal = significant and movement_is_material
breached = |deviation_pct| >= tolerance_pct  or  |deviation_abs| >= tolerance_absolute
```

**Why two tests and not one.** A modified z-score alone flags a KPI whose comparable
days happen to be nearly identical: with a tiny MAD, a trivial movement scores
enormously. So significance is paired with materiality, and materiality is expressed
as a *ratio of the KPI's own expected level* — never as an absolute amount. That is
the whole of the scale-awareness property: a KPI moving 22 → 30 and a KPI moving
2.3M → 2.4M are each measured against their own history and their own expected
level, and neither inherits a threshold from how large its numbers are. No universal
magnitude appears anywhere in the engine. `DEFAULT_RELATIVE_FLOOR_PCT = 1.0` is a
percentage, and a KPI that registers its own `relative_threshold_pct` overrides it.

The ladder, in order:

| # | Condition | Status |
|---|---|---|
| 1 | fewer than `min_reference_points` comparable dates | **LOW_CONFIDENCE** |
| 2 | comparable history spans less than the KPI's `min_history_days` | **LOW_CONFIDENCE** |
| 3 | no z-score obtainable **and** no tolerance registered | **LOW_CONFIDENCE** |
| 4 | statistically abnormal **and** (tolerance breached or none stated) | **ABNORMAL** |
| 5 | statistically abnormal **or** tolerance breached | **ABNORMAL** |
| 6 | otherwise | **NORMAL** |

Every rung writes a sentence into `reason`, so a verdict is never a bare label.

**Three statuses, and only three.** `DetectionStatus` has exactly `NORMAL`,
`ABNORMAL` and `LOW_CONFIDENCE`. There is no `WATCH`: an earlier version had one on
rung 5, and it was retired by migration `7c1f2e9a4b6d`, which rewrote stored
`WATCH` rows to `ABNORMAL`. A fourth status that meant "significant, or breached, or
nearly significant" collapsed three different situations into one label nobody could
act on, and it let a breached tolerance be reported as something softer than
abnormal. `test_source_governance.py` asserts the enum has no `WATCH` attribute, so
reintroducing it fails the suite rather than the review.

**The zero-MAD guard.** When every comparable value is identical, dividing by the
MAD would make any difference at all infinitely abnormal. `dispersion_of` falls
back in order: MAD when non-zero → scaled mean absolute deviation (× 1.253314) →
`DispersionBasis.NONE`. With no measurable spread, `modified_z_score` returns
`0.0` when the actual *equals* the repeated value (zero is then a fact, not a
fabrication) and `None` when it differs — never a manufactured score. Rung 3 then
hands the decision to the KPI's business tolerance, and a note discloses that the
result was computed without measurable spread.

`LOW_CONFIDENCE` is a first-class verdict, not an error: the measurement stands,
the verdict does not.

---

## 10. Requirement 9 — the separations, as they exist in code

| Concern | Where it lives | What it may not do |
|---|---|---|
| KPI definition | `KpiDefinition` / `KpiVersion` | know a comparison policy |
| KPI source | `resolve_binding` → `KpiBinding.table` | know a formula's statistics |
| KPI formula | approved formula spec → `execute_kpi` | be built from user text |
| Bucket configuration | `CompanyBucketConfig` → `BucketConfig` | contain a number |
| Detection engine | `detect()` | name a company, table, column, weekday or event |
| Presentation | `business_view()` vs `evidence()` | mix the two |

---

## 11. Requirement 10 — the API

| Method | Path | Permission |
|---|---|---|
| POST | `/companies/{company_id}/run-detection` | `detection.run` |
| POST | `/run-detection` (company in the body) | `detection.run` |
| POST | `/companies/{company_id}/run-detection/batch` | `detection.run` |
| GET | `/companies/{company_id}/detection-runs` | `analytics.read` |
| GET | `/companies/{company_id}/detection-runs/{run_id}` | `analytics.read` |
| GET | `/companies/{company_id}/detection/overview` | `analytics.read` |
| GET | `/companies/{company_id}/bucket-configs[/{id}]` | `kpi.read` |
| POST / PUT | `/companies/{company_id}/bucket-configs[/{id}]` | `detection.configure` |
| POST | `…/bucket-configs/{id}/propose`, `…/preview`, `…/extract` | `detection.configure` |
| POST | `…/bucket-configs/{id}/approve`, `…/archive` | `kpi.approve` |

`POST /run-detection` with `{company_id, kpi_id, target_date}` does exactly the
sequence in the specification: load the approved KPI contract → its source,
formula and time field → the approved bucket configuration → the actual → the
comparable dates → one value per date → the expected median → MAD and modified
z-score → the business tolerance → return the result and persist the run.

`evidence` is returned **only** to callers holding `kpi.read`; `run_id` and
`persisted` say whether the run was stored (persistence is on by default and can
be turned off per request for a what-if). Every call is audited either way.

The flat form exists because the specification names it. Its behaviour and its
enforcement are identical: the body's `company_id` is a *claim*, and
`resolve_access` turns it into an entitlement by looking up the caller's
membership, role and permissions before anything else happens.

---

## 12. Requirement 11 — the business surface

[Monitoring.tsx](../frontend/src/pages/Monitoring.tsx) shows five things and the
comparison in words:

```
  Revenue                                    ABNORMAL
  ₹6.0M
  ACTUAL
  ─────────────────────────────────────────────────
  Expected                                    ₹10.25M
  Deviation                                    -41.5%
  Comparison: Comparable Fridays
```

**Nothing in that file computes.** Every number arrives from
`POST /run-detection/batch` or from the stored run the overview hands back; the
only arithmetic in the file is choosing a colour. The frontend `DetectionResult`
type has no field for evidence at all, so statistics cannot leak in by accident
even when the server sends them.

Deliberate choices worth knowing:

* **Deviation is coloured by verdict, never by sign.** Refunds, cost per order and
  churn all get worse as they grow, and the engine has already weighed direction
  against the KPI's own tolerance. Colouring by sign would contradict that in
  green.
* **The last stored verdict renders before anyone presses anything** — more honest
  than a blank card, and far more honest than a number generated in the browser.
* **A KPI that cannot be evaluated is listed with its governance reason**, not
  silently dropped, with a link into KPI Setup.
* **Batch truncation is said out loud**: one run covers 25 KPIs, and the note tells
  you how many more keep their previous verdict.
* **An unconfigured comparison policy is announced**, because an unstated fallback
  is the kind of thing that gets discovered during an argument.

The nine tests in
[monitoring-business-view.test.tsx](../frontend/src/monitoring-business-view.test.tsx)
enforce this, and one of them enforces the *negative* half directly. The stub
returns the **full** `kpi.read` response — median 10,250,000, MAD 250,000,
modified z −11.4665, `SAME_DAY_OF_WEEK`, `aurora-weekly`, the reference dates and
the generated SQL — so absence from the DOM is a real result rather than an
artefact of a thin fixture. The stub also returns a `deviation_pct` that does not
agree with its own actual and expected, which is the only way to tell "printed the
server's field" apart from "recomputed it locally".

The API side of the same contract is
`test_the_business_view_hides_the_statistics_it_was_computed_from`, which asserts
the exact twelve keys of `result` and that none of `median`, `mad`, `z_score`,
`modified`, `sql`, `SAME_DAY_OF_WEEK` appears anywhere in it.

---

## 13. Requirement 12 — the generalization proof

Two tenants, provisioned end to end through the real API — connect a source,
approve a scope, profile it, register a KPI, validate it, approve it, approve a
comparison policy, run detection — and made to disagree about everything the
engine touches:

| | Company A (Aurora Retail) | Company B (Borealis Foods) |
|---|---|---|
| Table | `orders` | `sales_transactions` |
| Measure | `net_revenue` | `amount` |
| Time field | `order_date` (DATE) | `transaction_date` (TIMESTAMP) |
| Busiest weekday | Friday | Tuesday |
| Tolerance | 8% | 10% |
| Year-over-year | enabled | disabled |
| Target date | 2026-08-28 (Fri) | 2026-08-25 (Tue) |
| Actual | 6,000,000 | 830,000 |
| Expected | 10,250,000 | 825,000 |
| Deviation | −41.4634% | +0.61% |
| **Status** | **ABNORMAL** | **NORMAL** |
| Comparison | Comparable Fridays | Comparable Tuesdays |
| Bucket applied | `SAME_DAY_OF_WEEK` | `SAME_DAY_OF_WEEK` |

The same fixed slot, filled with different company values. Every figure is
asserted **exactly**, and the expected median and MAD are recomputed in the test
with the standard library rather than by calling the engine's own statistics — a
test that asks the engine to check itself proves nothing.

The fourteen tests in
[test_detection_generalization.py](../backend/tests/test_detection_generalization.py):

| Test | What it would catch |
|---|---|
| `test_one_engine_detects_for_two_companies_that_share_no_schema` | any company knowledge in the algorithm — one tenant would be wrong |
| `test_the_reference_set_is_the_configured_weekday_and_never_the_target` | a trailing window: every reference is the target's own weekday, and the target is never compared against itself (B = 26 points, A = 52 with the prior-year era, YoY factor 1.0) |
| `test_multiple_kpis_on_one_date_and_one_policy_reach_different_verdicts` | KPI-specific branching — A's order count is NORMAL on the day its revenue collapses; B's refunds are ABNORMAL (+100%) on the day its revenue looks fine, from a third table and time field |
| `test_a_kpis_threshold_follows_its_own_level_and_never_the_size_of_its_numbers` | a magnitude wired into the engine: revenue in millions and an order count in single digits, same table, same date, same policy, carry **different** floors (8% and 15%) read from their own registrations, `tolerance.absolute is None` on both, and the looser floor belongs to the *smaller* KPI |
| `test_the_same_kpi_history_and_date_reproduce_the_same_verdict` | non-determinism anywhere in the path — sampling, a clock, set or dict ordering reaching the median, the dispersion or bucket precedence. Two runs, identical statistics, identical reference dates, identical bucket signature |
| `test_a_timestamp_time_field_covers_the_whole_trading_day` | a day bound guessed from a column name instead of read from the profile |
| `test_changing_only_the_configuration_changes_the_verdict` | a hard-coded comparison basis: the same six orders are NORMAL against Fridays (expected 6.0) and ABNORMAL against all of August (expected 4.0, +50%) — nothing but the approved policy changed, and the actual is identical because a measurement cannot depend on a policy |
| `test_a_zero_spread_reference_window_is_never_given_a_fabricated_score` | division by a zero MAD: `dispersion_basis: NONE`, z = 0.0 because the actual matches exactly, NORMAL, and a note disclosing it |
| `test_the_business_view_hides_the_statistics_it_was_computed_from` | statistics leaking into the business payload |
| `test_the_algorithm_names_no_company_table_column_weekday_or_event` | a hidden `if company == …`: reads the three modules' source, rejects tenant vocabulary anywhere and weekday/month/event literals in executable code |
| `test_no_model_can_reach_the_arithmetic` | a convenience import of the model layer into the numeric path |
| `test_an_unapproved_configuration_is_invisible_to_detection` | a model's draft reaching the numbers before a human approved it |
| `test_batch_run_persists_agent_aggregate_and_linked_results` | an aggregate that does not add up: `kpi_count`, `processed_count`, `error_count` and the three status counts must reconcile, every result must carry its `agent_run_id`, and the run must be readable back from history |
| `test_kpi_handbook_extraction_persists_and_drives_real_detection` | an extracted policy that is trusted before validation and approval, or one the engine does not actually read afterwards |

Two of these are structural rather than behavioural, deliberately: a
company-specific branch would still pass every behavioural test as long as this
suite's two tenants happened to agree with it.

---

## 14. Verified state

```
backend     python -m pytest tests      →  298 passed, no warnings  (~3 min)
backend     python verify_no_llm.py     →  ALL CHECKS PASSED
frontend    npx tsc -b --noEmit         →  clean
frontend    npx vitest run              →  12 files, 99 passed
frontend    npm run build               →  clean production build
```

The deprecation warnings this section used to note — a `StarletteDeprecationWarning`
from `fastapi.testclient` and an Alembic `path_separator` warning from the
migration-head test — are gone; the suite now runs clean.

---

## 15. Where the engine stops, and what reads on from there

The engine answers one question — *did this number behave normally?* — and stops
there **by construction**, because everything downstream is a separate,
independently governed read over what it stored. That boundary is what lets the
rest of the platform build on a verdict without ever being able to alter it.

| Question | Answered by | Reads |
|---|---|---|
| Did this number behave normally? | this engine | the tenant's own data, once, per date |
| Why was it flagged? | [`services/explanation.py`](../backend/app/services/explanation.py) | the stored run |
| Which part of the business does it sit in? | [Investigation](INVESTIGATION.md) | the stored run, on request |
| What should be reviewed, and by whom? | [Recommendations](RECOMMENDATIONS.md) | the stored run and its newest stored breakdown |
| Who is told, without opening the app? | [`services/run_email.py`](../backend/app/services/run_email.py) | the Agent Run's stored results, plus the membership table for the address list |

Each of those reads the engine's output and never writes back to it: a
recommendation cannot change a verdict, and a breakdown cannot create one — the
run gate returns `409` for a date this engine never measured. So the four lines
below are the engine's own, and they hold whatever is layered above it.

* **It measures; it does not forecast.** Expected value is the median of
  comparable history, not a projection. Nothing here extrapolates. A forecasting
  service would be a new engine with its own stored results, not a setting on this
  one.
* **It locates; it does not claim a cause.** A verdict says a number is outside
  comparable history. Contribution then says which part of the business *accounts
  for* the movement, and the recommendation layer aims advice at that part — with
  the sentence *contribution alone does not establish causation* served on every
  card, and a test that fails the build if a causal verb ever reaches the payload.
* **It borrows no other KPI's history.** A KPI declaring a sparse-history strategy
  is told so in the `reason` field; substituting a peer baseline is a separate,
  explicitly requested analysis and is never applied silently.
* **It abstains rather than guesses.** A named event with no dates is reported
  unusable, an unapproved policy is never trusted, and thin evidence returns
  `LOW_CONFIDENCE` — which the recommendation layer honours by offering
  evidence-collection steps and **no** targeted intervention.

**Natural next layer.** Forecasting, threshold-based alerting and scheduled runs
are the obvious extensions, and the run history and stored evidence they would read
already exist — which is how the explanation, investigation and recommendation
layers were each added without touching the algorithm in §3. A completed run
already mails its own summary, rendered from the same stored rows the screen reads.

### The run summary mail, and who receives it

One completed Agent Run produces one message, and its body is the same chain the
screen shows: **KPI → date → actual vs expected → status → deviation → the largest
contributor → what happened → the recommended next step → the confidence in all of
it.** Every figure is a stored column re-rendered through the explanation service's
own formatters, so the mail cannot round a number differently from the Results page,
and no model produces or adjusts a figure in it — which the message says of itself,
in a footer beside the *contribution is not causation* line.

**No address is written into this codebase.** Recipients are resolved at send time
from `company_users`: the active members of *that* company whose role grants both
`analytics.read` **and** `investigation.read`. Both, not either — one run produces
one body, and that body carries contribution shares whenever the run has a stored
breakdown, so the alternative would be either mailing apportionment to someone not
entitled to read it or composing two versions of one answer. Requiring both keeps
the guarantee simple: *nothing in the mail is something its reader could not have
opened in the application themselves.* Adding an analyst to a company adds them to
its summaries; removing them removes them; no environment change either way.

`EMAIL_RECIPIENTS` is the fallback for the one case a membership table cannot answer
— a company with no entitled member, mid-setup or after its last analyst is
deactivated. When neither exists there is nobody to address, and that is reported as
the summary's state with its reason (alongside the transport's own reason, if the
deployment is also switched off, so an operator does not debug the same problem
twice). The audit row records *which of the two sources* was used and never the
addresses: a mailing list is personal data, and "why these people" is answered by
the source.

Verified in `tests/test_detection_generalization.py`: a company's own registered
users receive it and the fallback list does not; a viewer holding `analytics.read`
alone is excluded from a body containing contribution shares; the fallback engages
only when no member qualifies; and having nobody at all is a reported state that
leaves the run's results untouched.
