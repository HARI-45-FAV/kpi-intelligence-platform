"""KPI evaluation over a REST source, from the same governed formula spec.

Some sources cannot run SQL. A Supabase project connected with its REST secret
key is the case this platform actually meets: the key is a JWT for PostgREST, not
a database password, so there is no session to send a ``SELECT`` to -- and on many
projects PostgREST's aggregate functions are disabled outright (``PGRST123``).

The wrong response to that is to hard-code a fallback: a table name, a column, an
"orders per day" special case. This module does the same thing
:mod:`app.services.kpi_sql` does, from the same input, and produces the same
:class:`KpiResult`:

    approved FormulaSpec + source table + time field + day window
        -> filtered, projected read at the source
        -> deterministic aggregation in Python
        -> KpiValue

What is identical to the SQL path, and has to be:

* **The formula is the contract.** Aggregation, column, DISTINCT, ratio and
  ``null_handling`` all come from the spec that KPI registration validated and
  approved. Nothing here knows what any KPI is called.
* **Filtering happens at the source.** Every governed filter and both window
  bounds become PostgREST operators on the URL, so Postgres selects the window.
  Python receives the rows of one day, not a table.
* **Null semantics match SQL.** ``SUM`` over no non-null values is ``NULL``, not
  zero; ``COUNT`` counts non-null values; ``AVG`` over nothing is ``NULL``. The
  KPI's ``null_handling`` then decides what that means, in
  :func:`app.services.kpi_sql._combine` -- the *same* function the SQL path uses,
  so a KPI cannot mean one thing over Postgres and another over REST.
* **Identifiers are validated, never interpolated blindly.** The connector
  validates every column and table name before it reaches a URL.

What is honestly different, and is reported rather than hidden: the aggregate is
computed from transferred rows, so it is only correct if *every* matching row was
transferred. When the window holds more rows than the read cap allows, this module
declines to return a number instead of returning one derived from part of the
window. The one exception is a pure row count, which PostgREST reports exactly in
a header without transferring anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from app.connectors.supabase_rest import ProjectionPage, SupabaseRestConnector
from app.core.config import settings
from app.core.errors import ValidationFailure
from app.models.base import Aggregation
from app.services.kpi_formula import (
    _VALUELESS_OPERATORS,
    FilterSpec,
    FormulaSpec,
    MeasureSpec,
)
from app.services.kpi_sql import KpiResult, KpiValue, _as_float, _combine

#: Governed filter operator -> PostgREST operator. Only the operators the formula
#: grammar already permits appear here; anything else is refused rather than
#: approximated, because a filter that silently changes meaning between two
#: execution paths would make the same KPI mean two different things.
_REST_OPERATORS: dict[str, str] = {
    "=": "eq",
    "!=": "neq",
    "<>": "neq",
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "IN": "in",
    "NOT IN": "not.in",
    "IS NULL": "is",
    "IS NOT NULL": "not.is",
    "LIKE": "like",
}


def supports_rest_execution(connector: object) -> bool:
    return isinstance(connector, SupabaseRestConnector)


# ---------------------------------------------------------------------------
# Predicate rendering
# ---------------------------------------------------------------------------
def _literal(value: Any) -> str:
    """One filter value as PostgREST expects it on a URL."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _quoted_list(values: Sequence[Any]) -> str:
    # PostgREST's in.(...) list is comma-separated; a value containing a comma has
    # to be double-quoted or it would split into two values.
    parts = []
    for value in values:
        rendered = _literal(value)
        if any(character in rendered for character in ',"()'):
            escaped = rendered.replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(rendered)
    return "(" + ",".join(parts) + ")"


def _predicate(filter_spec: FilterSpec) -> tuple[str, str]:
    operator = _REST_OPERATORS.get(filter_spec.operator)
    if operator is None:  # pragma: no cover - guarded at parse time
        raise ValidationFailure(
            f"Filter operator {filter_spec.operator!r} is not permitted."
        )

    if filter_spec.operator in _VALUELESS_OPERATORS:
        # PostgREST spells null tests as is.null / not.is.null.
        return (filter_spec.column, f"{operator}.null")

    if filter_spec.operator in {"IN", "NOT IN"}:
        values = (
            filter_spec.value
            if isinstance(filter_spec.value, (list, tuple))
            else [filter_spec.value]
        )
        if not values:
            raise ValidationFailure(
                f"Filter on {filter_spec.column} has an empty value list."
            )
        return (filter_spec.column, f"{operator}.{_quoted_list(values)}")

    if filter_spec.operator == "LIKE":
        # SQL wildcards are % and _; PostgREST's like uses * for %.
        pattern = _literal(filter_spec.value).replace("%", "*")
        return (filter_spec.column, f"{operator}.{pattern}")

    return (filter_spec.column, f"{operator}.{_literal(filter_spec.value)}")


