"""The only place a language model touches detection: drafting configuration.

    Company document -> retrieval -> LLM -> bucket JSON -> validation -> approval -> engine

What the model is asked for is a *policy*, in a fixed JSON shape: which weekdays
a company treats as distinctive, which weeks of the month, which months form a
season, which dates an event fell on. That is genuinely a reading-comprehension
task over prose, and it is the one part of this feature a model is better at than
a rule.

What the model is never asked for, and structurally cannot supply:

* an actual KPI value,
* an expected value,
* a median, a MAD or a modified z-score,
* a deviation,
* a NORMAL / ABNORMAL / LOW_CONFIDENCE verdict.

The enforcement is not a prompt instruction -- it is the return type. This module
hands back a :class:`BucketDraft` whose payload has already been through
:func:`validate_bucket_config`, which accepts only the five known slots and
rejects every other key. A model that returned ``{"expected_value": 10250000}``
would fail validation with "Unknown bucket configuration key(s)". Numbers cannot
reach the engine through this door.

Three further things this module refuses to take on trust, each because a model
was observed doing it:

* **Search budget.** ``lookback_days``, ``min_reference_points`` and
  ``max_reference_points`` are platform concerns, and a model that answered
  ``lookback_days: 7`` would quietly reduce every comparison to a week of
  history. They are forced to the platform defaults, and any value the model
  offered is reported to the reviewer rather than applied.
* **Event dates.** The prompt forbids inventing them; models do it anyway. Every
  extracted date is checked against the document's own text and dropped if it
  does not appear there, because a plausible wrong date silently corrupts every
  comparison built on it.
* **Emptiness.** A document that yields no enabled slot produces an explicit
  NEEDS_REVIEW draft carrying the reason, not a blank success.

The draft is also not usable on arrival: it is stored as PROPOSED (or
NEEDS_REVIEW) and the detection engine reads APPROVED rows only, so a human has
to look at the extracted pattern before any number is computed from it.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date

from app.core.clock import utcnow
from app.core.errors import ValidationFailure
from app.core.telemetry import LlmUsage
from app.llm.config import LLMConfig, get_llm_config
from app.llm.provider import (
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMUnavailable,
    build_provider,
)
from app.models.base import BucketType
from app.services.bucket_config import (
    LOOKBACK_DEFAULT_DAYS,
    MAX_POINTS_DEFAULT,
    MIN_POINTS_DEFAULT,
    SLOT_KEYS,
    BucketConfig,
    validate_bucket_config,
)
from app.services.bucket_retrieval import Retrieval, retrieve_policy_passages

#: How much of a document the retriever will scan. Scoring is local, lexical and
#: linear, so this is a CPU bound rather than a cost bound -- generous enough that
#: a real handbook is read whole.
MAX_SCAN_CHARS = 400_000

#: What retrieval is allowed to put in front of the model. Deliberately much
#: smaller than the ceiling: a handbook's comparison policy is a few paragraphs,
#: and a self-hosted 8B model spends real wall-clock seconds per thousand tokens
#: of prompt. Shrinking the prompt to the passages that can actually answer the
#: question is both faster and more accurate than sending chapter one.
RETRIEVAL_CHAR_BUDGET = 6_000

#: Output budget for this call specifically. The deployment-wide
#: ``LLM_MAX_OUTPUT_TOKENS`` is tuned for short Copilot answers, and a policy
#: object listing several events and a season needs more room than that. Inheriting
#: a tight limit here does not produce a shorter answer -- it produces a JSON
#: object cut off mid-string, which is indistinguishable from a model that cannot
#: follow the format.
EXTRACTION_MAX_OUTPUT_TOKENS = 900

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

#: Keys whose value is a platform decision, never a model's.
_BUDGET_KEYS = ("lookback_days", "min_reference_points", "max_reference_points")

_PLATFORM_BUDGET = {
    "lookback_days": LOOKBACK_DEFAULT_DAYS,
    "min_reference_points": MIN_POINTS_DEFAULT,
    "max_reference_points": MAX_POINTS_DEFAULT,
}


SYSTEM_PROMPT = """You extract a company's KPI comparison policy from its own documentation.

