"""Company-scoped retrieval for bucket-configuration extraction.

The extraction prompt used to be "the first 12,000 characters of the document",
which is not retrieval -- it is truncation, and it has two costs a reviewer feels
directly:

* **Accuracy.** A KPI handbook spends most of its length on definitions,
  ownership, refresh cadence and glossary. The three paragraphs that actually
  state a comparison pattern ("Fridays run materially higher", "week 3 of the
  month", "the festival window") can sit anywhere in it, and a model reading
  page one while the pattern is on page nine returns an empty policy.
* **Latency and cost.** A self-hosted 8B model on CPU spends most of a
  three-minute request reading prose that cannot possibly contain a pattern.

So this module does the retrieval step: split the document into passages, score
each passage against the *vocabulary of comparison policy* -- not against a
company, a weekday or an event -- and hand the extractor only the passages that
scored, in document order, inside a character budget.

What makes this safe to do generically: the query is fixed and derived from the
five bucket slots the platform supports, so it is identical for every tenant. It
contains every weekday name and every month name because those are calendar
vocabulary, not company facts; which of them a company actually uses is decided
by the model reading that company's own document, and nothing here prefers Friday
over Tuesday. The scorer is lexical and deterministic -- no model runs in this
module -- so the same document always yields the same passages, which is what
makes an extraction reproducible when a reviewer asks why a slot was enabled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.copilot.text import chunk_text

#: Terms that signal a passage is *about* comparison policy. Weighted, because
#: "same day of week" is a far stronger signal than the bare word "day".
#:
#: Every weekday and every month appears with the same weight. That is the point:
#: the retrieval query cannot express a preference between companies, and a
#: document that only ever says "Tuesday" scores exactly as a document that only
#: ever says "Friday".
_SIGNAL_TERMS: dict[str, float] = {
    # -- the five slots, named the way documents name them ------------------
    "day of week": 6.0,
    "day-of-week": 6.0,
    "weekday": 5.0,
    "same weekday": 6.0,
    "week of month": 6.0,
    "week-of-month": 6.0,
    "month of year": 4.0,
    "season": 5.0,
    "seasonal": 5.0,
    "seasonality": 5.0,
    "festival": 5.0,
    "event": 4.0,
    "campaign": 3.5,
    "promotion": 3.0,
    "promotional": 3.0,
    "sale": 2.0,
    "holiday": 4.0,
    "year over year": 5.0,
    "year-over-year": 5.0,
    "yoy": 5.0,
    "anniversary": 4.0,
    "same period last year": 5.0,
    # -- the language of comparison itself ----------------------------------
    "comparable": 4.5,
    "comparison": 4.0,
    "compare": 3.5,
    "baseline": 4.0,
    "reference": 3.0,
    "expected": 3.0,
    "historical": 3.0,
    "history": 2.5,
    "pattern": 4.0,
    "recurring": 4.0,
    "cycle": 3.0,
    "cyclical": 3.0,
    "peak": 3.5,
    "trough": 3.0,
    "spike": 3.0,
    "uplift": 3.5,
    "calendar": 4.0,
    "fiscal": 3.0,
    "quarter": 2.5,
    "trend": 2.5,
    "growth": 2.0,
    "normal behaviour": 4.0,
    "normal behavior": 4.0,
    # -- calendar vocabulary: complete, unweighted between members ----------
    "monday": 3.0,
    "tuesday": 3.0,
    "wednesday": 3.0,
    "thursday": 3.0,
    "friday": 3.0,
    "saturday": 3.0,
    "sunday": 3.0,
    "weekend": 3.0,
    "january": 2.0,
    "february": 2.0,
    "march": 2.0,
    "april": 2.0,
    "may": 0.0,  # too common as a modal verb to carry signal
    "june": 2.0,
    "july": 2.0,
    "august": 2.0,
    "september": 2.0,
    "october": 2.0,
    "november": 2.0,
    "december": 2.0,
}

#: An ISO date in a passage is worth reading: business-event windows are stated
#: as dates, and the extractor is forbidden from inventing them.
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

#: Passages shorter than this are headings or fragments; they score but cannot
#: carry a policy statement on their own.
_MIN_PASSAGE_CHARS = 40


@dataclass(frozen=True)
class RetrievedPassage:
    """One scored passage, with enough identity to cite it back to a reviewer."""

    ordinal: int
    text: str
    score: float
    matched: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "ordinal": self.ordinal,
            "score": round(self.score, 3),
            "matched_terms": list(self.matched),
            "characters": len(self.text),
            # A short excerpt, so a reviewer can see *which* sentence drove a
            # slot without the response carrying the whole document back.
            "excerpt": self.text[:240] + ("…" if len(self.text) > 240 else ""),
        }


@dataclass(frozen=True)
class Retrieval:
    """What the extractor will read, and what it decided to skip."""

    passages: tuple[RetrievedPassage, ...]
    total_passages: int
    document_characters: int
    selected_characters: int
    #: True when the document contained no passage that mentions comparison
    #: policy at all. The caller turns this into NEEDS_REVIEW rather than
    #: sending prose that cannot answer the question.
    empty: bool

    @property
    def text(self) -> str:
        """The passages, in document order, as one prompt-ready excerpt."""

        return "\n\n".join(passage.text for passage in self.passages)

    def as_dict(self) -> dict:
        return {
            "strategy": "lexical-passage-retrieval",
            "passages_in_document": self.total_passages,
            "passages_selected": len(self.passages),
            "document_characters": self.document_characters,
            "selected_characters": self.selected_characters,
            "selected": [passage.as_dict() for passage in self.passages],
        }


def _score_passage(text: str) -> tuple[float, tuple[str, ...]]:
    lowered = text.lower()
    score = 0.0
    matched: list[str] = []
    for term, weight in _SIGNAL_TERMS.items():
        if weight <= 0:
            continue
        occurrences = lowered.count(term)
        if not occurrences:
            continue
        matched.append(term)
        # Diminishing returns: a passage that says "Friday" nine times is about
        # Fridays, but it is not nine times more relevant than one that says it
        # once, and rewarding repetition would favour tables of raw data.
        score += weight * (1.0 + min(occurrences - 1, 4) * 0.25)

    dates = len(_ISO_DATE.findall(text))
    if dates:
        matched.append("iso-date")
        score += 4.0 * (1.0 + min(dates - 1, 4) * 0.25)

    # Distinct signals beat a single loud one: a passage naming a weekday *and*
    # a comparison verb is a policy statement; one naming a month twelve times
    # is a calendar listing.
    if len(matched) >= 3:
        score *= 1.25
    return score, tuple(matched)


def retrieve_policy_passages(
    document_text: str,
    *,
    char_budget: int,
    max_passages: int = 24,
    target_chars: int = 900,
) -> Retrieval:
    """Select the passages of ``document_text`` that discuss comparison policy.

    ``char_budget`` is the hard cap on what reaches the model. Passages are
    chosen by score but emitted in document order, because a policy read out of
    sequence reads differently -- an exception stated after a rule is part of the
    rule.
    """

    text = (document_text or "").strip()
    if not text:
        return Retrieval((), 0, 0, 0, empty=True)

    chunks = chunk_text(text, target_chars=target_chars)
    scored: list[RetrievedPassage] = []
    for chunk in chunks:
        if len(chunk.text) < _MIN_PASSAGE_CHARS:
            continue
        score, matched = _score_passage(chunk.text)
        if score <= 0:
            continue
        scored.append(
            RetrievedPassage(
                ordinal=chunk.ordinal, text=chunk.text, score=score, matched=matched
            )
        )

    if not scored:
        return Retrieval((), len(chunks), len(text), 0, empty=True)

    scored.sort(key=lambda item: -item.score)
    chosen: list[RetrievedPassage] = []
    used = 0
    for passage in scored[: max(1, max_passages)]:
        cost = len(passage.text) + 2
        if used + cost > char_budget and chosen:
            continue
        chosen.append(passage)
        used += cost

    chosen.sort(key=lambda item: item.ordinal)
    return Retrieval(
        passages=tuple(chosen),
        total_passages=len(chunks),
        document_characters=len(text),
        selected_characters=used,
        empty=False,
    )
