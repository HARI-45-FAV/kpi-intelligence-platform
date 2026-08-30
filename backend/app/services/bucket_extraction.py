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
  "yoy_period":           {"enabled": bool}
}

Rules you must follow:

1. Enable a slot only when the document actually states the pattern. An absent
   statement means "enabled": false. Do not enable a slot because it seems
   plausible for this kind of business.
2. Never invent event dates. If the document names an event without giving its
   dates, return the name with an empty "dates" list. Every date you return is
   checked against the document text and discarded if it does not appear there,
   so a guessed date is wasted output.
3. Do not compute anything. You are not being asked for a KPI value, an
   expected value, an average, a median, a percentage, a growth rate, a
   multiplier, a threshold or a normal/abnormal judgement, and any such key will
   be rejected. An uplift figure mentioned in the document ("that day runs
   materially higher") is evidence that the weekday matters -- report the
   weekday, not the figure.
4. Do not set how much history to search or how many reference points to use.
   Those are platform settings and any value you supply is discarded.
5. Quote nothing from the document into the JSON except event names.
6. If the document describes no comparison pattern at all, return every slot
   with "enabled": false.
"""


USER_TEMPLATE = """Passages from this company's documentation follow between the
markers, in document order. Extract the comparison policy as JSON.

--- BEGIN DOCUMENT ---
{document}
--- END DOCUMENT ---

Return only the JSON object."""


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


#: Keys the model is allowed to fill in. Anything else is dropped *and reported*
#: rather than silently ignored, because a model trying to return a computed
#: number is exactly the boundary violation worth surfacing to a reviewer.
_ALLOWED_KEYS = frozenset(SLOT_KEYS.values()) | set(_BUDGET_KEYS)


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


def _ground_events(kept: dict, document_text: str) -> list[str]:
    """Drop event dates the document does not actually contain.

    Returns one note per discarded date. The event *name* is always kept: the
    company clearly has an event by that name, the reviewer can supply the real
    dates, and deleting the name would hide that the document mentioned it.

    Only the shapes :func:`_parse_events` accepts are inspected. Anything else is
    left untouched so validation reports the shape problem in its own words
    rather than this function guessing at it.
    """

    slot_key = SLOT_KEYS[BucketType.BUSINESS_EVENT]
    slot = kept.get(slot_key)
    if not isinstance(slot, dict):
        return []
    events = slot.get("events")
    haystack = _searchable(document_text)
    notes: list[str] = []

    def clean(name: str, raw_dates: object) -> list[str]:
        """Keep only grounded dates, reporting the rest."""

        if not isinstance(raw_dates, (list, tuple)):
            return []
        kept_dates: list[str] = []
        dropped: list[str] = []
        for item in raw_dates:
            text = item if isinstance(item, str) else str(item)
            if _is_grounded(text.strip()[:10], haystack):
                kept_dates.append(text)
            else:
                dropped.append(text)
        if dropped:
            notes.append(
                f"business_event '{name}': {len(dropped)} date(s) the model supplied "
                f"({', '.join(sorted(dropped))}) do not appear in the document, so they "
                "were discarded rather than used. Enter the real dates to make this "
                "event usable."
            )
        return kept_dates

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

    return notes


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
) -> BucketDraft:
    """A draft that says "nothing usable was found", explicitly.

    ``BucketConfig()`` with every slot disabled is not run through
    :func:`validate_bucket_config` -- that function refuses an all-disabled
    policy, correctly, because such a policy cannot drive a comparison. The point
    here is to return the empty result *with its reason attached* instead of a
    validation error the reviewer cannot act on.
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

    # Ground event dates in the document *before* validation, so an invented date
    # never reaches a BucketConfig at all.
    date_notes = _ground_events(kept, text)
    notes.extend(date_notes)
    if date_notes:
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
    )


def _any_slot_enabled(kept: dict) -> bool:
    for key in SLOT_KEYS.values():
        slot = kept.get(key)
        if isinstance(slot, dict) and slot.get("enabled") is True:
            return True
    return False
