"""The Copilot API.

One conversational endpoint and one status endpoint. Both sit behind the same
authorisation chain as every other route in this platform:
``require_permissions`` resolves the JWT to a user, the user plus the URL's
company to a membership, the membership to a role, and the role to permissions.
By the time a handler body runs, ``access.company`` is a company row the caller
demonstrably belongs to.

The ``company_id`` in the path is therefore never authorisation -- it is a claim
that has already been checked, and the handlers read ``access.company.id`` rather
than the path parameter so the two cannot drift.

``analytics.read`` is the gate. It is the permission that already governs seeing
dashboards and KPI values, and the Copilot explains exactly that material. Every
tool the model can then reach applies its *own* permission check on top, so an
ANALYST and an EXECUTIVE asking the same question get answers built from
different evidence.

The request body cannot carry a company, SQL, a filter, a tool choice or a
system-prompt override -- see ``app.copilot.schemas``. What it carries is what the
user is looking at, and every part of that is re-resolved against the database
before it reaches the model.

**One endpoint serves every panel.** Stage performance, detection detail, a
historical run, an investigation and the future-action view all post to this same
route with the same context shape; what differs is which coordinates they fill in.
That is deliberate. A Copilot per screen would mean five sets of rules about what
may be asserted from a figure, and they would diverge -- the fifth one written in a
hurry would be the one that invents a number. There is one set of rules, in
``app.copilot.prompts``, and one context resolver, in ``app.copilot.context``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.copilot.context import build_context
from app.copilot.orchestrator import answer_question
from app.copilot.schemas import (
    CopilotChatRequest,
    CopilotChatResponse,
    CopilotStatusOut,
)
from app.copilot.tools import PLANNED_TOOLS, REGISTRY
from app.core.deps import AccessContext, SessionDep, require_permissions
from app.core.telemetry import llm_usage_of
from app.llm.config import get_llm_config
from app.services import audit

router = APIRouter(tags=["copilot"])

# Written to the audit trail. Named as an interaction, not a governance action,
# because that is what it is: the Copilot cannot change anything.
COPILOT_QUESTION_ASKED = "copilot.question_asked"

# Which body of governed knowledge each permission unlocks for retrieval. Used to
# describe reach without assembling the corpus -- status is called on every page
# load, and chunking every document to answer it would be absurd.
_KNOWLEDGE_SOURCES: tuple[tuple[str, str], ...] = (
    ("kpi.read", "KPI contracts, versions, lineage, dimensions, drivers and validation state"),
    ("document.read", "company documents you are entitled to read, by version"),
    ("analytics.read", "table and column profiles, grain, freshness, relationships, join safety"),
    ("catalog.read", "published semantic catalog versions"),
    ("source.read", "data source registry and connector limitations"),
    ("telemetry.read", "recorded runtime and model-usage telemetry"),
)


@router.get(
    "/companies/{company_id}/copilot/status",
    response_model=CopilotStatusOut,
    summary="Whether the Copilot can answer, and what it can reach",
)
def copilot_status(
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> CopilotStatusOut:
    """Let the UI render an honest state on load rather than after a failure.

    ``tools_available`` and ``knowledge_sources`` are both filtered by the
    caller's own permissions, so this answers "what can *I* ask about", not
    "what exists somewhere".
    """
    config = get_llm_config()
    described = config.describe()
    return CopilotStatusOut(
        enabled=bool(described["enabled"]),
        available=bool(described["available"]),
        provider=str(described["provider"]),
        model=described["model"],  # type: ignore[arg-type]
        endpoint_host=described["endpoint_host"],  # type: ignore[arg-type]
        unavailable_reason=described["unavailable_reason"],  # type: ignore[arg-type]
        tools_available=(
            [spec.name for spec in REGISTRY.available_for(access)]
            if config.tool_calling_enabled
            else []
        ),
        knowledge_sources=[
            label for permission, label in _KNOWLEDGE_SOURCES if access.has(permission)
        ],
        planned_capabilities=[tool["name"] for tool in PLANNED_TOOLS],
    )


@router.post(
    "/companies/{company_id}/copilot/chat",
    response_model=CopilotChatResponse,
    summary="Ask a question about this company's governed knowledge",
)
async def copilot_chat(
    request: Request,
    payload: CopilotChatRequest,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> CopilotChatResponse:
    """Answer one question from this company's governed evidence.

    Not a 4xx when no model is configured: a deployment running with
    ``LLM_ENABLED=false`` is working as designed, and the response says so in
    ``unavailable_reason`` while still returning the evidence retrieval found.
    Turning a supported configuration into an error would push the frontend into
    treating a normal state as a fault.
    """
    hints = payload.context
    context = build_context(
        session,
        access,
        request_id=getattr(request.state, "request_id", None),
        panel=hints.panel,
        kpi_id=hints.kpi_id,
        kpi_version=hints.kpi_version,
        selected_date=hints.selected_date,
        dimension=hints.dimension,
        selected_entity=hints.selected_entity,
        agent_run_id=hints.agent_run_id,
    )

    # Model accounting lands on this request's ``execution_logs`` row via the
    # telemetry middleware. Token counts and the model name only -- the
    # accumulator has no field a prompt or credential could occupy.
    result = await answer_question(
        context, payload.message, usage_sink=llm_usage_of(request)
    )

    # The question itself is not stored: it is user-authored free text that may
    # quote business figures, and the audit trail is not the place for it. What
    # is recorded is that a question was asked, in what context, and which
    # governed tools ran -- which is what an auditor needs.
    audit.record(
        session,
        access=access,
        action=COPILOT_QUESTION_ASKED,
        resource_type="copilot",
        resource_id=context.kpi_definition.id if context.kpi_definition else None,
        resource_label=context.kpi_definition.name if context.kpi_definition else None,
        summary=(
            f"Copilot answered from {len(result.evidence)} evidence item(s)"
            if result.llm_available
            else "Copilot question received while no language model is configured"
        ),
        request=request,
        details={
            "page": hints.page,
            # The resolved values, not the hints: an auditor needs to know which
            # panel, dimension and entity the answer was actually built for, and a
            # rejected hint shows up as a note rather than as a claim.
            "panel": context.panel,
            "kpi_version": context.kpi_version.version if context.kpi_version else None,
            "selected_date": (
                context.selected_date.isoformat() if context.selected_date else None
            ),
            "dimension": context.dimension_name,
            "selected_entity": context.selected_entity,
            "agent_run_id": context.agent_run_id,
            "message_chars": len(payload.message),
            "llm_available": result.llm_available,
            "model": result.model,
            "iterations": result.iterations,
            "tools_called": [call.name for call in result.tool_calls],
            "evidence_count": len(result.evidence),
            "truncated": result.truncated,
        },
    )
    session.commit()

    return CopilotChatResponse.model_validate(result.as_dict())
