"""Evidence to action: what a stored result suggests someone should consider doing.

This is the last step of the chain the platform already implements — detect,
explain, locate — and it is deliberately the most conservative of the four.
Detection measures. Contribution apportions. Explanation restates. This module
*suggests*, which is the only one of the four that can put words in a manager's
mouth, so every sentence it produces is built from a stored row and framed as
something to review.

**Where every part of a recommendation comes from.** Nothing here queries a
company's source, recomputes a KPI, divides a share or estimates an outcome:

======================  =====================================================
Evidence / Finding      ``DetectionRun`` verdict and deviation, plus the
                        leader and share stored on ``ContributionRun``
Target Area             ``ContributionRun.leader_entity`` with its ``path``
                        and ``depth``, so a drill-down reads back as a chain
Business Lever          ``KpiDriver`` rows the company registered as
                        ``controllable``; the KPI family's defaults otherwise,
                        labelled as such
Recommended Action      the lever's review instruction, aimed at the area, in
                        the vocabulary of ``recommendation_config``
Potential Impact        ``KpiMaterialityRule.business_criticality`` weighted by
                        how concentrated the movement is — a band, never a figure
Recommended Owner       the lever's functional owner, or the area's own manager
                        where the lever follows the area
Confidence              ``explanation.confidence_for`` — the platform's existing
                        rating, not a second scale
Monitoring Plan         the lever's own metrics plus the KPI itself, for the
                        next comparable periods
======================  =====================================================

**The three sentences this module will not write.** It never says a contributor
caused a movement — every recommendation carries the causation note in full, not
behind a disclosure. It never attaches money or a percentage to what an action
would achieve, because the platform measures no counterfactual. And it never
recommends a business intervention on a result the platform could not judge: a
LOW CONFIDENCE verdict, or a rating of LOW confidence on an ABNORMAL one, produces
evidence-collection steps instead, because acting hard on an unjudgeable number is
the specific failure this platform exists to prevent.

**It degrades rather than guesses.** No stored breakdown means no target area, and
the recommendation says so and asks for one — it does not pick a plausible region.
A caller without ``investigation.read`` gets the same KPI-level shape, because the
alternative is naming a part of the business their role may not see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import AccessContext
from app.models.base import DetectionStatus
from app.models.detection import ContributionRun, DetectionRun
from app.models.kpi import KpiDriver, KpiMaterialityRule, KpiVersion
from app.services import contribution as contribution_service
from app.services import recommendation_config as config
from app.services.explanation import (
    KNOWN_STATUSES,
    Confidence,
    confidence_for,
    format_pct,
    format_signed,
    format_value,
    kpi_label,
)

#: What the platform is willing to say about a result, before any lever is chosen.
#: The stance decides the whole shape of the answer, which is why it is one value
#: rather than a set of flags a surface could combine into a contradiction.
STANCE_ACTION = "ACTION"  # ABNORMAL, judgeable: suggested actions follow
STANCE_MONITOR = "MONITOR"  # moved favourably: capture it, do not correct it
STANCE_NO_ACTION = "NO_ACTION"  # NORMAL: routine monitoring only
STANCE_EVIDENCE_FIRST = "EVIDENCE_FIRST"  # not judgeable: collect evidence, do not intervene
STANCE_UNREADABLE = "UNREADABLE"  # a verdict this platform does not issue


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TargetArea:
    """The most specific part of the business the stored evidence points at.

    ``chain`` is the drill-down as it was actually performed — ``["South",
    "Hyderabad", "Store 24"]`` — reconstructed from the stored breakdown's own
    ``path`` and leader rather than assembled by the browser. A single-element
    chain is the normal case and is not a lesser one; it means one breakdown was
    run and nobody drilled further.
    """

    dimension: str
    entity: str
    entity_type: str
    chain: tuple[str, ...]
    chain_label: str
    share_pct: float | None
    change: float | None
    shares_available: bool
    drill_next: tuple[str, ...]
    comparison_hint: str
    depth: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "entity": self.entity,
            "entity_type": self.entity_type,
            "chain": list(self.chain),
            "chain_label": self.chain_label,
            "share_pct": self.share_pct,
            "change": self.change,
            "shares_available": self.shares_available,
            "drill_next": list(self.drill_next),
            "comparison_hint": self.comparison_hint,
            "depth": self.depth,
        }


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One suggested action, with everything a reader needs to disagree with it.

    The eight parts are all present or explicitly absent — there is no shape where
    an action arrives without its evidence, its owner or its confidence, because
    each of those is what stops it being read as an instruction from the platform.

    ``key`` is deterministic over the lever and the area rather than over position
    in the list, so a reader's feedback stays attached to the recommendation it was
    given about even after a deeper breakdown reorders the list.
    """

    key: str
    priority: str
    finding: str
    why: tuple[str, ...]
    target: TargetArea | None
    lever_key: str
    lever_label: str
    lever_source: str  # KPI_DRIVER | KPI_FAMILY_DEFAULT
    lever_note: str
    driver_name: str | None
    action: str
    impact_level: str
    impact_basis: str
    owner: str
    confidence_level: str
    monitoring: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "priority": self.priority,
            "priority_label": config.PRIORITY_LABELS.get(self.priority, self.priority),
            "finding": self.finding,
            "why": list(self.why),
            "target_area": None if self.target is None else self.target.as_dict(),
            "lever": {
                "key": self.lever_key,
                "label": self.lever_label,
                "source": self.lever_source,
                "note": self.lever_note,
                "driver_name": self.driver_name,
            },
            "action": self.action,
            "impact": {
                "level": self.impact_level,
                "label": config.IMPACT_LABELS.get(self.impact_level, self.impact_level),
                "basis": self.impact_basis,
            },
            "owner": self.owner,
            "confidence": {
                "level": self.confidence_level,
                "meaning": config.confidence_meaning(
                    self.confidence_level, has_area=self.target is not None
                ),
            },
            "monitoring": {"metrics": list(self.monitoring), "window": config.REVIEW_WINDOW},
            "causation_note": config.CAUSATION_NOTE,
        }


