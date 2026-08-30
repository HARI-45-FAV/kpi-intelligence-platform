"""Governed tools over the data layer: sources, profiles, joins, reconciliation.

This is where the connector boundary matters most, so it is worth being exact
about what these tools do and do not touch.

They read **metadata the platform already computed and stored**: discovered
tables and columns, profiling statistics, detected grain, freshness
observations, inferred relationships, join-safety verdicts and reconciliation
results. All of it lives in the platform's own database.

They never read tenant business rows, and there is no path by which they could.
No tool takes SQL, a table expression or a filter; none opens a connector; none
touches ``DataSource.encrypted_credentials``. Column profiles are already
access-filtered by ``analysis_views.column_payload``, which decides readability
*before* emitting anything -- a column the caller may not see arrives as
``readable: false`` with a reason, never as values that were fetched and then
trimmed. Sample values inside a profile are the one place stored data appears,
and they are suppressed here unless the caller could read that column through
the analysis API anyway.

Everything is shaped by ``app.services.analysis_views``, the same code behind the
analysis endpoints, so a Copilot explanation of a join risk and the join risk on
screen cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.copilot.context import CopilotContext
from app.copilot.tools.base import ToolResult, ToolSpec, refuse
from app.core.deps import load_selected_table
from app.models.base import JoinSafetyLevel
from app.models.source import DataSource, SourceTable
from app.services.analysis_views import (
    reconciliation_payload,
    relationship_payload,
    relationship_summary,
    scoped_tables,
    table_profile_view,
)

SOURCE_READ = ("source.read",)
ANALYTICS_READ = ("analytics.read",)

_TABLE_ARG = {
    "type": "string",
    "description": (
        "Table name as it appears in the catalog, optionally schema-qualified "
        "(for example 'orders' or 'public.orders')."
    ),
}


def _find_table(context: CopilotContext, name: str) -> SourceTable | None:
    """Match a table by name within the company's approved data scope only.

    ``scoped_tables`` is the boundary: a table that exists in the source but was
    never enabled under Data Scope is not findable here, exactly as it is not
    profilable or KPI-bindable.
    """
    wanted = name.strip().lower()
    tables = scoped_tables(context.session, context.access)
    for table in tables:
        if wanted in (table.table_name.lower(), table.qualified_name.lower(), table.id.lower()):
            return table
    # Second pass on a partial match, which is how people actually refer to
    # tables in conversation ("the orders table").
    matches = [t for t in tables if wanted in t.table_name.lower()]
    return matches[0] if len(matches) == 1 else None


def _scope_hint(context: CopilotContext) -> str:
    tables = scoped_tables(context.session, context.access)
    if not tables:
        return (
            "No tables are in this company's approved data scope, so no profiling "
            "results exist."
        )
    return "Tables in scope: " + ", ".join(t.table_name for t in tables) + "."


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def get_data_source_summary(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    """Registered sources, their connection state and their known limitations.

    Credentials are never part of the result. ``encrypted_credentials`` and
    ``username`` are not read; the host is reported because it is how a person
    identifies which database is meant, and it is already visible in the sources
    UI.
    """
    sources = list(
        context.session.scalars(
            select(DataSource)
            .where(DataSource.company_id == context.company_id)
            .order_by(DataSource.name)
        )
    )
    if not sources:
        return refuse(
            f"{context.company_name} has no registered data sources, so there is no "
            "source metadata to report."
        )

    in_scope = {t.data_source_id for t in scoped_tables(context.session, context.access)}
    rows: list[dict[str, Any]] = []
    for source in sources:
        selected = [t for t in source.tables if t.selection and t.selection.enabled]
        rows.append(
            {
                "name": source.name,
                "source_type": source.source_type,
                "description": source.description,
                "host": source.host,
                "database": source.database_name,
                "schema": source.schema_name,
                "connection_status": source.connection_status,
                "last_tested_at": source.last_tested_at,
                "last_test_error": source.last_test_error,
                "refresh_frequency": source.refresh_frequency,
                "timezone": source.timezone,
                "known_limitations": source.known_limitations,
                "discovered_table_count": len(source.tables),
                "tables_in_scope": len(selected),
                "in_analytical_scope": source.id in in_scope,
                "last_discovered_at": source.last_discovered_at,
            }
        )

    described = "; ".join(
        f"{row['name']} ({row['source_type']}, {row['connection_status']}): "
        f"{row['discovered_table_count']} table(s) discovered, "
        f"{row['tables_in_scope']} in analytical scope"
        + (f". Limitations: {row['known_limitations']}" if row["known_limitations"] else "")
        for row in rows
    )
    return ToolResult(
        data={"sources": rows, "count": len(rows)},
        evidence=[
            {
                "source_type": "data_source",
                "source_id": None,
                "company_id": context.company_id,
                "title": f"Registered data sources for {context.company_name}",
                "content": (
                    f"{described}. Discovery alone grants no analytical access: only "
                    "tables an administrator has enabled under Data Scope are profiled "
                    "or usable in a KPI."
                ),
                "metadata": {"source_count": len(rows)},
            }
        ],
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def get_table_profile(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    name = str(arguments["table"])
    table = _find_table(context, name)
    if table is None:
        return refuse(
            f"No table matching '{name}' is in this company's approved data scope. "
            + _scope_hint(context)
        )

    # Re-loaded through the scope guard so the entitlement decision is made by the
    # same function every other read path uses.
    table = load_selected_table(context.session, table.id, context.access)
    view = table_profile_view(context.session, table, context.access)

    profile = view.get("profile")
    grain = view.get("grain") or {}
    freshness = view.get("freshness")
    columns = view.get("columns") or []
    withheld = [c["column_name"] for c in columns if not c["readable"]]

    if profile is None:
        lines = [
            f"{view['table']} is in scope but has not been profiled yet, so no row "
            "counts, completeness or quality results exist for it."
        ]
    else:
        lines = [
            f"{view['table']} profiled at {profile['profiled_at']}: "
            f"{profile['row_count']} rows, completeness {profile['completeness_pct']}%, "
            f"quality {profile['quality_status']} (score {profile['quality_score']}).",
            f"{profile['profiled_column_count']} column(s) profiled, "
            f"{profile['withheld_column_count']} withheld by access policy.",
        ]
        if profile.get("warnings"):
            lines.append(f"Warnings: {profile['warnings']}")

    if grain.get("detected"):
        lines.append(
            f"Grain: {grain.get('inferred_grain') or grain.get('declared_grain')} "
            f"({'unique' if grain.get('is_unique') else 'not unique'}, confidence "
            f"{grain.get('confidence')}, method {grain.get('method')})."
        )
    else:
        lines.append("Grain has not been detected for this table.")

    if freshness:
        lines.append(
            f"Freshness: {freshness['status']}"
            + (f", lag {freshness['lag_seconds']}s" if freshness["lag_seconds"] else "")
            + (f", covering {freshness['coverage_start']} to {freshness['coverage_end']}"
               if freshness["coverage_start"] else "")
            + "."
        )
    if withheld:
        lines.append(
            f"Columns withheld from you by access policy: {', '.join(withheld)}. "
            "Their statistics are not included."
        )

    # Column statistics are governed metadata, but ``sample_values`` holds actual
    # values from the tenant's tables. They are dropped here: a profile answers
    # "what does this column look like", which needs the shape, not the rows.
    for column in columns:
        if column.get("profile"):
            column["profile"] = {
                key: value
                for key, value in column["profile"].items()
                if key != "sample_values"
            }

    return ToolResult(
        data=view,
        evidence=[
            {
                "source_type": "table_profile",
                "source_id": table.id,
                "company_id": context.company_id,
                "title": f"Profile of {view['table']}",
                "content": "\n".join(lines),
                "metadata": {
                    "table": view["table"],
                    "profiled": profile is not None,
                    "withheld_columns": len(withheld),
                },
            }
        ],
        caveats=(
            [f"{len(withheld)} column(s) were withheld by access policy."] if withheld else []
        ),
    )


def get_column_profile(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    name = str(arguments["table"])
    column_name = str(arguments["column"]).strip()
    table = _find_table(context, name)
    if table is None:
        return refuse(
            f"No table matching '{name}' is in this company's approved data scope. "
            + _scope_hint(context)
        )
    table = load_selected_table(context.session, table.id, context.access)

    view = table_profile_view(context.session, table, context.access)
    columns = view.get("columns") or []
    match = next(
        (c for c in columns if c["column_name"].lower() == column_name.lower()), None
    )
    if match is None:
        available = ", ".join(c["column_name"] for c in columns)
        return refuse(
            f"{table.qualified_name} has no column '{column_name}'. Columns: {available}."
        )

    if not match["readable"]:
        return refuse(
            f"You are not entitled to {table.qualified_name}.{match['column_name']} "
            f"({match['withheld_reason']}). Its profile cannot be included in the answer."
        )

    profile = match.get("profile")
    header = (
        f"{table.qualified_name}.{match['column_name']} is {match['data_type']}, "
        f"semantic type {match['semantic_type']}, classification "
        f"{match['classification']}"
        + (", primary key" if match["is_primary_key"] else "")
        + (", foreign key" if match["is_foreign_key"] else "")
        + "."
    )
    if profile is None:
        content = f"{header} It has not been profiled, so no statistics exist for it."
    else:
        content = "\n".join(
            [
                header,
                f"{profile['row_count']} rows, {profile['null_count']} null "
                f"({profile['null_pct']}%), {profile['distinct_count']} distinct "
                f"({profile['distinct_pct']}%).",
                f"Range: min {profile['min']}, max {profile['max']}, mean {profile['mean']}."
                if profile["min"] is not None or profile["max"] is not None
                else "No numeric range recorded.",
                f"Uniqueness: {'unique' if profile['is_unique'] else 'not unique'}"
                + (", candidate key" if profile["is_candidate_key"] else "")
                + f". Quality: {profile['quality_status']}.",
            ]
            + ([f"Warnings: {profile['warnings']}"] if profile.get("warnings") else [])
        )

    # Sample values are real rows from the tenant's table. Excluded for the same
    # reason as in the table profile.
    payload = dict(match)
    if payload.get("profile"):
        payload["profile"] = {
            key: value for key, value in payload["profile"].items() if key != "sample_values"
        }

    return ToolResult(
        data={"table": table.qualified_name, "column": payload},
        evidence=[
            {
                "source_type": "column_profile",
                "source_id": table.id,
                "company_id": context.company_id,
                "title": f"Profile of {table.qualified_name}.{match['column_name']}",
                "content": content,
                "metadata": {
                    "table": table.qualified_name,
                    "column": match["column_name"],
                    "classification": match["classification"],
                },
            }
        ],
    )


# ---------------------------------------------------------------------------
# Relationships and joins
# ---------------------------------------------------------------------------
def get_relationship_summary(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    relationships = relationship_payload(context.session, context.access)
    if not relationships:
        return refuse(
            "No relationships have been detected between tables in this company's "
            "approved data scope. " + _scope_hint(context)
        )

    summary = relationship_summary(relationships)
    described = "; ".join(
        f"{r['from_table']}.{r['from_column']} -> {r['to_table']}.{r['to_column']} "
        f"({r['type']}, {'declared' if r['is_declared'] else r['method']}"
        + (f", {r['orphan_count']} orphan rows" if r["orphan_count"] else "")
        + ")"
        for r in relationships
    )
    return ToolResult(
        data={"relationships": relationships, "summary": summary},
        evidence=[
            {
                "source_type": "relationship",
                "source_id": None,
                "company_id": context.company_id,
                "title": f"Detected table relationships in {context.company_name}",
                "content": (
                    f"{summary['checked']} relationship(s): {summary['safe']} safe, "
                    f"{summary['needs_attention']} safe only with aggregation, "
                    f"{summary['unsafe']} risky, {summary['unrated']} unrated. "
                    f"{summary['material_count']} could change a KPI number. {described}."
                ),
                "metadata": {
                    "relationship_count": summary["checked"],
                    "material_count": summary["material_count"],
                },
            }
        ],
    )


def get_join_safety_summary(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    """Which joins can silently change a number, and why.

    The verdicts are computed by the deterministic join-safety analysis. This
    tool reports them; it does not judge, re-derive or soften them.
    """
    relationships = relationship_payload(context.session, context.access)
    rated = [r for r in relationships if r.get("join_safety")]
    if not rated:
        return refuse(
            "No join-safety analysis has been recorded for this company's tables, so "
            "there is no verdict to report."
        )

    only_risky = bool(arguments.get("only_risky"))
    rows = [
        {
            "from": f"{r['from_table']}.{r['from_column']}",
            "to": f"{r['to_table']}.{r['to_column']}",
            "relationship_type": r["type"],
            "orphan_count": r["orphan_count"],
            "orphan_pct": r["orphan_pct"],
            **r["join_safety"],
        }
        for r in rated
        if not only_risky or r["join_safety"]["level"] != JoinSafetyLevel.SAFE
    ]
    if not rows:
        return ToolResult(
            data={"joins": [], "all_safe": True},
            evidence=[
                {
                    "source_type": "join_safety",
                    "source_id": None,
                    "company_id": context.company_id,
                    "title": "All analysed joins are rated safe",
                    "content": (
                        f"Every one of the {len(rated)} analysed join(s) in this company "
                        "is rated SAFE by the deterministic join-safety analysis."
                    ),
                    "metadata": {"analysed": len(rated)},
                }
            ],
        )

    described = "\n".join(
        f"- {row['from']} -> {row['to']}: {row['level']}"
        + (f" (fan-out {row['fan_out_factor']}, max {row['max_fan_out']})"
           if row.get("fan_out_factor") else "")
        + (f", {row['orphan_count']} orphan rows" if row["orphan_count"] else "")
        + (f". {row['reason']}" if row.get("reason") else "")
        + (f" Guidance: {row['guidance']}" if row.get("guidance") else "")
        for row in rows
    )
    return ToolResult(
        data={"joins": rows, "all_safe": False, "analysed": len(rated)},
        evidence=[
            {
                "source_type": "join_safety",
                "source_id": None,
                "company_id": context.company_id,
                "title": f"Join safety findings in {context.company_name}",
                "content": (
                    f"{len(rows)} of {len(rated)} analysed join(s) need care. A fan-out "
                    "join duplicates rows on the many side, which inflates a SUM without "
                    "any error being raised; orphan rows are dropped by an inner join.\n"
                    f"{described}"
                ),
                "metadata": {"flagged": len(rows), "analysed": len(rated)},
            }
        ],
    )


def get_reconciliation_result(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    rows = reconciliation_payload(context.session, context.access)
    if not rows:
        return refuse(
            "No cross-source reconciliation has been recorded for this company, so "
            "there is no comparability verdict to report."
        )

    described = "\n".join(
        f"- {row['left_table']} vs {row['right_table']}: {row['status']}"
        + (f" (grain {row['left_grain']} vs {row['right_grain']}"
           f", time {row['left_time_grain']} vs {row['right_time_grain']})"
           if row["left_grain"] or row["right_grain"] else "")
        + (f", {row['time_overlap_days']} overlapping days"
           if row["time_overlap_days"] is not None else "")
        + (f". {row['reason']}" if row["reason"] else "")
        + (f" Guidance: {row['guidance']}" if row["guidance"] else "")
        for row in rows
    )
    return ToolResult(
        data={"reconciliations": rows, "count": len(rows)},
        evidence=[
            {
                "source_type": "reconciliation",
                "source_id": None,
                "company_id": context.company_id,
                "title": f"Cross-source reconciliation in {context.company_name}",
                "content": (
                    f"{len(rows)} table pair(s) assessed for comparability. Reconciliation "
                    "says whether two sources can be compared at all -- differing grain, "
                    "time grain or unmapped dimensions make a difference between them "
                    "meaningless rather than interesting.\n" + described
                ),
                "metadata": {"pair_count": len(rows)},
            }
        ],
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_data_source_summary",
        description=(
            "The company's registered data sources: type, connection status, refresh "
            "frequency, timezone, known limitations, and how many tables are in "
            "analytical scope. Never returns credentials."
        ),
        permissions=SOURCE_READ,
        parameters={"type": "object", "properties": {}, "required": []},
        handler=get_data_source_summary,
    ),
    ToolSpec(
        name="get_table_profile",
        description=(
            "Stored profiling results for one table in the approved data scope: row "
            "count, completeness, quality status, detected grain, freshness and per-column "
            "statistics. Reads previously computed metadata, not the table's rows."
        ),
        permissions=ANALYTICS_READ,
        parameters={
            "type": "object",
            "properties": {"table": _TABLE_ARG},
            "required": ["table"],
        },
        handler=get_table_profile,
    ),
    ToolSpec(
        name="get_column_profile",
        description=(
            "Stored profiling statistics for one column: type, semantic type, "
            "classification, null and distinct counts, range, uniqueness and quality. "
            "Refuses columns the caller is not entitled to see."
        ),
        permissions=ANALYTICS_READ,
        parameters={
            "type": "object",
            "properties": {
                "table": _TABLE_ARG,
                "column": {"type": "string", "description": "Column name."},
            },
            "required": ["table", "column"],
        },
        handler=get_column_profile,
    ),
    ToolSpec(
        name="get_relationship_summary",
        description=(
            "Detected and declared relationships between tables in scope, with "
            "cardinality, confidence, orphan-row counts and join-safety level. Use it for "
            "questions about how tables connect."
        ),
        permissions=ANALYTICS_READ,
        parameters={"type": "object", "properties": {}, "required": []},
        handler=get_relationship_summary,
    ),
    ToolSpec(
        name="get_join_safety_summary",
        description=(
            "Join-safety verdicts: which joins fan out and would inflate a SUM, which "
            "drop orphan rows, and the guidance recorded for each. Use it when asked "
            "whether a number can be trusted across tables."
        ),
        permissions=ANALYTICS_READ,
        parameters={
            "type": "object",
            "properties": {
                "only_risky": {
                    "type": "boolean",
                    "description": "Return only joins not rated SAFE. Default false.",
                }
            },
            "required": [],
        },
        handler=get_join_safety_summary,
    ),
    ToolSpec(
        name="get_reconciliation_result",
        description=(
            "Whether two sources can be compared at all: matching grain, time grain, "
            "shared and unmapped dimensions, and overlapping time range. Use it before "
            "explaining a difference between two sources."
        ),
        permissions=ANALYTICS_READ,
        parameters={"type": "object", "properties": {}, "required": []},
        handler=get_reconciliation_result,
    ),
)
