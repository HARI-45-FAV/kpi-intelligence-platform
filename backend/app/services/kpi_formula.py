"""The governed KPI formula contract.

A KPI's calculation is stored as a **structured specification**, not as free-text
SQL. The difference is not cosmetic:

* Validation can actually check something. "Does this column exist?", "Is this
  aggregation valid for this type?", "Does this double-count?" are answerable
  against a spec and unanswerable against an opaque SQL string.
* Column-level lineage falls out of the spec for free, so it cannot drift away
  from the calculation the way hand-maintained lineage does.
* SQL is *generated* from validated identifiers, so a KPI definition is not an
  injection vector — which it certainly would be if administrators typed SQL
  that the platform later executed.

Administrators still type ``SUM(orders.order_value)`` because that is what a
business definition looks like. A strict recursive-descent parser accepts exactly
the governed grammar and rejects everything else:

    formula  := term [ "/" term ]
    term     := AGG "(" [ "DISTINCT" ] operand ")"
    AGG      := SUM | COUNT | AVG | MIN | MAX
    operand  := "*" | [ table "." ] column

That covers every Sprint 1 KPI — Revenue, Orders, Unique Customers, AOV, Units
Sold — while leaving no room for a subquery, a join or a function call to sneak
into a governed definition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ValidationFailure
from app.models.base import Aggregation, KpiKind

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$]{0,127}"
_TERM_RE = re.compile(
    rf"""^\s*
    (?P<agg>SUM|COUNT|AVG|MIN|MAX)\s*
    \(\s*
    (?P<distinct>DISTINCT\s+)?
    (?:
        (?P<star>\*)
        |
        (?:(?P<table>{_IDENTIFIER})\s*\.\s*)?(?P<column>{_IDENTIFIER})
    )
    \s*\)
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Operators a governed filter may use. Anything else is rejected outright.
FILTER_OPERATORS = {
    "=": "=",
    "!=": "<>",
    "<>": "<>",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "IN": "IN",
    "NOT IN": "NOT IN",
    "IS NULL": "IS NULL",
    "IS NOT NULL": "IS NOT NULL",
    "LIKE": "LIKE",
}
_VALUELESS_OPERATORS = {"IS NULL", "IS NOT NULL"}

# Aggregations that only make sense over a numeric column.
NUMERIC_ONLY_AGGREGATIONS = {Aggregation.SUM, Aggregation.AVG}


@dataclass(slots=True)
class MeasureSpec:
    """One aggregate term of a KPI."""

    aggregation: str
    column: str
    table: str | None = None
    distinct: bool = False

    @property
    def is_count_star(self) -> bool:
        return self.aggregation == Aggregation.COUNT and self.column == "*"

    @property
    def effective_aggregation(self) -> str:
        """COUNT + DISTINCT is a distinct enough operation to name separately."""
        if self.aggregation == Aggregation.COUNT and self.distinct:
            return Aggregation.COUNT_DISTINCT
        return self.aggregation

    def render(self, *, qualified: bool = True) -> str:
        inner = "*" if self.column == "*" else (
            f"{self.table}.{self.column}" if (qualified and self.table) else self.column
        )
        prefix = "DISTINCT " if self.distinct else ""
        return f"{self.aggregation}({prefix}{inner})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "aggregation": self.aggregation,
            "effective_aggregation": self.effective_aggregation,
            "table": self.table,
            "column": self.column,
            "distinct": self.distinct,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MeasureSpec:
        return cls(
            aggregation=str(payload["aggregation"]).upper(),
            column=str(payload["column"]),
            table=payload.get("table"),
            distinct=bool(payload.get("distinct", False)),
        )


