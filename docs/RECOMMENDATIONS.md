# The Evidence-to-Action Recommendation Layer

A KPI moved, the movement was explained, and a breakdown located it. **So what
should someone actually do — and who?**

This is the fourth and last link in the platform's chain, and deliberately the most
conservative of the four. Detection measures. Contribution apportions. Explanation
restates. This layer *suggests*, which makes it the only one that can put words in a
manager's mouth — so every sentence it produces is assembled from a stored row and
framed as something to review.

> **Contribution establishes size. It does not establish causation.** Every
> recommendation carries that sentence in full, on the card, not behind a
> disclosure. No recommendation says a region caused anything; it says a region
> *accounts for the largest share of the observed movement*, and then what a
> business might consider reviewing as a result.

---

## 1. Architecture

```
        THE VERDICT ALREADY MEASURED            WHERE IT ALREADY LOCATED
        detection_runs row for (KPI, date)      newest ContributionRun for the
        actual · expected · deviation ·         same KPI and date: leader_entity,
        status                                  path, depth, share, sufficiency
                    │                                        │
                    └────────────────┬───────────────────────┘
                                     ▼
                          explanation.confidence_for()        ← the EXISTING scale, reused
                          HIGH | MEDIUM | LOW                    not a second opinion
                                     ▼
                    ┌────────────── THE STANCE ──────────────┐    ← §2
                    │ ACTION · MONITOR · NO_ACTION ·         │
                    │ EVIDENCE_FIRST · UNREADABLE            │
                    └────────────────┬───────────────────────┘
                                     ▼  (ACTION / MONITOR only)
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
      WHICH LEVER                                   WHICH AREA               ← §4, §5
      registered controllable KpiDriver             leader_entity → chain
      → else the KPI family's defaults              → dimension → entity role
      → else operational_review                     → owner, comparison hint
              │                                             │
              └──────────────────────┬──────────────────────┘
                                     ▼
                        up to 3 cards, 8 parts each                          ← §3
                    2 levers + 1 preventive monitoring card
                                     ▼
                      business_view()   |   evidence()
                      what a reader sees | kpi.read holders only
                                     ▼
                    feedback: useful? · how far did the review get?          ← §9
                    recommendation_feedback — the only row this layer writes
```

**Derived on read. Never generated, never stored.** There is no `recommendations`
table, no model call, and no query against a company's source. The same stored rows
always produce the same words in the same order — which is exactly what makes advice
safe to print beside a governed figure. A *persisted* recommendation would be free
to disagree with its own evidence the moment somebody drilled a level deeper, and
advice that no longer matches the numbers next to it is worse than no advice.

| Concern | Where |
|---|---|
| The engine: stance, levers, target, priority, wording | [recommendation.py](../backend/app/services/recommendation.py) |
| The vocabulary: levers, families, entity roles, fixed copy | [recommendation_config.py](../backend/app/services/recommendation_config.py) |
| Two routes, and the permission split between them | [api/v1/recommendations.py](../backend/app/api/v1/recommendations.py) |
| The one table: a reader's response | [models/recommendation.py](../backend/app/models/recommendation.py) |
| The panel | [Recommendations.tsx](../frontend/src/components/Recommendations.tsx) |
| 17 tests, mostly about what it refuses to say | [test_recommendations.py](../backend/tests/test_recommendations.py) |
| 7 tests pinning the rendered panel | [results-recommendations.test.tsx](../frontend/src/results-recommendations.test.tsx) |
| Live end-to-end verifier, 7 scenarios over real HTTP | [verify_recommendations_live.py](../backend/scripts/verify_recommendations_live.py) |

---

## 2. The stance: what shape of answer this result gets

One value, not a set of flags, because a surface that could combine flags could
combine them into a contradiction.