def _window_predicates(
    time_column: str | None,
    start: date | datetime | None,
    end: date | datetime | None,
) -> list[tuple[str, str]]:
    if not time_column:
        return []
    predicates: list[tuple[str, str]] = []
    if start is not None:
        predicates.append((time_column, f"gte.{_literal(start)}"))
    if end is not None:
        predicates.append((time_column, f"lte.{_literal(end)}"))
    return predicates


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _values(rows: Sequence[dict[str, Any]], column: str) -> list[float | None]:
    return [row.get(column) for row in rows]


def _numeric(raw: Sequence[Any]) -> list[float]:
    """Non-null values as floats. A non-numeric value is an error, not a zero."""

    out: list[float] = []
    for value in raw:
        if value is None:
            continue
        coerced = _as_float(value)
        if coerced is not None:
            out.append(coerced)
    return out


def _aggregate(measure: MeasureSpec, page: ProjectionPage) -> float | None:
    """Apply one governed aggregation to a fetched window, with SQL's semantics."""

    if measure.is_count_star:
        # Exact from the Content-Range header; no dependence on what was transferred.
        return float(page.total) if page.total is not None else float(len(page.rows))

    raw = _values(page.rows, measure.column)

    if measure.aggregation == Aggregation.COUNT:
        present = [value for value in raw if value is not None]
        if measure.distinct:
            # Hashable-only: a JSON column could arrive as a dict, and Postgres
            # would compare those by value. Rendering to text preserves that.
            return float(len({_hashable(value) for value in present}))
        return float(len(present))

    if measure.aggregation in {Aggregation.MIN, Aggregation.MAX}:
        present = [value for value in raw if value is not None]
        if not present:
            return None
        # MIN/MAX are meaningful on dates and text too, but a KPI value has to be
        # a number, so the comparison happens on the coerced numbers.
        numbers = _numeric(present)
        if not numbers:
            return None
        return min(numbers) if measure.aggregation == Aggregation.MIN else max(numbers)

    numbers = _numeric(raw)
    if measure.aggregation == Aggregation.SUM:
        # SUM over an empty set is NULL in SQL, not 0. The KPI's null_handling
        # decides what that means; this function does not pre-empt it.
        return float(sum(numbers)) if numbers else None
    if measure.aggregation == Aggregation.AVG:
        return (sum(numbers) / len(numbers)) if numbers else None

    raise ValidationFailure(  # pragma: no cover - grammar admits nothing else
        f"{measure.aggregation} is not an aggregation this source can evaluate."
    )


