"""Structured, evidence-grounded explanation of one stored result.

This module answers "explain this" for two surfaces — a KPI result and one node
of an investigation — and it answers it from stored rows only. Nothing here
queries a company's source, recomputes a KPI, divides a share or invents a
contributor: every figure it prints was already measured, already stored, and is
already defensible.

**Why this exists as a deterministic service rather than a prompt.** The
platform's language model is off by default, and a feature that only works when
someone configures an LLM endpoint is a feature that does not work. So the
sections are *assembled* here from governed evidence, and the model — when one is
configured — is asked to re-narrate those same sections from those same facts.
With no model the reader still gets the explanation; the prose is the platform's
own and is labelled as such. Either way the numbers come from one place.

**What the explanation is allowed to see** is decided by the caller's own
permissions, re-derived per request:

* the stored detection run — ``analytics.read``;
* its statistics (median, MAD, modified z, tolerance) — ``kpi.read``;
* the stored contribution breakdown — ``investigation.read``;
* approved business documents — ``document.read``.

A section whose evidence the caller may not read is not silently dropped: it says
what is missing and why, because "you are not entitled to this" and "this does
not exist" are different answers and a reader deserves to know which one they got.

**Two sentences this module will not write.** It never says a contributor
*caused* a movement — the wording is "accounts for" throughout — and it never
implies a statistical test fired when it did not. That second one matters on this
platform in particular: a movement can be ABNORMAL on materiality alone while the
modified z-score sits well inside its threshold, and a summary that says
"statistically significant" over that result would be false.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import AccessContext
from app.models.base import DetectionStatus
from app.models.detection import ContributionRun, DetectionRun
from app.models.investigation import InvestigationFinding

# ---------------------------------------------------------------------------
# Section names. Fixed, and in the order a reader asks the questions.
# ---------------------------------------------------------------------------
RESULT_SECTIONS: tuple[str, ...] = (
    "WHAT HAPPENED",
    "WHY IT WAS FLAGGED",
    "TOP CONTRIBUTORS",
    "SUPPORTING BUSINESS CONTEXT",
    "EVIDENCE LIMITATIONS",
    "CONFIDENCE LEVEL",
    "RECOMMENDED NEXT STEP",
)

NODE_SECTIONS: tuple[str, ...] = (
    "WHAT HAPPENED",
    "WHY THIS AREA MATTERS",
    "CONTRIBUTION TO THE MOVEMENT",
    "AVAILABLE BUSINESS CONTEXT",
    "EVIDENCE LIMITATIONS",
    "CONFIDENCE LEVEL",
    "RECOMMENDED NEXT STEP",
)

#: The verdicts this platform issues. A stored row carrying anything else is
#: legacy data from an earlier schema, and it is reported as unrecognised rather
#: than quietly folded into one of these.
KNOWN_STATUSES = frozenset(str(s) for s in DetectionStatus)

_STATUS_MEANING = {
    "NORMAL": "in line with comparable history",
    "ABNORMAL": "outside what this KPI tolerates against comparable history",
    "LOW_CONFIDENCE": "not judgeable — there was not enough comparable history",
}

#: Said out loud wherever it applies. A company with no approved comparison
#: policy is compared against a plain recent-days window, and an explanation that
#: presented that as the company's chosen basis would be misleading.
_FALLBACK_BASIS_NOTE = (
    "This company has no approved comparison policy in force, so the expected "
    "value came from the platform's documented recent-period fallback rather than "
    "from a comparison basis the business approved."
)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------
def format_value(
    value: float | int | None, unit: str | None = None, currency: str | None = None
) -> str:
    """A measured value, in its own unit, with no arithmetic applied.

    Thousands are grouped and a currency is named by its code rather than a
    symbol: ``INR 12,500,000`` is unambiguous in a sentence that may be read by
    someone in another market, where a bare ``₹`` glyph in plain text is not.
    """

    return _render(value, unit, currency, signed=False)


def format_signed(
    value: float | int | None, unit: str | None = None, currency: str | None = None
) -> str:
    """The same, with an explicit sign — for a movement, where direction is the point."""

    return _render(value, unit, currency, signed=True)


def _render(
    value: float | int | None, unit: str | None, currency: str | None, *, signed: bool
) -> str:
    if value is None:
        return "not available"
    magnitude = abs(value)
    decimals = 0 if magnitude >= 100 else (1 if magnitude >= 1 else 3)
    # The sign sits with the digits, never before the currency code: "INR -1,234"
    # is a negative amount of rupees, where "-INR 1,234" reads as a negated code.
    sign = ("-" if value < 0 else "+") if signed else ("-" if value < 0 else "")
    rendered = f"{sign}{magnitude:,.{decimals}f}"
    code = currency or ("INR" if unit == "currency" else None)
    if code:
        return f"{code} {rendered}"
    if unit and unit not in {"currency", "count", "number"}:
        return f"{rendered} {unit}"
    return rendered


def format_pct(value: float | int | None, *, signed: bool = True) -> str:
    if value is None:
        return "not available"
    sign = "+" if (signed and value >= 0) else ("-" if signed else "")
    return f"{sign}{abs(value):.1f}%"


def _direction(deviation: float | None) -> str:
    if deviation is None:
        return "moved"
    if deviation > 0:
        return "came in above"
    if deviation < 0:
        return "came in below"
    return "landed exactly on"


def kpi_label(name: str | None) -> str:
    """A KPI's name as a person should read it inside a sentence.

    The same rule the frontend's ``formatKpiName`` applies, restated here because
    this module writes prose rather than shipping a name to a heading: a sentence
    reading "net_revenue was 196" is a technical key in front of a business
    reader. Separators become spaces and each word takes a capital, while a short
    all-caps token (an acronym like AOV) and a deliberately mixed-case word
    (eCommerce) are left as their author wrote them.

    Presentation only. ``kpi_key`` is untouched and is still what the API filters
    and matches on.
    """

    if not name:
        return "This KPI"
    words = [word for word in re.split(r"[\s_\-.]+", name.strip()) if word]
    if not words:
        return "This KPI"
    rendered: list[str] = []
    for word in words:
        has_lower = any(character.islower() for character in word)
        has_upper = any(character.isupper() for character in word)
        if (has_upper and not has_lower and len(word) <= 4) or (has_upper and has_lower):
            rendered.append(word)
        else:
            rendered.append(word[:1].upper() + word[1:].lower())
    return " ".join(rendered)


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Confidence:
    """How much weight the explanation itself carries, and why.

    Deliberately not a probability. Nothing in this platform estimates one, so a
    number here would be invented precision. It is a three-level judgement with
    every reason that produced it listed beside it, so a reader can disagree with
    the judgement while still trusting the facts.
    """

    level: str  # HIGH | MEDIUM | LOW
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "reasons": list(self.reasons)}


def _confidence(
    run: DetectionRun,
    contribution: ContributionRun | None,
    *,
    statistics_visible: bool,
) -> Confidence:
    """Rate the explanation from the evidence actually behind it.

    Starts at HIGH and is reduced by each thing that genuinely weakens it. The
    rules are fixed and listed so the same evidence always produces the same
    level.
    """

    score = 2
    reasons: list[str] = []

    if run.status == str(DetectionStatus.LOW_CONFIDENCE):
        score = 0
        reasons.append(
            "The detection engine itself returned LOW CONFIDENCE for this result, "
            "so the measurement stands but the verdict does not."
        )
    elif run.status not in KNOWN_STATUSES:
        score = 0
        reasons.append(
            f"The stored verdict '{run.status}' is not one this platform issues, so "
            "it cannot be interpreted."
        )

    references = run.reference_count or 0
    if score > 0:
        if references < 3:
            score = 0
            reasons.append(
                f"Only {references} comparable period(s) were available, below the "
                "minimum this platform treats as judgeable."
            )
        elif references < 5:
            score -= 1
            reasons.append(
                f"{references} comparable periods is a thin basis, though above the "
                "minimum."
            )
        else:
            reasons.append(f"{references} comparable periods were available.")

    if run.bucket_applied == "TRAILING_PERIOD" and score > 0:
        score -= 1
        reasons.append(
            "The comparison used the recent-period fallback rather than an approved "
            "comparison policy."
        )

    if contribution is not None:
        if (contribution.withheld_count or 0) > 0 and score > 0:
            score -= 1
            reasons.append(
                f"{contribution.withheld_count} value(s) were withheld by your access "
                "scope, so the visible parts do not add up to the whole movement."
            )
        unexplained = contribution.unexplained_pct
        if unexplained is not None and abs(unexplained) > 40 and score > 0:
            score -= 1
            reasons.append(
                f"{abs(unexplained):.1f}% of the movement is not accounted for by the "
                "ranked contributors."
            )
        if not contribution.additive:
            reasons.append(
                "This KPI's parts do not sum to its whole, so shares are indicative "
                "of relative size rather than an exact decomposition."
            )
    else:
        reasons.append(
            "No stored breakdown was available, so nothing here attributes the "
            "movement to any part of the business."
        )

    if not statistics_visible:
        reasons.append(
            "The detection statistics are not visible to your role, so the flagging "
            "test could not be restated in full."
        )

    level = "HIGH" if score >= 2 else ("MEDIUM" if score == 1 else "LOW")
    return Confidence(level=level, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# The assembled explanation
# ---------------------------------------------------------------------------
@dataclass
class StructuredExplanation:
    """The labelled sections, the facts behind them, and what was withheld."""

    subject: str
    scope: str
    sections: dict[str, str]
    order: tuple[str, ...]
    facts: dict[str, Any]
    citations: list[dict[str, Any]]
    confidence: Confidence
    limitations: list[str] = field(default_factory=list)
    #: True when a language model wrote the prose. False means these sections are
    #: the platform's own deterministic assembly, which is stated to the reader.
    model_written: bool = False
    model: str | None = None

    def as_text(self) -> str:
        return "\n\n".join(
            f"{name}\n{self.sections[name]}" for name in self.order if self.sections.get(name)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "scope": self.scope,
            "order": list(self.order),
            "sections": [
                {"heading": name, "body": self.sections.get(name, "")} for name in self.order
            ],
            "text": self.as_text(),
            "facts": self.facts,
            "citations": self.citations,
            "confidence": self.confidence.as_dict(),
            "limitations": list(self.limitations),
            "model_written": self.model_written,
            "model": self.model,
        }


# ---------------------------------------------------------------------------
# Evidence gathering. Every read is company-scoped and permission-gated.
# ---------------------------------------------------------------------------
def latest_contribution(
    session: Session,
    access: AccessContext,
    run: DetectionRun,
    *,
    dimension: str | None = None,
    path: list[dict[str, Any]] | None = None,
) -> ContributionRun | None:
    """The stored breakdown for this movement, narrowed to a node when asked.

    Requires ``investigation.read``: a breakdown names parts of the business, and
    the permission that governs seeing those parts governs seeing them here too.
    Nothing is computed — if no analysis was ever run for this movement, the
    answer is that none was run.
    """

    if not access.has("investigation.read"):
        return None
    stmt = (
        select(ContributionRun)
        .where(
            ContributionRun.company_id == access.company.id,
            ContributionRun.kpi_key == run.kpi_key,
            ContributionRun.target_date == run.target_date,
        )
        .order_by(ContributionRun.executed_at.desc())
    )
    if dimension:
        stmt = stmt.where(ContributionRun.dimension == dimension)
    rows = list(session.scalars(stmt.limit(25)))
    if not rows:
        return None
    if path is not None:
        wanted = _normalise_path(path)
        for row in rows:
            if _normalise_path(row.path) == wanted:
                return row
        # A node with no stored breakdown of its own is a normal state: the parent
        # analysis is the closest governed evidence, and returning it is better
        # than returning nothing and inviting an estimate.
        return None if wanted else rows[0]
    return rows[0]


def _normalise_path(path: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(path, list):
        return ()
    steps: list[tuple[str, str]] = []
    for step in path:
        if isinstance(step, dict):
            dimension = str(step.get("dimension") or "")
            value = str(step.get("value") or "")
            if dimension and value:
                steps.append((dimension, value))
    return tuple(steps)


def findings_for(
    session: Session, access: AccessContext, run: DetectionRun
) -> list[InvestigationFinding]:
    """What people have already concluded about this movement."""

    if not access.has("investigation.read"):
        return []
    return list(
        session.scalars(
            select(InvestigationFinding)
            .where(
                InvestigationFinding.company_id == access.company.id,
                InvestigationFinding.kpi_key == run.kpi_key,
                InvestigationFinding.target_date == run.target_date,
            )
            .order_by(InvestigationFinding.created_at.desc())
        )
    )


def _statistics(run: DetectionRun, access: AccessContext) -> dict[str, Any] | None:
    """The stored statistics, for a caller entitled to read them.

    The same ``kpi.read`` gate the detection API applies to its ``evidence``
    block. Read from the run's own columns rather than from the JSON blob so a
    row written by an older version still explains itself.
    """

    if not access.has("kpi.read"):
        return None
    return {
        "median": run.median_value,
        "mad": run.mad,
        "dispersion_basis": run.dispersion_basis,
        "modified_z_score": run.modified_z_score,
        "z_threshold": run.z_threshold,
        "statistically_significant": bool(run.statistically_significant),
        "tolerance_pct": run.tolerance_pct,
        "tolerance_absolute": run.tolerance_absolute,
        "breached_tolerance": bool(run.breached_tolerance),
        "reference_count": run.reference_count,
        "reference_dates": list(run.reference_dates or []),
        "reference_values": list(run.reference_values or []),
        "bucket_applied": run.bucket_applied,
        "buckets_applied": list(run.buckets_applied or []),
        "bucket_config_key": run.bucket_config_key,
        "bucket_config_version": run.bucket_config_version,
        "yoy_applied": bool(run.yoy_applied),
        "yoy_adjustment_factor": run.yoy_adjustment_factor,
        "method": run.method,
        "reason": run.reason,
    }


# ---------------------------------------------------------------------------
# Section writers
# ---------------------------------------------------------------------------
def _what_happened(run: DetectionRun) -> str:
    unit, currency = run.unit, run.currency
    verdict = _STATUS_MEANING.get(run.status)
    lines = [
        f"On {run.target_date.isoformat()}, {kpi_label(run.kpi_name)} was "
        f"{format_value(run.actual_value, unit, currency)} against an expected "
        f"{format_value(run.expected_value, unit, currency)} — a movement of "
        f"{format_signed(run.deviation_absolute, unit, currency)} "
        f"({format_pct(run.deviation_pct)}), which "
        f"{_direction(run.deviation_pct)} expectation."
    ]
    if verdict:
        lines.append(f"The platform's verdict is {run.status.replace('_', ' ')}: {verdict}.")
    else:
        lines.append(
            f"The stored verdict is '{run.status}', which is not one of this "
            "platform's three verdicts and cannot be interpreted."
        )
    if run.comparison_label:
        lines.append(f"Expected was derived from: {run.comparison_label}.")
    return " ".join(lines)


def _why_flagged(run: DetectionRun, statistics: dict[str, Any] | None) -> str:
    if statistics is None:
        return (
            "The detection statistics behind this verdict are governed by the "
            "kpi.read permission, which your role does not hold, so the tests that "
            "produced it cannot be restated here. The verdict itself and the "
            "comparison basis above are what your role may see."
        )

    parts: list[str] = []
    count = statistics["reference_count"] or 0
    dates = statistics["reference_dates"]
    basis = statistics["bucket_applied"]
    parts.append(
        f"The engine selected {count} comparable period(s) using the "
        f"{str(basis).replace('_', ' ').lower()} basis"
        + (f" ({', '.join(str(d) for d in dates[:8])}" if dates else "")
        + (f", and {len(dates) - 8} more)" if dates and len(dates) > 8 else (")" if dates else ""))
        + "."
    )
    parts.append(
        "Their robust median was "
        f"{format_value(statistics['median'], run.unit, run.currency)}, with a median "
        f"absolute deviation of {format_value(statistics['mad'], run.unit, run.currency)}"
        + (
            f" (dispersion basis: {statistics['dispersion_basis']})"
            if statistics["dispersion_basis"]
            else ""
        )
        + "."
    )

    # Both tests, separately, and neither implied by the other. A movement can be
    # material without being statistically significant and the reverse, and the
    # honest reading is the one that names which test actually fired.
    z_value = statistics["modified_z_score"]
    threshold = statistics["z_threshold"]
    if z_value is not None and threshold is not None:
        significant = statistics["statistically_significant"]
        parts.append(
            f"The modified z-score is {z_value:.2f} against a threshold of "
            f"{threshold:.2f}, so the statistical test "
            + ("was met" if significant else "was not met")
            + "."
        )
    else:
        parts.append(
            "No modified z-score was computed for this result, so the statistical "
            "test did not contribute to the verdict."
        )

    breached = statistics["breached_tolerance"]
    tolerance_pct = statistics["tolerance_pct"]
    parts.append(
        "The materiality test compares the movement against this KPI's own "
        + (
            f"tolerance of {format_pct(tolerance_pct, signed=False)}"
            if tolerance_pct is not None
            else "configured tolerance"
        )
        + (
            f" and an absolute tolerance of "
            f"{format_value(statistics['tolerance_absolute'], run.unit, run.currency)}"
            if statistics["tolerance_absolute"] is not None
            else ""
        )
        + "; that tolerance "
        + ("was breached" if breached else "was not breached")
        + "."
    )

    if statistics["yoy_applied"]:
        factor = statistics["yoy_adjustment_factor"]
        parts.append(
            "Year-over-year references were re-based onto the current level"
            + (f" by a factor of {factor:.3f}" if factor is not None else "")
            + "."
        )

    if run.reason:
        parts.append(f"The engine recorded: {run.reason}")

    if basis == "TRAILING_PERIOD":
        parts.append(_FALLBACK_BASIS_NOTE)

    return " ".join(parts)


def _contributors_section(
    run: DetectionRun,
    contribution: ContributionRun | None,
    access: AccessContext,
) -> str:
    if not access.has("investigation.read"):
        return (
            "A breakdown of this movement across the business is governed by the "
            "investigation.read permission, which your role does not hold."
        )
    if contribution is None:
        return (
            "No breakdown has been run for this movement, so no part of the business "
            "is attributed here. Opening the Investigation Center and analysing this "
            "date produces one; until then, attributing the movement to any region, "
            "product or channel would be guesswork."
        )

    rows = [row for row in (contribution.contributors or []) if isinstance(row, dict)]
    if not rows:
        return (
            f"A breakdown by {contribution.dimension} was run for this movement but "
            "returned no parts within your access scope."
        )

    lines = [
        f"Broken down by {contribution.dimension}, "
        f"{len(rows)} of {contribution.ranked_count} part(s) are ranked."
    ]
    for index, row in enumerate(rows[:5], start=1):
        label = row.get("label") or row.get("entity") or "unnamed"
        change = row.get("change")
        share = row.get("share_pct")
        piece = (
            f"{index}. {label}: "
            f"{format_signed(change, run.unit, run.currency)}"
        )
        if share is not None and contribution.shares_available:
            piece += f", accounting for {format_pct(share)} of the observed movement"
        actual, expected = row.get("actual"), row.get("expected")
        if actual is not None and expected is not None:
            piece += (
                f" ({format_value(actual, run.unit, run.currency)} against a usual "
                f"{format_value(expected, run.unit, run.currency)})"
            )
        lines.append(piece + ".")

    if contribution.explained_pct is not None:
        lines.append(
            f"Together the ranked parts account for "
            f"{abs(contribution.explained_pct):.1f}% of the movement."
        )
    # The one sentence this section exists to protect.
    lines.append(
        "These are shares of an observed movement, not causes of it: a part that "
        "accounts for most of a movement is where the movement sits, not why it "
        "happened."
    )
    return " ".join(lines)


def _node_contribution_section(
    run: DetectionRun,
    contribution: ContributionRun | None,
    entity: str | None,
    access: AccessContext,
) -> str:
    if not access.has("investigation.read"):
        return (
            "This breakdown is governed by the investigation.read permission, which "
            "your role does not hold."
        )
    if contribution is None:
        return (
            "No stored breakdown covers this selection, so nothing here quantifies "
            "its contribution. Running the analysis for this node produces one."
        )
    rows = [row for row in (contribution.contributors or []) if isinstance(row, dict)]
    if entity:
        match = next(
            (
                row
                for row in rows
                if str(row.get("entity") or row.get("label") or "") == entity
            ),
            None,
        )
        if match is None:
            return (
                f"{entity} does not appear in the stored ranking for "
                f"{contribution.dimension} on {run.target_date.isoformat()}, so its "
                "contribution is not quantified in the governed evidence."
            )
        share = match.get("share_pct")
        text = (
            f"{entity} moved {format_signed(match.get('change'), run.unit, run.currency)} "
            f"against a usual {format_value(match.get('expected'), run.unit, run.currency)}"
        )
        if share is not None and contribution.shares_available:
            text += f", accounting for {format_pct(share)} of the observed movement"
        text += (
            f". The whole movement being apportioned is "
            f"{format_signed(run.deviation_absolute, run.unit, run.currency)}."
        )
        if match.get("note"):
            text += f" The engine noted: {match['note']}"
        return text + (
            " A share is a size, not a cause: this says where the movement sits, "
            "not why."
        )
    return _contributors_section(run, contribution, access)


def _why_area_matters(
    run: DetectionRun, contribution: ContributionRun | None, entity: str | None
) -> str:
    if entity is None:
        return (
            f"This is {kpi_label(run.kpi_name)}'s whole movement for "
            f"{run.target_date.isoformat()} — the total that every part below is "
            "measured against."
        )
    if contribution is None:
        return (
            f"{entity} was selected from this KPI's approved dimensions. Its size "
            "relative to the KPI is not quantified here because no stored breakdown "
            "covers this selection."
        )
    rows = [row for row in (contribution.contributors or []) if isinstance(row, dict)]
    match = next(
        (row for row in rows if str(row.get("entity") or row.get("label") or "") == entity),
        None,
    )
    if match is None:
        return (
            f"{entity} is a value of {contribution.dimension}, one of the dimensions "
            f"{kpi_label(run.kpi_name)} is registered to be broken down by. It does not appear "
            "in the stored ranking for this date."
        )
    rank = rows.index(match) + 1
    lead = (
        f"{entity} ranks {rank} of {contribution.ranked_count} by contribution to "
        f"this movement"
    )
    # ``rank == 1`` is the ranking's own leader; comparing against
    # ``leader_entity`` would be comparing a raw entity value against a stored
    # *label*, which are not always the same string.
    if rank == 1 and contribution.leader_is_sufficient:
        lead += (
            ", and on its own accounts for more of the movement than this platform "
            "treats as a sufficient explanation"
        )
    return (
        lead
        + f". It is a value of {contribution.dimension}, a dimension "
        f"{kpi_label(run.kpi_name)} is registered to be broken down by."
    )


def _business_context(citations: list[dict[str, Any]], access: AccessContext) -> str:
    if not access.has("document.read"):
        return (
            "Approved business documents are governed by the document.read "
            "permission, which your role does not hold, so no business context is "
            "attached to this explanation."
        )
    if not citations:
        return (
            "No approved business document in this company matched this KPI, date or "
            "dimension, so there is no documented business context to offer. Absence "
            "of a document is not evidence that nothing happened — it means nothing "
            "was recorded here that the platform may read."
        )
    lines = ["The following approved documents are the governed business context available:"]
    for index, item in enumerate(citations, start=1):
        label = item.get("label") or item.get("title") or "document"
        snippet = (item.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:240].rstrip() + "…"
        lines.append(f"[E{index}] {label}" + (f": {snippet}" if snippet else "") + ".")
    lines.append(
        "This is documented context, not an explanation of the movement: nothing "
        "here establishes that the two are connected."
    )
    return " ".join(lines)


def _limitations(
    run: DetectionRun,
    contribution: ContributionRun | None,
    statistics: dict[str, Any] | None,
    access: AccessContext,
    citations: list[dict[str, Any]],
) -> list[str]:
    items: list[str] = []
    if statistics is None:
        items.append(
            "The detection statistics were not readable by your role, so the flagging "
            "test is summarised only by its verdict."
        )
    elif run.bucket_applied == "TRAILING_PERIOD":
        items.append(_FALLBACK_BASIS_NOTE)

    references = run.reference_count or 0
    if references < 5:
        items.append(
            f"Only {references} comparable period(s) stood behind the expected value."
        )

    if not access.has("investigation.read"):
        items.append(
            "Your role cannot read investigations, so no part of the business is "
            "attributed in this explanation."
        )
    elif contribution is None:
        items.append(
            "No breakdown has been run for this movement, so no contributor is named."
        )
    else:
        if (contribution.withheld_count or 0) > 0:
            items.append(
                f"{contribution.withheld_count} value(s) were withheld by your row "
                "access scope, so the visible shares do not sum to the whole movement."
            )
        if contribution.unexplained_pct is not None and abs(contribution.unexplained_pct) > 5:
            items.append(
                f"{abs(contribution.unexplained_pct):.1f}% of the movement is not "
                "accounted for by the ranked parts shown."
            )
        if not contribution.additive:
            items.append(
                "This KPI's parts do not sum to its whole, so shares indicate "
                "relative size rather than an exact decomposition."
            )
        for warning in contribution.warnings or []:
            if isinstance(warning, str) and warning:
                items.append(warning)

    if not citations:
        items.append(
            "No approved business document was attached, so no documented reason for "
            "the movement is available."
        )

    items.append(
        "Contribution is not causation. This explanation reports what was measured "
        "and what is documented; it does not establish why the movement happened."
    )
    return items


def _recommended_next_step(
    run: DetectionRun,
    contribution: ContributionRun | None,
    access: AccessContext,
    *,
    entity: str | None = None,
) -> str:
    """What to do next, derived from the same stored evidence as every other section.

    A recommendation is where an explanation is most tempted to become a verdict, so
    this one is deliberately an instruction to the reader rather than a claim about
    the business: the next step to *take*, never the reason the movement happened.
    The engine measures shares of a movement, and a share stays a size here too —
    "start with the largest part" is a search order, and saying so is not the same as
    saying that part moved the total.

    Fixed rules over the stored run and its stored breakdown, so the same evidence
    always yields the same step and nothing is generated. It also respects the
    caller's scope: without ``investigation.read`` there is no ranking to point at,
    so the step is to ask someone who has it rather than to name a part this reader
    was never shown.
    """

    steps: list[str] = []
    may_investigate = access.has("investigation.read")
    label = kpi_label(run.kpi_name)
    references = run.reference_count or 0

    if run.status not in KNOWN_STATUSES:
        steps.append(
            f"Do not action this row. The stored verdict '{run.status}' is not one "
            f"this platform issues, so re-run {label} for "
            f"{run.target_date.isoformat()} to obtain one that can be read."
        )
    elif run.status == str(DetectionStatus.LOW_CONFIDENCE):
        steps.append(
            f"Hold this result rather than acting on it: {references} comparable "
            "period(s) is below what this platform treats as judgeable. Register "
            "more history for this KPI, or widen its comparison policy so more "
            "periods qualify as comparable, then re-run the date."
        )
    elif run.status == str(DetectionStatus.NORMAL):
        steps.append(
            f"No action indicated. {label} sits inside what it tolerates against "
            "comparable history for this date, so there is nothing here to "
            "investigate."
        )
    else:
        # ABNORMAL. The size is established; what is missing is where to look.
        top = None
        if contribution is not None:
            rows = [row for row in (contribution.contributors or []) if isinstance(row, dict)]
            top = rows[0] if rows else None

        if not may_investigate:
            steps.append(
                f"Ask a colleague who holds investigation access to break this "
                f"movement down along an approved dimension: locating it needs a "
                f"ranking your role may not read."
            )
        elif top is not None and contribution is not None:
            part = top.get("label") or top.get("entity") or "the top-ranked part"
            share = top.get("share_pct")
            opener = (
                f"Start with {part} in the {contribution.dimension} breakdown"
                if entity is None or part != entity
                else f"Stay with {part} in the {contribution.dimension} breakdown"
            )
            if share is not None and contribution.shares_available:
                steps.append(
                    f"{opener}: it accounts for {format_pct(share)} of the observed "
                    "movement, which makes it the largest single part to confirm or "
                    "rule out first."
                )
            else:
                steps.append(f"{opener}: it is the largest ranked part of the movement.")
            steps.append(
                "Check it against the business calendar and any approved document "
                "for the date, then record what you conclude as a finding. The share "
                "is a size, not an explanation."
            )
        else:
            steps.append(
                "Break this date down along an approved dimension in the "
                "Investigation Center. Until a breakdown exists, no part of the "
                "business is attributed here and there is nothing to check first."
            )

    # Conditions that change *how* to read the step above, appended only when they
    # actually hold for this run.
    if run.status in KNOWN_STATUSES and run.status != str(DetectionStatus.LOW_CONFIDENCE):
        if 0 < references < 5:
            steps.append(
                f"Treat the size cautiously: only {references} comparable periods "
                "were available."
            )
        if run.bucket_applied == "TRAILING_PERIOD":
            steps.append(
                "Approving a comparison policy for this KPI would put its expected "
                "value on a basis the business chose rather than the platform's "
                "recent-period fallback."
            )
    if contribution is not None and (contribution.withheld_count or 0) > 0 and may_investigate:
        steps.append(
            f"{contribution.withheld_count} value(s) sit outside your row access "
            "scope, so confirm the ranking with someone whose scope covers them "
            "before treating it as complete."
        )

    return " ".join(steps)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_result_explanation(
    session: Session,
    access: AccessContext,
    run: DetectionRun,
    *,
    citations: list[dict[str, Any]] | None = None,
) -> StructuredExplanation:
    """Explain one KPI result, from its stored run and whatever governed evidence
    the caller is entitled to see."""

    statistics = _statistics(run, access)
    contribution = latest_contribution(session, access, run)
    cites = citations or []
    limitations = _limitations(run, contribution, statistics, access, cites)
    confidence = _confidence(run, contribution, statistics_visible=statistics is not None)

    sections = {
        "WHAT HAPPENED": _what_happened(run),
        "WHY IT WAS FLAGGED": _why_flagged(run, statistics),
        "TOP CONTRIBUTORS": _contributors_section(run, contribution, access),
        "SUPPORTING BUSINESS CONTEXT": _business_context(cites, access),
        "EVIDENCE LIMITATIONS": " ".join(limitations),
        "CONFIDENCE LEVEL": (
            f"{confidence.level}. " + " ".join(confidence.reasons)
        ),
        "RECOMMENDED NEXT STEP": _recommended_next_step(run, contribution, access),
    }

    return StructuredExplanation(
        subject=f"{kpi_label(run.kpi_name)} · {run.target_date.isoformat()}",
        scope="result",
        sections=sections,
        order=RESULT_SECTIONS,
        facts=_facts(run, contribution, statistics, entity=None),
        citations=cites,
        confidence=confidence,
        limitations=limitations,
    )


def build_node_explanation(
    session: Session,
    access: AccessContext,
    run: DetectionRun,
    *,
    dimension: str | None = None,
    entity: str | None = None,
    path: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> StructuredExplanation:
    """Explain one node of an investigation — the KPI's whole movement, or one
    part of it — from the stored breakdown that covers it."""

    statistics = _statistics(run, access)
    contribution = latest_contribution(
        session, access, run, dimension=dimension, path=path
    )
    if contribution is None and dimension:
        # Fall back to any stored breakdown for this movement rather than none: the
        # parent ranking is real governed evidence about this node's neighbourhood,
        # and the section says which level it came from.
        contribution = latest_contribution(session, access, run)
    cites = citations or []
    limitations = _limitations(run, contribution, statistics, access, cites)
    confidence = _confidence(run, contribution, statistics_visible=statistics is not None)

    scope_label = (
        f"{dimension}: {entity}" if dimension and entity else (f"By {dimension}" if dimension else "Whole KPI movement")
    )
    sections = {
        "WHAT HAPPENED": _what_happened(run),
        "WHY THIS AREA MATTERS": _why_area_matters(run, contribution, entity),
        "CONTRIBUTION TO THE MOVEMENT": _node_contribution_section(
            run, contribution, entity, access
        ),
        "AVAILABLE BUSINESS CONTEXT": _business_context(cites, access),
        "EVIDENCE LIMITATIONS": " ".join(limitations),
        "CONFIDENCE LEVEL": f"{confidence.level}. " + " ".join(confidence.reasons),
        "RECOMMENDED NEXT STEP": _recommended_next_step(
            run, contribution, access, entity=entity
        ),
    }

    return StructuredExplanation(
        subject=f"{kpi_label(run.kpi_name)} · {run.target_date.isoformat()} · {scope_label}",
        scope="node",
        sections=sections,
        order=NODE_SECTIONS,
        facts=_facts(run, contribution, statistics, entity=entity),
        citations=cites,
        confidence=confidence,
        limitations=limitations,
    )


def _facts(
    run: DetectionRun,
    contribution: ContributionRun | None,
    statistics: dict[str, Any] | None,
    *,
    entity: str | None,
) -> dict[str, Any]:
    """The governed numbers, as data.

    This is what a language model is given when one is configured. It is the same
    material the deterministic sections were written from, so a model narration
    and the platform's own prose can never disagree about a figure — and there is
    nothing here for a model to recompute, because every derived value it might
    need is already present.
    """

    payload: dict[str, Any] = {
        "kpi": run.kpi_name,
        "kpi_key": run.kpi_key,
        "target_date": run.target_date.isoformat(),
        "unit": run.unit,
        "currency": run.currency,
        "actual": run.actual_value,
        "expected": run.expected_value,
        "movement_absolute": run.deviation_absolute,
        "movement_pct": run.deviation_pct,
        "verdict": run.status,
        "comparison_basis": run.comparison_label,
        "engine_headline": run.headline,
        "detection_run_id": run.id,
    }
    if statistics is not None:
        payload["statistics"] = statistics
    if contribution is not None:
        payload["breakdown"] = {
            "dimension": contribution.dimension,
            "path": list(contribution.path or []),
            "ranked_count": contribution.ranked_count,
            "explained_pct": contribution.explained_pct,
            "unexplained_pct": contribution.unexplained_pct,
            "additive": contribution.additive,
            "shares_available": contribution.shares_available,
            "withheld_count": contribution.withheld_count,
            "leader_entity": contribution.leader_entity,
            "leader_share_pct": contribution.leader_share_pct,
            "leader_is_sufficient": contribution.leader_is_sufficient,
            "contributors": [
                {
                    key: row.get(key)
                    for key in (
                        "label",
                        "entity",
                        "actual",
                        "expected",
                        "change",
                        "share_pct",
                        "absolute_share_pct",
                        "reference_count",
                        "note",
                    )
                }
                for row in (contribution.contributors or [])[:10]
                if isinstance(row, dict)
            ],
        }
    if entity:
        payload["selected_entity"] = entity
    return payload


def stored_run(
    session: Session, access: AccessContext, kpi_key: str, target_date: date
) -> DetectionRun | None:
    """The most recent stored run for one KPI and date, company-scoped."""

    return session.scalars(
        select(DetectionRun)
        .where(
            DetectionRun.company_id == access.company.id,
            DetectionRun.kpi_key == kpi_key,
            DetectionRun.target_date == target_date,
        )
        .order_by(DetectionRun.executed_at.desc())
        .limit(1)
    ).first()
