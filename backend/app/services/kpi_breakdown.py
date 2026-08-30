"""Reading a KPI grouped by a column that need not live on the KPI's own table.

Detection reads a KPI as one number over one table, and
:mod:`app.services.kpi_sql` does exactly that -- correctly, and unchanged by this
module. Investigation asks a harder question, because the parts of a business are
not all recorded at the same level of detail as the KPI. A KPI measured once per
record, broken down by something recorded once per *line* of that record, has no
single-table answer at all.

The wrong answer is a join and a ``SUM``. Joining a record-level total to a
line-level table repeats the total once per line, and summing it multiplies the
KPI: the same movement, several times over, with percentages that add to some
number nobody can explain. Refusing to break down along a finer table is also an
answer, and a poor one -- it is precisely the breakdown a person wants.

**What this module does instead: deterministic apportionment.** For every record,
the finer table's own numeric column decides how much of that record's measured
value belongs to each of its lines:

    part(record, line) = measure(record) x weight(line) / total_weight(record)

``total_weight(record)`` is the sum over *all* the record's lines, never a
filtered subset, so for any one record the parts sum to exactly ``measure``. Sum
that over records and the KPI's own total is unchanged -- which is the
requirement, not a nicety. A breakdown that moved the number the business
already saw would be a second answer to a settled question.

Three consequences, all deliberate:

* **A missing weight is a zero, not a guess.** A line with no weight receives
  none of the record's value; its siblings receive all of it.
* **A record whose lines carry no weight at all cannot be apportioned**, so it is
  excluded from the parts rather than divided by zero or spread evenly. Its value
  is still inside the KPI, so it shows up as movement the breakdown does not
  account for -- reported, not hidden.
* **Only a plain total can be apportioned.** A count of records, a distinct count
  and a ratio are refused with a reason, because there is no weighting that makes
  a fraction of a record, or a ratio of parts, true.

Everything else -- the same table, the ordinary case -- goes straight through to
:func:`app.services.kpi_execution.execute_kpi_any` untouched, so the query
detection runs and the query an investigation runs are the same query.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.connectors.base import DataSourceConnector
from app.connectors.sql import SqlConnector
from app.core.errors import ConnectorError, Conflict
from app.models.base import Aggregation
from app.services.detection import KpiBinding
from app.services.investigation_map import MappedRelationship, relationship_for
from app.services.kpi_execution import execute_kpi_any
from app.services.kpi_formula import (
    FILTER_OPERATORS,
    FilterSpec,
    FormulaSpec,
    _VALUELESS_OPERATORS,
)

# Imported rather than restated: null handling and float coercion must mean
# exactly the same thing on this path as on the single-table one, and a second
# copy of "SUM over an empty set is NULL unless the contract says zero" is a
# second place for the two to drift apart.
from app.services.kpi_sql import KpiResult, KpiValue, _as_float, _combine

#: Query-local aliases. Short, fixed, and never derived from user input.
_ANCHOR = "kb_a"
_LINE = "kb_l"
_WEIGHT = "kb_w"


# ---------------------------------------------------------------------------
# Where a column lives
# ---------------------------------------------------------------------------
def _is_local(table: str | None, anchor_table: str) -> bool:
    """Whether a spec's table reference means the KPI's own table."""

    if not table:
        return True
    return str(table).lower() == (anchor_table or "").lower()


def _related_tables(
    spec: FormulaSpec,
    anchor_table: str,
    *,
    group_table: str | None,
) -> set[str]:
    """Every table this read touches other than the KPI's own."""

    out: set[str] = set()
    for filter_spec in spec.filters:
        if not _is_local(filter_spec.table, anchor_table):
            out.add(str(filter_spec.table).lower())
    if group_table and not _is_local(group_table, anchor_table):
        out.add(str(group_table).lower())
    return out