| Stance | When | What the reader gets |
|---|---|---|
| `ACTION` | `ABNORMAL`, moved adversely, confidence above LOW | Up to three suggested actions, aimed at the located area |
| `MONITOR` | `ABNORMAL`, moved **favourably** for the KPI's registered direction | The same structure, aimed at *repeating* it rather than correcting it — capped at medium priority |
| `NO_ACTION` | `NORMAL` | "No corrective action is currently recommended. Performance remains within the expected range. Continue routine monitoring." No cards |
| `EVIDENCE_FIRST` | `LOW_CONFIDENCE`, **or** `ABNORMAL` rated LOW confidence | "Evidence insufficient for targeted action" + four evidence-collection steps. No lever, no owner, no action |
| `UNREADABLE` | A stored verdict this platform does not issue | Re-run the KPI. Nothing is invented from a status the engine cannot interpret |

**Abstention is a first-class outcome, not an error path.** A LOW-confidence result
is the case where a confident-sounding recommendation would do the most damage, so
that branch is the one with no lever in it at all. The four steps offered instead
are about the evidence itself: collect more comparable history, validate that the
breakdown follows the dimension the business actually manages, check completeness
for the date, review source freshness.

---

## 3. The eight parts, and where each one comes from

Every card carries all eight, or explicitly carries none — there is no shape where
an action arrives without its evidence, its owner or its confidence, because each of
those is what stops it reading as an instruction from the platform.

| # | Part | Source |
|---|---|---|
| 1 | **Evidence / Finding** | `DetectionRun` verdict, actual, expected and deviation, plus the leader and share stored on `ContributionRun` |
| 2 | **Target Area** | `ContributionRun.leader_entity` with its `path` and `depth`, rendered as a chain (`North › STORE`) with its entity type and share |
| 3 | **Relevant Business Lever to Review** | A `KpiDriver` row the company registered as *controllable*, matched by driver name then driver type; otherwise the KPI family's default, labelled as a default |
| 4 | **Recommended Action** | The lever's review instruction, aimed at the area, in the entity role's own review scope and comparison vocabulary |
| 5 | **Potential Impact** | `KpiMaterialityRule.business_criticality`, adjusted by how concentrated the movement is. A band with a stated basis — never a figure |
| 6 | **Recommended Owner** | The lever's functional owner, or the affected area's own manager where the lever follows the area |
| 7 | **Confidence** | `explanation.confidence_for` — the platform's existing rating, worded for whether an area has actually been named |
| 8 | **Monitoring Plan** | The KPI itself, then the lever's metrics, then the family's, for the *Next 3 comparable periods* |

Plus, on every card: an expandable **Why this recommendation?** listing the stored
figures the card was assembled from, and the causation note in full.

`key` is deterministic over the lever and the area (`order_volume|north › store`)
rather than over position in the list, so a reader's recorded response stays attached
to the recommendation it was given about even after a deeper breakdown reorders the
set.

---

## 4. Choosing a lever: what the company declared wins

```
registered controllable KpiDriver          →  lever_source = KPI_DRIVER
    matched on driver NAME first (whole word), then on driver TYPE
        ↓ none matched
the KPI family's candidate levers          →  lever_source = KPI_FAMILY_DEFAULT
    revenue · refunds · churn, each with adverse and favourable lists
        ↓ family unknown
operational_review                         →  the honest default
    "no controllable driver has been registered for this KPI"
```

Name before type, because the name is what a person wrote and the type is a coarse
bucket: a driver called *Out-of-stock hours* typed `SUPPLY` should land on inventory
availability, not on whichever supply-typed lever happens to be declared first. The
card always says which of the three routes produced it, so a reader can tell a
company-declared lever from a platform default at a glance.

Matching is whole-word on a normalised token stream (`net_revenue`, `Net-Revenue`
and `NET REVENUE` are the same three tokens), never substring — substring matching
is how "region" finds *sub-regional adjustment* and how "sku" finds *risk*, and a
confidently wrong lever is worse than a generic one that is honest about knowing
less.

**17 levers** across three families, each with a label, a functional owner, whether
ownership follows the area, a practical review instruction and its own monitoring
metrics: order volume, average order value, product mix, pricing, promotions, store
operations, inventory availability · product quality, delivery performance, supplier
performance, return experience, customer experience · customer support, retention
campaigns, product experience, customer engagement · and operational review.

At most **two** lever cards are offered, plus one preventive monitoring card. A
ranked list of seven possible levers is not a recommendation; it is the same
undifferentiated evidence the reader already had.

---

