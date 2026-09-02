"""Grounding event dates against the document that stated them.

The unit under test is the rule that decides which of a model's event dates may
reach a comparison. It has three outcomes and they are not interchangeable:

* the document contains the whole date -- used as given;
* the document contains its day and month but no year -- a recurring window, so
  the platform supplies the years and the model's year is thrown away;
* the document contains neither -- discarded, and the draft is marked for review.

The middle case is the one that made the ``business_event`` slot unusable in
practice: a handbook writes "NovaFest (15-21 Oct)", which is a real statement of a
real window and contains no year at all. Requiring a full ISO rendering discarded
every date of it, left the event nameless of dates, and pushed an otherwise
complete four-slot extraction into NEEDS_REVIEW where the engine never reads it.

Nothing here asserts on a weekday, a month or an event that any one company uses.
The fixtures below invent their own vocabulary precisely so that passing them
cannot depend on the platform having learned a tenant's calendar.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from app.core.clock import utcnow
from app.llm.config import get_llm_config
from app.llm.provider import LLMProvider, LLMResponse, LLMUsage
from app.services.bucket_extraction import (
    EVENT_YEARS_BACK,
    EVENT_YEARS_FORWARD,
    _document_month_days,
    _expand_recurring,
    extract_bucket_config,
)


class ScriptedModel(LLMProvider):
    """Returns a fixed policy object, so the test exercises everything but the model."""

    def __init__(self, payload: dict) -> None:
        super().__init__(get_llm_config())
        self.payload = payload
        self.prompts: list[str] = []

    async def generate(self, messages, tools=None, stream=False):
        self.prompts.append("\n".join(m.content or "" for m in messages))
        return LLMResponse(
            text=json.dumps(self.payload),
            model="test/policy-reader",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=10),
        )

    async def aclose(self) -> None:
        return None


def draft_from(document: str, answer: dict):
    return asyncio.run(extract_bucket_config(document, provider=ScriptedModel(answer)))


def event_dates(draft, name: str) -> list[str]:
    for event in draft.config.business_event.events:
        if event.name == name:
            return [day.isoformat() for day in event.dates]
    raise AssertionError(f"no event named {name!r} in {draft.config.as_dict()}")


# ---------------------------------------------------------------------------
# The document scanner
# ---------------------------------------------------------------------------
def test_scanner_reads_a_day_month_range_as_every_day_in_it():
    """"15-21 Oct" states seven days; only two of them appear as numbers.

    Reading the endpoints alone would ground the edges of an event window and
    discard its middle, which is the same as not supporting ranges at all.
    """

    found = _document_month_days("the festival runs 15 21 oct each year")
    assert found == {(10, day) for day in range(15, 22)}


def test_scanner_accepts_either_word_order_and_both_month_spellings():
    assert (3, 7) in _document_month_days("closed on 7 march for the audit")
    assert (3, 7) in _document_month_days("closed on march 7 for the audit")
    assert (3, 7) in _document_month_days("closed on 7 mar for the audit")


def test_scanner_does_not_read_a_year_as_a_day():
    """"October 2025" names a month and a year, not the 20th of October."""

    assert _document_month_days("reviewed in october 2025") == set()


def test_scanner_takes_a_descending_pair_literally_rather_than_inverting_it():
    """"21 15 Oct" is two dates in prose, not a backwards range."""

    assert _document_month_days("see 21 15 oct notes") == {(10, 21), (10, 15)}


def test_expansion_covers_the_platform_window_and_skips_impossible_days():
    this_year = utcnow().date().year
    years = {date.fromisoformat(d).year for d in _expand_recurring(6, 10)}
    assert years == set(
        range(this_year - EVENT_YEARS_BACK, this_year + EVENT_YEARS_FORWARD + 1)
    )
    # 29 February exists only in leap years, and the expansion emits it only there.
    for iso in _expand_recurring(2, 29):
        assert date.fromisoformat(iso).month == 2
        assert date.fromisoformat(iso).day == 29


# ---------------------------------------------------------------------------
# End to end through extract_bucket_config
# ---------------------------------------------------------------------------
RECURRING_DOC = (
    "Comparison policy. Trading on Tuesday is unlike other days and is compared "
    "with prior Tuesdays. The Lantern Week promotion runs 15 21 oct every year "
    "and its days are comparable only with each other."
)

RECURRING_ANSWER = {
    "same_day_of_week": {"enabled": True, "days": ["Tuesday"]},
    "business_event": {
        "enabled": True,
        "events": [
            {
                "name": "Lantern Week",
                # A year the document never states. It must not survive.
                "dates": [f"2019-10-{day}" for day in range(15, 22)],
            }
        ],
    },
}


def test_recurring_window_is_expanded_and_the_models_year_is_discarded():
    draft = draft_from(RECURRING_DOC, RECURRING_ANSWER)
    dates = event_dates(draft, "Lantern Week")

    assert dates, "a window the document states must survive grounding"
    # Every emitted date sits inside the stated day-month window...
    assert {(date.fromisoformat(d).month, date.fromisoformat(d).day) for d in dates} == {
        (10, day) for day in range(15, 22)
    }
    # ...across the platform's years, and not the year the model supplied.
    this_year = utcnow().date().year
    assert {date.fromisoformat(d).year for d in dates} == set(
        range(this_year - EVENT_YEARS_BACK, this_year + EVENT_YEARS_FORWARD + 1)
    )
    assert 2019 not in {date.fromisoformat(d).year for d in dates}


def test_a_recurring_window_does_not_by_itself_demand_review():
    """The regression this suite exists for.

    Nothing was invented: the day and month are the document's, the years are the
    platform's calendar. A draft in that state is proposable, and marking it
    NEEDS_REVIEW is what stopped four correctly-extracted slots reaching the
    engine.
    """

    draft = draft_from(RECURRING_DOC, RECURRING_ANSWER)
    assert draft.needs_review is False
    assert draft.review_reasons == []
    assert draft.config.business_event.unusable_names == ()
    # The expansion is still reported, because a reviewer should know the years
    # were derived rather than read.
    assert any("without a year" in note for note in draft.notes)


def test_an_ungrounded_date_is_still_discarded_and_still_demands_review():
    """The no-invention rule is relaxed about *years*, not about dates."""

    answer = {
        "same_day_of_week": {"enabled": True, "days": ["Tuesday"]},
        "business_event": {
            "enabled": True,
            # March appears nowhere in the document.
            "events": [{"name": "Lantern Week", "dates": ["2024-03-02"]}],
        },
    }
    draft = draft_from(RECURRING_DOC, answer)

    assert event_dates(draft, "Lantern Week") == []
    assert draft.needs_review is True
    assert any("do not appear in the document" in note for note in draft.notes)


def test_a_fully_stated_date_is_kept_exactly_and_not_expanded():
    """A document that gives the year means that year, not every year."""

    document = (
        "Comparison policy. Compare like weekdays. The relaunch fell on 2024-08-09 "
        "and that date stands alone."
    )
    answer = {
        "same_day_of_week": {"enabled": True, "days": ["Tuesday"]},
        "business_event": {
            "enabled": True,
            "events": [{"name": "Relaunch", "dates": ["2024-08-09"]}],
        },
    }
    draft = draft_from(document, answer)

    assert event_dates(draft, "Relaunch") == ["2024-08-09"]
    assert draft.needs_review is False


@pytest.mark.parametrize(
    "weekday, iso",
    [("Monday", 1), ("Thursday", 4), ("Sunday", 7)],
)
def test_no_weekday_is_privileged_over_another(weekday: str, iso: int):
    """The same document shape must work whichever weekday it names."""

    document = (
        f"Comparison policy. {weekday} trading is distinctive and is compared with "
        f"prior {weekday}s rather than with the previous seven days."
    )
    draft = draft_from(
        document, {"same_day_of_week": {"enabled": True, "days": [weekday]}}
    )
    assert draft.config.same_day_of_week.days == (iso,)
    assert draft.needs_review is False