def _hashable(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return repr(value)
    return value


def _needs_every_row(spec: FormulaSpec) -> bool:
    """True when a partial transfer would give a wrong answer.

    Only a pure row count survives truncation, because its value comes from the
    source's own count rather than from the rows.
    """

    for _role, measure in spec.measures:
        if not measure.is_count_star:
            return True
    return False


def _projection(spec: FormulaSpec, group_by: Sequence[str] = ()) -> list[str]:
    """The columns that must be transferred: measures, plus any breakdown columns.

    Filter columns are deliberately absent. Every governed filter is applied at
    the source as a PostgREST predicate, so its column never needs to reach
    Python -- and transferring it would widen the read for no reason.

    A breakdown column is the one exception, and only because there is no way
    around it: the grouping happens here, so the value each row falls under has
    to arrive. It is named by an approved ``KpiDimension``, never by a caller.
    """

    columns: list[str] = []
    for _role, measure in spec.measures:
        if not measure.is_count_star and measure.column not in columns:
            columns.append(measure.column)
    for column in group_by:
        if column not in columns:
            columns.append(column)
    return columns


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def execute_kpi_rest(
    connector: SupabaseRestConnector,
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
    """Evaluate one KPI for one window over a REST source.

    Returns the same :class:`KpiResult` the SQL path returns, so the detection
    engine cannot tell which execution path produced a value -- only the
    descriptor in ``sql`` reveals it, and that is there to be read.

    ``group_by`` asks for the same window broken down by one or more columns, as
    ``GROUP BY`` would. The read is unchanged -- one filtered window, one request
    -- and the grouping is then applied to the rows that arrived, with the same
    aggregation and the same ``null_handling`` each group would get on the SQL
    path. ``limit`` keeps the largest groups and drops the tail.
    """

    group_columns = list(group_by or [])
    predicates = [
        *_window_predicates(time_column, start, end),
        *(_predicate(filter_spec) for filter_spec in spec.filters),
    ]
    columns = _projection(spec, group_columns)

    page = connector.fetch_projection(
        schema,
        table,
        columns=columns,
        predicates=predicates,
    )

    # Exact from Content-Range, so it holds even when the rows themselves were
    # capped; it falls back to what was transferred only if the header was absent.
    matched_rows = page.total if page.total is not None else len(page.rows)

    # A breakdown always needs every row, even for a pure row count: the exact
    # count in the header is for the whole window, and nothing reports it per
    # group. So a truncated read cannot be grouped honestly at all.
    if page.truncated and (group_columns or _needs_every_row(spec)):
        # Declining is the only honest answer: the window held more rows than the
        # cap allowed, so any number computed here would be an understatement
        # presented as a measurement.
        note = (
            f"{page.total:,} rows match this window but only {len(page.rows):,} could be "
            f"read (connector_max_rows_returned={settings.connector_max_rows_returned:,}), "
            "so no value was computed rather than one derived from part of the window. "
            "Register this project as a PostgreSQL source to push the aggregate down."
        )
        return KpiResult(
            rows=[
                KpiValue(
                    value=None,
                    numerator=None,
                    denominator=None,
                    note=note,
                    matched_rows=matched_rows,
                )
            ],
            sql=page.descriptor,
            row_count=1,
        )

    if group_columns:
        return _grouped_result(spec, page, group_columns, limit)

    numerator = _aggregate(spec.numerator, page)
    denominator = _aggregate(spec.denominator, page) if spec.denominator else None
    value, note = _combine(spec, numerator, denominator, matched_rows=matched_rows)

    return KpiResult(
        rows=[
            KpiValue(
                value=value,
                numerator=numerator,
                denominator=denominator,
                note=note,
                matched_rows=matched_rows,
            )
        ],
        sql=page.descriptor,
        row_count=1,
    )


def _grouped_result(
    spec: FormulaSpec,
    page: ProjectionPage,
    group_columns: list[str],
    limit: int | None,
) -> KpiResult:
    """Aggregate one fetched window per distinct combination of group values.

    Deliberately the same shape the SQL path returns -- ``KpiValue.group`` keyed
    by column name, largest numerator first -- so a caller cannot tell which path
    produced a breakdown. ``None`` is a group of its own, as it is in SQL's
    ``GROUP BY``, rather than being folded into another or dropped.
    """

    buckets: dict[tuple, list[dict[str, Any]]] = {}
    for row in page.rows:
        key = tuple(_hashable(row.get(column)) for column in group_columns)
        buckets.setdefault(key, []).append(row)

    values: list[KpiValue] = []
    for key, rows in buckets.items():
        # total is this group's own row count: the header count belongs to the
        # whole window and would make every group's COUNT(*) the same number.
        group_page = ProjectionPage(rows=rows, total=len(rows), descriptor=page.descriptor)
        numerator = _aggregate(spec.numerator, group_page)
        denominator = _aggregate(spec.denominator, group_page) if spec.denominator else None
        value, note = _combine(spec, numerator, denominator, matched_rows=len(rows))
        # The key is rendered for hashing; the row's own value is what the caller
        # sees, so a date or a number is not turned into its repr.
        original = rows[0]
        values.append(
            KpiValue(
                value=value,
                numerator=numerator,
                denominator=denominator,
                group={column: original.get(column) for column in group_columns},
                note=note,
                matched_rows=len(rows),
            )
        )

    values.sort(key=lambda item: (item.numerator is None, -(item.numerator or 0.0)))
    if limit:
        values = values[: int(limit)]

    return KpiResult(rows=values, sql=page.descriptor, row_count=len(values))