@dataclass(slots=True)
class FilterSpec:
    column: str
    operator: str
    value: Any = None
    table: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "operator": self.operator,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FilterSpec:
        operator = str(payload.get("operator", "=")).upper().strip()
        if operator not in FILTER_OPERATORS:
            raise ValidationFailure(
                f"Filter operator {operator!r} is not permitted.",
                details={"allowed": sorted(FILTER_OPERATORS)},
            )
        if operator not in _VALUELESS_OPERATORS and payload.get("value") is None:
            raise ValidationFailure(f"Filter on {payload.get('column')} requires a value.")
        return cls(
            column=str(payload["column"]),
            operator=operator,
            value=payload.get("value"),
            table=payload.get("table"),
        )


@dataclass(slots=True)
class FormulaSpec:
    kind: str
    numerator: MeasureSpec
    denominator: MeasureSpec | None = None
    filters: list[FilterSpec] = field(default_factory=list)
    null_handling: str = "TREAT_AS_ZERO"

    @property
    def measures(self) -> list[tuple[str, MeasureSpec]]:
        items = [("NUMERATOR", self.numerator)]
        if self.denominator is not None:
            items.append(("DENOMINATOR", self.denominator))
        return items

    @property
    def referenced_columns(self) -> list[tuple[str, str | None, str]]:
        """(role, table, column) for every column the calculation touches."""
        refs = [
            (role, measure.table, measure.column)
            for role, measure in self.measures
            if measure.column != "*"
        ]
        refs += [("FILTER", f.table, f.column) for f in self.filters]
        return refs

    def render(self) -> str:
        base = self.numerator.render()
        if self.denominator is not None:
            return f"{base} / {self.denominator.render()}"
        return base

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "expression": self.render(),
            "numerator": self.numerator.as_dict(),
            "denominator": self.denominator.as_dict() if self.denominator else None,
            "filters": [f.as_dict() for f in self.filters],
            "null_handling": self.null_handling,
            "grammar_version": 1,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FormulaSpec:
        if not payload:
            raise ValidationFailure("Formula specification is empty.")
        numerator = MeasureSpec.from_dict(payload["numerator"])
        denominator_payload = payload.get("denominator")
        denominator = MeasureSpec.from_dict(denominator_payload) if denominator_payload else None
        return cls(
            kind=KpiKind.RATIO if denominator else KpiKind.SIMPLE,
            numerator=numerator,
            denominator=denominator,
            filters=[FilterSpec.from_dict(f) for f in payload.get("filters") or []],
            null_handling=payload.get("null_handling") or "TREAT_AS_ZERO",
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_formula(
    expression: str,
    *,
    default_table: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    null_handling: str = "TREAT_AS_ZERO",
) -> FormulaSpec:
    """Parse a governed formula expression into a structured specification."""
    raw = (expression or "").strip()
    if not raw:
        raise ValidationFailure("Formula is required.")
    if len(raw) > 400:
        raise ValidationFailure("Formula is unreasonably long for a governed definition.")

    # Anything the grammar cannot express is refused with a pointed message
    # rather than a generic parse error.
    for forbidden, hint in (
        (";", "statement separators"),
        ("--", "SQL comments"),
        ("/*", "SQL comments"),
        ("SELECT", "subqueries"),
        ("JOIN", "joins"),
        ("UNION", "set operations"),
        ("CASE", "conditional expressions"),
    ):
        if forbidden.lower() in raw.lower():
            raise ValidationFailure(
                f"Formulas may not contain {hint}. Use the structured fields "
                "(filters, dimensions) instead.",
                details={"formula": raw},
            )

    parts = raw.split("/")
    if len(parts) > 2:
        raise ValidationFailure(
            "A KPI formula may contain at most one division. "
            "Define intermediate KPIs if you need more."
        )

    numerator = _parse_term(parts[0], default_table=default_table)
    denominator = _parse_term(parts[1], default_table=default_table) if len(parts) == 2 else None

    return FormulaSpec(
        kind=KpiKind.RATIO if denominator else KpiKind.SIMPLE,
        numerator=numerator,
        denominator=denominator,
        filters=[FilterSpec.from_dict(f) for f in filters or []],
        null_handling=null_handling,
    )


def _parse_term(fragment: str, *, default_table: str | None) -> MeasureSpec:
    match = _TERM_RE.match(fragment or "")
    if match is None:
        raise ValidationFailure(
            f"Could not parse {fragment.strip()!r}. Expected a single aggregate such as "
            "SUM(orders.order_value) or COUNT(DISTINCT orders.order_id).",
            details={
                "allowed_aggregations": ["SUM", "COUNT", "AVG", "MIN", "MAX"],
                "grammar": "AGG([DISTINCT] [table.]column) optionally divided by another term",
            },
        )

    aggregation = match.group("agg").upper()
    distinct = bool(match.group("distinct"))
    star = match.group("star")

    if star:
        if aggregation != Aggregation.COUNT:
            raise ValidationFailure(f"{aggregation}(*) is not meaningful. Use COUNT(*).")
        if distinct:
            raise ValidationFailure("COUNT(DISTINCT *) is not valid.")
        return MeasureSpec(aggregation=Aggregation.COUNT, column="*", table=None)

    # Only COUNT has a governed DISTINCT form. SUM(DISTINCT x) is almost always a
    # modelling mistake, and rejecting it here keeps it out of a saved contract
    # rather than failing later at SQL generation.
    if distinct and aggregation != Aggregation.COUNT:
        raise ValidationFailure(
            f"{aggregation}(DISTINCT ...) is not a governed aggregation. "
            "Only COUNT(DISTINCT ...) may use DISTINCT.",
            details={"aggregation": aggregation},
        )

    return MeasureSpec(
        aggregation=aggregation,
        column=match.group("column"),
        table=match.group("table") or default_table,
        distinct=distinct,
    )


def spec_from_stored(
    formula_spec: dict[str, Any] | None,
    *,
    expression: str | None = None,
    default_table: str | None = None,
) -> FormulaSpec:
    """Rebuild a spec from a persisted KPI version, falling back to re-parsing."""
    if formula_spec and formula_spec.get("numerator"):
        return FormulaSpec.from_dict(formula_spec)
    if expression:
        return parse_formula(expression, default_table=default_table)
    raise ValidationFailure("KPI version has no usable formula.")


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LineageEntry:
    role: str
    table: str | None
    column: str | None
    transformation: str | None = None
    notes: str | None = None


def lineage_entries(
    spec: FormulaSpec,
    *,
    default_table: str | None,
    time_field: str | None = None,
    dimensions: list[tuple[str, str | None, str]] | None = None,
    drivers: list[tuple[str, str | None, str | None]] | None = None,
) -> list[LineageEntry]:
    """Derive column-level lineage directly from the calculation contract."""
    entries: list[LineageEntry] = []

    for role, measure in spec.measures:
        entries.append(
            LineageEntry(
                role=role,
                table=measure.table or default_table,
                column=None if measure.column == "*" else measure.column,
                transformation=measure.effective_aggregation,
                notes="row count" if measure.is_count_star else None,
            )
        )

    for filter_spec in spec.filters:
        entries.append(
            LineageEntry(
                role="FILTER",
                table=filter_spec.table or default_table,
                column=filter_spec.column,
                transformation=f"{filter_spec.operator}",
            )
        )

    if time_field:
        entries.append(
            LineageEntry(
                role="TIME", table=default_table, column=time_field, transformation="time axis"
            )
        )

    for name, table, column in dimensions or []:
        entries.append(
            LineageEntry(
                role="DIMENSION",
                table=table or default_table,
                column=column,
                transformation="breakdown",
                notes=name,
            )
        )

    for name, table, column in drivers or []:
        if not column:
            continue
        entries.append(
            LineageEntry(
                role="DRIVER",
                table=table or default_table,
                column=column,
                transformation="candidate driver",
                notes=name,
            )
        )

    return entries
