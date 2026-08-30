"""Governed tools over the KPI layer.

Everything here reads approved governance material: definitions, immutable
versions, the structured formula contract, lineage regenerated from that
contract, declared dimensions and drivers, and stored validation runs. The
Copilot can explain all of it. It can change none of it -- there is no tool that
writes, submits, approves, activates or deprecates anything, so a request to
"just activate v3" has no mechanism behind it, not merely a refusal.

Two deliberate reuses:

* ``export_contract`` is the same function the KPI contract API serves, so the
  Copilot's account of a formula is the account the governance screen shows.
* ``latest_validation_summary`` reads the stored run. It does not revalidate, so
  asking the Copilot about a KPI cannot quietly execute checks against the
  tenant's database, and a KPI that has never been validated is reported as
  never validated.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.copilot.context import CopilotContext
from app.copilot.tools.base import ToolResult, ToolSpec, refuse
from app.core.deps import load_scoped
from app.core.errors import NotFound
from app.models.base import KpiStatus
from app.models.kpi import KpiDefinition, KpiVersion
from app.services.kpi_governance import export_contract
from app.services.kpi_validation import latest_validation_summary

# Every tool in this module needs exactly this, and nothing more.
KPI_READ = ("kpi.read",)

_KPI_ARG = {
    "type": "string",
    "description": (
        "The KPI's business key (for example 'revenue') or its definition id, as it "
        "appears in the current context. Never a KPI from another company."
    ),
}
_VERSION_ARG = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "Version number. Omit for the version currently in force, which is the "
        "ACTIVE one where a KPI has been activated."
    ),
}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------
def _definition(context: CopilotContext, kpi: str | None) -> KpiDefinition | None:
    """Resolve a KPI reference inside the caller's company, or fall back to context.

    Both ``load_scoped`` and the key lookup are company-bounded, so an id
    belonging to another tenant behaves exactly like an id that does not exist.
    """
    if not kpi:
        return context.kpi_definition

    session, access = context.session, context.access
    try:
        return load_scoped(session, KpiDefinition, kpi, access)
    except NotFound:
        pass
    return session.scalar(
        select(KpiDefinition).where(
            KpiDefinition.company_id == access.company.id, KpiDefinition.kpi_key == kpi
        )
    )


def _version(
    context: CopilotContext, definition: KpiDefinition, number: int | None
) -> KpiVersion | None:
    versions = list(definition.versions)
    if not versions:
        return None
    if number is not None:
        return next((v for v in versions if v.version == number), None)
    # No version asked for: prefer what the user is looking at, then what is
    # live, then the newest draft.
    if (
        context.kpi_version is not None
        and context.kpi_version.kpi_id == definition.id
    ):
        return context.kpi_version
    return next(
        (v for v in versions if v.status == KpiStatus.ACTIVE),
        max(versions, key=lambda v: v.version),
    )


def _resolve(
    context: CopilotContext, arguments: dict[str, Any]
) -> tuple[KpiDefinition, KpiVersion] | ToolResult:
    """Shared front half of every version-scoped tool."""
    kpi = arguments.get("kpi")
    definition = _definition(context, kpi)
    if definition is None:
        target = f"'{kpi}'" if kpi else "the KPI in the current context"
        return refuse(
            f"No KPI matching {target} exists in {context.company_name}. Do not guess "
            "which KPI was meant."
        )

    number = arguments.get("version")
    version = _version(context, definition, number)
    if version is None:
        detail = f"version {number}" if number is not None else "any stored version"
        return refuse(f"{definition.name} has no {detail}.")
    return definition, version


def _identity(definition: KpiDefinition, version: KpiVersion) -> dict[str, Any]:
    return {
        "kpi_key": definition.kpi_key,
        "name": definition.name,
        "version": version.version,
        "version_status": version.status,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def get_active_kpis(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    definitions = list(
        context.session.scalars(
            select(KpiDefinition)
            .where(KpiDefinition.company_id == context.company_id)
            .order_by(KpiDefinition.name)
        )
    )
    include_all = bool(arguments.get("include_all_statuses"))

    rows: list[dict[str, Any]] = []
    for definition in definitions:
        active = next(
            (v for v in definition.versions if v.status == KpiStatus.ACTIVE), None
        )
        if active is None and not include_all:
            continue
        latest = (
            max(definition.versions, key=lambda v: v.version) if definition.versions else None
        )
        shown = active or latest
        rows.append(
            {
                "kpi_key": definition.kpi_key,
                "name": definition.name,
                "definition_status": definition.status,
                "short_description": definition.short_description,
                "active_version": active.version if active else None,
                "latest_version": latest.version if latest else None,
                "latest_version_status": latest.status if latest else None,
                "unit": shown.unit if shown else None,
                "currency": shown.currency if shown else None,
                "direction": shown.direction if shown else None,
                "formula": shown.formula_expression if shown else None,
            }
        )

    if rows:
        described = "; ".join(
            f"{row['name']} ({row['kpi_key']}): "
            + (
                f"{row['formula'] or 'no formula recorded'}, active v{row['active_version']}"
                if row["active_version"]
                else "not activated"
            )
            for row in rows
        )
        scope = "all lifecycle states" if include_all else "with an ACTIVE version"
        content = f"{len(rows)} KPI(s), {scope}. {described}."
    else:
        content = (
            "This company has no ACTIVE KPI versions."
            if definitions
            else "This company has no KPIs registered."
        )

    inactive = sum(
        1
        for definition in definitions
        if not any(v.status == KpiStatus.ACTIVE for v in definition.versions)
    )
    caveats = (
        [f"{inactive} KPI(s) exist without an ACTIVE version and are not listed here."]
        if inactive and not include_all
        else []
    )
    return ToolResult(
        data={"kpis": rows, "count": len(rows)},
        evidence=[
            {
                "source_type": "kpi_contract",
                "source_id": None,
                "company_id": context.company_id,
                "title": f"Governed KPI register for {context.company_name}",
                "content": content,
                "metadata": {"kpi_count": len(rows)},
            }
        ],
        caveats=caveats,
    )


def get_kpi_definition(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    resolved = _resolve(context, arguments)
    if isinstance(resolved, ToolResult):
        return resolved
    definition, version = resolved

    contract = export_contract(context.session, version)
    # The whole contract is governed metadata, but the answer only needs the
    # meaning of the KPI. Lineage, dimensions and drivers have their own tools,
    # so sending them here would spend context on material the model did not ask
    # for.
    data = {
        key: contract.get(key)
        for key in (
            "kpi_id",
            "name",
            "version",
            "status",
            "business_definition",
            "purpose",
            "kind",
            "formula",
            "formula_spec",
            "aggregation",
            "numerator",
            "denominator",
            "filters",
            "is_additive",
            "additivity_note",
            "unit",
            "currency",
            "direction",
            "null_handling",
            "time_field",
            "time_grain",
            "timezone",
            "calendar",
            "source",
            "governance",
        )
    }

    lines = [
        f"Business definition: {version.business_definition}",
        f"Formula: {version.formula_expression}",
        f"Kind: {version.kind}; aggregation: {version.aggregation or 'n/a'}; "
        f"null handling: {version.null_handling}",
        f"Unit: {version.unit or 'unspecified'}"
        + (f" ({version.currency})" if version.currency else "")
        + f"; direction: {version.direction}",
        f"Time field: {version.time_field or 'unspecified'} at {version.time_grain} grain"
        + (f" in {version.timezone}" if version.timezone else ""),
        f"Lifecycle status: {version.status}",
    ]
    if version.purpose:
        lines.insert(1, f"Purpose: {version.purpose}")
    if contract.get("is_additive") is False and contract.get("additivity_note"):
        lines.append(f"Additivity: {contract['additivity_note']}")
    if version.filters:
        lines.append(f"Filters applied: {version.filters}")

    return ToolResult(
        data=data,
        evidence=[
            {
                "source_type": "kpi_contract",
                "source_id": version.id,
                "company_id": context.company_id,
                "title": f"{definition.name} v{version.version} governed contract",
                "content": "\n".join(lines),
                "metadata": {
                    "kpi_key": definition.kpi_key,
                    "kpi_version": version.version,
                    "status": version.status,
                },
            }
        ],
    )


def get_kpi_version(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    """Lifecycle and provenance of the versions, not their calculation."""
    kpi = arguments.get("kpi")
    definition = _definition(context, kpi)
    if definition is None:
        target = f"'{kpi}'" if kpi else "the KPI in the current context"
        return refuse(f"No KPI matching {target} exists in {context.company_name}.")

    versions = sorted(definition.versions, key=lambda v: v.version)
    if not versions:
        return refuse(f"{definition.name} has no stored versions yet.")

    rows = [
        {
            "version": v.version,
            "status": v.status,
            "business_definition": v.business_definition,
            "formula": v.formula_expression,
            "proposal_origin": v.proposal_origin,
            "supersedes_version": v.supersedes_version,
            "submitted_at": v.submitted_at,
            "reviewed_at": v.reviewed_at,
            "approved_at": v.approved_at,
            "approval_reason": v.approval_reason,
            "rejection_reason": v.rejection_reason,
            "activated_at": v.activated_at,
            "deprecated_at": v.deprecated_at,
            "last_validation_status": v.last_validation_status,
            "last_validated_at": v.last_validated_at,
            "definition_source": v.definition_source,
        }
        for v in versions
    ]
    active = next((v for v in versions if v.status == KpiStatus.ACTIVE), None)

    history = "; ".join(
        f"v{row['version']} is {row['status']}"
        + (f", activated {row['activated_at']}" if row["activated_at"] else "")
        + (f", rejected: {row['rejection_reason']}" if row["rejection_reason"] else "")
        for row in rows
    )
    return ToolResult(
        data={
            "kpi_key": definition.kpi_key,
            "name": definition.name,
            "definition_status": definition.status,
            "active_version": active.version if active else None,
            "versions": rows,
        },
        evidence=[
            {
                "source_type": "kpi_contract",
                "source_id": definition.id,
                "company_id": context.company_id,
                "title": f"{definition.name} version history",
                "content": (
                    f"{len(rows)} version(s). {history}. "
                    + (
                        f"v{active.version} is in force."
                        if active
                        else "No version is ACTIVE, so this KPI is not in force."
                    )
                    + " Versions are immutable: editing an approved KPI creates the next "
                    "version in DRAFT rather than changing the one already in use."
                ),
                "metadata": {"kpi_key": definition.kpi_key, "version_count": len(rows)},
            }
        ],
    )


def get_kpi_validation_summary(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    resolved = _resolve(context, arguments)
    if isinstance(resolved, ToolResult):
        return resolved
    definition, version = resolved

    summary = latest_validation_summary(context.session, version)
    if summary is None:
        return ToolResult(
            data={**_identity(definition, version), "validated": False},
            evidence=[
                {
                    "source_type": "kpi_validation",
                    "source_id": version.id,
                    "company_id": context.company_id,
                    "title": f"{definition.name} v{version.version} has never been validated",
                    "content": (
                        "No validation run is on record for this version. There is no "
                        "pass, fail or warning to report, and nothing can be inferred "
                        "about the KPI's correctness from the absence of a run."
                    ),
                    "metadata": {"kpi_key": definition.kpi_key, "kpi_version": version.version},
                }
            ],
        )

    failing = [c for c in summary["checks"] if c["status"] == "FAIL"]
    blocking = [c for c in failing if c["is_blocking"]]
    warned = [c for c in summary["checks"] if c["status"] == "WARN"]

    detail = "\n".join(
        f"- {c['label']} [{c['test_type']}]: {c['status']}"
        + (f" -- {c['message']}" if c["message"] else "")
        + (
            f" (expected {c['expected']}, actual {c['actual']})"
            if c["expected"] or c["actual"]
            else ""
        )
        + ("" if c["is_blocking"] else " (advisory)")
        for c in summary["checks"]
    )
    return ToolResult(
        data={
            **_identity(definition, version),
            "validated": True,
            **{
                key: summary[key]
                for key in (
                    "run_id",
                    "overall_status",
                    "ready_for_approval",
                    "summary",
                    "started_at",
                    "passed",
                    "failed",
                    "warned",
                    "checks",
                )
            },
            "blocking_failures": [c["label"] for c in blocking],
        },
        evidence=[
            {
                "source_type": "kpi_validation",
                "source_id": summary["run_id"],
                "company_id": context.company_id,
                "title": (
                    f"{definition.name} v{version.version} validation: "
                    f"{summary['overall_status']}"
                ),
                "content": (
                    f"Run {summary['run_id']} at {summary['started_at']}: "
                    f"{summary['passed']} passed, {summary['failed']} failed, "
                    f"{summary['warned']} warned. "
                    + (
                        f"{len(blocking)} blocking failure(s) prevent activation."
                        if blocking
                        else "No blocking failure prevents activation."
                    )
                    + f"\n{detail}"
                ),
                "metadata": {
                    "kpi_key": definition.kpi_key,
                    "kpi_version": version.version,
                    "overall_status": summary["overall_status"],
                },
            }
        ],
        caveats=(
            ["These are stored results from the last run, not a fresh validation."]
            if failing or warned
            else []
        ),
    )


def get_kpi_lineage(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    resolved = _resolve(context, arguments)
    if isinstance(resolved, ToolResult):
        return resolved
    definition, version = resolved

    rows = [
        {
            "role": item.role,
            "data_source": item.data_source_name,
            "schema": item.schema_name,
            "table": item.table_name,
            "column": item.column_name,
            "transformation": item.transformation,
            "notes": item.notes,
        }
        for item in sorted(version.lineage, key=lambda i: (i.role, i.table_name or ""))
    ]
    if not rows:
        return refuse(
            f"{definition.name} v{version.version} has no lineage recorded. Lineage is "
            "generated from the formula contract, so this version's formula has not "
            "been bound to source columns."
        )

    described = "; ".join(
        f"{row['role']}: {row['schema']}.{row['table']}.{row['column']}"
        + (f" via {row['transformation']}" if row["transformation"] else "")
        for row in rows
    )
    return ToolResult(
        data={**_identity(definition, version), "lineage": rows},
        evidence=[
            {
                "source_type": "kpi_lineage",
                "source_id": version.id,
                "company_id": context.company_id,
                "title": f"{definition.name} v{version.version} column lineage",
                "content": (
                    f"Derived from the governed formula contract, not maintained by hand. "
                    f"{described}."
                ),
                "metadata": {"kpi_key": definition.kpi_key, "kpi_version": version.version},
            }
        ],
    )


def get_kpi_dimensions(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    resolved = _resolve(context, arguments)
    if isinstance(resolved, ToolResult):
        return resolved
    definition, version = resolved

    rows = [
        {
            "dimension_name": d.dimension_name,
            "source_table": d.source_table,
            "source_column": d.source_column,
            "hierarchy": d.hierarchy,
            "allowed": d.allowed,
            "is_default_breakdown": d.is_default_breakdown,
            "approx_cardinality": d.approx_cardinality,
            "notes": d.notes,
        }
        for d in sorted(version.dimensions, key=lambda d: d.dimension_name)
    ]
    if not rows:
        return refuse(
            f"{definition.name} v{version.version} has no declared dimensions, so there "
            "is no governed way to break it down."
        )

    allowed = [row for row in rows if row["allowed"]]
    return ToolResult(
        data={**_identity(definition, version), "dimensions": rows},
        evidence=[
            {
                "source_type": "kpi_dimension",
                "source_id": version.id,
                "company_id": context.company_id,
                "title": f"{definition.name} v{version.version} governed dimensions",
                "content": (
                    "A declared dimension is a valid way to slice this KPI. It is not an "
                    "instruction to monitor each value, and no per-dimension analysis has "
                    "been run. "
                    + "; ".join(
                        f"{row['dimension_name']} from {row['source_table']}."
                        f"{row['source_column']}"
                        + (
                            f", ~{row['approx_cardinality']} values"
                            if row["approx_cardinality"]
                            else ""
                        )
                        + ("" if row["allowed"] else " (not allowed)")
                        + (" (default breakdown)" if row["is_default_breakdown"] else "")
                        for row in rows
                    )
                ),
                "metadata": {
                    "kpi_key": definition.kpi_key,
                    "kpi_version": version.version,
                    "allowed_count": len(allowed),
                },
            }
        ],
    )


def get_kpi_drivers(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    resolved = _resolve(context, arguments)
    if isinstance(resolved, ToolResult):
        return resolved
    definition, version = resolved

    rows = [
        {
            "driver_name": d.driver_name,
            "driver_type": d.driver_type,
            "source_table": d.source_table,
            "source_column": d.source_column,
            "controllable": d.controllable,
            "measurement_method": d.measurement_method,
            "notes": d.notes,
        }
        for d in sorted(version.drivers, key=lambda d: d.driver_name)
    ]
    if not rows:
        return refuse(
            f"{definition.name} v{version.version} has no registered drivers, so there "
            "are no governed candidate explanations for its movements."
        )

    return ToolResult(
        data={**_identity(definition, version), "drivers": rows},
        evidence=[
            {
                "source_type": "kpi_driver",
                "source_id": version.id,
                "company_id": context.company_id,
                "title": f"{definition.name} v{version.version} registered drivers",
                "content": (
                    "These are candidate explanatory factors registered by the business. "
                    "They are hypotheses to investigate, not measured causes: no driver "
                    "analysis, attribution or correlation has been computed. "
                    + "; ".join(
                        f"{row['driver_name']} ({row['driver_type']}"
                        + (", controllable" if row["controllable"] else ", not controllable")
                        + ")"
                        + (
                            f" measured from {row['source_table']}.{row['source_column']}"
                            if row["source_column"]
                            else ""
                        )
                        for row in rows
                    )
                ),
                "metadata": {
                    "kpi_key": definition.kpi_key,
                    "kpi_version": version.version,
                    "driver_count": len(rows),
                },
            }
        ],
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_active_kpis",
        description=(
            "List this company's governed KPIs with their lifecycle state, formula and "
            "unit. Use it to find out which KPIs exist before asking about one."
        ),
        permissions=KPI_READ,
        parameters={
            "type": "object",
            "properties": {
                "include_all_statuses": {
                    "type": "boolean",
                    "description": (
                        "Include KPIs that are not activated (DRAFT, PROPOSED, "
                        "UNDER_REVIEW, REJECTED, DEPRECATED). Default false."
                    ),
                }
            },
            "required": [],
        },
        handler=get_active_kpis,
    ),
    ToolSpec(
        name="get_kpi_definition",
        description=(
            "The governed contract for one KPI version: business definition, purpose, "
            "structured formula, aggregation, filters, unit, direction, time grain and "
            "additivity. This is the authoritative meaning of the KPI."
        ),
        permissions=KPI_READ,
        parameters={
            "type": "object",
            "properties": {"kpi": _KPI_ARG, "version": _VERSION_ARG},
            "required": [],
        },
        handler=get_kpi_definition,
    ),
    ToolSpec(
        name="get_kpi_version",
        description=(
            "Lifecycle history of a KPI's versions: which is ACTIVE, when each was "
            "submitted, reviewed, approved, activated or rejected, and why. Use it for "
            "questions about governance state rather than calculation."
        ),
        permissions=KPI_READ,
        parameters={
            "type": "object",
            "properties": {"kpi": _KPI_ARG},
            "required": [],
        },
        handler=get_kpi_version,
    ),
    ToolSpec(
        name="get_kpi_validation_summary",
        description=(
            "The stored result of the last governance validation run for a KPI version: "
            "each check's status, expected and actual values, and which failures block "
            "activation. Reads recorded results; runs nothing."
        ),
        permissions=KPI_READ,
        parameters={
            "type": "object",
            "properties": {"kpi": _KPI_ARG, "version": _VERSION_ARG},
            "required": [],
        },
        handler=get_kpi_validation_summary,
    ),
    ToolSpec(
        name="get_kpi_lineage",
        description=(
            "Column-level lineage for a KPI version: which source table and column feeds "
            "the numerator, denominator, time field, filters and dimensions. Generated "
            "from the formula contract."
        ),
        permissions=KPI_READ,
        parameters={
            "type": "object",
            "properties": {"kpi": _KPI_ARG, "version": _VERSION_ARG},
            "required": [],
        },
        handler=get_kpi_lineage,
    ),
    ToolSpec(
        name="get_kpi_dimensions",
        description=(
            "The dimensions a KPI version may be sliced by, with their source columns, "
            "hierarchies and approximate cardinality. Declaring a dimension does not mean "
            "it is monitored."
        ),
        permissions=KPI_READ,
        parameters={
            "type": "object",
            "properties": {"kpi": _KPI_ARG, "version": _VERSION_ARG},
            "required": [],
        },
        handler=get_kpi_dimensions,
    ),
    ToolSpec(
        name="get_kpi_drivers",
        description=(
            "Candidate explanatory factors registered against a KPI version, and whether "
            "the business can control each. These are hypotheses, not measured causes."
        ),
        permissions=KPI_READ,
        parameters={
            "type": "object",
            "properties": {"kpi": _KPI_ARG, "version": _VERSION_ARG},
            "required": [],
        },
        handler=get_kpi_drivers,
    ),
)