@dataclass
class RecommendationSet:
    """Everything the Results page shows under "what to consider doing next"."""

    kpi_key: str
    kpi_name: str
    target_date: str
    verdict: str
    stance: str
    movement_direction: str  # ADVERSE | FAVOURABLE | FLAT | UNKNOWN
    headline: str
    body: str
    confidence: Confidence
    evidence_summary: dict[str, Any]
    target: TargetArea | None
    recommendations: list[Recommendation] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    monitoring_metrics: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    #: True when a breakdown would sharpen these recommendations from the KPI level
    #: to a named area. The one piece of state the page acts on with a button.
    awaiting_breakdown: bool = False
    #: Technical provenance, for a caller entitled to read method.
    provenance: dict[str, Any] = field(default_factory=dict)

    # -- renderings ---------------------------------------------------------
    def executive_view(self) -> dict[str, Any]:
        """The five lines an executive reads, and nothing else.

        Not a different answer — the same recommendation set, narrowed. The
        analyst view adds the evidence behind these lines rather than replacing
        them, so two people looking at one result never see two conclusions.
        """

        top = self.recommendations[0] if self.recommendations else None
        return {
            "what_happened": self.headline,
            "largest_contributor": None if self.target is None else self.target.chain_label,
            "largest_contributor_share": None if self.target is None else self.target.share_pct,
            "top_action": None if top is None else top.action,
            "owner": None if top is None else top.owner,
            "impact": None
            if top is None
            else config.IMPACT_LABELS.get(top.impact_level, top.impact_level),
            "confidence": self.confidence.level,
        }

    def business_view(self) -> dict[str, Any]:
        return {
            "kpi": self.kpi_name,
            "kpi_key": self.kpi_key,
            "target_date": self.target_date,
            "verdict": self.verdict,
            "stance": self.stance,
            "movement_direction": self.movement_direction,
            "headline": self.headline,
            "body": self.body,
            "confidence": self.confidence.as_dict(),
            "evidence_summary": self.evidence_summary,
            "target_area": None if self.target is None else self.target.as_dict(),
            "recommendations": [item.as_dict() for item in self.recommendations],
            "next_steps": list(self.next_steps),
            "monitoring": {
                "metrics": list(self.monitoring_metrics),
                "window": config.REVIEW_WINDOW,
            },
            "limitations": list(self.limitations),
            "awaiting_breakdown": self.awaiting_breakdown,
            "causation_note": config.CAUSATION_NOTE,
            "action_preamble": config.ACTION_PREAMBLE,
            "executive": self.executive_view(),
        }

    def evidence(self) -> dict[str, Any]:
        return dict(self.provenance)