def apportionment_note(binding: KpiBinding, dimension: Any) -> str | None:
    """How this breakdown was produced, in business language -- or ``None``.

    ``None`` is the ordinary case: the dimension is recorded on the same table the
    KPI is measured on, nothing is apportioned, and there is nothing to disclose.
    """

    table = getattr(dimension, "source_table", None)
    if _is_local(table, binding.table.table_name):
        return None
    return (
        f"{getattr(dimension, 'dimension_name', 'this dimension')} is recorded in more "
        f"detail than {binding.name} is measured. Each record's value is divided "
        "between its own lines in proportion to their size, so the parts below still "
        f"add up to the {binding.name} figure shown above. A record whose lines carry "
        "no size cannot be divided and is left out of the parts -- it stays inside the "
        "total, and appears as movement the breakdown does not account for."
    )


# ---------------------------------------------------------------------------
# The public read
# ---------------------------------------------------------------------------
def read_kpi(
    connector: DataSourceConnector,
    binding: KpiBinding,
    spec: FormulaSpec,
    *,
    day: date,
    dimension: Any | None = None,
    limit: int | None = None,
) -> KpiResult:
    """Evaluate ``spec`` for one day, optionally grouped by ``dimension``.

    The KPI's registered source, time field and window rule are used exactly as
    detection uses them. Whether one table or two are read is decided here and
    nowhere above it.
    """

    start, end = binding.window_for(day)
    return execute_grouped(
        connector,
        spec,
        schema=binding.table.schema_name,
        anchor_table=binding.table.table_name,
        time_column=binding.time_field,
        start=start,
        end=end,
        group_column=None if dimension is None else dimension.source_column,
        group_table=None if dimension is None else getattr(dimension, "source_table", None),
        limit=limit,
        kpi_label=binding.name,
    )


def execute_grouped(
    connector: DataSourceConnector,
    spec: FormulaSpec,
    *,
    schema: str,
    anchor_table: str,
    time_column: str | None = None,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    group_column: str | None = None,
    group_table: str | None = None,
    limit: int | None = None,
    kpi_label: str = "This KPI",
) -> KpiResult:
    """One KPI read. Single-table when it can be, apportioned when it must be."""

    related = _related_tables(spec, anchor_table, group_table=group_table)

    if not related:
        # Byte-identical to what detection runs. Nothing below this line executes.
        return execute_kpi_any(
            connector,
            spec,
            schema=schema,
            table=anchor_table,
            time_column=time_column,
            start=start,
            end=end,
            group_by=None if group_column is None else [group_column],
            limit=limit,
        )

    if len(related) > 1:
        raise Conflict(
            f"{kpi_label} cannot be broken down across more than one additional level "
            "of detail in a single step. Drill down one level at a time.",
            details={"levels": sorted(related)},
        )

    related_table = next(iter(related))
    relationship = relationship_for(anchor_table, related_table)
    if relationship is None or not relationship.allocation_weight:
        raise Conflict(
            f"{kpi_label} has no recorded way of matching its own records to the more "
            "detailed level this breakdown needs, so the split cannot be made without "
            "guessing. Register the relationship between the two before breaking this "
            "KPI down by it.",
            details={"kpi": kpi_label},
        )

    if not isinstance(connector, SqlConnector):
        raise Conflict(
            f"{kpi_label} lives on a source that can only be read one table at a time, "
            "so it cannot be broken down by something recorded at a finer level of "
            "detail. Breakdowns recorded alongside the KPI itself are available.",
            details={"source_type": str(connector.source_type)},
        )

    sql, params, ordered_group = _build_apportioned_query(
        connector,
        spec,
        schema=schema,
        anchor_table=anchor_table,
        relationship=relationship,
        time_column=time_column,
        start=start,
        end=end,
        group_column=group_column,
        group_on_related=bool(group_column) and not _is_local(group_table, anchor_table),
        limit=limit,
        kpi_label=kpi_label,
    )
    rows = connector._run(sql, params, guard=False)

    values: list[KpiValue] = []
    for row in rows:
        numerator = _as_float(row.get("numerator"))
        matched = row.get("matched_rows")
        matched_rows = int(matched) if matched is not None else None
        value, note = _combine(spec, numerator, None, matched_rows=matched_rows)
        group = (
            {ordered_group: row.get("grp_0")} if ordered_group is not None else {}
        )
        values.append(
            KpiValue(
                value=value,
                numerator=numerator,
                denominator=None,
                group=group,
                note=note,
                matched_rows=matched_rows,
            )
        )
    return KpiResult(rows=values, sql=sql, row_count=len(values))


