"""Contribution analysis: which part of the business accounts for a movement.

This answers exactly one question, and it is not the detection question:

    *The KPI moved. Which approved dimension value explains the largest share of
    that movement?*

Detection already ran, at the KPI level, continuously, and produced an actual, an
expected value, a deviation and one of NORMAL / ABNORMAL / LOW_CONFIDENCE. This
module never re-decides any of that. It takes the movement detection measured and
splits it into parts, using the same KPI, the same governed formula, the same
source table, the same time field and the same comparable dates the stored run
used -- so the parts add up to the whole the business has already seen, and asking
the same question tomorrow gives the same answer.

Four rules hold this module in shape.

**A share is not a verdict.** The largest contributor to a movement is usually
just the largest part of the business. Nothing in a *breakdown* labels an entity
abnormal, assigns it a status or scores it, because entity-level anomaly detection
is a separate analysis with its own comparable history -- and conflating the two
would turn "North is 60% of the company" into "North has a problem". That separate
analysis lives in :func:`classify_entity`, runs only when a person names one
entity, and borrows the platform's own detection engine rather than inventing a
second classification.

**A share is not a cause.** The output is arithmetic on measured values. It says
what a movement is *composed of*, never what produced it.

**The dimension comes from governance, not from the caller.** A breakdown column
is read from an approved :class:`~app.models.kpi.KpiDimension` on the KPI version
in question. A caller names a dimension; it cannot supply a column. That is what
keeps the engine free of any company's vocabulary -- there is no "region", no
"product", no "channel" anywhere in this file, and the same code serves a company
whose hierarchy is Branch -> Service.

**Entitlement is re-checked per row.** Every returned entity is a value the caller
is allowed to see, decided by the same
:meth:`~app.core.deps.AccessContext.permits_scope_value` predicate that governs
document retrieval. A caller who names an entity outside their scope is refused
rather than served, whether they reached it by drilling down or by typing it in.

What the shares are computed against deserves stating plainly: the *whole* KPI
movement, including any part of it the caller may not see. That is deliberate.
The alternative -- re-basing the percentages on the visible rows -- would tell a
regionally scoped reader that their own region explains 100% of a company
movement, which is false. Reporting how much of the movement the visible rows
account for is honest; silently redefining the denominator is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.core.clock import utcnow
from app.core.config import settings
from app.core.deps import AccessContext
from app.core.errors import Conflict, NotFound, ValidationFailure
from app.models.base import Aggregation
from app.models.detection import ContributionRun, DetectionRun
from app.models.kpi import KpiDimension, KpiVersion
from app.services import detection, investigation_map, kpi_breakdown
from app.services.detection import KpiBinding
from app.services.kpi_formula import FilterSpec, FormulaSpec
from app.services.kpi_sql import KpiResult, KpiValue
from app.services.robust_stats import median

#: What this module accepts as a dimension: a governed row, or -- for a KPI whose
#: dimensions are not registered yet -- a declared stand-in carrying the same
#: attributes. The engine reads ``dimension_name``, ``source_column``,
#: ``source_table`` and ``hierarchy`` off either and branches on neither, which is
#: what lets the declared map be deleted later without touching this file.
Dimension = KpiDimension | investigation_map.MappedDimension

#: How many groups one breakdown read may return. Ordered largest-first by the
#: execution layer, so a cap keeps the contributors that matter and drops a tail
#: -- and the drop is reported, because it is the one thing that can make the
#: parts fail to reconcile with the whole.
MAX_BREAKDOWN_ROWS = 500

#: What a null dimension value is called on screen. A row whose dimension is not
#: set is a real part of the business and is never folded into another group or
#: quietly dropped; SQL's ``GROUP BY`` keeps it, and so does this.
UNSET_LABEL = "(not set)"


# ---------------------------------------------------------------------------
# Which dimensions may be used
# ---------------------------------------------------------------------------
def available_dimensions(session: Session, version: KpiVersion) -> list[Dimension]:
    """The approved breakdowns for this KPI version, default first.

    ``allowed`` is the governance switch: a dimension registered but not allowed
    is a declared column, not a permitted analysis, and never reaches a query.

    A KPI with no approved dimension at all falls back to
    :func:`app.services.investigation_map.dimensions_for`, which returns whatever
    was declared for that KPI's own source table -- in declared order, which is the
    order of the hierarchy. That fallback is metadata, not data: it says which
    column a breakdown may read, never what the breakdown will find. Registering
    dimensions on the KPI takes precedence over it in every case, so the map goes
    quiet on its own as governance catches up.
    """

    rows = session.scalars(
        select(KpiDimension).where(
            KpiDimension.company_id == version.company_id,
            KpiDimension.kpi_version_id == version.id,
        )
    ).all()
    permitted = [row for row in rows if row.allowed]
    if not permitted:
        return list(investigation_map.dimensions_for(session, version))
    return sorted(
        permitted,
        key=lambda row: (not row.is_default_breakdown, row.dimension_name.lower()),
    )


def resolve_dimension(
    session: Session,
    version: KpiVersion,
    name: str | None,
) -> Dimension:
    """The dimension to break down by: the one named, or this KPI's default.

    Naming nothing is the common case -- the investigation surface opens on
    whichever breakdown the KPI registered as its default -- and naming something
    unapproved is refused with the list of what is approved, so the caller learns
    the company's own vocabulary rather than guessing at it.
    """

    dimensions = available_dimensions(session, version)
    if not dimensions:
        raise Conflict(
            f"'{version.definition.name}' has no approved dimension to break down by. "
            "A breakdown reads a dimension registered with the KPI and marked "
            "allowed; it does not choose a column on its own.",
            details={"kpi_version_id": version.id},
        )

    wanted = (name or "").strip()
    if not wanted:
        return dimensions[0]

    for dimension in dimensions:
        if dimension.dimension_name.lower() == wanted.lower():
            return dimension

    raise NotFound(
        f"'{wanted}' is not an approved dimension of this KPI.",
        details={"approved": [row.dimension_name for row in dimensions]},
    )


def next_dimensions(
    session: Session,
    version: KpiVersion,
    dimension: Dimension,
) -> list[str]:
    """Where a drill-down may go next, from this dimension's registered hierarchy.

    The hierarchy is the company's, declared at registration: Region -> Product
    for one company, Country -> Category for another, Branch -> Service for a
    third. Anything it names that is not itself an approved dimension is dropped
    rather than offered, because offering it would produce a dead end one click
    later.
    """

    approved = {row.dimension_name.lower(): row.dimension_name for row in available_dimensions(session, version)}
    out: list[str] = []
    for candidate in dimension.hierarchy or []:
        key = str(candidate).strip().lower()
        if key and key != dimension.dimension_name.lower() and key in approved:
            name = approved[key]
            if name not in out:
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# Entity path: the ancestors already chosen in a drill-down
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EntitySelection:
    """One narrowing already applied: an approved dimension and a permitted value."""

    dimension: Dimension
    value: str

    @property
    def name(self) -> str:
        return self.dimension.dimension_name


def resolve_selection(
    session: Session,
    version: KpiVersion,
    access: AccessContext,
    dimension_name: str,
    value: str,
) -> EntitySelection:
    """Turn "Region = North" into a governed, entitled narrowing -- or refuse it.

    Both halves are checked, in this order: the dimension must be approved for
    this KPI version, and the value must be inside the caller's row scope. The
    second check is the one that matters most here, because a drill-down is the
    one place a caller supplies a business value by hand.
    """

    dimension = resolve_dimension(session, version, dimension_name)
    stated = (value or "").strip()
    if not stated:
        raise ValidationFailure(f"A value is required to narrow by {dimension.dimension_name}.")
    if not access.permits_scope_value(dimension.dimension_name, stated):
        raise NotFound(
            f"No {dimension.dimension_name} matching '{stated}' is available to you.",
        )
    return EntitySelection(dimension=dimension, value=stated)


def _narrowed_spec(spec: FormulaSpec, selections: list[EntitySelection]) -> FormulaSpec:
    """The KPI's own formula, plus one equality filter per chosen ancestor.

    Built through the governed filter grammar rather than by string surgery, so a
    drill-down is expressed the same way the KPI's registered filters are and is
    validated and quoted by the same code on the way to the source.
    """

    if not selections:
        return spec
    extra = [
        FilterSpec(
            column=selection.dimension.source_column,
            operator="=",
            value=selection.value,
            table=selection.dimension.source_table,
        )
        for selection in selections
    ]
    return FormulaSpec(
        kind=spec.kind,
        numerator=spec.numerator,
        denominator=spec.denominator,
        filters=[*spec.filters, *extra],
        null_handling=spec.null_handling,
    )


# ---------------------------------------------------------------------------
# Additivity: whether shares of a movement mean anything at all
# ---------------------------------------------------------------------------
def is_additive(spec: FormulaSpec) -> bool:
    """Whether the parts of this KPI sum to the whole.

    Only a total does. A ratio, an average, a minimum, a maximum and a distinct
    count do not: the average of the parts is not the average of the whole, and
    two regions' distinct customers overlap. For those KPIs this module still
    reports each part's own movement -- which is a real measurement -- but reports
    no share, because a percentage of a movement would not be arithmetic.
    """

    if spec.denominator is not None:
        return False
    measure = spec.numerator
    if measure.is_count_star:
        return True
    if measure.effective_aggregation == Aggregation.COUNT_DISTINCT:
        return False
    return measure.aggregation in {Aggregation.SUM, Aggregation.COUNT}


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Contributor:
    """One part of the business, and what it did -- not a judgement about it."""

    entity: str | None
    label: str
    actual: float | None
    expected: float | None
    change: float | None
    #: Signed share of the KPI's movement, in percent. ``None`` when the KPI is
    #: not additive or the KPI did not move.
    share_pct: float | None
    #: Size of that share regardless of direction, which is what a ranking is on.
    absolute_share_pct: float | None
    #: How many comparable dates this entity actually had a value on. A small
    #: number is the honest reason to distrust its expected value.
    reference_count: int
    matched_rows: int | None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "label": self.label,
            "actual": self.actual,
            "expected": self.expected,
            "change": self.change,
            "share_pct": self.share_pct,
            "absolute_share_pct": self.absolute_share_pct,
            "reference_count": self.reference_count,
            "matched_rows": self.matched_rows,
            "note": self.note,
        }


@dataclass(frozen=True)
class ContributionAnalysis:
    """A movement, split into parts, with everything needed to defend the split."""

    kpi_key: str
    kpi_name: str
    kpi_version: int
    kpi_version_id: str
    target_date: date
    unit: str | None
    currency: str | None

    dimension: str
    #: The ancestors already chosen, as ``[{"dimension": ..., "value": ...}]``.
    path: list[dict[str, str]]

    kpi_actual: float | None
    kpi_expected: float | None
    kpi_movement: float | None
    #: The detection verdict for the KPI, carried through unchanged. It belongs to
    #: the KPI and to no contributor.
    kpi_status: str | None
    comparison_label: str | None

    contributors: list[Contributor]
    top_k: int
    ranked_count: int
    #: How much of the KPI's movement the returned contributors account for.
    explained_pct: float | None
    #: Movement the breakdown does not reconcile: a dropped tail, rows outside the
    #: caller's scope, or a KPI whose parts do not sum.
    unexplained_pct: float | None
    #: True when the leading contributor alone accounts for most of the movement,
    #: which is a reason for a person to stop drilling -- not a verdict.
    leader_is_sufficient: bool
    sufficiency_pct: float
    additive: bool
    shares_available: bool

    next_dimensions: list[str]
    withheld_count: int
    reference_dates: list[date]
    warnings: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    detection_run_id: str | None = None

    # -- renderings ---------------------------------------------------------
    @property
    def movement_pct(self) -> float | None:
        """The movement as a percentage of what was expected.

        Computed here rather than on the surface so that every reader divides by the
        same thing -- the size of the expectation, sign discarded, which keeps a
        movement away from a negative expectation from reporting a reversed
        direction. ``None`` when there is nothing to divide by, which is a fact to
        display, not a zero.
        """

        if self.kpi_movement is None or self.kpi_expected in (None, 0):
            return None
        return self.kpi_movement / abs(self.kpi_expected) * 100.0

    def business_view(self) -> dict:
        """What the investigation surface shows. No SQL, no statistics, no scores."""

        return {
            "kpi": self.kpi_name,
            "kpi_key": self.kpi_key,
            "target_date": self.target_date.isoformat(),
            "dimension": self.dimension,
            "path": self.path,
            "actual": self.kpi_actual,
            "expected": self.kpi_expected,
            "movement": self.kpi_movement,
            "movement_pct": self.movement_pct,
            "status": self.kpi_status,
            "comparison": self.comparison_label,
            "unit": self.unit,
            "currency": self.currency,
            "contributors": [item.as_dict() for item in self.contributors],
            "top_k": self.top_k,
            "ranked_count": self.ranked_count,
            "explained_pct": self.explained_pct,
            "unexplained_pct": self.unexplained_pct,
            "leader_is_sufficient": self.leader_is_sufficient,
            "sufficiency_pct": self.sufficiency_pct,
            "shares_available": self.shares_available,
            "next_dimensions": self.next_dimensions,
            "notes": self.warnings,
        }

    def evidence(self) -> dict:
        """The technical record, for callers entitled to see method."""

        return {
            "kpi_version": self.kpi_version,
            "kpi_version_id": self.kpi_version_id,
            "detection_run_id": self.detection_run_id,
            "dimension": self.dimension,
            "additive": self.additive,
            "reference_dates": [day.isoformat() for day in self.reference_dates],
            "withheld_by_scope": self.withheld_count,
            "queries": self.queries,
        }


# ---------------------------------------------------------------------------
# Reading one breakdown
# ---------------------------------------------------------------------------
def _entity_key(value: Any) -> str | None:
    """A dimension value as text, or ``None`` for a genuinely unset one."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _breakdown(
    connector: DataSourceConnector,
    binding: KpiBinding,
    spec: FormulaSpec,
    dimension: Dimension,
    day: date,
) -> tuple[dict[str | None, KpiValue], str]:
    """The KPI, grouped by one dimension, for one day. Keyed by entity.

    Delegated to :func:`app.services.kpi_breakdown.read_kpi`, which decides whether
    the dimension can be read alongside the KPI or has to be apportioned to it. A
    dimension recorded on the KPI's own table takes the same path detection takes,
    and produces the same query.
    """

    result = kpi_breakdown.read_kpi(
        connector,
        binding,
        spec,
        day=day,
        dimension=dimension,
        limit=MAX_BREAKDOWN_ROWS,
    )
    column = dimension.source_column
    rows: dict[str | None, KpiValue] = {}
    for row in result.rows:
        rows[_entity_key(row.group.get(column))] = row
    return rows, result.sql


