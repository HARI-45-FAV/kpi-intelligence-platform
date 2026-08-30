"""Request and response shapes for the Copilot API.

Kept in the Copilot package rather than added to ``app.schemas`` because the
request model carries a security property specific to this endpoint: the client
sends *what it is looking at*, and every field of that is a hint to be re-resolved
server-side, never an authorisation or a fact. Keeping the model next to
``build_context`` -- the code that does the re-resolving -- makes the pairing hard
to miss.

Note what the request cannot express. There is no company field (the URL and the
membership decide that), no SQL, no filter, no tool selection, no system-prompt
override, and no way to supply a KPI value or definition for the model to trust.
The frontend can say "the user is looking at KPI X, version 3, on this date, at
the North row of the region breakdown"; it cannot say what X means, what it
measured, or whether that measurement was abnormal.

That last omission is the deliberate one. Every panel that opens this Copilot has
an actual, an expected value and a deviation on screen, and it would be natural to
post them along with the question. They are not accepted. A request that could
state the actual could state a false actual and have the model explain it as fact,
so the request carries coordinates and the figures are re-read from the run the
platform stored. One context shape serves every panel precisely because it
contains no panel's numbers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CopilotSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class CopilotRequestContext(CopilotSchema):
    """What the user is looking at. Every field is a hint, re-resolved server-side.

    A ``kpi_id`` from another company resolves to nothing, exactly as a deleted id
    would, because ``build_context`` puts it through ``load_scoped``. A
    ``dimension`` that is not approved for the KPI, or an ``entity`` outside the
    caller's row scope, is dropped with a note for the same reason.
    """

    panel: str | None = Field(
        default=None,
        max_length=40,
        description=(
            "Which panel the question was asked from -- stage_performance, "
            "detection_detail, historical_run, investigation, future_action. Decides "
            "which verified result the answer is anchored to; an unrecognised value "
            "is ignored."
        ),
    )
    kpi_id: str | None = Field(
        default=None,
        max_length=80,
        description="KPI definition id or business key currently on screen.",
    )
    kpi_version: int | None = Field(
        default=None, ge=1, le=10_000, description="KPI version currently on screen."
    )
    selected_date: str | None = Field(
        default=None, max_length=32, description="Date selected on the dashboard (ISO)."
    )
    dimension: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Name of the breakdown being viewed. Checked against the KPI version's "
            "approved dimensions; anything else is ignored."
        ),
    )
    selected_entity: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "The value selected within that breakdown, e.g. a region or product. "
            "Checked against the caller's row scope. Carried as what the user "
            "selected, never as a fact about the data."
        ),
    )
    agent_run_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "The agent run whose stored results are on screen. Resolved inside this "
            "company so a historical answer comes from the run that produced it."
        ),
    )
    page: str | None = Field(
        default=None,
        max_length=80,
        description="Router path the question came from, for the audit trail only.",
    )


class CopilotChatRequest(CopilotSchema):
    message: str = Field(min_length=1, max_length=4_000)
    context: CopilotRequestContext = Field(default_factory=CopilotRequestContext)


class EvidenceOut(CopilotSchema):
    """One citable item the answer was built from."""

    evidence_id: str
    source_type: str
    source_id: str | None = None
    title: str
    content: str
    is_placeholder: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallOut(CopilotSchema):
    """A governed tool the assistant used, for the audit trail."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    error: str | None = None
    caveats: list[str] = Field(default_factory=list)


class CopilotChatResponse(CopilotSchema):
    answer: str
    evidence: list[EvidenceOut] = Field(default_factory=list)
    # The context the platform resolved, echoed back so the UI can show what the
    # answer was actually about rather than what it thought it asked about.
    context: dict[str, Any] = Field(default_factory=dict)
    llm_available: bool
    model: str | None = None
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    iterations: int = 0
    usage: dict[str, int] = Field(default_factory=dict)
    unavailable_reason: str | None = None
    truncated: bool = False


class CopilotStatusOut(CopilotSchema):
    """Whether the Copilot can answer, and what it is able to reach.

    Lets the UI render an honest disabled state on load instead of discovering it
    from a failed question. Contains no credential and no endpoint URL -- only the
    host, from ``LLMConfig.describe``.
    """

    enabled: bool
    available: bool
    provider: str
    model: str | None = None
    endpoint_host: str | None = None
    unavailable_reason: str | None = None
    tools_available: list[str] = Field(default_factory=list)
    knowledge_sources: list[str] = Field(default_factory=list)
    planned_capabilities: list[str] = Field(default_factory=list)
