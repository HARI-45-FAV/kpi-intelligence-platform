"""The recommendation vocabulary: business language, kept out of the engine.

``app.services.recommendation`` decides *whether* a movement warrants a suggested
action, *which* part of the business it should be aimed at, and *how much* weight
it carries. This module supplies the words: what a lever is called, who normally
owns it, what reviewing it looks like in practice, and what to watch afterwards.

**Why the split exists at all.** The rest of this platform is built on the rule
that no company's vocabulary lives in code — dimensions, hierarchies, comparison
policies and drivers are all registered by the company and read back. A
recommendation layer cannot fully honour that rule, because a suggested action
has to be written in some language before anyone can read it. So the compromise
is drawn deliberately:

* Everything a company *declared* wins. A registered controllable
  :class:`~app.models.kpi.KpiDriver` is the lever; the dimension names in a KPI's
  own registration decide what kind of area is being talked about; the company's
  declared hierarchy decides where a drill-down may go next.
* This file is only the **fallback and the phrasing** — the sentence to write once
  the engine knows which lever and which area, and a defensible default set of
  levers for a KPI whose drivers nobody has registered yet.
* Nothing here is matched on a company name, a region, a store or a product. The
  patterns match *metric and dimension vocabulary* ("region", "refund", "store"),
  which is a property of the English the company chose for its own metadata.

**Two things this file may never contain.** A cause, and a number. Every lever is
phrased as something to *review* — "Relevant Business Lever to Review" — because
contribution establishes size and not causation, and a lever that was written as
"the reason" would be read as one. And every impact is a qualitative band, because
this platform measures no counterfactual and therefore cannot say what an action
would be worth.

Extending it is intentionally dull: add a :class:`Lever` to ``LEVERS``, name its
key in a :class:`KpiFamily`, or add an :class:`EntityRole` pattern. No engine code
changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.base import DriverType

# ---------------------------------------------------------------------------
# Vocabulary matching
# ---------------------------------------------------------------------------
#: Everything in this module matches on a *normalised* name: lowercase, with
#: separators collapsed to single spaces, so ``net_revenue``, ``Net-Revenue`` and
#: ``NET REVENUE`` are the same token stream to a pattern.
_SEPARATORS = re.compile(r"[\s_\-./]+")


def normalise(value: str | None) -> str:
    """Lowercase a metric or dimension name into a matchable token stream."""

    if not value:
        return ""
    return _SEPARATORS.sub(" ", str(value).strip().lower())


def matches(value: str | None, patterns: tuple[str, ...]) -> bool:
    """Whether a normalised name contains any of ``patterns`` as a whole word.

    Whole-word rather than substring, because substring matching is how "region"
    finds "sub-regional adjustment" and how "sku" finds "risk": a false lever is
    worse than a generic one, since the generic one is honest about knowing less.
    """

    haystack = f" {normalise(value)} "
    return any(f" {pattern} " in haystack for pattern in patterns)


# ---------------------------------------------------------------------------
# Levers: what a business can actually pull
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Lever:
    """One controllable area of the business, and how to talk about reviewing it.

    ``label`` is what a reader sees under "Relevant Business Lever to Review".
    ``review_action`` is the practical instruction — specific enough to start on a
    Monday morning, and never phrased as a conclusion about why the KPI moved.

    ``owner`` is the function that normally holds this lever. ``owner_follows_entity``
    decides whether the *area* overrides it: store operations belong to whoever runs
    the affected store, while pricing belongs to the category function regardless of
    which region the movement showed up in.
    """

    key: str
    label: str
    owner: str
    #: True when the affected area's own manager owns this lever rather than the
    #: functional owner named above.
    owner_follows_entity: bool
    #: The instruction. ``{area}`` is substituted with the affected area's label, or
    #: with a KPI-level phrase when no area has been established.
    review_action: str
    #: What to watch afterwards. Metric names a business reader recognises — no
    #: platform internals, and no promise that any of them will move.
    monitoring: tuple[str, ...]
    #: Registered driver types this lever answers to.
    driver_types: frozenset[str] = field(default_factory=frozenset)
    #: Driver-name vocabulary this lever answers to, matched whole-word.
    driver_patterns: tuple[str, ...] = ()


LEVERS: dict[str, Lever] = {
    # -- Revenue-shaped levers ---------------------------------------------
    "order_volume": Lever(
        key="order_volume",
        label="Order Volume",
        owner="Regional Sales Manager",
        owner_follows_entity=True,
        review_action=(
            "Review order volume for {area} against comparable periods: transaction "
            "counts, active customers and any days with no recorded activity. Confirm "
            "whether fewer orders or smaller orders account for the movement before "
            "committing to a corrective action."
        ),
        monitoring=("Order volume", "Transaction count", "Active customers"),
        driver_types=frozenset({str(DriverType.VOLUME)}),
        driver_patterns=("volume", "orders", "order volume", "transactions", "traffic", "footfall"),
    ),
    "average_order_value": Lever(
        key="average_order_value",
        label="Average Order Value",
        owner="Regional Sales Manager",
        owner_follows_entity=True,
        review_action=(
            "Compare average order value for {area} against comparable periods and "
            "against peer areas. Separate basket size from unit price, and check "
            "whether discounting is carrying the difference."
        ),
        monitoring=("Average order value", "Units per order", "Discount rate"),
        driver_types=frozenset({str(DriverType.PRICE)}),
        driver_patterns=("aov", "average order value", "basket", "ticket size", "order value"),
    ),
    "product_mix": Lever(
        key="product_mix",
        label="Product Mix",
        owner="Category Manager",
        owner_follows_entity=False,
        review_action=(
            "Review the product and category mix sold in {area} against comparable "
            "periods. Identify which categories moved with the total and which held "
            "steady, and check whether the mix shifted towards lower-value lines."
        ),
        monitoring=("Category share of sales", "Units by category", "Mix-adjusted revenue"),
        driver_types=frozenset({str(DriverType.MIX)}),
        driver_patterns=("mix", "product mix", "category mix", "assortment", "range"),
    ),
    "pricing": Lever(
        key="pricing",
        label="Pricing",
        owner="Pricing / Category Manager",
        owner_follows_entity=False,
        review_action=(
            "Review price points and realised prices for {area} against comparable "
            "periods, including any price changes that took effect in the window. "
            "Check list price, realised price and net price separately."
        ),
        monitoring=("Realised price", "Price changes in effect", "Margin per order"),
        driver_types=frozenset({str(DriverType.PRICE)}),
        driver_patterns=("price", "pricing", "rate card", "tariff", "list price"),
    ),
    "promotions": Lever(
        key="promotions",
        label="Promotions",
        owner="Marketing Manager",
        owner_follows_entity=False,
        review_action=(
            "Review which campaigns and promotions were live for {area} in this window "
            "against comparable periods, including any that ended. Check spend, reach "
            "and redemption alongside the KPI rather than in isolation."
        ),
        monitoring=("Campaign spend", "Promotion redemption rate", "Promoted vs non-promoted sales"),
        driver_types=frozenset({str(DriverType.MARKETING)}),
        driver_patterns=("promotion", "promotions", "campaign", "marketing", "discount", "offer"),
    ),
    "store_operations": Lever(
        key="store_operations",
        label="Store Operations",
        owner="Store Operations Manager",
        owner_follows_entity=True,
        review_action=(
            "Review operations for {area} across this window: trading hours actually "
            "kept, staffing levels, downtime, and any recorded operational incidents. "
            "Compare against peer locations trading the same days."
        ),
        monitoring=("Hours traded", "Staffing coverage", "Recorded operational incidents"),
        driver_types=frozenset({str(DriverType.OTHER)}),
        driver_patterns=("operations", "store operations", "staffing", "labour", "labor", "service", "uptime"),
    ),
    "inventory_availability": Lever(
        key="inventory_availability",
        label="Inventory Availability",
        owner="Inventory Manager",
        owner_follows_entity=False,
        review_action=(
            "Check stock availability for {area} across this window: out-of-stock "
            "hours, fill rate and inbound delays on the lines that matter most there. "
            "Confirm whether unavailable stock coincides with the movement."
        ),
        monitoring=("Out-of-stock rate", "Fill rate", "Inbound delivery delays"),
        driver_types=frozenset({str(DriverType.SUPPLY)}),
        driver_patterns=("inventory", "stock", "availability", "supply", "fulfilment", "fulfillment"),
    ),
    # -- Refund / return-shaped levers -------------------------------------
    "product_quality": Lever(
        key="product_quality",
        label="Product Quality",
        owner="Category Manager",
        owner_follows_entity=False,
        review_action=(
            "Review recorded return reasons for {area} and group them by product and "
            "batch. Confirm whether quality-coded reasons account for the movement "
            "before escalating to a supplier or a line."
        ),
        monitoring=("Return reason mix", "Returns per product line", "Quality complaint volume"),
        driver_types=frozenset({str(DriverType.SUPPLY)}),
        driver_patterns=("quality", "defect", "damage", "faulty"),
    ),
    "delivery_performance": Lever(
        key="delivery_performance",
        label="Delivery Performance",
        owner="Operations Manager",
        owner_follows_entity=True,
        review_action=(
            "Review delivery performance serving {area} in this window: on-time rate, "
            "transit times and failed-delivery reasons, compared with comparable "
            "periods and with areas served by the same routes."
        ),
        monitoring=("On-time delivery rate", "Average transit time", "Failed delivery rate"),
        driver_types=frozenset({str(DriverType.EXTERNAL)}),
        driver_patterns=("delivery", "logistics", "shipping", "courier", "transit", "dispatch"),
    ),
    "supplier_performance": Lever(
        key="supplier_performance",
        label="Supplier Performance",
        owner="Procurement / Supplier Manager",
        owner_follows_entity=False,
        review_action=(
            "Review supplier performance behind the lines sold in {area}: incoming "
            "quality checks, batch history and any supplier incidents recorded for "
            "this window."
        ),
        monitoring=("Supplier defect rate", "Incoming inspection failures", "Supplier incident count"),
        driver_types=frozenset({str(DriverType.SUPPLY)}),
        driver_patterns=("supplier", "vendor", "procurement", "sourcing"),
    ),
    "return_experience": Lever(
        key="return_experience",
        label="Return Experience",
        owner="Customer Experience Manager",
        owner_follows_entity=False,
        review_action=(
            "Review how returns are being raised and handled for {area}: which "
            "channels they arrive through, how long they take to resolve, and whether "
            "any policy or process change took effect in this window."
        ),
        monitoring=("Return processing time", "Returns by channel", "Repeat return rate"),
        driver_types=frozenset({str(DriverType.OTHER)}),
        driver_patterns=("return", "returns", "refund", "refunds", "rma", "chargeback"),
    ),
    "customer_experience": Lever(
        key="customer_experience",
        label="Customer Experience",
        owner="Customer Experience Manager",
        owner_follows_entity=False,
        review_action=(
            "Review customer-experience signals for {area} across this window: "
            "complaint volume, satisfaction scores and the reasons customers give, "
            "compared with comparable periods."
        ),
        monitoring=("Complaint volume", "Satisfaction score", "Complaint reason mix"),
        driver_types=frozenset({str(DriverType.OTHER)}),
        driver_patterns=("experience", "satisfaction", "nps", "csat", "complaint", "feedback"),
    ),
    # -- Churn / retention-shaped levers -----------------------------------
    "customer_support": Lever(
        key="customer_support",
        label="Customer Support",
        owner="Customer Experience Manager",
        owner_follows_entity=False,
        review_action=(
            "Review support load and outcomes for {area} in this window: contact "
            "volume, first-response and resolution times, and unresolved cases still "
            "open. Compare against comparable periods."
        ),
        monitoring=("Support contact volume", "First-response time", "Unresolved case count"),
        driver_types=frozenset({str(DriverType.OTHER)}),
        driver_patterns=("support", "service desk", "helpdesk", "tickets", "escalations"),
    ),
    "retention_campaigns": Lever(
        key="retention_campaigns",
        label="Retention Campaigns",
        owner="Marketing Manager",
        owner_follows_entity=False,
        review_action=(
            "Review which retention and win-back activity was running for {area} in "
            "this window, and which cohorts it reached. Check whether any programme "
            "lapsed or changed shortly before the movement."
        ),
        monitoring=("Retention campaign reach", "Win-back conversion rate", "Renewal rate"),
        driver_types=frozenset({str(DriverType.MARKETING)}),
        driver_patterns=("retention", "win back", "winback", "loyalty", "renewal", "lifecycle"),
    ),
    "product_experience": Lever(
        key="product_experience",
        label="Product Experience",
        owner="Product Manager",
        owner_follows_entity=False,
        review_action=(
            "Review product usage and friction for {area} across this window: feature "
            "adoption, error and failure rates, and any release that shipped inside "
            "the window."
        ),
        monitoring=("Active usage rate", "Error / failure rate", "Feature adoption"),
        driver_types=frozenset({str(DriverType.OTHER)}),
        driver_patterns=("product", "usage", "adoption", "onboarding", "engagement quality"),
    ),
    "customer_engagement": Lever(
        key="customer_engagement",
        label="Customer Engagement",
        owner="Marketing Manager",
        owner_follows_entity=False,
        review_action=(
            "Review engagement for {area} across this window: contact frequency, "
            "channel response rates and the share of customers who went quiet before "
            "the movement appeared."
        ),
        monitoring=("Engagement rate", "Dormant customer share", "Channel response rate"),
        driver_types=frozenset({str(DriverType.MARKETING)}),
        driver_patterns=("engagement", "activity", "frequency", "communication", "outreach"),
    ),
    # -- The honest default ------------------------------------------------
    "operational_review": Lever(
        key="operational_review",
        label="Operational Performance",
        owner="KPI Owner",
        owner_follows_entity=True,
        review_action=(
            "Review how {area} performed across this window against comparable "
            "periods, and against areas of similar size. Establish what changed there "
            "before choosing a corrective action, since no controllable driver has "
            "been registered for this KPI."
        ),
        monitoring=("Volume of activity", "Recorded operational events"),
        driver_types=frozenset(),
        driver_patterns=(),
    ),
}

#: The lever used when nothing else matches. Named rather than invented on the fly
#: so its wording is reviewable alongside every other lever.
FALLBACK_LEVER = "operational_review"


# ---------------------------------------------------------------------------
# KPI families: which levers are even plausible for this metric
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class KpiFamily:
    """A metric shape, and the levers a business normally reaches for.

    ``adverse_levers`` apply when the movement went the wrong way for this KPI's
    registered direction; ``favourable_levers`` apply when it went the right way,
    where the useful question is not what to fix but what to repeat. Both are
    *candidate* levers: a registered controllable driver always outranks them.
    """

    key: str
    label: str
    patterns: tuple[str, ...]
    adverse_levers: tuple[str, ...]
    favourable_levers: tuple[str, ...]
    #: Metrics worth watching for this family regardless of which lever was chosen.
    #: ``{area}`` and ``{kpi}`` are substituted when the plan is written. A metric
    #: written with ``{area}`` is dropped entirely when no breakdown names an area,
    #: so a watch list never asks a reader to watch somewhere unidentified.
    monitoring: tuple[str, ...] = ()


KPI_FAMILIES: tuple[KpiFamily, ...] = (
    KpiFamily(
        key="revenue",
        label="Revenue and sales",
        patterns=(
            "revenue", "sales", "gmv", "turnover", "bookings", "billings",
            "net revenue", "gross revenue", "income", "aov", "order value", "orders",
        ),
        adverse_levers=(
            "order_volume",
            "average_order_value",
            "product_mix",
            "inventory_availability",
            "promotions",
            "store_operations",
            "pricing",
        ),
        favourable_levers=("order_volume", "product_mix", "promotions", "average_order_value"),
        monitoring=("Share of total {kpi} held by {area}",),
    ),
    KpiFamily(
        key="refunds",
        label="Refunds and returns",
        patterns=(
            "refund", "refunds", "return", "returns", "chargeback", "chargebacks",
            "cancellation", "cancellations", "rma", "credit note", "reversal",
        ),
        adverse_levers=(
            "product_quality",
            "delivery_performance",
            "supplier_performance",
            "return_experience",
            "customer_experience",
        ),
        favourable_levers=("product_quality", "return_experience", "delivery_performance"),
        monitoring=("Refund rate for {area}, against the company average",),
    ),
    KpiFamily(
        key="churn",
        label="Churn and retention",
        patterns=(
            "churn", "attrition", "retention", "lapsed", "cancellation rate",
            "renewal", "repeat rate", "active customers", "subscribers",
        ),
        adverse_levers=(
            "customer_support",
            "retention_campaigns",
            "product_experience",
            "pricing",
            "customer_engagement",
        ),
        favourable_levers=("retention_campaigns", "customer_engagement", "product_experience"),
        monitoring=("Churn rate for {area}, against the company average",),
    ),
)

#: Used when a KPI's name matches no family. Its levers are deliberately generic:
#: a metric this platform has no vocabulary for should receive a review
#: instruction, not a guess at which part of the business controls it.
GENERIC_FAMILY = KpiFamily(
    key="generic",
    label="General performance",
    patterns=(),
    adverse_levers=("operational_review",),
    favourable_levers=("operational_review",),
    monitoring=(),
)


def family_for(kpi_key: str | None, kpi_name: str | None) -> KpiFamily:
    """Which metric family this KPI belongs to. ``GENERIC_FAMILY`` when unknown."""

    for family in KPI_FAMILIES:
        if matches(kpi_key, family.patterns) or matches(kpi_name, family.patterns):
            return family
    return GENERIC_FAMILY


# ---------------------------------------------------------------------------
# Entity roles: what kind of area the movement was located in
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EntityRole:
    """What a dimension *is*, in business terms, and who answers for it.

    Matched against the dimension name the company registered — not against a
    fixed list of dimensions this platform expects to exist. A company that slices
    Revenue by ``branch`` gets the store role; one that slices by ``cost_centre``
    gets the generic role and an honest instruction, which is better than a
    confident sentence about a kind of area nobody described.
    """

    key: str
    #: How the dimension is named in a sentence: "Region", "Store", "Product Category".
    label: str
    patterns: tuple[str, ...]
    owner: str
    #: The headline instruction for this kind of area, e.g. "regional performance review".
    review_scope: str
    #: What comparing like with like means here.
    comparison_hint: str


ENTITY_ROLES: tuple[EntityRole, ...] = (
    EntityRole(
        key="region",
        label="Region",
        patterns=(
            "region", "regions", "zone", "zones", "territory", "territories",
            "state", "states", "province", "cluster", "market", "markets", "geo", "country",
        ),
        owner="Regional Sales Manager",
        review_scope="a regional performance review",
        comparison_hint="Compare against regions of similar size and against the same region in comparable periods.",
    ),
    EntityRole(
        key="city",
        label="City",
        patterns=("city", "cities", "town", "district", "locality", "metro", "pincode", "postcode", "area"),
        owner="Area / City Manager",
        review_scope="a city-level operational review",
        comparison_hint="Compare the affected locations against other cities trading the same days.",
    ),
    EntityRole(
        key="store",
        label="Store",
        patterns=(
            "store", "stores", "outlet", "outlets", "branch", "branches", "shop",
            "location", "locations", "site", "sites", "warehouse", "terminal",
        ),
        owner="Store Operations Manager",
        review_scope="a store operations review",
        comparison_hint="Compare against peer stores of similar size and format trading the same days.",
    ),
    EntityRole(
        key="category",
        label="Product Category",
        patterns=(
            "category", "categories", "subcategory", "product", "products", "sku",
            "skus", "item", "items", "brand", "brands", "line", "department", "assortment",
        ),
        owner="Category Manager",
        review_scope="a category performance review",
        comparison_hint="Compare against sibling categories and against the same category in comparable periods.",
    ),
    EntityRole(
        key="channel",
        label="Channel",
        patterns=("channel", "channels", "platform", "marketplace", "medium", "source", "device", "app"),
        owner="Channel / Marketing Manager",
        review_scope="a channel performance review",
        comparison_hint="Compare against the other channels serving the same customers in this window.",
    ),
    EntityRole(
        key="segment",
        label="Customer Segment",
        patterns=(
            "segment", "segments", "customer", "customers", "cohort", "tier",
            "account", "accounts", "membership", "plan",
        ),
        owner="Customer Experience Manager",
        review_scope="a customer segment review",
        comparison_hint="Compare against adjacent segments and against this segment in comparable periods.",
    ),
    EntityRole(
        key="team",
        label="Team",
        patterns=("team", "teams", "agent", "agents", "employee", "staff", "rep", "advisor", "department"),
        owner="Operations Manager",
        review_scope="a team performance review",
        comparison_hint="Compare against teams carrying comparable workload in the same window.",
    ),
)

#: For a dimension whose vocabulary this file does not recognise. It names the
#: dimension the company actually registered rather than pretending to a type.
GENERIC_ENTITY_ROLE = EntityRole(
    key="generic",
    label="Business Area",
    patterns=(),
    owner="KPI Owner",
    review_scope="a focused review of this part of the business",
    comparison_hint="Compare against comparable values of the same dimension in comparable periods.",
)


def entity_role_for(dimension: str | None) -> EntityRole:
    """The role a registered dimension plays. ``GENERIC_ENTITY_ROLE`` when unknown."""

    for role in ENTITY_ROLES:
        if matches(dimension, role.patterns):
            return role
    return GENERIC_ENTITY_ROLE


# ---------------------------------------------------------------------------
# Driver → lever
# ---------------------------------------------------------------------------
def lever_for_driver(driver_name: str | None, driver_type: str | None) -> Lever | None:
    """The lever a registered driver corresponds to, if any.

    Name before type, because the name is what a person wrote and the type is a
    coarse bucket: a driver called "Out-of-stock hours" typed ``SUPPLY`` should
    land on inventory availability rather than on whichever supply-typed lever
    happens to be declared first.
    """

    for lever in LEVERS.values():
        if lever.driver_patterns and matches(driver_name, lever.driver_patterns):
            return lever
    if driver_type:
        wanted = str(driver_type).strip().upper()
        for lever in LEVERS.values():
            if wanted in lever.driver_types and lever.key != FALLBACK_LEVER:
                return lever
    return None


# ---------------------------------------------------------------------------
# Priority and potential impact
# ---------------------------------------------------------------------------
#: The visual ranks a recommendation can carry. ``PREVENTIVE_ACTION`` is not a
#: weaker version of the other two: it is the rank for "nothing is wrong here yet,
#: watch it more closely", which is a different instruction rather than a smaller one.
PRIORITY_LABELS: dict[str, str] = {
    "HIGH_PRIORITY": "High priority",
    "MEDIUM_PRIORITY": "Medium priority",
    "PREVENTIVE_ACTION": "Preventive action",
}

IMPACT_LABELS: dict[str, str] = {
    "HIGH": "High potential impact",
    "MEDIUM": "Medium potential impact",
    "LOW": "Low potential impact",
}

#: Weight from the KPI's own registered business criticality. A company that said
#: this metric is HIGH criticality has already told the platform how much a
#: movement in it matters, so nothing here re-decides that.
_CRITICALITY_SCORE: dict[str, int] = {"HIGH": 2, "CRITICAL": 2, "MEDIUM": 1, "LOW": 0}


def impact_band(
    *,
    business_criticality: str | None,
    leader_share_pct: float | None,
    shares_available: bool,
) -> tuple[str, str]:
    """A qualitative potential-impact band, and the basis it rests on.

    Deliberately qualitative. The platform measures no counterfactual, so any
    figure attached to "what this action is worth" would be invented — and a made-up
    number is the single fastest way for a recommendation to become indefensible.

    The band is the KPI's registered criticality, adjusted by how concentrated the
    movement is: acting on an area that holds most of a movement has more room to
    matter than acting on one that holds a sliver. When shares are not arithmetic
    for the KPI, concentration is unknown and only criticality applies.
    """

    criticality = (business_criticality or "MEDIUM").strip().upper()
    score = _CRITICALITY_SCORE.get(criticality, 1)
    basis = [f"this KPI is registered as {criticality} business criticality"]

    if shares_available and leader_share_pct is not None:
        magnitude = abs(leader_share_pct)
        if magnitude >= 50:
            score += 1
            basis.append(f"the target area holds {magnitude:.1f}% of the observed movement")
        elif magnitude >= 25:
            basis.append(f"the target area holds {magnitude:.1f}% of the observed movement")
        else:
            score -= 1
            basis.append(
                f"the target area holds only {magnitude:.1f}% of the observed movement, "
                "so the movement is spread across several parts"
            )
    else:
        basis.append("no arithmetic share is available for this KPI, so concentration is unknown")

    level = "HIGH" if score >= 3 else ("MEDIUM" if score >= 1 else "LOW")
    return level, "Potential impact rated because " + ", and ".join(basis) + "."


# ---------------------------------------------------------------------------
# Fixed wording
# ---------------------------------------------------------------------------
#: Said on every recommendation, in full, wherever one is shown. The engine can
#: rank contribution but it cannot establish causation, and a reader who acts on a
#: recommendation is entitled to that distinction without expanding anything.
CAUSATION_NOTE = (
    "This recommendation is based on contribution and available evidence. "
    "Contribution alone does not establish causation."
)

#: How far ahead a monitoring plan looks. Comparable periods rather than days,
#: because "comparable" is the only window this platform's own comparison policy
#: can defend — and there is no scheduler behind this, so it is an instruction to a
#: person rather than a claim that the platform will be watching.
REVIEW_WINDOW = "Next 3 comparable periods"

#: The prefix that turns evidence into a suggestion rather than a finding.
ACTION_PREAMBLE = "Based on this evidence, the following actions are recommended for review."

CONFIDENCE_MEANING: dict[str, str] = {
    "HIGH": "Strong evidence supports prioritising this area.",
    "MEDIUM": "This area is strongly associated with the movement, but additional validation is recommended before acting.",
    "LOW": "Evidence is insufficient for a specific intervention.",
}

#: The same three levels, worded for a result no breakdown has aimed yet. The
#: evidence behind a movement can be strong while saying nothing about *where* to
#: act, and a confidence line that says "this area" when none is named would be
#: claiming the one thing this result does not have.
CONFIDENCE_MEANING_NO_AREA: dict[str, str] = {
    "HIGH": "The movement itself is well evidenced, but no breakdown yet names where to act.",
    "MEDIUM": "The movement is established, though additional validation is recommended before acting — and no breakdown yet names where.",
    "LOW": "Evidence is insufficient for a specific intervention.",
}


def confidence_meaning(level: str, *, has_area: bool) -> str:
    """What this confidence level licenses, worded for what the result actually has."""

    source = CONFIDENCE_MEANING if has_area else CONFIDENCE_MEANING_NO_AREA
    return source.get(level, "")

#: What to do instead, when confidence is LOW. Evidence collection, never a
#: business intervention: an aggressive action on an unjudgeable result is exactly
#: the mistake this platform exists to prevent.
LOW_CONFIDENCE_NEXT_STEPS: tuple[str, ...] = (
    "Collect additional comparable history for this KPI so the movement can be tested against a fuller basis.",
    "Validate the affected dimensions — confirm the breakdown is being read along the dimension the business actually manages.",
    "Check data completeness for this date, including any partial loads or missing rows.",
    "Review source freshness, so the measurement is not resting on data that had not finished loading.",
)

NORMAL_HEADLINE = "No corrective action is currently recommended."
NORMAL_BODY = "Performance remains within the expected range. Continue routine monitoring."
LOW_CONFIDENCE_HEADLINE = "Evidence insufficient for targeted action"
LOW_CONFIDENCE_BODY = "No direct intervention is recommended until additional evidence is available."