# ---------------------------------------------------------------------------
# Reading the stored evidence
# ---------------------------------------------------------------------------
def _version_for(session: Session, access: AccessContext, run: DetectionRun) -> KpiVersion | None:
    """The exact governed version detection ran on, inside the caller's company.

    Read by id from the run rather than re-resolved by KPI key: a recommendation
    that used a newer version's direction or criticality than the one that produced
    the verdict would be advising on a definition the result never used.
    """

    version = session.get(KpiVersion, run.kpi_version_id)
    if version is None or version.company_id != access.company.id:
        return None
    return version


def _stored_breakdowns(
    session: Session, access: AccessContext, run: DetectionRun
) -> list[ContributionRun]:
    """Every stored breakdown of this movement, deepest and most recent first.

    Gated on ``investigation.read`` for the same reason
    :func:`app.services.explanation.latest_contribution` is: a breakdown names parts
    of the business, and the permission that governs seeing those parts governs
    seeing them here.

    Ordered by depth so a drill-down that reached a store is preferred over the
    region-level read that preceded it — the most specific evidence stored is the
    most specific area a recommendation may name.
    """

    if not access.has("investigation.read"):
        return []
    rows = list(
        session.scalars(
            select(ContributionRun)
            .where(
                ContributionRun.company_id == access.company.id,
                ContributionRun.kpi_key == run.kpi_key,
                ContributionRun.target_date == run.target_date,
            )
            .order_by(ContributionRun.depth.desc(), ContributionRun.executed_at.desc())
            .limit(25)
        )
    )
    return [row for row in rows if (row.contributors or [])]


def _leader(breakdown: ContributionRun) -> dict[str, Any] | None:
    """The top-ranked part, as stored. A ranking, never a verdict about that part."""

    rows = [row for row in (breakdown.contributors or []) if isinstance(row, dict)]
    return rows[0] if rows else None


def _target_from(
    session: Session,
    version: KpiVersion | None,
    breakdown: ContributionRun,
) -> TargetArea | None:
    leader = _leader(breakdown)
    if leader is None:
        return None
    entity = str(leader.get("label") or leader.get("entity") or "").strip()
    if not entity:
        return None

    ancestors = [
        str(step.get("value"))
        for step in (breakdown.path or [])
        if isinstance(step, dict) and step.get("value")
    ]
    chain = tuple(ancestors + [entity])
    role = config.entity_role_for(breakdown.dimension)

    drill_next: tuple[str, ...] = ()
    if version is not None:
        for row in contribution_service.available_dimensions(session, version):
            if row.dimension_name.lower() == (breakdown.dimension or "").lower():
                drill_next = tuple(contribution_service.next_dimensions(session, version, row))
                break

    return TargetArea(
        dimension=breakdown.dimension,
        entity=entity,
        entity_type=role.label,
        chain=chain,
        chain_label=" → ".join(chain),
        share_pct=leader.get("share_pct") if breakdown.shares_available else None,
        change=leader.get("change"),
        shares_available=bool(breakdown.shares_available),
        drill_next=drill_next,
        comparison_hint=role.comparison_hint,
        depth=int(breakdown.depth or 0),
    )


def _movement_direction(run: DetectionRun, version: KpiVersion | None) -> str:
    """Whether this movement went the wrong way *for this KPI*.

    Read from the version's registered ``direction``, because "revenue fell" and
    "refunds fell" are opposite news and only the KPI's own registration knows
    which. With no version resolvable the direction is UNKNOWN rather than assumed,
    and the caller is told so in the limitations.
    """

    movement = run.deviation_absolute
    if movement is None:
        movement = run.deviation_pct
    if movement is None:
        return "UNKNOWN"
    if movement == 0:
        return "FLAT"
    if version is None:
        return "UNKNOWN"
    higher_is_better = (version.direction or "HIGHER_IS_BETTER").strip().upper() != "LOWER_IS_BETTER"
    rose = movement > 0
    return "FAVOURABLE" if rose == higher_is_better else "ADVERSE"