## 5. The area, and how the advice adapts to it

The target area is not a string — it is the stored breakdown's leader with its full
drill chain, its registered dimension, and the *kind* of area that dimension
represents.

```
Region     → Regional Sales Manager      → "a regional performance review"
City       → Area / City Manager         → "a city-level operational review"
Store      → Store Operations Manager    → "a store operations review"
Category   → Category Manager            → "a category performance review"
Channel    → Channel / Marketing Manager → "a channel performance review"
Segment    → Customer Experience Manager → "a customer segment review"
Team       → Operations Manager          → "a team performance review"
```

Matched against the dimension name **the company registered**, not against a fixed
list of dimensions the platform expects to exist. A company slicing revenue by
`branch` gets the store role; one slicing by `cost_centre` gets the generic
*Business Area* role, which names the dimension they actually registered and gives
an honest instruction rather than a confident sentence about a kind of area nobody
described.

Each role also supplies what *comparing like with like* means there — peer stores of
similar size and format trading the same days; other channels serving the same
customers in this window — so the action is specific enough to start on a Monday.

**Drilling re-aims the advice.** `North → North › STORE` changes the entity type
from Region to Channel, and with it the owner, the review scope and the comparison
hint. Nothing is regenerated: the deeper `ContributionRun` is simply the newest one,
and the card is a reading of it.

**With no breakdown at all, it degrades rather than guesses.** No area is named, the
cards are scoped to the KPI, `awaiting_breakdown` is set, and the panel offers a
*Sharpen with a breakdown* button. It does not pick a plausible region. A caller
without `investigation.read` gets the same KPI-level shape and is told why, because
the alternative is naming a part of the business their role may not see.

---

## 6. Priority and potential impact

```python
HIGH_PRIORITY  ⟸  confidence is HIGH  and  an area is named  and  concentrated
                  (concentrated = the stored breakdown's own leader_is_sufficient
                   flag, or |share| ≥ 25%)
MEDIUM_PRIORITY ⟸ everything else, and every favourable movement without exception
PREVENTIVE_ACTION ⟸ the third card: nothing is wrong here yet, watch it more closely
```

`PREVENTIVE_ACTION` is not a weaker version of the other two — it is a *different
instruction*, which is why it has its own rank rather than a lower one.

Potential impact is the KPI's own registered business criticality, moved one band up
when the target area holds ≥ 50% of the movement and one band down when it holds
< 25%, with the basis printed underneath:

> *Potential impact rated because this KPI is registered as HIGH business
> criticality, and the target area holds 100.0% of the observed movement.*

**Qualitative on purpose.** This platform measures no counterfactual, so any figure
attached to "what this action is worth" would be invented — and an invented number
is the fastest way for a recommendation to become indefensible in the room where it
matters. When shares are not arithmetic for a KPI, concentration is stated as
unknown and only criticality applies.

---

## 7. Confidence, reused rather than re-invented

`explanation.confidence_for(run, breakdown, access)` is the platform's existing
rating, called through a public wrapper added for exactly this purpose. A *second*
confidence scale on the same result would be the platform disagreeing with itself in
two panels of one page.

| Level | With an area named | With no area named |
|---|---|---|
| HIGH | Strong evidence supports prioritising this area. | The movement itself is well evidenced, but no breakdown yet names where to act. |
| MEDIUM | This area is strongly associated with the movement, but additional validation is recommended before acting. | The movement is established, though additional validation is recommended before acting — and no breakdown yet names where. |
| LOW | Evidence is insufficient for a specific intervention. | Evidence is insufficient for a specific intervention. |

The two columns exist because the evidence behind a movement can be strong while
saying nothing at all about *where* to act, and a confidence line reading "this area"
when none is named would be claiming the one thing the result does not have. The same
rule governs the monitoring plan: a metric written with `{area}` is dropped entirely
rather than filled with a stand-in, so a watch list never asks a reader to go and
watch somewhere unidentified.

---

## 8. Two personas, one conclusion

The panel has an **Executive / Analyst** switch. It is not two answers — it is the
same recommendation set, narrowed:

