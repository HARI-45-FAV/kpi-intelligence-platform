"""Deterministic SQL generation from a governed KPI contract.

This is the boundary the hackathon brief cares about: **KPI values are produced
by SQL, never by a language model.** Every identifier here comes from a validated
formula spec and is quoted by the connector; every literal is a bound parameter.

Ratios are computed by projecting numerator and denominator separately and
dividing in Python. That is deliberate — it avoids dialect differences in
division-by-zero behaviour, makes a zero denominator an explicit, reportable
condition rather than a NULL, and hands the analyst persona the components that
explain the ratio rather than just its value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.connectors.sql import SqlConnector
from app.core.errors import ConnectorError, ValidationFailure
from app.models.base import Aggregation
from app.services.kpi_formula import (
    FILTER_OPERATORS,
    FilterSpec,
    FormulaSpec,
    MeasureSpec,
    _VALUELESS_OPERATORS,
)

MAX_GROUP_BY_COLUMNS = 3


@dataclass(slots=True)
class KpiQuery:
    sql: str
    params: dict[str, Any]
    is_ratio: bool
    group_by: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KpiValue:
    value: float | None
    numerator: float | None
    denominator: float | None
    group: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }
        if self.group:
            payload["group"] = self.group
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(slots=True)
class KpiResult:
    rows: list[KpiValue]
    sql: str
    row_count: int

    @property
    def scalar(self) -> KpiValue | None:
        return self.rows[0] if self.rows else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.as_dict() for row in self.rows],
            "row_count": self.row_count,
            "sql": self.sql,
        }


def build_kpi_query(
    connector: SqlConnector,
    spec: FormulaSpec,
    *,
    schema: str,
    table: str,
    time_column: str | None = None,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    group_by: list[str] | None = None,
    limit: int | None = None,
) -> KpiQuery:
    """Compose the aggregate query for a KPI contract."""
    target_schema = connector.resolve_schema(schema)
    relation = connector.qualify(target_schema, table)
    params: dict[str, Any] = {}

    group_columns = list(group_by or [])
    if len(group_columns) > MAX_GROUP_BY_COLUMNS:
        raise ValidationFailure(
            f"At most {MAX_GROUP_BY_COLUMNS} breakdown columns may be requested at once."
        )

    projections = [f"{_render_measure(connector, spec.numerator)} AS numerator"]
    if spec.denominator is not None:
        projections.append(f"{_render_measure(connector, spec.denominator)} AS denominator")

    grouped_quoted: list[str] = []
    for index, column in enumerate(group_columns):
        quoted = connector.quote(column, kind="breakdown column")
        grouped_quoted.append(quoted)
        projections.insert(index, f"{quoted} AS grp_{index}")

    where, where_params = _build_where(
        connector, spec.filters, time_column=time_column, start=start, end=end
    )
    params.update(where_params)

    sql = f"SELECT {', '.join(projections)} FROM {relation}"  # noqa: S608 - identifiers validated
    if where:
        sql += f" WHERE {where}"
    if grouped_quoted:
        sql += " GROUP BY " + ", ".join(grouped_quoted)
        # Largest contributor first: the only ordering that makes a truncated
        # breakdown meaningful.
        sql += " ORDER BY numerator DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    return KpiQuery(sql=sql, params=params, is_ratio=spec.denominator is not None, group_by=group_columns)


def execute_kpi(
    connector: SqlConnector,
    spec: FormulaSpec,
    *,
    schema: str,
    table: str,
    time_column: str | None = None,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    group_by: list[str] | None = None,
    limit: int | None = None,
) -> KpiResult:
    query = build_kpi_query(
        connector,
        spec,
        schema=schema,
        table=table,
        time_column=time_column,
        start=start,
        end=end,
        group_by=group_by,
        limit=limit,
    )
    rows = connector._run(query.sql, query.params, guard=False)

    values: list[KpiValue] = []
    for row in rows:
        numerator = _as_float(row.get("numerator"))
        denominator = _as_float(row.get("denominator")) if query.is_ratio else None
        value, note = _combine(spec, numerator, denominator)
        group = {
            column: row.get(f"grp_{index}")
            for index, column in enumerate(query.group_by)
        }
        values.append(
            KpiValue(
                value=value,
                numerator=numerator,
                denominator=denominator,
                group=group,
                note=note,
            )
        )

    return KpiResult(rows=values, sql=query.sql, row_count=len(values))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _render_measure(connector: SqlConnector, measure: MeasureSpec) -> str:
    if measure.is_count_star:
        return "COUNT(*)"
    column = connector.quote(measure.column, kind="measure column")
    if measure.aggregation == Aggregation.COUNT and measure.distinct:
        return f"COUNT(DISTINCT {column})"
    if measure.distinct:
        # Only COUNT has a governed DISTINCT form; SUM(DISTINCT x) is almost
        # always a modelling mistake rather than an intent.
        raise ValidationFailure(
            f"{measure.aggregation}(DISTINCT ...) is not permitted. "
            "Only COUNT(DISTINCT ...) is a governed aggregation."
        )
    if measure.aggregation == Aggregation.AVG:
        # Force floating-point division: AVG over an integer column silently
        # truncates on some engines.
        return f"AVG({column} * 1.0)"
    return f"{measure.aggregation}({column})"


def _build_where(
    connector: SqlConnector,
    filters: list[FilterSpec],
    *,
    time_column: str | None,
    start: date | datetime | None,
    end: date | datetime | None,
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if time_column and (start is not None or end is not None):
        quoted = connector.quote(time_column, kind="time column")
        if start is not None:
            clauses.append(f"{quoted} >= :_start")
            params["_start"] = connector.temporal_param(start)
        if end is not None:
            clauses.append(f"{quoted} <= :_end")
            params["_end"] = connector.temporal_param(end)

    for index, filter_spec in enumerate(filters):
        quoted = connector.quote(filter_spec.column, kind="filter column")
        operator = FILTER_OPERATORS.get(filter_spec.operator)
        if operator is None:  # pragma: no cover - guarded at parse time
            raise ValidationFailure(f"Filter operator {filter_spec.operator!r} is not permitted.")

        if filter_spec.operator in _VALUELESS_OPERATORS:
            clauses.append(f"{quoted} {operator}")
            continue

        if operator in {"IN", "NOT IN"}:
            values = filter_spec.value if isinstance(filter_spec.value, list | tuple) else [filter_spec.value]
            if not values:
                raise ValidationFailure(f"Filter on {filter_spec.column} has an empty value list.")
            placeholders = []
            for position, item in enumerate(values):
                name = f"_f{index}_{position}"
                params[name] = item
                placeholders.append(f":{name}")
            clauses.append(f"{quoted} {operator} ({', '.join(placeholders)})")
            continue

        name = f"_f{index}"
        params[name] = filter_spec.value
        clauses.append(f"{quoted} {operator} :{name}")

    return (" AND ".join(clauses), params)


# ---------------------------------------------------------------------------
# Value combination
# ---------------------------------------------------------------------------
def _combine(
    spec: FormulaSpec, numerator: float | None, denominator: float | None
) -> tuple[float | None, str | None]:
    if spec.denominator is None:
        if numerator is None:
            # SUM over an empty set is NULL, not 0. Which of those the business
            # means is a governed choice, not an implementation detail.
            if spec.null_handling == "TREAT_AS_ZERO":
                return (0.0, "no matching rows; null treated as zero per KPI contract")
            return (None, "no matching rows")
        return (numerator, None)

    if denominator in (None, 0):
        return (None, "denominator is zero or empty; ratio is undefined")
    if numerator is None:
        return (0.0 if spec.null_handling == "TREAT_AS_ZERO" else None, "numerator is null")
    return (numerator / denominator, None)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover
        raise ConnectorError(f"KPI query returned a non-numeric value: {value!r}") from exc