def _with_display_labels(
    connector: DataSourceConnector,
    dimension: Dimension,
    contributors: list[Contributor],
) -> list[Contributor]:
    """Give each contributor a human name, where the source carries one.

    Only the label changes. ``entity`` stays the value the source holds, because it
    is what a drill-down filters on and what a stored run is compared against later
    -- a display name that quietly became the key would break both.
    """

    codes = [item.entity for item in contributors if item.entity is not None]
    if not codes:
        return contributors
    names = kpi_breakdown.labels_for(connector, dimension, codes)
    if not names:
        return contributors
    return [
        item
        if item.entity is None or item.entity not in names
        else replace(item, label=names[item.entity])
        for item in contributors
    ]


def _reference_dates(run: DetectionRun | None) -> list[date]:
    """The comparable dates the stored detection run used, most recent last.

    Reusing them is what makes a breakdown reconcile with the number on screen.
    Choosing fresh ones here would produce parts of a different whole -- the same
    KPI, the same date, and an expectation the business never saw.
    """

    if run is None:
        return []
    out: list[date] = []
    for raw in run.reference_dates or []:
        try:
            out.append(date.fromisoformat(str(raw)))
        except (TypeError, ValueError):  # pragma: no cover - stored by this platform
            continue
    return sorted(out)