```
EXECUTIVE                                  ANALYST / MANAGER
what happened                              …everything in the executive view, plus
largest contributor + its share            the full evidence strip, all three cards,
the single top action                      "why this recommendation", the stored
its owner                                  figures behind each line, per-card
its potential impact band                  causation notes, and the limitations
overall confidence                         section
```

The analyst view *adds the evidence behind those lines* rather than replacing them,
so two people looking at one result can never come away with two conclusions. No new
role, permission or auth path was introduced for this — it is a presentation switch
over one payload, and the genuinely permission-gated split is the separate
`business_view()` / `evidence()` boundary that every other surface uses.

---

## 9. The feedback loop

The one thing this layer writes, and the only signal in it that nothing else can
derive: whether a human found the advice useful, and how far their review got.

```
👍 Useful      ○ Not started
👎 Not useful  ◐ In review        + an optional note
⚠ Needs review ● Action taken
```

* **One row per reader per recommendation key**, upserted — submitting again is a
  correction, not a second opinion.
* **The key is validated against the recommendations this run actually produces**,
  rather than accepted as given. Feedback on advice the platform never offered would
  be an orphan row no screen could show, and validating it here also means the lever
  and target area stored beside it are the engine's own and not the client's.
* **Read company-wide, not per-reader.** A manager who marked an action taken has
  said something the next reader needs to know; hiding it would let two people start
  the same review.
* **Structurally unable to move a verdict.** The table has no column detection or
  contribution reads, and `CASCADE`s on the run. Every submission is audited as
  `recommendation.feedback_recorded`.
* Writing needs `investigation.read` — the same gate investigation findings use,
  because both are a person putting their name to a conclusion.

---

## 10. API

| Method | Path | Permission |
|---|---|---|
| GET | `/companies/{id}/detection-runs/{run_id}/recommendations` | `analytics.read` |
| POST | `/companies/{id}/detection-runs/{run_id}/recommendation-feedback` | `investigation.read` |

There is no `POST` that generates, because a recommendation is a *view* of a
detection run and its stored breakdown.

Three permission tiers on one payload:

* `analytics.read` — the same gate as the result itself — reads the advice.
* `investigation.read` **sharpens** it to a named area, because naming a region or a
  store is naming a part of the business, and the permission that governs seeing a
  breakdown governs seeing one quoted back inside advice.
* `kpi.read` additionally receives `evidence`: which stored rows were read, which KPI
  family matched, the registered direction and the registered criticality.

`load_scoped` resolves the run inside the caller's own company, so a run id from
another tenant is a `404` — indistinguishable from a deleted one.

```json
{
  "result": {
    "stance": "ACTION", "verdict": "ABNORMAL", "movement_direction": "ADVERSE",
    "headline": "Revenue moved outside what its comparable history supports on 2026-08-28.",
    "body": "North accounts for 100.0% of the observed movement — the largest share of any part ranked at this level — so the actions below are aimed there.",
    "confidence": {"level": "HIGH", "reasons": ["…"]},
    "evidence_summary": {"actual": …, "expected": …, "deviation_pct": …, "reference_count": 52, "comparison": "Comparable Fridays", "top_contributor": "North", "top_contributor_share_pct": 100.0},
    "target_area": {"chain": ["North"], "entity_type": "Region", "share_pct": 100.0, "drill_next": ["channel"]},
    "action_preamble": "Based on this evidence, the following actions are recommended for review.",
    "recommendations": [{"key": "order_volume|north", "priority": "HIGH_PRIORITY", "…": "…"}],
    "monitoring": {"metrics": ["…"], "window": "Next 3 comparable periods"},
    "limitations": ["…"],
    "awaiting_breakdown": false,
    "causation_note": "This recommendation is based on contribution and available evidence. Contribution alone does not establish causation.",
    "executive": {"what_happened": "…", "top_action": "…", "owner": "…", "confidence": "HIGH"}
  },
  "feedback": [], "feedback_options": {"usefulness": ["USEFUL", "NOT_USEFUL", "NEEDS_REVIEW"], "action_status": ["NOT_STARTED", "IN_REVIEW", "ACTION_TAKEN"]},
  "may_submit_feedback": true
}
```

`feedback_options` is offered by the server rather than hardcoded in the client, so a
screen can never present a response the writer would reject.