# ---------------------------------------------------------------------------
# The apportioned query
# ---------------------------------------------------------------------------
def _build_apportioned_query(
    connector: SqlConnector,
    spec: FormulaSpec,
    *,
    schema: str,
    anchor_table: str,
    relationship: MappedRelationship,
    time_column: str | None,
    start: date | datetime | None,
    end: date | datetime | None,
    group_column: str | None,
    group_on_related: bool,
    limit: int | None,
    kpi_label: str,
) -> tuple[str, dict[str, Any], str | None]:
    measure = spec.numerator
    if (
        spec.denominator is not None
        or measure.is_count_star
        or measure.distinct
        or measure.aggregation != Aggregation.SUM
    ):
        raise Conflict(
            f"{kpi_label} is not a plain total, so it cannot be divided between levels "
            "of detail: one record spans several of them, and any division would either "
            "count that record more than once or hand back a fraction of it. This KPI "
            "can still be broken down by anything recorded alongside it.",
            details={"kpi": kpi_label},
        )

    target_schema = connector.resolve_schema(schema)
    anchor_relation = connector.qualify(target_schema, anchor_table)
    line_relation = connector.qualify(target_schema, relationship.table)

    measure_column = connector.quote(measure.column, kind="measure column")
    weight_column = connector.quote(relationship.allocation_weight, kind="weight column")
    line_key = connector.quote(relationship.foreign_key, kind="match column")
    anchor_key = connector.quote(relationship.parent_key, kind="match column")

    params: dict[str, Any] = {}
    clauses: list[str] = []

    if time_column and (start is not None or end is not None):
        quoted = connector.quote(time_column, kind="time column")
        if start is not None:
            clauses.append(f"{_ANCHOR}.{quoted} >= :_start")
            params["_start"] = connector.temporal_param(start)
        if end is not None:
            clauses.append(f"{_ANCHOR}.{quoted} <= :_end")
            params["_end"] = connector.temporal_param(end)

    # A record whose lines carry no size at all is excluded rather than divided by
    # zero. It stays inside the KPI, and the caller reports the difference.
    clauses.append(f"{_WEIGHT}.kb_total > 0")

    for index, filter_spec in enumerate(spec.filters):
        alias = _ANCHOR if _is_local(filter_spec.table, anchor_table) else _LINE
        clause, filter_params = _render_filter(connector, filter_spec, alias=alias, index=index)
        clauses.append(clause)
        params.update(filter_params)

    projections: list[str] = []
    group_expression: str | None = None
    if group_column:
        alias = _LINE if group_on_related else _ANCHOR
        quoted = connector.quote(group_column, kind="breakdown column")
        group_expression = f"{alias}.{quoted}"
        projections.append(f"{group_expression} AS grp_0")

    # ``* 1.0`` forces floating-point division: an integer measure divided by an
    # integer weight truncates on some engines, and a truncated share would not
    # reconcile with the KPI.
    projections.append(
        f"SUM({_ANCHOR}.{measure_column} * 1.0 * COALESCE({_LINE}.{weight_column}, 0)"
        f" / {_WEIGHT}.kb_total) AS numerator"
    )
    projections.append("COUNT(*) AS matched_rows")

    sql = (  # noqa: S608 - every identifier is validated and quoted by the connector
        f"WITH kb_weights AS ("
        f"SELECT {line_key} AS kb_key, SUM(COALESCE({weight_column}, 0)) AS kb_total"
        f" FROM {line_relation} GROUP BY {line_key})"
        f" SELECT {', '.join(projections)}"
        f" FROM {anchor_relation} AS {_ANCHOR}"
        f" JOIN {line_relation} AS {_LINE}"
        f" ON {_LINE}.{line_key} = {_ANCHOR}.{anchor_key}"
        f" JOIN kb_weights AS {_WEIGHT} ON {_WEIGHT}.kb_key = {_ANCHOR}.{anchor_key}"
        f" WHERE {' AND '.join(clauses)}"
    )
    if group_expression:
        sql += f" GROUP BY {group_expression} ORDER BY numerator DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"

    return sql, params, group_column