Your entire output is one JSON object describing which historical dates are
comparable to a given date for this company. Return nothing else: no prose, no
explanation, no code fences.

The object may contain only these keys:

{
  "same_day_of_week":     {"enabled": bool, "days": [weekday names or 1-7, 1=Monday]},
  "same_week_of_month":   {"enabled": bool, "weeks": [1-5]},
  "same_month_or_season": {"enabled": bool, "months": [month names or 1-12]},
  "business_event":       {"enabled": bool, "events": [{"name": str, "dates": ["YYYY-MM-DD", ...]}]},
  "yoy_period":           {"enabled": bool},
  "kpi_overrides":        [{"kpi": str, ...any of the five keys above}]
}

The five slots at the top describe the company as a whole. "kpi_overrides" is
for the exception: a measure the document singles out as behaving differently
from the rest.

Rules you must follow:

1. Enable a slot only when the document actually states the pattern. An absent
   statement means "enabled": false. Do not enable a slot because it seems
   plausible for this kind of business.
2. Never invent event dates. Report only the days the document itself states.
   Every date you return is checked against the document text and discarded if
   neither the full date nor its day-and-month appears there, so a guessed date
   is wasted output. If the document names an event without giving any dates at
   all, return the name with an empty "dates" list.
3. Documents usually state an annual window as a day and a month with no year
   ("the festival runs 15-21 Oct"). Write out every day in that window as a
   YYYY-MM-DD date and use any year you like: the platform keeps the day and
   month, discards your year, and works out for itself which years the window
   covers. List each day of the window separately -- the two endpoints alone
   describe only two days, not the span between them.
