"""Governed tools over the platform's own runtime behaviour.

Two questions people actually ask a Copilot about a governed platform: "what has
this thing been doing?" and "is any of this AI?". Both are answered from recorded
telemetry rather than from prose.

``get_execution_summary`` reads ``execution_logs``, which the telemetry
middleware writes for every request: latency, connector queries, errors, and the
model columns. When ``LLM_ENABLED=false`` the model-call count is a genuine zero
read from the database, not a claim.

``get_platform_capabilities`` exists because the most likely wrong answer this
Copilot could give is a confident one about a feature that does not exist --
anomaly detection, forecasting, baselines. Rather than relying on the system
prompt alone to suppress that, the boundary is a retrievable fact the model can
cite.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.copilot.context import CopilotContext
from app.copilot.tools.base import ToolResult, ToolSpec
from app.llm.config import get_llm_config
from app.models.observability import ExecutionLog

TELEMETRY_READ = ("telemetry.read",)

# What the deterministic platform does today, and what it does not. Kept next to
# the tool that serves it so the two cannot drift.
DELIVERED = (
    "multi-tenant isolation, authentication and role-based entitlement",
    "data source registry and schema discovery",
    "administrator-approved analytical scope (Data Scope)",
    "access-aware column and table profiling",
    "grain detection, relationship inference, join-safety and reconciliation analysis",
    "freshness and quality assessment",
    "versioned semantic catalog",
    "company document store with versioning and role scoping",
    "governed KPI contracts with immutable versions, lifecycle and validation",
    "deterministic KPI calculation by SQL pushdown to the tenant's own database",
    "detection: a KPI evaluated on a date against the company's own approved "
    "comparison policy, producing an actual, an expected value, a deviation and a "
    "NORMAL / ABNORMAL / LOW_CONFIDENCE verdict",
    "comparison policy extracted from company documentation and approved before use",
    "audit trail and execution telemetry",
)

NOT_AVAILABLE = (
    "forecasting",
    "KPI alerting or notification",
    "contribution or driver attribution analysis",
    "dimensional or root-cause analysis of a detected deviation",
    "automated investigation runs",
    "narrative generation over computed results",
    "recommendations",
)


def get_execution_summary(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    company_id = context.company_id
    service = (arguments.get("service") or "").strip() or None

    query = select(
        func.count(ExecutionLog.id),
        func.avg(ExecutionLog.duration_ms),
        func.max(ExecutionLog.duration_ms),
        func.sum(ExecutionLog.query_count),
        func.sum(ExecutionLog.query_duration_ms),
        func.sum(ExecutionLog.rows_returned),
        func.sum(ExecutionLog.llm_calls),
        func.sum(ExecutionLog.prompt_tokens),
        func.sum(ExecutionLog.completion_tokens),
        func.sum(ExecutionLog.estimated_cost_usd),
    ).where(ExecutionLog.company_id == company_id)
    if service:
        query = query.where(ExecutionLog.service == service)
    totals = context.session.execute(query).one()

    errors = context.session.scalar(
        select(func.count(ExecutionLog.id)).where(
            ExecutionLog.company_id == company_id, ExecutionLog.status == "ERROR"
        )
    )
    per_service = context.session.execute(
        select(
            ExecutionLog.service,
            func.count(ExecutionLog.id),
            func.avg(ExecutionLog.duration_ms),
            func.sum(ExecutionLog.query_count),
        )
        .where(ExecutionLog.company_id == company_id)
        .group_by(ExecutionLog.service)
        .order_by(func.count(ExecutionLog.id).desc())
    ).all()

    requests = int(totals[0] or 0)
    if not requests:
        return ToolResult(
            data={"requests": 0},
            evidence=[
                {
                    "source_type": "execution_telemetry",
                    "source_id": None,
                    "company_id": company_id,
                    "title": "No execution telemetry recorded",
                    "content": (
                        "No requests have been recorded for this company"
                        + (f" against the '{service}' service" if service else "")
                        + ", so there is nothing to report about runtime behaviour."
                    ),
                    "metadata": {"service": service},
                }
            ],
        )

    data = {
        "service": service,
        "requests": requests,
        "errors": int(errors or 0),
        "latency_ms": {
            "avg": round(float(totals[1]), 1) if totals[1] is not None else None,
            "max": int(totals[2]) if totals[2] else None,
        },
        "connector": {
            "queries": int(totals[3] or 0),
            "query_ms": int(totals[4] or 0),
            "rows_returned": int(totals[5] or 0),
        },
        "llm": {
            "calls": int(totals[6] or 0),
            "prompt_tokens": int(totals[7] or 0),
            "completion_tokens": int(totals[8] or 0),
            "estimated_cost_usd": round(float(totals[9]), 4) if totals[9] else 0.0,
        },
        "by_service": [
            {
                "service": row[0],
                "requests": int(row[1] or 0),
                "avg_ms": round(float(row[2]), 1) if row[2] is not None else None,
                "connector_queries": int(row[3] or 0),
            }
            for row in per_service
        ],
    }

    llm = data["llm"]
    breakdown = ", ".join(
        f"{row['service']} {row['requests']}" for row in data["by_service"][:8]
    )
    return ToolResult(
        data=data,
        evidence=[
            {
                "source_type": "execution_telemetry",
                "source_id": None,
                "company_id": company_id,
                "title": f"Execution telemetry for {context.company_name}",
                "content": (
                    f"{requests} recorded request(s)"
                    + (f" for the '{service}' service" if service else "")
                    + f", {data['errors']} ended in error. Average latency "
                    f"{data['latency_ms']['avg']} ms, maximum "
                    f"{data['latency_ms']['max']} ms. Connector work: "
                    f"{data['connector']['queries']} query/queries taking "
                    f"{data['connector']['query_ms']} ms and returning "
                    f"{data['connector']['rows_returned']} row(s). "
                    f"Model calls: {llm['calls']} "
                    f"({llm['prompt_tokens']} prompt tokens, "
                    f"{llm['completion_tokens']} completion tokens, estimated cost "
                    f"${llm['estimated_cost_usd']}). "
                    f"Requests by service: {breakdown}."
                ),
                "metadata": {"requests": requests, "llm_calls": llm["calls"]},
            }
        ],
    )


def get_platform_capabilities(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    """What this platform version can and cannot do.

    Deliberately available to any authenticated member: it discloses nothing
    about the company, only about the software.
    """
    config = get_llm_config()
    described = config.describe()
    data = {
        "delivered": list(DELIVERED),
        "not_available": list(NOT_AVAILABLE),
        "kpi_values": (
            "Computed on demand from the governed formula contract, pushed down to the "
            "tenant's database as a read-only SQL aggregate. A figure shown on a screen "
            "is a stored detection result for that KPI and date, or nothing."
        ),
        "copilot": {
            "enabled": described["enabled"],
            "available": described["available"],
            "model": described["model"],
        },
    }
    return ToolResult(
        data=data,
        evidence=[
            {
                "source_type": "platform_capability",
                "source_id": None,
                "company_id": context.company_id,
                "title": "What this platform version does and does not do",
                "content": (
                    "Delivered: " + "; ".join(DELIVERED) + ".\n"
                    "Not built in this version: " + "; ".join(NOT_AVAILABLE) + ". "
                    "There is no engine behind any of those, so a question that needs one "
                    "has no answer here -- say it is not available rather than estimating. "
                    "KPI values are computed on demand by pushing the governed formula to "
                    "the tenant's own database as a read-only SQL aggregate; a figure on a "
                    "screen is a stored detection result for that KPI and date, or nothing."
                ),
                "metadata": {"copilot_enabled": described["enabled"]},
            }
        ],
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_execution_summary",
        description=(
            "Recorded runtime telemetry for this company: request counts, errors, "
            "latency, connector queries and rows, and model call counts, tokens and cost. "
            "Use it for questions about what the platform has been doing or whether any "
            "model calls have been made."
        ),
        permissions=TELEMETRY_READ,
        parameters={
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": (
                        "Optional: restrict to one service, e.g. 'kpis', 'analysis', "
                        "'sources', 'copilot'."
                    ),
                }
            },
            "required": [],
        },
        handler=get_execution_summary,
    ),
    ToolSpec(
        name="get_platform_capabilities",
        description=(
            "What this platform version can and cannot do, and how KPI values are "
            "produced. Call it before answering any question about anomalies, expected "
            "values, baselines, forecasts, monitoring or recommendations, so the answer "
            "states the real capability boundary instead of implying one."
        ),
        permissions=(),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=get_platform_capabilities,
    ),
)
