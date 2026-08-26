"""Discovery-assisted KPI proposals.

The platform reads the profiled catalog and *proposes* candidate KPIs. It never
decides what a business metric means. Every proposal arrives with the evidence
that produced it, so an administrator can approve, edit or reject it on informed
grounds — the governed-contract principle from the architecture.

Proposals are deterministic and derived from structure, with a business-vocabulary
hint layered on top for naming only:

* an additive numeric measure          -> ``SUM(...)``
* an identifier that repeats           -> ``COUNT(DISTINCT ...)``  (a real count)
* a 0/1 flag                           -> ``SUM(flag) / COUNT(*)`` (a rate)
* a value measure over an order count  -> ``SUM(...) / COUNT(DISTINCT ...)`` (an average)

Naming hints never affect the *formula* — only the suggested label. A column
called ``order_value`` is proposed as Revenue because that is what the business
usually calls it, but the calculation comes from the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import (
    Aggregation,
    DriverType,
    JoinSafetyLevel,
    KpiKind,
    SemanticType,
    TimeGrain,
)
from app.models.profiling import ColumnProfile, JoinSafety, TableGrain, TableProfile, TableRelationship
from app.models.source import SelectedTable, SourceColumn, SourceTable

# --- Naming vocabulary (labels only, never formulas) -----------------------
_VALUE_HINTS = {
    "revenue": ("Revenue", "Total recognised revenue.", "HIGH"),
    "order_value": ("Revenue", "Total recognised sales revenue across orders.", "HIGH"),
    "item_value": ("Item Revenue", "Total value of individual order line items.", "MEDIUM"),
    "net_revenue": ("Net Revenue", "Revenue net of returns and adjustments.", "HIGH"),
    "gross_revenue": ("Gross Revenue", "Revenue before deductions.", "HIGH"),
    "sales_amount": ("Revenue", "Total sales amount.", "HIGH"),
    "amount": ("Total Amount", "Sum of transaction amounts.", "MEDIUM"),
    "total_value": ("Total Value", "Sum of transaction values.", "MEDIUM"),
    "campaign_spend": ("Marketing Spend", "Total campaign investment.", "MEDIUM"),
    "spend": ("Spend", "Total spend.", "MEDIUM"),
    "cost": ("Cost", "Total cost.", "MEDIUM"),
    "quantity": ("Units Sold", "Total units sold. Supporting metric for volume analysis.", "LOW"),
    "units": ("Units Sold", "Total units sold.", "LOW"),
    "qty": ("Units Sold", "Total units sold.", "LOW"),
    "impressions": ("Impressions", "Total advertising impressions delivered.", "LOW"),
    "clicks": ("Clicks", "Total advertising clicks.", "LOW"),
}

_COUNT_HINTS = {
    "order_id": ("Orders", "Number of distinct orders placed.", "HIGH"),
    "customer_id": ("Unique Customers", "Number of distinct customers who transacted.", "HIGH"),
    "product_id": ("Products Sold", "Number of distinct products sold.", "LOW"),
    "campaign_id": ("Active Campaigns", "Number of distinct campaigns running.", "LOW"),
    "session_id": ("Sessions", "Number of distinct sessions.", "MEDIUM"),
    "invoice_id": ("Invoices", "Number of distinct invoices.", "MEDIUM"),
}

# Ratios worth proposing: (value column hint, count column hint) -> label
_RATIO_HINTS = {
    ("order_value", "order_id"): (
        "aov",
        "Average Order Value (AOV)",
        "Average revenue per order. Revenue divided by order count.",
        "HIGH",
    ),
    ("revenue", "order_id"): (
        "aov",
        "Average Order Value (AOV)",
        "Average revenue per order.",
        "HIGH",
    ),
    ("order_value", "customer_id"): (
        "revenue_per_customer",
        "Revenue per Customer",
        "Average revenue generated per distinct customer.",
        "MEDIUM",
    ),
    ("item_value", "order_id"): (
        "average_basket_value",
        "Average Basket Value",
        "Average line-item value per order.",
        "MEDIUM",
    ),
}

_DRIVER_HINTS: dict[str, str] = {
    "quantity": DriverType.VOLUME,
    "units": DriverType.VOLUME,
    "qty": DriverType.VOLUME,
    "unit_price": DriverType.PRICE,
    "price": DriverType.PRICE,
    "list_price": DriverType.PRICE,
    "discount": DriverType.PRICE,
    "sector": DriverType.MIX,
    "category": DriverType.MIX,
    "segment": DriverType.MIX,
    "channel": DriverType.MIX,
    "campaign_spend": DriverType.MARKETING,
    "spend": DriverType.MARKETING,
    "impressions": DriverType.MARKETING,
    "clicks": DriverType.MARKETING,
    "stock": DriverType.SUPPLY,
    "inventory": DriverType.SUPPLY,
    "units_on_hand": DriverType.SUPPLY,
}

# A breakdown with more distinct values than this is an entity list, not a
# dimension. Keeping it low is what stops "monitor every product" creeping in.
MAX_DIMENSION_CARDINALITY = 200
MIN_DIMENSION_CARDINALITY = 2


@dataclass(slots=True)
class DimensionProposal:
    dimension_name: str
    source_table_id: str
    source_table: str
    source_column: str
    approx_cardinality: int | None
    is_default_breakdown: bool = False
    notes: str | None = None

    def as_dict(self) -> dict:
        return {
            "dimension_name": self.dimension_name,
            "source_table_id": self.source_table_id,
            "source_table": self.source_table,
            "source_column": self.source_column,
            "approx_cardinality": self.approx_cardinality,
            "is_default_breakdown": self.is_default_breakdown,
            "notes": self.notes,
        }


@dataclass(slots=True)
class DriverProposal:
    driver_name: str
    driver_type: str
    source_table_id: str | None
    source_table: str | None
    source_column: str | None
    controllable: bool
    measurement_method: str | None = None

    def as_dict(self) -> dict:
        return {
            "driver_name": self.driver_name,
            "driver_type": self.driver_type,
            "source_table_id": self.source_table_id,
            "source_table": self.source_table,
            "source_column": self.source_column,
            "controllable": self.controllable,
            "measurement_method": self.measurement_method,
        }


@dataclass(slots=True)
class KpiProposal:
    kpi_key: str
    name: str
    business_definition: str
    kind: str
    formula_expression: str
    data_source_id: str
    source_table_id: str
    source_table: str
    time_field: str | None
    time_grain: str
    unit: str | None
    direction: str
    confidence: float
    business_criticality: str
    dimensions: list[DimensionProposal] = field(default_factory=list)
    drivers: list[DriverProposal] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kpi_key": self.kpi_key,
            "name": self.name,
            "business_definition": self.business_definition,
            "kind": self.kind,
            "formula_expression": self.formula_expression,
            "data_source_id": self.data_source_id,
            "source_table_id": self.source_table_id,
            "source_table": self.source_table,
            "time_field": self.time_field,
            "time_grain": self.time_grain,
            "unit": self.unit,
            "direction": self.direction,
            "confidence": self.confidence,
            "business_criticality": self.business_criticality,
            "dimensions": [d.as_dict() for d in self.dimensions],
            "drivers": [d.as_dict() for d in self.drivers],
            "evidence": self.evidence,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def propose_kpis(session: Session, company_id: str) -> list[KpiProposal]:
    """Scan the approved data scope and return candidate KPI contracts."""
    tables = _selected_tables(session, company_id)
    if not tables:
        return []

    profiles = _column_profiles(session, tables)
    table_profiles = _table_profiles(session, tables)
    grains = _grains(session, tables)
    safe_joins = _safe_join_targets(session, tables)

    proposals: list[KpiProposal] = []
    for table in tables:
        grain = grains.get(table.id)
        table_profile = table_profiles.get(table.id)
        if table_profile is None:
            # Unprofiled tables are skipped rather than guessed at: a proposal
            # without cardinality evidence is not reviewable.
            continue

        dimensions = _dimension_proposals(table, profiles, safe_joins.get(table.id, []))
        drivers = _driver_proposals(table, profiles)
        time_field = grain.time_column if grain else None
        time_grain = (grain.time_grain if grain and grain.time_grain else TimeGrain.DAY)

        measures = _measure_candidates(table, profiles)
        counts = _count_candidates(table, profiles)

        for measure in measures:
            proposals.append(
                _simple_proposal(
                    table=table,
                    column=measure["column"],
                    aggregation=Aggregation.SUM,
                    label=measure["label"],
                    definition=measure["definition"],
                    criticality=measure["criticality"],
                    confidence=measure["confidence"],
                    unit="currency" if measure["is_currency"] else "count",
                    time_field=time_field,
                    time_grain=time_grain,
                    dimensions=dimensions,
                    drivers=drivers,
                    profile=profiles.get(measure["column"].id),
                    table_profile=table_profile,
                    grain=grain,
                )
            )

        for count in counts:
            proposals.append(
                _count_proposal(
                    table=table,
                    column=count["column"],
                    label=count["label"],
                    definition=count["definition"],
                    criticality=count["criticality"],
                    confidence=count["confidence"],
                    equals_row_count=count["equals_row_count"],
                    time_field=time_field,
                    time_grain=time_grain,
                    dimensions=dimensions,
                    drivers=drivers,
                    profile=profiles.get(count["column"].id),
                    table_profile=table_profile,
                    grain=grain,
                )
            )

        proposals.extend(
            _ratio_proposals(
                table=table,
                measures=measures,
                counts=counts,
                time_field=time_field,
                time_grain=time_grain,
                dimensions=dimensions,
                drivers=drivers,
                table_profile=table_profile,
                grain=grain,
            )
        )

    # Strongest candidates first, and stable for equal confidence.
    proposals.sort(key=lambda p: (-p.confidence, p.kpi_key))
    return _deduplicate(proposals)


# ---------------------------------------------------------------------------
# Candidate detection
# ---------------------------------------------------------------------------
def _measure_candidates(
    table: SourceTable, profiles: dict[str, ColumnProfile]
) -> list[dict]:
    candidates: list[dict] = []
    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        if column.semantic_type != SemanticType.NUMERIC_MEASURE:
            continue
        profile = profiles.get(column.id)
        if profile is None or profile.access_withheld:
            continue
        # A constant column has nothing to measure.
        if profile.distinct_count is not None and profile.distinct_count <= 1:
            continue

        hint = _match_hint(column.column_name, _VALUE_HINTS)
        label, definition, criticality = hint or (
            f"Total {_titleise(column.column_name)}",
            f"Sum of {table.table_name}.{column.column_name}.",
            "LOW",
        )
        confidence = 0.85 if hint else 0.45
        if profile.null_pct and profile.null_pct > 10:
            confidence -= 0.15
        candidates.append(
            {
                "column": column,
                "label": label,
                "definition": definition,
                "criticality": criticality,
                "confidence": round(max(0.2, confidence), 3),
                "is_currency": _looks_like_currency(column.column_name),
                "hint_key": _hint_key(column.column_name, _VALUE_HINTS),
            }
        )
    return candidates


def _count_candidates(table: SourceTable, profiles: dict[str, ColumnProfile]) -> list[dict]:
    candidates: list[dict] = []
    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        if column.semantic_type != SemanticType.IDENTIFIER:
            continue
        profile = profiles.get(column.id)
        if profile is None or profile.access_withheld or not profile.distinct_count:
            continue
        # Counting a PII identifier is fine; exposing its values is not. The
        # count itself stays available so "Unique Customers" remains possible.
        hint = _match_hint(column.column_name, _COUNT_HINTS)
        label, definition, criticality = hint or (
            f"Distinct {_titleise(column.column_name)}",
            f"Count of distinct {table.table_name}.{column.column_name} values.",
            "LOW",
        )
        equals_row_count = bool(profile.is_unique)
        confidence = 0.85 if hint else 0.4
        if equals_row_count:
            # Still a valid definition, just less interesting: it equals the row
            # count at this grain today.
            confidence -= 0.05
        candidates.append(
            {
                "column": column,
                "label": label,
                "definition": definition,
                "criticality": criticality,
                "confidence": round(max(0.2, confidence), 3),
                "equals_row_count": equals_row_count,
                "hint_key": _hint_key(column.column_name, _COUNT_HINTS),
            }
        )
    return candidates


# ---------------------------------------------------------------------------
# Proposal builders
# ---------------------------------------------------------------------------
def _simple_proposal(
    *,
    table: SourceTable,
    column: SourceColumn,
    aggregation: str,
    label: str,
    definition: str,
    criticality: str,
    confidence: float,
    unit: str | None,
    time_field: str | None,
    time_grain: str,
    dimensions: list[DimensionProposal],
    drivers: list[DriverProposal],
    profile: ColumnProfile | None,
    table_profile: TableProfile | None,
    grain: TableGrain | None,
) -> KpiProposal:
    warnings = _shared_warnings(time_field, table_profile, grain)
    if profile and profile.negative_count:
        warnings.append(
            f"{column.column_name} contains {profile.negative_count} negative value(s); "
            "confirm whether returns should be included."
        )
    if profile and profile.null_pct:
        warnings.append(f"{column.column_name} is {profile.null_pct:.2f}% null.")

    return KpiProposal(
        kpi_key=_slug(label),
        name=label,
        business_definition=definition,
        kind=KpiKind.SIMPLE,
        formula_expression=f"{aggregation}({table.table_name}.{column.column_name})",
        data_source_id=table.data_source_id,
        source_table_id=table.id,
        source_table=table.table_name,
        time_field=time_field,
        time_grain=time_grain,
        unit=unit,
        direction="HIGHER_IS_BETTER",
        confidence=_confidence_with_context(confidence, table_profile, time_field),
        business_criticality=criticality,
        dimensions=dimensions,
        drivers=drivers,
        evidence={
            "reason": (
                f"{column.column_name} is an additive numeric measure with "
                f"{profile.distinct_count if profile else 'unknown'} distinct values."
            ),
            "column": column.column_name,
            "semantic_type": column.semantic_type,
            "aggregation": aggregation,
            "row_count": table_profile.row_count if table_profile else None,
            "min": profile.min_value if profile else None,
            "max": profile.max_value if profile else None,
            "mean": profile.mean_value if profile else None,
            "null_pct": profile.null_pct if profile else None,
            "table_grain": grain.inferred_grain if grain else None,
            "quality_status": table_profile.quality_status if table_profile else None,
            "method": "deterministic profile scan",
        },
        warnings=warnings,
    )


def _count_proposal(
    *,
    table: SourceTable,
    column: SourceColumn,
    label: str,
    definition: str,
    criticality: str,
    confidence: float,
    equals_row_count: bool,
    time_field: str | None,
    time_grain: str,
    dimensions: list[DimensionProposal],
    drivers: list[DriverProposal],
    profile: ColumnProfile | None,
    table_profile: TableProfile | None,
    grain: TableGrain | None,
) -> KpiProposal:
    warnings = _shared_warnings(time_field, table_profile, grain)
    if equals_row_count:
        warnings.append(
            f"{column.column_name} is unique in {table.table_name} today, so this "
            "equals the row count. COUNT(DISTINCT ...) is still the safer "
            "definition if the grain ever changes."
        )
    return KpiProposal(
        kpi_key=_slug(label),
        name=label,
        business_definition=definition,
        kind=KpiKind.SIMPLE,
        formula_expression=f"COUNT(DISTINCT {table.table_name}.{column.column_name})",
        data_source_id=table.data_source_id,
        source_table_id=table.id,
        source_table=table.table_name,
        time_field=time_field,
        time_grain=time_grain,
        unit="count",
        direction="HIGHER_IS_BETTER",
        confidence=_confidence_with_context(confidence, table_profile, time_field),
        business_criticality=criticality,
        dimensions=dimensions,
        drivers=drivers,
        evidence={
            "reason": (
                f"{column.column_name} is an identifier with "
                f"{profile.distinct_count if profile else 'unknown'} distinct values "
                f"across {profile.row_count if profile else 'unknown'} rows."
            ),
            "column": column.column_name,
            "distinct_count": profile.distinct_count if profile else None,
            "row_count": profile.row_count if profile else None,
            "unique_in_table": equals_row_count,
            "aggregation": Aggregation.COUNT_DISTINCT,
            "table_grain": grain.inferred_grain if grain else None,
            "method": "deterministic profile scan",
        },
        warnings=warnings,
    )


def _ratio_proposals(
    *,
    table: SourceTable,
    measures: list[dict],
    counts: list[dict],
    time_field: str | None,
    time_grain: str,
    dimensions: list[DimensionProposal],
    drivers: list[DriverProposal],
    table_profile: TableProfile | None,
    grain: TableGrain | None,
) -> list[KpiProposal]:
    proposals: list[KpiProposal] = []
    for measure in measures:
        for count in counts:
            hint = _RATIO_HINTS.get((measure["hint_key"], count["hint_key"]))
            if hint is None:
                continue
            key, label, definition, criticality = hint
            measure_column = measure["column"].column_name
            count_column = count["column"].column_name
            proposals.append(
                KpiProposal(
                    kpi_key=key,
                    name=label,
                    business_definition=definition,
                    kind=KpiKind.RATIO,
                    formula_expression=(
                        f"SUM({table.table_name}.{measure_column}) / "
                        f"COUNT(DISTINCT {table.table_name}.{count_column})"
                    ),
                    data_source_id=table.data_source_id,
                    source_table_id=table.id,
                    source_table=table.table_name,
                    time_field=time_field,
                    time_grain=time_grain,
                    unit="currency" if measure["is_currency"] else "ratio",
                    direction="HIGHER_IS_BETTER",
                    confidence=_confidence_with_context(
                        min(measure["confidence"], count["confidence"]) + 0.05,
                        table_profile,
                        time_field,
                    ),
                    business_criticality=criticality,
                    dimensions=dimensions,
                    drivers=drivers,
                    evidence={
                        "reason": (
                            f"{measure_column} is an additive measure and {count_column} "
                            "an identifier in the same table, so their ratio is "
                            "computable without a join."
                        ),
                        "numerator": f"SUM({measure_column})",
                        "denominator": f"COUNT(DISTINCT {count_column})",
                        "single_table": True,
                        "table_grain": grain.inferred_grain if grain else None,
                        "method": "deterministic profile scan",
                    },
                    warnings=[
                        *_shared_warnings(time_field, table_profile, grain),
                        "Ratio KPIs are not additive: they must be recomputed at each "
                        "level of aggregation, never summed or averaged from parts.",
                    ],
                )
            )
    return proposals


# ---------------------------------------------------------------------------
# Dimensions and drivers
# ---------------------------------------------------------------------------
def _dimension_proposals(
    table: SourceTable,
    profiles: dict[str, ColumnProfile],
    safe_join_tables: list[tuple[SourceTable, str]],
) -> list[DimensionProposal]:
    proposals: list[DimensionProposal] = []

    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        if column.semantic_type not in {SemanticType.CATEGORICAL, SemanticType.BOOLEAN_FLAG}:
            continue
        profile = profiles.get(column.id)
        if profile is None or profile.access_withheld:
            continue
        distinct = profile.distinct_count or 0
        if distinct < MIN_DIMENSION_CARDINALITY or distinct > MAX_DIMENSION_CARDINALITY:
            continue
        proposals.append(
            DimensionProposal(
                dimension_name=column.column_name.lower(),
                source_table_id=table.id,
                source_table=table.table_name,
                source_column=column.column_name,
                approx_cardinality=distinct,
                is_default_breakdown=column.column_name.lower() in {"region", "sector", "channel"},
                notes="Same table as the measure: no join required.",
            )
        )

    # Dimensions reachable through a join the safety analysis rated SAFE.
    for parent, join_column in safe_join_tables:
        for column in sorted(parent.columns, key=lambda c: c.ordinal_position):
            if column.semantic_type != SemanticType.CATEGORICAL:
                continue
            profile = profiles.get(column.id)
            if profile is None or profile.access_withheld:
                continue
            distinct = profile.distinct_count or 0
            if distinct < MIN_DIMENSION_CARDINALITY or distinct > MAX_DIMENSION_CARDINALITY:
                continue
            name = column.column_name.lower()
            if any(existing.dimension_name == name for existing in proposals):
                continue
            proposals.append(
                DimensionProposal(
                    dimension_name=name,
                    source_table_id=parent.id,
                    source_table=parent.table_name,
                    source_column=column.column_name,
                    approx_cardinality=distinct,
                    notes=(
                        f"Reached via a join on {join_column} rated SAFE by join-safety "
                        "analysis (no row multiplication)."
                    ),
                )
            )

    return proposals


def _driver_proposals(
    table: SourceTable, profiles: dict[str, ColumnProfile]
) -> list[DriverProposal]:
    drivers: list[DriverProposal] = []
    seen: set[str] = set()
    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        profile = profiles.get(column.id)
        if profile is not None and profile.access_withheld:
            continue
        driver_type = _match_driver_type(column.column_name)
        if driver_type is None or driver_type in seen:
            continue
        seen.add(driver_type)
        drivers.append(
            DriverProposal(
                driver_name=driver_type.title().replace("_", " "),
                driver_type=driver_type,
                source_table_id=table.id,
                source_table=table.table_name,
                source_column=column.column_name,
                controllable=driver_type in {DriverType.PRICE, DriverType.MARKETING, DriverType.MIX},
                measurement_method=f"observed from {table.table_name}.{column.column_name}",
            )
        )

    # Seasonality is always a candidate explanation and has no source column.
    if not any(d.driver_type == DriverType.SEASONALITY for d in drivers):
        drivers.append(
            DriverProposal(
                driver_name="Seasonality",
                driver_type=DriverType.SEASONALITY,
                source_table_id=None,
                source_table=None,
                source_column=None,
                controllable=False,
                measurement_method="derived from the KPI's own history",
            )
        )
    return drivers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _shared_warnings(
    time_field: str | None, table_profile: TableProfile | None, grain: TableGrain | None
) -> list[str]:
    warnings: list[str] = []
    if not time_field:
        warnings.append(
            "No time column was identified for this table, so the KPI cannot be "
            "tracked over time until one is set."
        )
    if table_profile and table_profile.quality_status in {"WARNING", "POOR"}:
        warnings.append(
            f"Source table quality is {table_profile.quality_status}: "
            f"{'; '.join(table_profile.warnings[:3])}"
        )
    if table_profile and table_profile.withheld_column_count:
        warnings.append(
            f"{table_profile.withheld_column_count} column(s) were withheld from "
            "profiling by access policy, so this proposal is based on a partial view."
        )
    if grain and grain.is_unique is False:
        warnings.append(
            f"Table grain is only {(grain.confidence or 0) * 100:.1f}% unique "
            f"({grain.inferred_grain}); duplicate rows may inflate totals."
        )
    return warnings


def _confidence_with_context(
    base: float, table_profile: TableProfile | None, time_field: str | None
) -> float:
    score = base
    if not time_field:
        score -= 0.2
    if table_profile:
        if table_profile.quality_status == "POOR":
            score -= 0.2
        elif table_profile.quality_status == "WARNING":
            score -= 0.08
        if table_profile.withheld_column_count:
            score -= 0.05
    return round(max(0.1, min(0.99, score)), 3)


def _match_hint(name: str, table: dict) -> tuple[str, str, str] | None:
    lowered = name.lower()
    if lowered in table:
        return table[lowered]
    # Longest match first so "net_revenue" does not resolve as "revenue".
    for hint in sorted(table, key=len, reverse=True):
        if hint in lowered:
            return table[hint]
    return None


def _hint_key(name: str, table: dict) -> str | None:
    lowered = name.lower()
    if lowered in table:
        return lowered
    for hint in sorted(table, key=len, reverse=True):
        if hint in lowered:
            return hint
    return None


def _match_driver_type(name: str) -> str | None:
    lowered = name.lower()
    for hint in sorted(_DRIVER_HINTS, key=len, reverse=True):
        if hint in lowered:
            return _DRIVER_HINTS[hint]
    return None


def _looks_like_currency(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in ("value", "revenue", "amount", "spend", "cost", "price", "sales", "margin")
    )


def _titleise(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("_", " ").split())


def _slug(label: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in label.lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:80]


def _deduplicate(proposals: list[KpiProposal]) -> list[KpiProposal]:
    """One proposal per (key, formula): the same measure can appear in several
    tables, and the highest-confidence binding wins."""
    seen: dict[tuple[str, str], KpiProposal] = {}
    for proposal in proposals:
        key = (proposal.kpi_key, proposal.formula_expression)
        if key not in seen:
            seen[key] = proposal
    # Also collapse duplicate keys across tables, keeping the strongest.
    best: dict[str, KpiProposal] = {}
    for proposal in seen.values():
        current = best.get(proposal.kpi_key)
        if current is None or proposal.confidence > current.confidence:
            best[proposal.kpi_key] = proposal
    return sorted(best.values(), key=lambda p: (-p.confidence, p.kpi_key))


# ---------------------------------------------------------------------------
# Catalog reads
# ---------------------------------------------------------------------------
def _selected_tables(session: Session, company_id: str) -> list[SourceTable]:
    return list(
        session.scalars(
            select(SourceTable)
            .join(SelectedTable, SelectedTable.source_table_id == SourceTable.id)
            .where(
                SourceTable.company_id == company_id,
                SelectedTable.enabled.is_(True),
            )
            .order_by(SourceTable.table_name)
        )
    )


def _column_profiles(session: Session, tables: list[SourceTable]) -> dict[str, ColumnProfile]:
    if not tables:
        return {}
    rows = session.scalars(
        select(ColumnProfile).where(
            ColumnProfile.source_table_id.in_([t.id for t in tables])
        )
    )
    return {row.source_column_id: row for row in rows}


def _table_profiles(session: Session, tables: list[SourceTable]) -> dict[str, TableProfile]:
    if not tables:
        return {}
    rows = session.scalars(
        select(TableProfile).where(TableProfile.source_table_id.in_([t.id for t in tables]))
    )
    return {row.source_table_id: row for row in rows}


def _grains(session: Session, tables: list[SourceTable]) -> dict[str, TableGrain]:
    if not tables:
        return {}
    rows = session.scalars(
        select(TableGrain).where(TableGrain.source_table_id.in_([t.id for t in tables]))
    )
    return {row.source_table_id: row for row in rows}


def _safe_join_targets(
    session: Session, tables: list[SourceTable]
) -> dict[str, list[tuple[SourceTable, str]]]:
    """Per table, the parent tables it can join to without multiplying rows."""
    by_id = {table.id: table for table in tables}
    if not by_id:
        return {}
    rows = session.execute(
        select(TableRelationship, JoinSafety)
        .join(JoinSafety, JoinSafety.relationship_id == TableRelationship.id)
        .where(TableRelationship.source_table_id.in_(list(by_id)))
    ).all()

    result: dict[str, list[tuple[SourceTable, str]]] = {}
    for relationship, safety in rows:
        if safety.safety_level != JoinSafetyLevel.SAFE:
            continue
        parent = by_id.get(relationship.target_table_id)
        if parent is None:
            continue
        result.setdefault(relationship.source_table_id, []).append(
            (parent, relationship.source_column)
        )
    return result