def analyse(
    session: Session,
    access: AccessContext,
    connector: DataSourceConnector,
    binding: KpiBinding,
    run: DetectionRun,
    dimension: Dimension,
    *,
    selections: list[EntitySelection] | None = None,
    top_k: int | None = None,
) -> ContributionAnalysis:
    """Split the movement in ``run`` across one approved dimension.

    Deterministic throughout: the KPI comes from its registration, the movement
    from the stored run, the comparable dates from that same run, and each part's
    expected value from the same robust median the engine used on the whole. Given
    the same stored run and the same source data, this returns the same answer
    every time.
    """

    warnings: list[str] = []
    queries: list[str] = []
    chosen = list(selections or [])
    spec = _narrowed_spec(binding.spec, chosen)
    requested_k = top_k if top_k is not None else settings.contribution_top_k
    effective_k = max(1, min(int(requested_k), settings.contribution_max_top_k))
    if requested_k != effective_k:
        warnings.append(
            f"Top {requested_k} was requested; {effective_k} is the maximum this "
            "platform returns in one breakdown."
        )

    additive = is_additive(spec)
    if not additive:
        warnings.append(
            f"'{binding.name}' is an average, a ratio or a distinct count, so its parts "
            "do not add up to the whole. Each value below is that part's own movement, "
            "measured the same way; no share of the KPI movement is reported, because "
            "there is no arithmetic that would make one true."
        )

    # A dimension recorded in finer detail than the KPI is measured in has to be
    # apportioned to it. Saying so is not a footnote: it is the reason the parts
    # still reconcile with the number above them, and the reason a small remainder
    # may not be attributable to any of them.
    divided = kpi_breakdown.apportionment_note(binding, dimension)
    if divided:
        warnings.append(divided)

    # --- the movement being split, exactly as the business already saw it ------
    kpi_actual = run.actual_value
    kpi_expected = run.expected_value
    kpi_movement = (
        kpi_actual - kpi_expected
        if kpi_actual is not None and kpi_expected is not None
        else None
    )

    # --- the target date, broken down -----------------------------------------
    target_rows, target_sql = _breakdown(connector, binding, spec, dimension, run.target_date)
    queries.append(target_sql)
    if len(target_rows) >= MAX_BREAKDOWN_ROWS:
        warnings.append(
            f"{binding.name} has at least {MAX_BREAKDOWN_ROWS:,} distinct "
            f"{dimension.dimension_name} values on this date. The largest were kept, so "
            "the shares below are still measured against the whole movement, but a long "
            "tail of small contributors is not listed."
        )

    # --- the comparable dates, broken down the same way -----------------------
    references = _reference_dates(run)
    cap = settings.contribution_max_reference_dates
    if len(references) > cap:
        dropped = len(references) - cap
        references = references[-cap:]
        warnings.append(
            f"The KPI was compared against {len(references) + dropped} dates; the "
            f"{cap} most recent were used for this breakdown, so an entity's expected "
            "value here may rest on a shorter history than the KPI's own."
        )
    if not references:
        warnings.append(
            "The stored run recorded no comparable dates, so no expected value can be "
            "given per contributor. Only what each one actually did on this date is shown."
        )

    # Every day is read first, then the entity universe is taken across all of
    # them. Filling history day by day would give an entity that is absent from
    # one date a history made only of the dates it appeared on, quietly raising
    # its expectation and inflating the movement attributed to it.
    per_day: list[dict[str | None, KpiValue]] = []
    for day in references:
        rows, sql = _breakdown(connector, binding, spec, dimension, day)
        queries.append(sql)
        per_day.append(rows)

    entities: set[str | None] = set(target_rows)
    for rows in per_day:
        entities |= set(rows)

    treat_missing_as_zero = additive and spec.null_handling == "TREAT_AS_ZERO"
    history: dict[str | None, list[float]] = {}
    for entity in entities:
        series: list[float] = []
        for rows in per_day:
            value = rows.get(entity)
            if value is not None and value.value is not None:
                series.append(value.value)
            elif treat_missing_as_zero:
                # The entity had no rows that day. For a total, that is a measured
                # zero, exactly as the whole-KPI read would report it.
                series.append(0.0)
        history[entity] = series

    # --- assemble, entitlement first ------------------------------------------
    withheld = 0
    prepared: list[Contributor] = []
    for entity in entities:
        label = entity if entity is not None else UNSET_LABEL
        if entity is not None and not access.permits_scope_value(
            dimension.dimension_name, entity
        ):
            withheld += 1
            continue
        row = target_rows.get(entity)
        actual = row.value if row is not None else (0.0 if treat_missing_as_zero else None)
        values = history.get(entity, [])
        expected = median(values) if values else None
        change = (
            actual - expected if actual is not None and expected is not None else None
        )
        prepared.append(
            Contributor(
                entity=entity,
                label=label,
                actual=actual,
                expected=expected,
                change=change,
                share_pct=None,
                absolute_share_pct=None,
                reference_count=len(values),
                matched_rows=row.matched_rows if row is not None else 0,
                note=row.note if row is not None else None,
            )
        )

    if withheld:
        warnings.append(
            f"{withheld} {dimension.dimension_name} value(s) are outside your data "
            "scope and are not listed. The percentages are still measured against the "
            "whole KPI movement, so they do not add up to 100%."
        )

    # --- shares, against the whole movement -----------------------------------
    shares_available = additive and kpi_movement not in (None, 0)
    if additive and kpi_movement in (None, 0):
        warnings.append(
            "The KPI's expected and actual values are the same, or one of them was not "
            "recorded, so there is no movement to apportion."
        )

    scaled: list[Contributor] = []
    for item in prepared:
        share = None
        absolute = None
        if shares_available and item.change is not None and kpi_movement:
            share = item.change / kpi_movement * 100.0
            absolute = abs(share)
        scaled.append(
            Contributor(
                entity=item.entity,
                label=item.label,
                actual=item.actual,
                expected=item.expected,
                change=item.change,
                share_pct=share,
                absolute_share_pct=absolute,
                reference_count=item.reference_count,
                matched_rows=item.matched_rows,
                note=item.note,
            )
        )

    # Ranked by how much of the movement each accounts for, largest first. Sign is
    # deliberately ignored: a part moving hard against the KPI explains as much of
    # the movement as one moving with it.
    scaled.sort(
        key=lambda item: (
            item.change is None,
            -abs(item.change or 0.0),
            item.label.lower(),
        )
    )
    ranked_count = len(scaled)
    contributors = _with_display_labels(connector, dimension, scaled[:effective_k])

    explained = None
    unexplained = None
    if shares_available and kpi_movement:
        listed = sum(item.change for item in contributors if item.change is not None)
        explained = listed / kpi_movement * 100.0
        unexplained = 100.0 - explained

    leader = contributors[0] if contributors else None
    sufficiency = settings.contribution_sufficiency_pct
    leader_is_sufficient = bool(
        leader is not None
        and leader.absolute_share_pct is not None
        and leader.absolute_share_pct >= sufficiency
    )

    return ContributionAnalysis(
        kpi_key=binding.kpi_key,
        kpi_name=binding.name,
        kpi_version=binding.version.version,
        kpi_version_id=binding.version.id,
        target_date=run.target_date,
        unit=run.unit,
        currency=run.currency,
        dimension=dimension.dimension_name,
        path=[{"dimension": item.name, "value": item.value} for item in chosen],
        kpi_actual=kpi_actual,
        kpi_expected=kpi_expected,
        kpi_movement=kpi_movement,
        kpi_status=run.status,
        comparison_label=run.comparison_label,
        contributors=contributors,
        top_k=effective_k,
        ranked_count=ranked_count,
        explained_pct=explained,
        unexplained_pct=unexplained,
        leader_is_sufficient=leader_is_sufficient,
        sufficiency_pct=sufficiency,
        additive=additive,
        shares_available=shares_available,
        next_dimensions=next_dimensions(session, binding.version, dimension),
        withheld_count=withheld,
        reference_dates=references,
        warnings=warnings,
        queries=queries,
        detection_run_id=run.id,
    )


