"""One entry point for evaluating a KPI, whichever source it lives on.

The detection engine asks the same question for every KPI on every date -- *what
is this KPI's value for this window?* -- and that question has one answer per
source, not one answer per source technology. This module is the only place that
knows there is more than one way to get it:

* a SQL source gets the aggregate pushed down (:mod:`app.services.kpi_sql`);
* a REST source gets a filtered, projected read and a deterministic aggregation
  (:mod:`app.services.kpi_rest`).

Both start from the same approved ``FormulaSpec`` and both return the same
``KpiResult``, so nothing above this line branches on source type. That matters
more than it looks: the alternative -- letting the engine handle "the SQL case"
and treat everything else as unavailable -- is exactly how a platform ends up
with a detection feature that cannot run against the source the customer
actually connected.

The dispatch is on *capability*, not on a source-type name, so adding a
connector means teaching this function about it in one place and touching neither
the engine nor the API.
"""

from __future__ import annotations

from datetime import date, datetime

from app.connectors.base import DataSourceConnector
from app.connectors.sql import SqlConnector
from app.core.errors import ConnectorError
from app.services.kpi_formula import FormulaSpec
from app.services.kpi_rest import execute_kpi_rest, supports_rest_execution
from app.services.kpi_sql import KpiResult, execute_kpi


def execution_mode(connector: DataSourceConnector) -> str:
    """How this connector produces a KPI value, in a word fit for a log line."""

    if isinstance(connector, SqlConnector):
        return "sql_pushdown"
    if supports_rest_execution(connector):
        return "rest_projection"
    return "unsupported"


def can_execute(connector: DataSourceConnector) -> bool:
    return execution_mode(connector) != "unsupported"


def unsupported_reason(connector: DataSourceConnector) -> str:
    return (
        f"A {connector.source_type} source cannot be evaluated for detection on this "
        "build: it can neither run SQL nor serve a filtered row read. Register the data "
        "behind a SQL or Supabase source to make its KPIs detectable."
    )


def execute_kpi_any(
    connector: DataSourceConnector,
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
    """Evaluate ``spec`` over ``table`` for one window, by whichever path works.

    ``group_by`` asks for the same KPI broken down by one or more columns instead
    of as a single value, and ``limit`` keeps only the largest groups. Both go to
    whichever path is in use, so contribution analysis reads the KPI through the
    governed formula exactly as detection does -- one aggregate per part of the
    business rather than one for the whole -- and a breakdown means the same thing
    over a REST source as over Postgres.
    """

    if isinstance(connector, SqlConnector):
        return execute_kpi(
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
    if supports_rest_execution(connector):
        return execute_kpi_rest(
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
    raise ConnectorError(
        unsupported_reason(connector),
        details={"source_type": str(connector.source_type), "capability": "kpi_execution"},
    )