# ---------------------------------------------------------------------------
# Choosing levers
# ---------------------------------------------------------------------------
def _levers(
    session: Session,
    version: KpiVersion | None,
    family: config.KpiFamily,
    direction: str,
) -> list[tuple[config.Lever, str, str | None]]:
    """Candidate levers as ``(lever, source, driver_name)``, best first.

    Registered drivers outrank this module's defaults, and only ``controllable``
    ones are considered at all — the column exists precisely to record whether the
    business can pull a factor, and a recommendation to review something nobody
    can change is noise. A driver the company registered but did not mark
    controllable is left where it is: a candidate explanation, not an action.
    """

    chosen: list[tuple[config.Lever, str, str | None]] = []
    seen: set[str] = set()

    if version is not None:
        drivers = list(
            session.scalars(
                select(KpiDriver)
                .where(
                    KpiDriver.company_id == version.company_id,
                    KpiDriver.kpi_version_id == version.id,
                    KpiDriver.controllable.is_(True),
                )
                .order_by(KpiDriver.created_at.asc())
            )
        )
        for driver in drivers:
            lever = config.lever_for_driver(driver.driver_name, driver.driver_type)
            if lever is None or lever.key in seen:
                continue
            seen.add(lever.key)
            chosen.append((lever, "KPI_DRIVER", driver.driver_name))

    defaults = family.favourable_levers if direction == "FAVOURABLE" else family.adverse_levers
    for key in defaults:
        lever = config.LEVERS.get(key)
        if lever is None or lever.key in seen:
            continue
        seen.add(lever.key)
        chosen.append((lever, "KPI_FAMILY_DEFAULT", None))

    if not chosen:
        chosen.append((config.LEVERS[config.FALLBACK_LEVER], "KPI_FAMILY_DEFAULT", None))
    return chosen


def _lever_note(source: str, driver_name: str | None, family: config.KpiFamily) -> str:
    if source == "KPI_DRIVER":
        return (
            f"Registered as a controllable driver of this KPI ({driver_name}), so the "
            "business has already stated it can pull this lever."
        )
    return (
        f"No controllable driver is registered for this KPI, so this lever comes from "
        f"the platform's {family.label.lower()} defaults and should be confirmed against "
        "how this business actually operates."
    )


def _owner(lever: config.Lever, target: TargetArea | None) -> str:
    """Who to hand it to: the area's manager where the lever follows the area."""

    if lever.owner_follows_entity and target is not None:
        return config.entity_role_for(target.dimension).owner
    return lever.owner


def _monitoring(
    lever: config.Lever, family: config.KpiFamily, run: DetectionRun, target: TargetArea | None
) -> tuple[str, ...]:
    """What to watch next: the KPI first, then the lever's and family's companions.

    Metrics written with ``{area}`` are dropped rather than filled with a stand-in
    when no breakdown names an area. A plan that says "watch this for the affected
    area" without naming one asks the reader to watch somewhere the evidence has not
    identified, which is the same overreach in a watch list as in an action.
    """

    label = kpi_label(run.kpi_name)
    area = None if target is None else target.chain_label
    out: list[str] = [label if area is None else f"{label} for {area}"]
    for metric in (*lever.monitoring, *family.monitoring):
        if area is None and "{area}" in metric:
            continue
        text = metric.format(area=area or "", kpi=label)
        if text not in out:
            out.append(text)
    return tuple(out)


# ---------------------------------------------------------------------------
# Writing one recommendation
# ---------------------------------------------------------------------------
def _finding_sentence(run: DetectionRun, target: TargetArea | None) -> str:
    """The evidence line: why this recommendation is on screen at all.

    "Accounts for", exactly as the contributors panel says it, because the two are
    reading the same stored share and a recommendation that upgraded the verb would
    be the one place on the page where a size quietly became a cause.
    """

    label = kpi_label(run.kpi_name)
    movement = run.deviation_absolute
    tone = "downward" if (movement or 0) < 0 else "upward"

    if target is None:
        return (
            f"{label} moved {format_signed(run.deviation_absolute, run.unit, run.currency)} "
            f"({format_pct(run.deviation_pct)}) against its expected "
            f"{format_value(run.expected_value, run.unit, run.currency)}, and no stored "
            "breakdown yet attributes that movement to any part of the business."
        )
    if target.share_pct is not None:
        return (
            f"{target.chain_label} accounts for {abs(target.share_pct):.1f}% of the observed "
            f"{tone} movement in {label}."
        )
    return (
        f"{target.chain_label} is the largest ranked part of the observed {tone} movement in "
        f"{label}. No arithmetic share is available for this KPI, so the ranking indicates "
        "relative size rather than an exact decomposition."
    )


