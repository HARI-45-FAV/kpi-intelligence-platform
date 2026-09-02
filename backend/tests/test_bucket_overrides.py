"""One company-wide policy, plus a policy for the measures a document singles out.

A handbook rarely states a single rule for everything. It states how the business
generally behaves and then names the exceptions: "orders peak in the third week,
unlike everything else". Reading only the general rule loses the exception;
reading only the exception loses everything else.

So an extraction may now produce more than one configuration: the company-wide
one, and one per measure the document gives a pattern of its own. The engine
already prefers a KPI-scoped configuration over the company-wide one
(:func:`app.services.detection.load_bucket_config_row`), so this adds rows rather
than a code path, and every measure the document does not single out keeps the
company-wide policy unchanged.

The boundary is the point of most of what follows. An override reaches the engine
through the same door as the company-wide policy, so it is put through the same
checks -- unknown keys quarantined, event dates grounded in the same document,
search budget forced to the platform's, payload validated against the same
five-slot contract. An override that skipped any of those would be the way around
the boundary rather than a convenience.

Nothing here names a measure, weekday or event that any real company uses. The
documents below invent their own vocabulary so that passing cannot depend on the
platform having learned a tenant's business.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.llm.config import get_llm_config
from app.llm.provider import LLMProvider, LLMResponse, LLMUsage
from app.services.bucket_extraction import extract_bucket_config


class ScriptedModel(LLMProvider):
    def __init__(self, payload: dict) -> None:
        super().__init__(get_llm_config())
        self.payload = payload

    async def generate(self, messages, tools=None, stream=False):
        return LLMResponse(
            text=json.dumps(self.payload),
            model="test/policy-reader",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=10),
        )

    async def aclose(self) -> None:
        return None


def draft_from(document: str, answer: dict):
    return asyncio.run(extract_bucket_config(document, provider=ScriptedModel(answer)))


#: A document that states a general pattern and one exception to it.
DOCUMENT = (
    "Comparison policy. Trading is compared with the same weekday in earlier weeks, "
    "because Tuesday behaves unlike the rest of the week. Handling Volume is the "
    "exception: it follows the position of the week within the month rather than the "
    "weekday, and settles in the third week. The Lantern Week promotion runs 15 21 oct "
    "every year."
)

GENERAL = {"enabled": True, "days": ["Tuesday"]}


def answer(**overrides: object) -> dict:
    body: dict = {"same_day_of_week": GENERAL}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# What an override is, and what it is not
# ---------------------------------------------------------------------------
def test_a_named_measure_gets_its_own_policy_without_changing_the_general_one():
    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {
                    "kpi": "Handling Volume",
                    "same_week_of_month": {"enabled": True, "weeks": [3]},
                }
            ]
        ),
    )

    # The company-wide policy is exactly what the document said generally.
    assert draft.config.same_day_of_week.days == (2,)
    assert draft.config.same_week_of_month.enabled is False
    assert draft.needs_review is False

    (override,) = draft.overrides
    assert override.label == "Handling Volume"
    assert override.config.same_week_of_month.weeks == (3,)
    assert override.needs_review is False
    # The override is a policy in its own right, not a patch onto the general one:
    # what it does not state is off, so approving it cannot silently inherit a
    # pattern a reviewer never read on that screen.
    assert override.config.same_day_of_week.enabled is False


def test_a_measure_the_document_only_mentions_is_not_an_override():
    """An override with no pattern is the model noticing a name, not reading a rule.

    Storing it would put a configuration in front of an approver that cannot
    select a single comparable date, and approving it would take that measure
    *off* the company-wide policy that currently serves it correctly.
    """

    draft = draft_from(
        DOCUMENT, answer(kpi_overrides=[{"kpi": "Handling Volume", "yoy_period": {"enabled": False}}])
    )

    assert draft.overrides == []
    assert any("states no comparison pattern of its own" in note for note in draft.notes)
    # And the company-wide extraction is untouched by the discarded override.
    assert draft.config.same_day_of_week.days == (2,)
    assert draft.needs_review is False


def test_an_override_without_a_name_is_discarded():
    draft = draft_from(
        DOCUMENT,
        answer(kpi_overrides=[{"same_week_of_month": {"enabled": True, "weeks": [3]}}]),
    )

    assert draft.overrides == []
    assert any("named no measure" in note for note in draft.notes)


def test_the_same_measure_twice_keeps_the_first_and_says_so():
    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {"kpi": "Handling Volume", "same_week_of_month": {"enabled": True, "weeks": [3]}},
                {"kpi": "handling volume", "same_week_of_month": {"enabled": True, "weeks": [1]}},
            ]
        ),
    )

    (override,) = draft.overrides
    assert override.config.same_week_of_month.weeks == (3,)
    assert any("only the first was kept" in note for note in draft.notes)


def test_a_document_with_no_overrides_behaves_exactly_as_before():
    draft = draft_from(DOCUMENT, answer())

    assert draft.overrides == []
    assert draft.as_dict()["kpi_overrides"] == []
    assert draft.needs_review is False


@pytest.mark.parametrize("label_key", ["kpi", "kpi_name", "name", "measure", "label"])
def test_the_measure_name_is_read_from_any_reasonable_key(label_key: str):
    """Which key a model uses for the name is not worth a failed extraction."""

    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {label_key: "Handling Volume", "same_week_of_month": {"enabled": True, "weeks": [3]}}
            ]
        ),
    )

    (override,) = draft.overrides
    assert override.label == "Handling Volume"
    # The name key is not mistaken for a slot on the way through.
    assert override.config.same_week_of_month.weeks == (3,)


# ---------------------------------------------------------------------------
# The boundary holds on the override path too
# ---------------------------------------------------------------------------
def test_an_override_cannot_smuggle_a_computed_number_past_the_contract():
    """The five-slot contract is what stops a model returning arithmetic.

    If an override were merged without validation, that key would be the way
    around it -- so the same quarantine runs here, and the attempt is reported on
    the override rather than swallowed.
    """

    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {
                    "kpi": "Handling Volume",
                    "same_week_of_month": {"enabled": True, "weeks": [3]},
                    "expected_value": 10_250_000,
                    "deviation_pct": 12.5,
                }
            ]
        ),
    )

    (override,) = draft.overrides
    assert "expected_value" not in override.payload
    assert "deviation_pct" not in override.payload
    assert override.needs_review is True
    assert any("outside the configuration contract" in r for r in override.review_reasons)


def test_an_override_cannot_shorten_the_search_budget():
    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {
                    "kpi": "Handling Volume",
                    "same_week_of_month": {"enabled": True, "weeks": [3]},
                    "lookback_days": 7,
                }
            ]
        ),
    )

    (override,) = draft.overrides
    assert override.config.lookback_days != 7
    assert any("search budget" in note for note in override.notes)


def test_an_override_event_date_is_grounded_in_the_same_document():
    """The date rules are the document's, not the slot's.

    "15-21 Oct" is stated, so it grounds and the platform supplies the years. A
    March date is not stated anywhere, so it is discarded and the override is the
    thing marked for review -- the company-wide policy is still fine.
    """

    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {
                    "kpi": "Handling Volume",
                    "business_event": {
                        "enabled": True,
                        "events": [
                            {"name": "Lantern Week", "dates": ["2019-10-15", "2019-10-18"]},
                            {"name": "Invented", "dates": ["2024-03-02"]},
                        ],
                    },
                }
            ]
        ),
    )

    (override,) = draft.overrides
    events = {event.name: event for event in override.config.business_event.events}
    lantern = [day.isoformat() for day in events["Lantern Week"].dates]
    assert lantern, "a window the document states must survive on the override path"
    assert {(d.month, d.day) for d in events["Lantern Week"].dates} == {(10, 15), (10, 18)}
    assert 2019 not in {d.year for d in events["Lantern Week"].dates}

    assert events["Invented"].dates == ()
    assert override.needs_review is True
    assert any("do not appear in the document" in note for note in override.notes)
    # The company-wide draft is unaffected by a bad date inside an override.
    assert draft.needs_review is False


def test_an_override_cannot_nest_further_overrides():
    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {
                    "kpi": "Handling Volume",
                    "same_week_of_month": {"enabled": True, "weeks": [3]},
                    "kpi_overrides": [
                        {"kpi": "Something Else", "yoy_period": {"enabled": True}}
                    ],
                }
            ]
        ),
    )

    assert [item.label for item in draft.overrides] == ["Handling Volume"]


def test_overrides_survive_a_document_that_states_no_general_policy():
    """A document may describe only its exceptions.

    The company-wide draft is then correctly empty and needs review, but the
    measure-specific policy it *did* state is the only usable thing in the
    document and must not be thrown away with it.
    """

    draft = draft_from(
        DOCUMENT,
        {
            "same_day_of_week": {"enabled": False},
            "kpi_overrides": [
                {"kpi": "Handling Volume", "same_week_of_month": {"enabled": True, "weeks": [3]}}
            ],
        },
    )

    assert draft.needs_review is True
    assert not draft.config.enabled_buckets
    (override,) = draft.overrides
    assert override.config.same_week_of_month.weeks == (3,)


def test_the_override_section_is_not_reported_as_a_boundary_violation():
    """``kpi_overrides`` is part of the contract, so returning it is not "loose"."""

    draft = draft_from(
        DOCUMENT,
        answer(
            kpi_overrides=[
                {"kpi": "Handling Volume", "same_week_of_month": {"enabled": True, "weeks": [3]}}
            ]
        ),
    )

    assert draft.rejected_keys == []
    assert draft.review_reasons == []
    # And it never reaches the validated company-wide payload as a slot.
    assert "kpi_overrides" not in draft.payload