### The three surfaces that read it

One derivation, three places, and no second copy of the wording anywhere:

| Surface | Anchored on | Re-asks when |
|---|---|---|
| **Result detail** | the stored run the reader opened | the reader runs a breakdown from the card |
| **Investigation** | `evidence.detection_run_id` of the movement or contribution on screen | the reader drills to another node — the set is derived from the *deepest* stored breakdown, so `Region → Product` re-aims the advice at the narrower area even though the run id has not changed |
| **Monitoring** | the run behind a Biggest Movement, Also Abnormal or headline row | the reader opens a different row's Copilot summary |

The monitoring surface reaches it through the same `POST /results/explain` and the
same `Recommendations` component the Results screen uses, which is what keeps the
"no invented figure" guarantee true there without a second renderer to audit: the
request body carries `kpi_id` and `target_date` and **nothing else** — not the
actual, expected or deviation the row is displaying at that moment — so the server
re-reads every figure from the run it stored. A frontend test asserts the serialized
body contains none of the numbers on screen.

Investigation deliberately passes no breakdown runner: that page *is* the breakdown
runner, so a "sharpen with a breakdown" button would only ask the reader to do again
what they are already doing.

The same derivation also composes the **recommended next step** in the post-run
summary mail — see [DETECTION.md §15](DETECTION.md#the-run-summary-mail-and-who-receives-it).

---

## 11. The three guarantees, and how they are enforced

**It never upgrades a share to a cause.** Every card carries the causation note in
full. `test_recommendations.py` scans every string in the served payload for causal
verbs and fails if one appears — as does the live verifier, on the wire, for all
seven scenarios.

**It invents no figures.** Every number in the prose is a column of the stored
detection run or the stored breakdown, re-rendered. Potential impact is a band with
a stated basis. The same suite scans for guarantee vocabulary, so a sentence
promising an outcome fails the build.

**It will not recommend an intervention on a result the platform could not judge.**
LOW confidence produces evidence steps and no lever, no owner and no action. NORMAL
produces no corrective action at all. Only an `ABNORMAL` result the existing
confidence logic rates above LOW gets a suggested action — and the limitations
section is written for the shape the reader is actually seeing, so a NORMAL result
never carries a caveat about how its actions were scoped.

## 12. Verified state

```
backend    pytest tests/test_recommendations.py            →  17 passed
frontend   npx vitest run results-recommendations          →   7 passed
backend    scripts/verify_recommendations_live.py          →  exit 0, 7 scenarios
```
The live verifier provisions a real tenant over HTTP against a running uvicorn,
stores three real verdicts, and prints exactly what the panel renders for each:

1. `ABNORMAL` with no breakdown → no area named, "locate the affected area first"
2. after a region breakdown → re-aimed at `North [Region] (100.0%)`, HIGH priority
3. drilled a level deeper → `North › STORE [Channel]`, owner and comparison change
4. one recorded response, read back, verdict unmoved
5. `NORMAL` → `NO_ACTION`, no cards
6. `LOW_CONFIDENCE` → `EVIDENCE_FIRST`, four evidence steps
7. a viewer → advice without the entity detail, feedback refused with `403`

## 13. Extending it

Intentionally dull, and no engine code changes:

* **A new lever** — add a `Lever` to `LEVERS` with its label, owner, review action
  and monitoring metrics, and name its key in a `KpiFamily`.
* **A new KPI family** — add a `KpiFamily` with its name patterns and its adverse
  and favourable lever lists.
* **A new kind of area** — add an `EntityRole` with its dimension patterns, owner,
  review scope and comparison hint.
* **A company-specific lever** — register it as a controllable `KpiDriver` on the KPI
  version. Declared beats configured, and the card will say so.

The natural next steps are all additive: persisting the derived set alongside a
response so feedback can be read against the exact wording a person saw; a per-KPI
digest of responses so a repeatedly-unhelpful lever can be demoted from a family's
defaults; letting a company edit lever labels, owners and monitoring metrics through
the governance surface rather than in code; and carrying the target area's own
comparable history into the card so *"against areas of similar size"* becomes a
figure rather than an instruction.