def _why_lines(
    run: DetectionRun,
    target: TargetArea | None,
    confidence: Confidence,
    lever: config.Lever,
    source: str,
    driver_name: str | None,
    family: config.KpiFamily,
) -> tuple[str, ...]:
    """The expandable evidence trail. Facts and their provenance, in reader order."""

    lines = [
        f"KPI verdict: {run.status.replace('_', ' ')}",
        f"Deviation: {format_signed(run.deviation_absolute, run.unit, run.currency)} "
        f"({format_pct(run.deviation_pct)}) against an expected "
        f"{format_value(run.expected_value, run.unit, run.currency)}",
        f"Comparison basis: {run.comparison_label or run.bucket_applied.replace('_', ' ').title()}"
        f" · {run.reference_count} comparable period"
        f"{'' if run.reference_count == 1 else 's'}",
    ]
    if target is None:
        lines.append("Top contributor: not established — no breakdown of this movement is stored")
    else:
        lines.append(
            f"Top contributor: {target.entity} in the {target.dimension} breakdown"
            + (f" (within {' → '.join(target.chain[:-1])})" if len(target.chain) > 1 else "")
        )
        lines.append(
            "Contribution: "
            + (
                f"{abs(target.share_pct):.1f}% of the observed movement"
                if target.share_pct is not None
                else "largest ranked part; no arithmetic share available for this KPI"
            )
        )
    lines.append(
        f"Confidence: {confidence.level} — "
        + config.confidence_meaning(confidence.level, has_area=target is not None)
    )
    lines.append(f"Lever: {lever.label} — {_lever_note(source, driver_name, family)}")
    return tuple(lines)


def _action_sentence(
    lever: config.Lever,
    target: TargetArea | None,
    role: config.EntityRole,
    direction: str,
    run: DetectionRun,
) -> str:
    """The instruction. Specific enough to start, phrased as a review, never a fix.

    Composed rather than templated whole, so the same lever reads correctly whether
    it is aimed at a region with cities under it, a store with nothing under it, or
    the KPI itself because no breakdown exists yet.
    """

    label = kpi_label(run.kpi_name)
    if target is None:
        return (
            f"Locate the affected area before acting: break this movement down along an "
            f"approved dimension of {label} for {run.target_date.isoformat()}, then review "
            f"the parts that account for the largest share of it. Until a breakdown is "
            f"stored, {lever.label.lower()} is a plausible lever for this KPI rather than a "
            "targeted one."
        )

    area = target.chain_label
    opening: str
    if direction == "FAVOURABLE":
        opening = (
            f"Capture what worked: document what changed in {area} during this window and "
            f"test whether it can be repeated in comparable areas."
        )
    else:
        onward = ""
        if target.drill_next:
            # "its channel breakdown" rather than "the channels": dimension names are
            # whatever the business registered, and pluralising them by rule produces
            # "citys" often enough to be worth avoiding entirely.
            names = " or ".join(name.replace("_", " ") for name in target.drill_next[:2])
            onward = (
                f", starting with its {names} breakdown to see which parts account for the "
                "largest share of the movement"
            )
        opening = f"Prioritise {role.review_scope} of {area}{onward}."

    return " ".join(
        [
            opening,
            lever.review_action.format(area=area),
            role.comparison_hint,
        ]
    )


def _build_one(
    *,
    run: DetectionRun,
    target: TargetArea | None,
    lever: config.Lever,
    source: str,
    driver_name: str | None,
    family: config.KpiFamily,
    confidence: Confidence,
    direction: str,
    priority: str,
    criticality: str | None,
) -> Recommendation:
    role = (
        config.entity_role_for(target.dimension)
        if target is not None
        else config.GENERIC_ENTITY_ROLE
    )
    impact_level, impact_basis = config.impact_band(
        business_criticality=criticality,
        leader_share_pct=None if target is None else target.share_pct,
        shares_available=bool(target is not None and target.shares_available),
    )
    return Recommendation(
        key=f"{lever.key}|{'' if target is None else config.normalise(target.chain_label)}",
        priority=priority,
        finding=_finding_sentence(run, target),
        why=_why_lines(run, target, confidence, lever, source, driver_name, family),
        target=target,
        lever_key=lever.key,
        lever_label=lever.label,
        lever_source=source,
        lever_note=_lever_note(source, driver_name, family),
        driver_name=driver_name,
        action=_action_sentence(lever, target, role, direction, run),
        impact_level=impact_level,
        impact_basis=impact_basis,
        owner=_owner(lever, target),
        confidence_level=confidence.level,
        monitoring=_monitoring(lever, family, run, target),
    )