def _render_filter(
    connector: SqlConnector,
    filter_spec: FilterSpec,
    *,
    alias: str,
    index: int,
) -> tuple[str, dict[str, Any]]:
    """One governed filter, qualified to the table it was declared against."""

    quoted = f"{alias}.{connector.quote(filter_spec.column, kind='filter column')}"
    operator = FILTER_OPERATORS.get(filter_spec.operator)
    if operator is None:  # pragma: no cover - guarded at parse time
        raise Conflict(f"Filter operator {filter_spec.operator!r} is not permitted.")

    if filter_spec.operator in _VALUELESS_OPERATORS:
        return f"{quoted} {operator}", {}

    params: dict[str, Any] = {}
    if operator in {"IN", "NOT IN"}:
        raw = filter_spec.value
        values = raw if isinstance(raw, list | tuple) else [raw]
        if not values:
            raise Conflict(f"Filter on {filter_spec.column} has an empty value list.")
        placeholders = []
        for position, item in enumerate(values):
            name = f"_kb{index}_{position}"
            params[name] = item
            placeholders.append(f":{name}")
        return f"{quoted} {operator} ({', '.join(placeholders)})", params

    name = f"_kb{index}"
    params[name] = filter_spec.value
    return f"{quoted} {operator} :{name}", params


# ---------------------------------------------------------------------------
# Display names for a dimension whose values are identifiers
# ---------------------------------------------------------------------------
def labels_for(
    connector: DataSourceConnector,
    dimension: Any,
    entities: list[str],
) -> dict[str, str]:
    """Human names for identifier-valued entities, when the source carries them.

    Cosmetic and strictly optional. The identifier remains the entity a drill-down
    is performed on, so a missing or unreadable name changes what a reader sees and
    nothing else -- which is why a failure here falls back to the identifier
    instead of failing the investigation.
    """

    table = getattr(dimension, "label_table", None)
    key = getattr(dimension, "label_key", None)
    column = getattr(dimension, "label_column", None)
    wanted = [value for value in dict.fromkeys(entities) if value]
    if not (table and key and column) or not wanted:
        return {}
    if not isinstance(connector, SqlConnector):
        return {}

    quoted_key = connector.quote(str(key), kind="lookup column")
    quoted_label = connector.quote(str(column), kind="label column")
    relation = connector.qualify(connector.resolve_schema(None), str(table))
    params = {f"_kbl{index}": value for index, value in enumerate(wanted)}
    placeholders = ", ".join(f":{name}" for name in params)
    sql = (  # noqa: S608 - identifiers validated and quoted by the connector
        f"SELECT {quoted_key} AS kb_ref, {quoted_label} AS kb_label"
        f" FROM {relation} WHERE {quoted_key} IN ({placeholders})"
    )
    try:
        rows = connector._run(sql, params, guard=False)
    except ConnectorError:
        return {}

    out: dict[str, str] = {}
    for row in rows:
        reference = row.get("kb_ref")
        label = row.get("kb_label")
        if reference is None or label is None:
            continue
        text = str(label).strip()
        if text:
            out[str(reference)] = text
    return out