4. Do not compute anything. You are not being asked for a KPI value, an
   expected value, an average, a median, a percentage, a growth rate, a
   multiplier, a threshold or a normal/abnormal judgement, and any such key will
   be rejected. An uplift figure mentioned in the document ("that day runs
   materially higher") is evidence that the weekday matters -- report the
   weekday, not the figure.
5. Do not set how much history to search or how many reference points to use.
   Those are platform settings and any value you supply is discarded.
6. Quote nothing from the document into the JSON except event names.
7. If the document describes no comparison pattern at all, return every slot
   with "enabled": false.
8. Use "kpi_overrides" only where the document names a measure and gives it a
   pattern different from the general one ("orders peak in the third week,
   unlike everything else"). Write the measure's name exactly as the document
   writes it: the platform matches that name against the measures this company
   has registered and discards any name it cannot match. An override states that
   measure's whole policy, so repeat any general slot that still applies to it.
   A measure the document merely mentions is not an override. Omit the key
   entirely when the document states one policy for everything, which is the
   usual case.
"""


USER_TEMPLATE = """Passages from this company's documentation follow between the
markers, in document order. Extract the comparison policy as JSON.

--- BEGIN DOCUMENT ---
{document}
--- END DOCUMENT ---

Return only the JSON object."""


@dataclass
class KpiOverrideDraft:
    """A policy the document stated for one named measure specifically.

    ``label`` is the measure's name *as the document wrote it*, deliberately not
    a KPI key: this module reads prose and has no access to what the company has
    registered. Matching the label to a registered ``kpi_key`` is the caller's
    job, which keeps the extraction testable without a database and keeps a model
    from ever naming a key the company does not have.
    """

    label: str
    config: BucketConfig
    payload: dict
    notes: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_reasons)

    def as_dict(self) -> dict:
        return {
            "kpi_label": self.label,
            "buckets": self.config.as_dict(),
            "warnings": list(self.config.warnings),
            "notes": self.notes,
            "needs_review": self.needs_review,
            "review_reasons": self.review_reasons,
        }


@dataclass
class BucketDraft:
    """A validated but unapproved configuration, plus how it was produced."""

    config: BucketConfig
    payload: dict
    model: str | None
    raw_keys: list[str] = field(default_factory=list)
    rejected_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: True when a person must look at this before it can mean anything: no slot
    #: was enabled, dates were discarded as ungrounded, or the model tried to
    #: cross the boundary. The caller stores these as NEEDS_REVIEW.
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    #: How the passages sent to the model were chosen, so an extraction can be
    #: explained and reproduced.
    retrieval: dict | None = None
    #: Measures the document gave their own pattern. Each becomes its own
    #: configuration row scoped to that KPI; the engine already prefers a
    #: KPI-scoped row over the company-wide one, so nothing downstream changes.
    overrides: list[KpiOverrideDraft] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "buckets": self.config.as_dict(),
            "model": self.model,
            "warnings": list(self.config.warnings),
            "rejected_keys": self.rejected_keys,
            "notes": self.notes,
            "needs_review": self.needs_review,
            "review_reasons": self.review_reasons,
            "retrieval": self.retrieval,
            "kpi_overrides": [override.as_dict() for override in self.overrides],
        }


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a model turn, tolerantly but not creatively."""

    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    match = _JSON_BLOCK.search(candidate)
    if not match:
        raise ValidationFailure(
            "The model did not return a JSON object, so there is nothing to review. "
            "The configuration can be entered by hand instead."
        )
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(
            f"The model returned malformed JSON ({exc.msg}), so it was discarded rather "
            "than repaired."
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationFailure("The model returned JSON that is not an object.")
    return parsed


#: The per-measure section. Allowed through :func:`_quarantine` so it is not
#: reported as a boundary violation, then split off before validation -- it is a
#: list of policies, not a slot, and ``validate_bucket_config`` would reject it.
_OVERRIDES_KEY = "kpi_overrides"

#: Keys the model is allowed to fill in. Anything else is dropped *and reported*
#: rather than silently ignored, because a model trying to return a computed
#: number is exactly the boundary violation worth surfacing to a reviewer.
_ALLOWED_KEYS = frozenset(SLOT_KEYS.values()) | set(_BUDGET_KEYS) | {_OVERRIDES_KEY}


def _quarantine(payload: dict) -> tuple[dict, list[str], list[str]]:
    kept: dict = {}
    rejected: list[str] = []
    notes: list[str] = []
    for key, value in payload.items():
        name = str(key)
        if name in _ALLOWED_KEYS:
            kept[name] = value
        else:
            rejected.append(name)
    if rejected:
        notes.append(
            "The model returned key(s) outside the configuration contract "
            f"({', '.join(sorted(rejected))}); they were discarded. Only comparison "
            "policy crosses this boundary -- every number is computed by the engine."
        )
    return kept, rejected, notes


def _force_budget(kept: dict) -> list[str]:
    """Overwrite the search budget with the platform's, reporting any attempt.

    ``setdefault`` would let a model-supplied value win, which is how a policy
    extraction ends up shortening the comparison window. The values are replaced
    unconditionally; what the model asked for is reported so a reviewer can set
    it deliberately afterwards if it was actually right.
    """

    offered = {key: kept[key] for key in _BUDGET_KEYS if key in kept}
    kept.update(_PLATFORM_BUDGET)
    if not offered:
        return []
    listed = ", ".join(f"{key}={offered[key]!r}" for key in sorted(offered))
    return [
        f"The model tried to set the search budget ({listed}); the platform defaults "
        f"were used instead ("
        + ", ".join(f"{key}={_PLATFORM_BUDGET[key]}" for key in _BUDGET_KEYS)
        + "). Change them deliberately on the configuration if the document really "
        "specifies a different window."
    ]


# ---------------------------------------------------------------------------
# Grounding extracted dates in the document
# ---------------------------------------------------------------------------
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _searchable(text: str) -> str:
    """Lowercase the text and collapse punctuation, so ``2023-10-15`` and
    ``2023/10/15`` compare equal without a format-by-format search."""

    return _NON_ALNUM.sub(" ", (text or "").lower())


def _date_renderings(iso: str) -> tuple[str, ...]:
    """The ways a document might write one date.

    Month names come from :mod:`calendar`, so this contains no month literal and
    behaves the same for every tenant.
    """

    try:
        year, month, day = (int(part) for part in iso.split("-")[:3])
        name = calendar.month_name[month].lower()
        abbr = calendar.month_abbr[month].lower()
    except (ValueError, IndexError):
        return ()

    numeric = {
        f"{year} {month:02d} {day:02d}",
        f"{year} {month} {day}",
        f"{day:02d} {month:02d} {year}",
        f"{day} {month} {year}",
        f"{month:02d} {day:02d} {year}",
        f"{month} {day} {year}",
    }
    worded = {
        f"{day} {name} {year}",
        f"{day:02d} {name} {year}",
        f"{name} {day} {year}",
        f"{name} {day:02d} {year}",
        f"{day} {abbr} {year}",
        f"{abbr} {day} {year}",
    }
    return tuple(numeric | worded)


def _is_grounded(iso: str, haystack: str) -> bool:
    return any(rendering in haystack for rendering in _date_renderings(iso))


# --- recurring windows stated without a year -------------------------------
# A handbook states an annual event the way a person says it: "NovaFest
# (15-21 Oct)". There is no year in that, and there should not be -- the window
# recurs. Requiring a full year-month-day rendering therefore discards the most
# common way a document states an event, which is what made the business_event
# slot unfillable from a real handbook.
#
# So a date is grounded two ways. Either the document contains the whole date, or
# it contains the *day and month* -- and then the platform, not the model, decides
# which years that window covers. The model's year is thrown away either way, so
# no year that a model invented can reach a comparison.

#: Years a recurring window is expanded across. Back far enough to cover
#: ``LOOKBACK_MAX_DAYS`` (five years), forward one so an occurrence that has not
#: happened yet is still comparable when it does. Dates outside a company's
#: configured lookback simply never match a candidate, so a generous span costs
#: nothing but makes the slot work without re-extraction each year.
EVENT_YEARS_BACK = 5
EVENT_YEARS_FORWARD = 1

#: February is bounded at 29 so a window stated as "29 Feb" is not rejected
#: outright; the expansion then emits it only in years that actually have one.
_LEAP_YEAR = 2024


def _month_tokens() -> dict[str, int]:
    """Every spelling of a month name, mapped to its number. No literals."""

    tokens: dict[str, int] = {}
    for month in range(1, 13):
        for word in (calendar.month_name[month], calendar.month_abbr[month]):
            token = word.strip().lower()
            if token:
                tokens[token] = month
    return tokens


_MONTH_TOKENS = _month_tokens()
# Longest first, so "march" is not matched as "mar" with a stray "ch" left over.
_MONTH_ALTERNATION = "|".join(
    re.escape(token) for token in sorted(_MONTH_TOKENS, key=len, reverse=True)
)

#: "15 oct" and "15 21 oct" -- the second number is the end of a range. The
#: trailing ``\b`` on each number is what stops "october 2025" being read as day
#: 20 of October.
_DAY_THEN_MONTH = re.compile(
    rf"\b(\d{{1,2}})\b(?:\s+(\d{{1,2}})\b)?\s+({_MONTH_ALTERNATION})\b"
)
#: "oct 15" and "oct 15 21".
_MONTH_THEN_DAY = re.compile(
    rf"\b({_MONTH_ALTERNATION})\s+(\d{{1,2}})\b(?:\s+(\d{{1,2}})\b)?"
)


def _plausible(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(_LEAP_YEAR, month)[1]


def _document_month_days(haystack: str) -> set[tuple[int, int]]:
    """The (month, day) positions the document actually names, ranges expanded.

    A range matters as much as a single date: "15-21 Oct" states seven days, and
    only its two endpoints appear as numbers. Reading it as two dates would ground
    the edges of an event window and discard its middle.
    """

    found: set[tuple[int, int]] = set()

    def add(month: int, first: str | None, second: str | None) -> None:
        if first is None:
            return
        start = int(first)
        end = int(second) if second is not None else start
        # A descending pair is not a range -- it is two dates that happen to be
        # adjacent in the prose. Take them literally rather than inverting them.
        days = range(start, end + 1) if start <= end else (start, end)
        for day in days:
            if _plausible(month, day):
                found.add((month, day))

    for first, second, name in _DAY_THEN_MONTH.findall(haystack):
        add(_MONTH_TOKENS[name], first, second or None)
    for name, first, second in _MONTH_THEN_DAY.findall(haystack):
        add(_MONTH_TOKENS[name], first, second or None)
    return found


def _month_day_of(iso: str) -> tuple[int, int] | None:
    """The (month, day) a model-supplied date points at, ignoring its year."""

    try:
        _, month, day = (int(part) for part in iso.split("-")[:3])
    except (ValueError, IndexError):
        return None
    return (month, day) if _plausible(month, day) else None


def _expand_recurring(month: int, day: int) -> list[str]:
    """The same calendar position in every year the platform may compare across."""

    this_year = utcnow().date().year
    years = range(this_year - EVENT_YEARS_BACK, this_year + EVENT_YEARS_FORWARD + 1)
    return [
        date(year, month, day).isoformat()
        for year in years
        if day <= calendar.monthrange(year, month)[1]
    ]


def _ground_events(kept: dict, document_text: str) -> tuple[list[str], bool]:
    """Reconcile the model's event dates with what the document actually says.

    Three outcomes per date, and the difference between them is the whole point:

    * the document contains the full date -- kept as it stands;
    * the document contains its day and month but no year -- a recurring window,
      so the platform expands it across the years it may compare and the model's
      year is discarded;
    * the document contains neither -- dropped, because a plausible wrong date
      silently corrupts every comparison built on it.

    Returns the notes to show a reviewer, and whether anything was dropped. Only a
    drop is a reason to review: an expansion used the document's own day and month
    and a year this platform derived from the calendar.

    The event *name* is always kept. The company clearly has an event by that name,
    the reviewer can supply real dates, and deleting the name would hide that the
    document mentioned it.
    """

    slot_key = SLOT_KEYS[BucketType.BUSINESS_EVENT]
    slot = kept.get(slot_key)
    if not isinstance(slot, dict):
        return [], False
    events = slot.get("events")
    haystack = _searchable(document_text)
    month_days = _document_month_days(haystack)
    notes: list[str] = []
    dropped_any = False

    def clean(name: str, raw_dates: object) -> list[str]:
        nonlocal dropped_any
        if not isinstance(raw_dates, (list, tuple)):
            return []
        kept_dates: set[str] = set()
        dropped: list[str] = []
        recurring: list[str] = []
        for item in raw_dates:
            text = (item if isinstance(item, str) else str(item)).strip()[:10]
            if _is_grounded(text, haystack):
                kept_dates.add(text)
                continue
            position = _month_day_of(text)
            if position is not None and position in month_days:
                kept_dates.update(_expand_recurring(*position))
                recurring.append(text)
            else:
                dropped.append(text)

        if recurring:
            positions = sorted(
                {pos for pos in (_month_day_of(item) for item in recurring) if pos}
            )
            stated = ", ".join(
                f"{day} {calendar.month_name[month]}" for month, day in positions
            )
            notes.append(
                f"business_event '{name}': the document states {stated} without a "
                "year, so it was read as a window that recurs. The platform expanded "
                f"it across {EVENT_YEARS_BACK} years back and {EVENT_YEARS_FORWARD} "
                "forward; the year the model supplied was discarded. Narrow the dates "
                "by hand if the event ran only once."
            )
        if dropped:
            dropped_any = True
            notes.append(
                f"business_event '{name}': {len(dropped)} date(s) the model supplied "
                f"({', '.join(sorted(dropped))}) do not appear in the document -- not as "
                "a full date and not as a day and month -- so they were discarded rather "
                "than used. Enter the real dates to make this event usable."
            )
        return sorted(kept_dates)

    if isinstance(events, dict):
        slot["events"] = {
            str(name): clean(str(name), dates) for name, dates in events.items()
        }
    elif isinstance(events, (list, tuple)):
        rebuilt: list[object] = []
        for entry in events:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("event") or entry.get("label")
                if isinstance(name, str) and "dates" in entry:
                    entry = {**entry, "dates": clean(name, entry.get("dates"))}
            rebuilt.append(entry)
        slot["events"] = rebuilt

    return notes, dropped_any


# ---------------------------------------------------------------------------
# Per-measure overrides
# ---------------------------------------------------------------------------
#: How a model might label the measure an override applies to. The first of
#: these that carries text is the name; the remaining keys of the entry are read
#: as slots.
_LABEL_KEYS = ("kpi", "kpi_name", "name", "measure", "label")


def _override_label(entry: dict) -> tuple[str, str]:
    """The measure's name and the key that carried it, or two empty strings."""

    for key in _LABEL_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200], key
    return "", ""


def _override_drafts(
    raw: object, document_text: str
) -> tuple[list[KpiOverrideDraft], list[str]]:
    """Turn the model's per-measure section into validated drafts, one per name.

    Each override goes through exactly the same treatment as the company-wide
    policy -- unknown keys quarantined, event dates grounded in the same
    document, search budget forced to the platform's, payload validated against
    the same five-slot contract. An override reaches the engine by the same door,
    so a looser path here would be the way around the boundary rather than a
    convenience.

    An override is dropped, with a note, when it names no measure or states no
    pattern. Neither is a reviewable draft: the first cannot be matched against
    anything the company registered, and the second is the model having noticed a
    measure rather than having read a policy for it.
    """

    if not isinstance(raw, (list, tuple)):
        return [], []

    drafts: list[KpiOverrideDraft] = []
    notes: list[str] = []
    seen: set[str] = set()

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label, label_key = _override_label(entry)
        if not label:
            notes.append(
                "A per-measure override named no measure, so it was discarded -- there "
                "is nothing to scope it to."
            )
            continue
        folded = label.casefold()
        if folded in seen:
            notes.append(
                f"More than one override was returned for '{label}'; only the first was "
                "kept."
            )
            continue

        body = {key: value for key, value in entry.items() if key != label_key}
        kept, rejected, own_notes = _quarantine(body)
        kept.pop(_OVERRIDES_KEY, None)  # no nesting: an override cannot carry overrides
        reasons: list[str] = []
        if rejected:
            reasons.append(
                f"The override for '{label}' returned key(s) outside the configuration "
                f"contract ({', '.join(sorted(rejected))})."
            )

        date_notes, dropped = _ground_events(kept, document_text)
        own_notes.extend(date_notes)
        if dropped:
            reasons.append(
                f"Event dates in the override for '{label}' were discarded because the "
                "document does not contain them."
            )
        own_notes.extend(_force_budget(kept))

        if not _any_slot_enabled(kept):
            notes.append(
                f"The document mentions '{label}' but states no comparison pattern of "
                "its own for it, so no override was drafted. It keeps the company-wide "
                "policy."
            )
            continue

        try:
            validated = validate_bucket_config(kept)
        except ValidationFailure as exc:
            notes.append(
                f"The override for '{label}' is not a valid configuration "
                f"({exc.message}), so it was discarded. The company-wide policy still "
                "applies to it."
            )
            continue

        seen.add(folded)
        drafts.append(
            KpiOverrideDraft(
                label=label,
                config=validated,
                payload=validated.as_dict(),
                notes=own_notes,
                review_reasons=reasons,
            )
        )

    return drafts, notes


def _record(usage: LlmUsage | None, config: LLMConfig, response: LLMResponse) -> None:
    if usage is None:
        return
    usage.record(
        model=response.model or config.model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        cost_usd=config.estimate_cost_usd(
            response.usage.prompt_tokens, response.usage.completion_tokens
        ),
    )


def _unconfigured_draft(
    *,
    model: str | None,
    reasons: list[str],
    notes: list[str],
    raw_keys: list[str] | None = None,
    rejected_keys: list[str] | None = None,
    retrieval: dict | None = None,
    overrides: list[KpiOverrideDraft] | None = None,
) -> BucketDraft:
    """A draft that says "nothing usable was found", explicitly.

    ``BucketConfig()`` with every slot disabled is not run through
    :func:`validate_bucket_config` -- that function refuses an all-disabled
    policy, correctly, because such a policy cannot drive a comparison. The point
    here is to return the empty result *with its reason attached* instead of a
    validation error the reviewer cannot act on.

    Overrides are carried through rather than dropped: a document that states a
    pattern only for named measures and none for the company as a whole has said
    something usable about those measures, and discarding it because the
    company-wide slot came out empty would lose the only policy it contained.
    """

    empty = BucketConfig(warnings=tuple(reasons))
    return BucketDraft(
        config=empty,
        payload=empty.as_dict(),
        model=model,
        raw_keys=raw_keys or [],
        rejected_keys=rejected_keys or [],
        notes=notes,
        needs_review=True,
        review_reasons=reasons,
        retrieval=retrieval,
        overrides=overrides or [],
    )


async def extract_bucket_config(
    document_text: str,
    *,
    provider: LLMProvider | None = None,
    config: LLMConfig | None = None,
    usage_sink: LlmUsage | None = None,
) -> BucketDraft:
    """Turn company documentation into a reviewable bucket configuration.

    Raises :class:`LLMUnavailable` when no model is configured -- the caller
    reports that as a 503 and the configuration is entered by hand, which is the
    supported path on a deployment that runs without a model.
    """

    text = (document_text or "").strip()
    if not text:
        raise ValidationFailure("There is no document text to read.")

    cfg = config or get_llm_config()
    if provider is None and not cfg.is_available:
        raise LLMUnavailable(
            cfg.unavailable_reason
            or "No language model is configured, so a configuration cannot be drafted "
            "from documentation. It can still be entered directly.",
            details={"provider": cfg.provider, "enabled": cfg.enabled},
        )

    # --- retrieval ---------------------------------------------------------
    retrieval: Retrieval = retrieve_policy_passages(
        text[:MAX_SCAN_CHARS], char_budget=RETRIEVAL_CHAR_BUDGET
    )
    if retrieval.empty:
        # No model call: nothing in this document mentions comparison policy, and
        # a three-minute round trip to confirm that would only produce the same
        # all-disabled answer with less explanation attached.
        return _unconfigured_draft(
            model=None,
            reasons=[
                "No passage in this document mentions a comparison pattern -- no "
                "weekday, week of month, season, event or year-over-year language was "
                "found anywhere in it. Nothing was sent to the model."
            ],
            notes=[
                "Check that the uploaded version is the document that states the "
                "comparison policy, or enter the configuration directly.",
            ],
            retrieval=retrieval.as_dict(),
        )

    excerpt = retrieval.text

    # --- one model turn, with an output budget sized for this task ----------
    call_config = replace(
        cfg,
        max_output_tokens=max(cfg.max_output_tokens, EXTRACTION_MAX_OUTPUT_TOKENS),
    )
    active = provider or build_provider(call_config)
    messages = [
        LLMMessage.system(SYSTEM_PROMPT),
        LLMMessage.user(USER_TEMPLATE.format(document=excerpt)),
    ]
    try:
        response = await active.generate(messages, tools=None)
    finally:
        if provider is None:
            await active.aclose()
    _record(usage_sink, call_config, response)

    if response.finish_reason == "length":
        raise ValidationFailure(
            "The model's answer was cut off by its output limit before the JSON was "
            f"complete, so it was discarded rather than repaired. Raise "
            f"LLM_MAX_OUTPUT_TOKENS above {call_config.max_output_tokens} or enter the "
            "configuration directly."
        )

    parsed = _extract_json(response.text)
    raw_keys = sorted(str(key) for key in parsed.keys())
    kept, rejected, notes = _quarantine(parsed)

    review_reasons: list[str] = []
    if rejected:
        review_reasons.append(
            "The model returned key(s) outside the configuration contract "
            f"({', '.join(sorted(rejected))}). The rest of the extraction may be "
            "similarly loose, so read it before approving."
        )

    # Split the per-measure section off before validation: it is a list of
    # policies, not a slot, and it is graded on its own. Each override is
    # validated by the same contract, so nothing reaches the engine through this
    # branch that could not reach it through the company-wide one.
    overrides, override_notes = _override_drafts(kept.pop(_OVERRIDES_KEY, None), text)
    notes.extend(override_notes)
    if overrides:
        listed = ", ".join(f"'{override.label}'" for override in overrides)
        notes.append(
            f"The document gives {len(overrides)} measure(s) a pattern of their own "
            f"({listed}). Each becomes a configuration scoped to that measure, which "
            "the engine prefers over the company-wide one; every other measure keeps "
            "the company-wide policy. A name that matches nothing this company has "
            "registered is reported and not stored."
        )

    # Reconcile event dates with the document *before* validation, so a date the
    # document never stated never reaches a BucketConfig at all.
    date_notes, dates_dropped = _ground_events(kept, text)
    notes.extend(date_notes)
    if dates_dropped:
        review_reasons.append(
            "Event dates were discarded because the document does not contain them."
        )

    notes.extend(_force_budget(kept))

    model_name = response.model or call_config.model
    try:
        validated = validate_bucket_config(kept)
    except ValidationFailure as exc:
        # An all-disabled policy is the common case here and is a legitimate
        # reading of a document that never states a pattern. Report it as a
        # reviewable draft rather than as a failed request; any other validation
        # failure is a genuinely malformed answer and stays an error.
        if not _any_slot_enabled(kept):
            return _unconfigured_draft(
                model=model_name,
                reasons=[
                    "The model read the retrieved passages and found no comparison "
                    "pattern it could state, so every slot is disabled and the "
                    "configuration cannot select comparable dates yet."
                ],
                notes=notes,
                raw_keys=raw_keys,
                rejected_keys=rejected,
                retrieval=retrieval.as_dict(),
                overrides=overrides,
            )
        raise ValidationFailure(
            f"The extracted configuration is not valid: {exc.message}"
        ) from exc

    if retrieval.selected_characters < retrieval.document_characters:
        notes.append(
            f"{len(retrieval.passages)} of {retrieval.total_passages} passages "
            f"({retrieval.selected_characters:,} of {retrieval.document_characters:,} "
            "characters) were sent to the model -- the ones that mention comparison "
            "policy. The rest of the document was not read."
        )
    notes.append(
        "This is a draft. The detection engine ignores it until a person approves it."
    )

    # A configuration whose only enabled slot cannot ever apply is not usable, and
    # saying so now is cheaper than a reviewer approving it and getting
    # LOW_CONFIDENCE on every date.
    if any(warning for warning in validated.warnings):
        review_reasons.extend(
            warning
            for warning in validated.warnings
            if "cannot select comparable days" in warning or "can never apply" in warning
        )

    return BucketDraft(
        config=validated,
        payload=validated.as_dict(),
        model=model_name,
        raw_keys=raw_keys,
        rejected_keys=rejected,
        notes=notes,
        needs_review=bool(review_reasons),
        review_reasons=review_reasons,
        retrieval=retrieval.as_dict(),
        overrides=overrides,
    )


def _any_slot_enabled(kept: dict) -> bool:
    for key in SLOT_KEYS.values():
        slot = kept.get(key)
        if isinstance(slot, dict) and slot.get("enabled") is True:
            return True
    return False
