"""The governed tool layer: the model's entire vocabulary.

``REGISTRY`` is built once at import and holds every action the Copilot can
perform. Nothing outside this package can add to it at runtime, and the registry
itself rejects any tool that would let the model choose a tenant or supply SQL
(see ``base.FORBIDDEN_PARAMETERS``).

What is deliberately absent is as important as what is present. There is no
``execute_sql``, no ``run_query``, no ``database_connection``, no
``get_connector_credentials``, and no tool that returns tenant business rows.
Column profiles are the closest thing to data, and their stored sample values are
stripped before they leave the tool. The model cannot read a row of the tenant's
orders table through this layer because no function exists that would do it.

``PLANNED_TOOLS`` names deterministic services this platform does not have. They
are listed, not stubbed: a stub returning a plausible forecast or a plausible cause
would be the exact failure this platform is built to avoid. The list exists so the
Copilot can say *"this version does not do that"* precisely instead of inventing an
answer, and so the extension point is documented where the tools live. A name
leaves this list only when the real computation lands, on the same commit -- the
system prompt is generated from it, so a stale entry would have the Copilot deny a
capability while its result sits in the evidence. ``get_recommended_action`` left
on the commit that landed ``services.recommendation`` for exactly that reason.
"""

from __future__ import annotations

from app.copilot.tools import (
    contribution_tools,
    data_tools,
    document_tools,
    kpi_tools,
    observability_tools,
)
from app.copilot.tools.base import (
    FORBIDDEN_PARAMETERS,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    refuse,
    validate_arguments,
)

REGISTRY = ToolRegistry()
for _module in (kpi_tools, document_tools, data_tools, observability_tools, contribution_tools):
    for _spec in _module.TOOLS:
        REGISTRY.register(_spec)


# What this platform genuinely does not compute. Detection is not here: a KPI's
# actual, expected value, deviation and NORMAL/ABNORMAL/LOW_CONFIDENCE status are
# computed, stored and reach the Copilot as evidence, so listing them as absent
# would make the Copilot deny a figure it is holding. Contribution is not here
# either, for the same reason and one stronger: two registered tools read stored
# breakdowns. Nor is recommendation, since ``services.recommendation`` derives a
# governed next action from a stored result and the result screen renders it --
# what the Copilot lacks there is a *tool*, not the capability, and the panel note
# for action questions says so rather than denying the feature. What remains here
# is everything past measurement that no service computes -- prediction, cause,
# escalation rules, threshold learning -- and each entry stays until a
# deterministic service with its own stored results replaces it.
PLANNED_TOOLS: tuple[dict[str, str], ...] = (
    {
        "name": "get_forecast",
        "needs": "a forecasting engine",
        "description": "Where a KPI is projected to go, rather than where it has been.",
    },
    {
        "name": "get_causal_attribution",
        "needs": "causal inference",
        "description": (
            "What caused a movement. Contribution analysis measures which part of the "
            "business accounts for a movement, which is a share and not a cause."
        ),
    },
    {
        "name": "get_alert_history",
        "needs": "alerting and escalation rules",
        "description": (
            "Which results were escalated to whom, and when. A completed run mails its "
            "own stored summary, but no threshold rule, routing table or escalation "
            "history exists to read back."
        ),
    },
    {
        "name": "get_anomaly_feedback_state",
        "needs": "feedback learning",
        "description": (
            "What reviewers accepted or rejected, and how thresholds moved in response. "
            "Recommendation feedback is recorded, and deliberately moves nothing: no "
            "detection path reads it, so there is no learned state to report."
        ),
    },
)

PLANNED_TOOL_NAMES: frozenset[str] = frozenset(tool["name"] for tool in PLANNED_TOOLS)

__all__ = [
    "FORBIDDEN_PARAMETERS",
    "PLANNED_TOOLS",
    "PLANNED_TOOL_NAMES",
    "REGISTRY",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "refuse",
    "validate_arguments",
]