def _preventive(
    run: DetectionRun,
    target: TargetArea | None,
    family: config.KpiFamily,
    confidence: Confidence,
    criticality: str | None,
) -> Recommendation:
    """Watch this more closely, rather than act on it.

    A rank of its own rather than a weak version of the others: raising monitoring
    on comparable periods is what a business does when it does not yet know enough
    to intervene, and offering it as a third-choice corrective action would misread
    it. Nothing is scheduled — this platform runs no watcher, so the plan is an
    instruction to a person.
    """

    label = kpi_label(run.kpi_name)
    area = None if target is None else target.chain_label
    scope = label if area is None else f"{label} for {area}"
    lever = config.LEVERS[config.FALLBACK_LEVER]
    impact_level, impact_basis = config.impact_band(
        business_criticality=criticality,
        leader_share_pct=None if target is None else target.share_pct,
        shares_available=bool(target is not None and target.shares_available),
    )
    return Recommendation(
        key=f"preventive|{'' if area is None else config.normalise(area)}",
        priority="PREVENTIVE_ACTION",
        finding=(
            f"This date is one observation. Whether the movement in {label} persists is not "
            "established by a single evaluation."
        ),
        why=(
            f"KPI verdict: {run.status.replace('_', ' ')}",
            f"Comparable periods behind the expectation: {run.reference_count}",
            "Persistence across periods is not something one stored result can show.",
            f"Confidence: {confidence.level}",
        ),
        target=target,
        lever_key="monitoring",
        lever_label="Ongoing Monitoring",
        lever_source="KPI_FAMILY_DEFAULT",
        lever_note="Increased monitoring is available for any KPI and needs no registered driver.",
        driver_name=None,
        action=(
            f"Increase monitoring of {scope} across similar periods: re-evaluate the "
            f"next comparable dates and confirm whether this movement repeats before treating it "
            f"as a pattern. Record what you conclude as an investigation finding so the next "
            f"reader starts from it."
        ),
        impact_level="LOW" if impact_level == "HIGH" else impact_level,
        impact_basis=impact_basis + " Monitoring changes what is known, not what is sold.",
        owner="KPI Owner",
        confidence_level=confidence.level,
        monitoring=_monitoring(lever, family, run, target),
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build(session: Session, access: AccessContext, run: DetectionRun) -> RecommendationSet:
    """Turn one stored result into what the business should consider doing next.

    Deterministic: the same stored rows always produce the same recommendations, in
    the same order, with the same wording. No model is called and nothing is
    generated, which is what makes this safe to show beside governed figures.
    """

    version = _version_for(session, access, run)
    family = config.family_for(run.kpi_key, run.kpi_name)
    direction = _movement_direction(run, version)

    breakdowns = _stored_breakdowns(session, access, run)
    breakdown = breakdowns[0] if breakdowns else None
    target = _target_from(session, version, breakdown) if breakdown is not None else None

    confidence = confidence_for(run, breakdown, access)

    criticality: str | None = None
    if version is not None:
        rule = session.scalars(
            select(KpiMaterialityRule).where(
                KpiMaterialityRule.company_id == version.company_id,
                KpiMaterialityRule.kpi_version_id == version.id,
            )
        ).first()
        criticality = None if rule is None else rule.business_criticality

    label = kpi_label(run.kpi_name)
    # Only an abnormal, judgeable result carries action cards. Decided here rather
    # than inside each branch so the limitations can be written for the shape the
    # reader is actually going to see.
    offers_actions = run.status == str(DetectionStatus.ABNORMAL) and confidence.level != "LOW"
    limitations = _limitations(
        run, version, breakdown, access, confidence, target, offers_actions=offers_actions
    )

    # -- Which shape of answer this result gets -----------------------------
    if run.status not in KNOWN_STATUSES:
        return RecommendationSet(
            kpi_key=run.kpi_key,
            kpi_name=run.kpi_name,
            target_date=run.target_date.isoformat(),
            verdict=run.status,
            stance=STANCE_UNREADABLE,
            movement_direction=direction,
            headline="This result cannot be acted on",
            body=(
                f"The stored verdict '{run.status}' is not one this platform issues, so no "
                f"recommendation can be derived from it. Re-run {label} for "
                f"{run.target_date.isoformat()} to obtain a verdict that can be read."
            ),
            confidence=confidence,
            evidence_summary=_evidence_summary(run, target),
            target=target,
            next_steps=[
                f"Re-run {label} for {run.target_date.isoformat()} on the current engine.",
            ],
            limitations=limitations,
            provenance=_provenance(run, version, breakdown, family, direction, criticality),
        )

    if run.status == str(DetectionStatus.LOW_CONFIDENCE) or (
        run.status == str(DetectionStatus.ABNORMAL) and confidence.level == "LOW"
    ):
        return RecommendationSet(
            kpi_key=run.kpi_key,
            kpi_name=run.kpi_name,
            target_date=run.target_date.isoformat(),
            verdict=run.status,
            stance=STANCE_EVIDENCE_FIRST,
            movement_direction=direction,
            headline=config.LOW_CONFIDENCE_HEADLINE,
            body=(
                f"{config.LOW_CONFIDENCE_BODY} Recommended next step: collect additional "
                "evidence or validate the affected dimensions before taking corrective "
                "action."
            ),
            confidence=confidence,
            evidence_summary=_evidence_summary(run, target),
            target=target,
            next_steps=list(config.LOW_CONFIDENCE_NEXT_STEPS),
            monitoring_metrics=list(_monitoring(config.LEVERS[config.FALLBACK_LEVER], family, run, target)),
            limitations=limitations,
            provenance=_provenance(run, version, breakdown, family, direction, criticality),
        )

    if run.status == str(DetectionStatus.NORMAL):
        return RecommendationSet(
            kpi_key=run.kpi_key,
            kpi_name=run.kpi_name,
            target_date=run.target_date.isoformat(),
            verdict=run.status,
            stance=STANCE_NO_ACTION,
            movement_direction=direction,
            headline=config.NORMAL_HEADLINE,
            body=config.NORMAL_BODY,
            confidence=confidence,
            evidence_summary=_evidence_summary(run, target),
            target=None,
            monitoring_metrics=list(_monitoring(config.LEVERS[config.FALLBACK_LEVER], family, run, None)),
            limitations=limitations,
            provenance=_provenance(run, version, breakdown, family, direction, criticality),
        )

    # -- ABNORMAL, and judgeable -------------------------------------------
    levers = _levers(session, version, family, direction)
    concentrated = bool(
        target is not None
        and (
            (breakdown is not None and breakdown.leader_is_sufficient)
            or (target.share_pct is not None and abs(target.share_pct) >= 25)
        )
    )
    primary_priority = (
        "HIGH_PRIORITY"
        if (confidence.level == "HIGH" and target is not None and concentrated)
        else "MEDIUM_PRIORITY"
    )
    if direction == "FAVOURABLE":
        # Nothing is wrong. The valuable action is to learn from it, which is a
        # medium-priority review at most -- never a high-priority intervention.
        primary_priority = "MEDIUM_PRIORITY"

    recommendations: list[Recommendation] = []
    for index, (lever, source, driver_name) in enumerate(levers[:2]):
        recommendations.append(
            _build_one(
                run=run,
                target=target,
                lever=lever,
                source=source,
                driver_name=driver_name,
                family=family,
                confidence=confidence,
                direction=direction,
                priority=primary_priority if index == 0 else "MEDIUM_PRIORITY",
                criticality=criticality,
            )
        )
    recommendations.append(_preventive(run, target, family, confidence, criticality))

    if direction == "FAVOURABLE":
        headline = (
            f"{label} moved favourably on {run.target_date.isoformat()} — the recommended "
            "actions are about repeating it, not correcting it."
        )
        stance = STANCE_MONITOR
    else:
        headline = (
            f"{label} moved outside what its comparable history supports on "
            f"{run.target_date.isoformat()}."
        )
        stance = STANCE_ACTION

    # The panel prints ``action_preamble`` immediately above the cards, so this body
    # must not repeat it. Its job is to say what the set is aimed at.
    if target is None:
        body = (
            "No stored breakdown attributes this movement to any part of the business yet, so "
            "the actions below are scoped to the KPI. Break the movement down to target them."
        )
    elif target.share_pct is not None:
        body = (
            f"{target.chain_label} accounts for {abs(target.share_pct):.1f}% of the observed "
            f"movement — the largest share of any part ranked at this level — so the actions "
            "below are aimed there."
        )
    else:
        body = (
            f"{target.chain_label} is the largest ranked part of this movement, so the actions "
            "below are aimed there. No arithmetic share is available for this KPI."
        )

    return RecommendationSet(
        kpi_key=run.kpi_key,
        kpi_name=run.kpi_name,
        target_date=run.target_date.isoformat(),
        verdict=run.status,
        stance=stance,
        movement_direction=direction,
        headline=headline,
        body=body,
        confidence=confidence,
        evidence_summary=_evidence_summary(run, target),
        target=target,
        recommendations=recommendations,
        monitoring_metrics=list(recommendations[0].monitoring),
        limitations=limitations,
        awaiting_breakdown=target is None and access.has("investigation.read"),
        provenance=_provenance(run, version, breakdown, family, direction, criticality),
    )


def _evidence_summary(run: DetectionRun, target: TargetArea | None) -> dict[str, Any]:
    """The figures a recommendation rests on, echoed so a reader can check them.

    Every value is a stored column re-rendered. Deliberately no statistics: those
    are ``kpi.read`` material and the detection API already gates them, so
    duplicating them here would be a second door to the same room.
    """

    return {
        "verdict": run.status,
        "actual": run.actual_value,
        "expected": run.expected_value,
        "deviation_absolute": run.deviation_absolute,
        "deviation_pct": run.deviation_pct,
        "unit": run.unit,
        "currency": run.currency,
        "comparison": run.comparison_label,
        "reference_count": run.reference_count,
        "top_contributor": None if target is None else target.entity,
        "top_contributor_chain": None if target is None else list(target.chain),
        "top_contributor_share_pct": None if target is None else target.share_pct,
        "breakdown_dimension": None if target is None else target.dimension,
    }


def _limitations(
    run: DetectionRun,
    version: KpiVersion | None,
    breakdown: ContributionRun | None,
    access: AccessContext,
    confidence: Confidence,
    target: TargetArea | None,
    *,
    offers_actions: bool,
) -> list[str]:
    """What these recommendations do not know. Said out loud, never inferred away.

    ``offers_actions`` is false for the shapes that recommend nothing. Those keep the
    limitations that describe the *evidence* and drop the ones that describe how an
    action was scoped or rated, since a reader looking at "no corrective action is
    recommended" is owed neither.
    """

    out: list[str] = [config.CAUSATION_NOTE]
    if version is None:
        out.append(
            "The KPI version behind this result could not be read, so the registered "
            "direction, drivers and business criticality did not inform these recommendations."
        )
    if not access.has("investigation.read"):
        out.append(
            "Your role does not hold investigation access, so no part of the business is "
            "named here. A colleague who holds it can locate the movement."
        )
    elif breakdown is None and offers_actions:
        out.append(
            "No breakdown of this movement is stored, so these recommendations are scoped to "
            "the KPI rather than to an area."
        )
    if breakdown is not None and (breakdown.withheld_count or 0) > 0:
        out.append(
            f"{breakdown.withheld_count} value(s) sit outside your row access scope, so the "
            "ranking behind the target area may be incomplete."
        )
    if target is not None and not target.shares_available:
        out.append(
            "This KPI's parts do not sum to its whole, so the target area is the largest ranked "
            "part rather than a measured share of the movement."
        )
    if confidence.level != "HIGH":
        out.append(
            f"Confidence in this result is {confidence.level}. "
            + config.confidence_meaning(confidence.level, has_area=target is not None)
        )
    if offers_actions:
        out.append(
            "Potential impact is a qualitative band. This platform measures no counterfactual and "
            "does not estimate what an action would be worth."
        )
    return out


def _provenance(
    run: DetectionRun,
    version: KpiVersion | None,
    breakdown: ContributionRun | None,
    family: config.KpiFamily,
    direction: str,
    criticality: str | None,
) -> dict[str, Any]:
    return {
        "detection_run_id": run.id,
        "kpi_version": run.kpi_version,
        "kpi_version_id": run.kpi_version_id,
        "kpi_family": family.key,
        "registered_direction": None if version is None else version.direction,
        "movement_direction": direction,
        "business_criticality": criticality,
        "contribution_run_id": None if breakdown is None else breakdown.id,
        "contribution_depth": None if breakdown is None else breakdown.depth,
        "contribution_dimension": None if breakdown is None else breakdown.dimension,
        "contribution_executed_at": None
        if breakdown is None
        else breakdown.executed_at.isoformat(),
        "shares_available": None if breakdown is None else bool(breakdown.shares_available),
        "leader_is_sufficient": None if breakdown is None else bool(breakdown.leader_is_sufficient),
        "unexplained_pct": None if breakdown is None else breakdown.unexplained_pct,
        "generated_by": "deterministic_rules",
    }