# ---------------------------------------------------------------------------
# Storing the result
# ---------------------------------------------------------------------------
def persist_analysis(
    session: Session,
    analysis: ContributionAnalysis,
    *,
    entry_point: str = "AUTOMATIC",
    executed_by_user_id: str | None = None,
    duration_ms: int | None = None,
) -> ContributionRun:
    """Store an investigation so it can be re-displayed, audited and defended.

    The ranked parts are stored as returned, which is what lets a months-old
    investigation be shown again without re-querying a source whose rows have moved
    on -- the same reason a detection run keeps its reference values.

    ``leader_entity`` and ``leader_share_pct`` are the top row of the ranking,
    denormalised so a history list can be built without opening the JSON. They
    record which part accounted for the most movement and are not, and must not be
    read as, a finding about that part.
    """

    version = session.get(KpiVersion, analysis.kpi_version_id)
    if version is None:  # pragma: no cover - the analysis was built from this row
        raise NotFound("The KPI version this analysis was computed on no longer exists.")

    leader = analysis.contributors[0] if analysis.contributors else None
    row = ContributionRun(
        company_id=version.company_id,
        detection_run_id=analysis.detection_run_id,
        kpi_definition_id=version.kpi_id,
        kpi_version_id=analysis.kpi_version_id,
        kpi_key=analysis.kpi_key,
        kpi_name=analysis.kpi_name,
        kpi_version=analysis.kpi_version,
        target_date=analysis.target_date,
        unit=analysis.unit,
        currency=analysis.currency,
        dimension=analysis.dimension,
        path=analysis.path,
        depth=len(analysis.path),
        entry_point=entry_point,
        kpi_actual=analysis.kpi_actual,
        kpi_expected=analysis.kpi_expected,
        kpi_movement=analysis.kpi_movement,
        kpi_status=analysis.kpi_status,
        contributors=[item.as_dict() for item in analysis.contributors],
        top_k=analysis.top_k,
        ranked_count=analysis.ranked_count,
        explained_pct=analysis.explained_pct,
        unexplained_pct=analysis.unexplained_pct,
        leader_entity=None if leader is None else leader.label[:200],
        leader_share_pct=None if leader is None else leader.share_pct,
        leader_is_sufficient=analysis.leader_is_sufficient,
        additive=analysis.additive,
        shares_available=analysis.shares_available,
        reference_dates=[day.isoformat() for day in analysis.reference_dates],
        withheld_count=analysis.withheld_count,
        warnings=analysis.warnings,
        queries=analysis.queries,
        query_count=len(analysis.queries),
        duration_ms=duration_ms,
        executed_by_user_id=executed_by_user_id,
        executed_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# One entity, over time: the manual analysis case
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EntityProfile:
    """One part of the business, measured over a window and judged on one date.

    Two halves, and the difference between them matters.

    The **trend** -- ``points``, ``latest``, ``typical`` -- is a measured history for
    a person to read. The **verdict** -- ``status`` and the figures around it -- is
    the platform's own detection engine, run on this entity's own comparable
    history because someone named this entity. Both exist only on request. Nothing
    on this platform sweeps entities: detection runs for every KPI every day, and
    an entity is analysed when a person asks for it and not before.

    The status is the engine's, not a second opinion. It comes from the same
    classification, the same approved comparison policy and the same registered
    tolerance the KPI itself is judged by, so ABNORMAL means here exactly what it
    means on the dashboard. ``None`` means the engine was not run -- never that it
    was run and found nothing.
    """

    kpi_key: str
    kpi_name: str
    kpi_version: int
    dimension: str
    entity: str
    unit: str | None
    currency: str | None
    points: list[dict[str, Any]]
    latest: float | None
    typical: float | None
    change_vs_typical: float | None
    change_pct_vs_typical: float | None
    observed_days: int
    warnings: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    # --- the engine's verdict for this entity on the target date ---------------
    target_date: date | None = None
    status: str | None = None
    expected: float | None = None
    variance: float | None = None
    variance_pct: float | None = None
    direction: str | None = None
    headline: str | None = None
    status_reason: str | None = None
    comparison_label: str | None = None
    reference_dates: list[str] = field(default_factory=list)
    share_of_kpi_pct: float | None = None

    def business_view(self) -> dict:
        return {
            "kpi": self.kpi_name,
            "kpi_key": self.kpi_key,
            "dimension": self.dimension,
            "entity": self.entity,
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "unit": self.unit,
            "currency": self.currency,
            "points": self.points,
            "latest": self.latest,
            "typical": self.typical,
            "change_vs_typical": self.change_vs_typical,
            "change_pct_vs_typical": self.change_pct_vs_typical,
            "observed_days": self.observed_days,
            # The engine's verdict for this entity. One classification, the KPI's
            # own -- these names mean what they mean everywhere else.
            "actual": self.latest,
            "expected": self.expected,
            "variance": self.variance,
            "variance_pct": self.variance_pct,
            "direction": self.direction,
            "status": self.status,
            "headline": self.headline,
            "status_reason": self.status_reason,
            "comparison_label": self.comparison_label,
            "share_of_kpi_pct": self.share_of_kpi_pct,
            "notes": self.warnings,
        }


def profile_entity(
    connector: DataSourceConnector,
    binding: KpiBinding,
    dimension: Dimension,
    selection: EntitySelection,
    days: list[date],
) -> EntityProfile:
    """Evaluate the KPI for one entity across ``days``, through its own formula.

    One read per day, narrowed to the entity by a governed filter -- the same
    source, formula and time field the KPI is registered with. ``typical`` is the
    robust median of the window, the same statistic the detection engine uses for an
    expectation, so the comparison a reader makes here is the same kind of
    comparison the platform makes for a KPI.
    """

    spec = _narrowed_spec(binding.spec, [selection])
    points: list[dict[str, Any]] = []
    queries: list[str] = []
    warnings: list[str] = []
    values: list[float] = []

    divided = kpi_breakdown.apportionment_note(binding, selection.dimension)
    if divided:
        warnings.append(divided)

    for day in sorted(days):
        result = kpi_breakdown.read_kpi(connector, binding, spec, day=day)
        queries.append(result.sql)
        row = result.scalar
        value = row.value if row is not None else None
        if value is not None:
            values.append(value)
        points.append(
            {
                "date": day.isoformat(),
                "value": value,
                "matched_rows": row.matched_rows if row is not None else None,
                "note": row.note if row is not None else None,
            }
        )

    latest = points[-1]["value"] if points else None
    # The comparison excludes the day being read, so the day is not part of its own
    # expectation -- the same separation the detection engine keeps.
    earlier = [
        point["value"]
        for point in points[:-1]
        if point["value"] is not None
    ]
    typical = median(earlier) if earlier else None
    change = latest - typical if latest is not None and typical is not None else None
    change_pct = (
        change / abs(typical) * 100.0
        if change is not None and typical not in (None, 0)
        else None
    )

    if len(earlier) < 3:
        warnings.append(
            f"Only {len(earlier)} earlier day(s) in this window had a value for "
            f"{selection.value}, which is too little to call anything typical. The "
            "figures stand; the comparison does not."
        )
    if not values:
        warnings.append(
            f"{binding.name} produced no value for {selection.value} on any day in this "
            "window. That may mean no rows, or a source that could not be read."
        )

    return EntityProfile(
        kpi_key=binding.kpi_key,
        kpi_name=binding.name,
        kpi_version=binding.version.version,
        dimension=dimension.dimension_name,
        entity=selection.value,
        unit=binding.version.unit,
        currency=binding.version.currency,
        points=points,
        latest=latest,
        typical=typical,
        change_vs_typical=change,
        change_pct_vs_typical=change_pct,
        observed_days=len(values),
        warnings=warnings,
        queries=queries,
    )


def classify_entity(
    session: Session,
    connector: DataSourceConnector,
    binding: KpiBinding,
    dimension: Dimension,
    selection: EntitySelection,
    target_date: date,
    *,
    profile: EntityProfile,
    kpi_actual: float | None = None,
) -> EntityProfile:
    """Run the platform's own detection engine on one entity, once, on request.

    This is the whole of entity-level anomaly detection on this platform, and its
    shape is deliberate: there is no scheduled sweep, no per-entity monitoring and
    no second opinion. :func:`app.services.detection.detect` decides the verdict --
    the same comparable-date policy, the same robust expectation, the same
    dispersion, the same modified z-score, the same registered tolerance and the
    same wording the KPI itself is judged by. All this function does is narrow
    *what is read* to the one entity that was asked about, through the governed
    filter the rest of this module uses, and hand the engine the reader that knows
    how to reach a dimension recorded at a finer grain than the KPI.

    Two consequences worth being explicit about, because both are safety
    properties rather than conveniences:

    * The KPI's registered tolerance travels unchanged. An *absolute* threshold is
      naturally conservative at this scale -- one part of the business moves by
      less than the whole, so it breaches such a threshold less often, never more
      -- and the statistical route is scale-free, being measured against this
      entity's own comparable history and nothing else's. So an entity is never
      flagged merely for being small, and never excused for it either.
    * The verdict belongs to the entity, not to the KPI. It does not change the
      KPI's own status, is not persisted as a detection run, and nothing on the
      dashboard reads it.
    """

    config, config_row = detection.policy_for(
        session, binding.version.company_id, binding.kpi_key
    )
    narrowed = _narrowed_spec(binding.spec, [selection])

    def read_entity(
        conn: DataSourceConnector, bound: KpiBinding, day: date
    ) -> KpiResult:
        return kpi_breakdown.read_kpi(conn, bound, narrowed, day=day)

    outcome = detection.detect(
        connector,
        binding,
        config,
        target_date,
        materiality=binding.version.materiality,
        config_row=config_row,
        read=read_entity,
    )

    variance = outcome.deviation_absolute
    if variance is None or abs(variance) < 1e-12:
        direction = "FLAT"
    else:
        direction = "UP" if variance > 0 else "DOWN"

    # The entity's share of the KPI on this date -- a relative size, offered so a
    # reader knows how much of the business this verdict is about. It is measured
    # against the KPI's own stored figure, never against a total recomputed here,
    # and is omitted rather than guessed when there is nothing to divide by.
    share = None
    if kpi_actual not in (None, 0) and outcome.actual is not None:
        share = outcome.actual / abs(kpi_actual) * 100.0

    return replace(
        profile,
        target_date=target_date,
        status=str(outcome.status),
        expected=outcome.expected,
        variance=variance,
        variance_pct=outcome.deviation_pct,
        direction=direction,
        headline=outcome.headline,
        status_reason=outcome.reason,
        comparison_label=outcome.comparison_label,
        reference_dates=[point.day.isoformat() for point in outcome.references],
        share_of_kpi_pct=share,
        warnings=[*profile.warnings, *outcome.notes],
        queries=[*profile.queries, *([outcome.source.get("query")] if outcome.source.get("query") else [])],
    )


# ---------------------------------------------------------------------------
# Which entities are worth looking at: the picker behind "choose an entity"
# ---------------------------------------------------------------------------
def top_entities(
    connector: DataSourceConnector,
    access: AccessContext,
    binding: KpiBinding,
    dimension: Dimension,
    day: date,
    *,
    selections: list[EntitySelection] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """The dimension's most substantial values on one date, read from the source.

    This exists so that nobody has to type a business value from memory to start an
    analysis. Every entry is measured: the list is one grouped read of the KPI on
    the date in question, ranked by size, entitlement-filtered per row by the same
    predicate that governs a breakdown. Nothing here is enumerated in code -- a
    company that renames its territories tomorrow gets the new names for free, and
    an empty result means the source had no rows, which is worth showing as itself.

    ``share_of_total_pct`` is a share of the date's measured total, not of a
    movement, and is reported only for a KPI whose parts sum -- these are relative
    sizes, offered to help someone choose, and they carry no verdict about any
    entity on the list.
    """

    spec = _narrowed_spec(binding.spec, list(selections or []))
    rows, _sql = _breakdown(connector, binding, spec, dimension, day)

    permitted: list[tuple[str | None, KpiValue]] = []
    for entity, row in rows.items():
        if entity is not None and not access.permits_scope_value(
            dimension.dimension_name, entity
        ):
            continue
        permitted.append((entity, row))

    permitted.sort(
        key=lambda pair: (
            pair[1].value is None,
            -abs(pair[1].value or 0.0),
            (pair[0] or UNSET_LABEL).lower(),
        )
    )
    chosen = permitted[: max(1, int(limit))]

    total = sum(abs(row.value) for _entity, row in permitted if row.value is not None)
    shares_meaningful = is_additive(spec) and total > 0
    names = kpi_breakdown.labels_for(
        connector, dimension, [entity for entity, _row in chosen if entity is not None]
    )

    out: list[dict[str, Any]] = []
    for entity, row in chosen:
        label = UNSET_LABEL if entity is None else names.get(entity, entity)
        out.append(
            {
                "entity": entity,
                "label": label,
                "value": row.value,
                "share_of_total_pct": (
                    abs(row.value) / total * 100.0
                    if shares_meaningful and row.value is not None
                    else None
                ),
                "matched_rows": row.matched_rows,
            }
        )
    return out
