"""The Copilot turn: retrieve, offer tools, ask the model, return with evidence.

The flow, and why it is in this order:

1. **Retrieve first.** Governed knowledge relevant to the question is assembled
   from the caller's own company before the model is contacted, so the model
   starts from platform facts rather than from its training data.
2. **Offer only what the caller may use.** ``REGISTRY.available_for`` filters
   tools by the caller's permissions, so a VIEWER's turn never learns that a
   document tool exists.
3. **Loop, bounded.** Tool calls are executed and fed back for at most
   ``llm_max_tool_iterations`` rounds. Every result goes through the registry,
   which re-checks permissions and validates arguments; a refusal is returned to
   the model as data so it can adjust rather than ending the turn.
4. **Answer with its evidence.** The response carries the evidence list the
   answer was built from, so a reader can check any claim against a KPI version,
   a document passage or a profiling result.

Two states never reach the model at all, and both are deterministic:

* No model configured -- the answer says so and still returns the retrieved
  evidence, because retrieval is the platform's own work and does not need one.
* No evidence found -- asking anyway would invite an answer from training data,
  which is the exact failure this platform exists to prevent.

The orchestrator holds no company logic of its own. Scope arrives resolved in
``CopilotContext``; the model is never asked which company it is in, and could
not act on the answer if it were.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from sqlalchemy import select

from app.copilot.context import CopilotContext
from app.copilot.evidence import (
    EvidenceBundle,
    contribution_run_evidence,
    detection_run_evidence,
    no_contribution_notice,
    no_detection_run_notice,
)
from app.copilot.prompts import (
    no_evidence_answer,
    system_prompt,
    unavailable_answer,
    user_prompt,
)
from app.copilot.retrieval import retrieve
from app.copilot.tools import REGISTRY
from app.core.telemetry import LlmUsage
from app.llm.config import LLMConfig, get_llm_config
from app.llm.provider import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    LLMUnavailable,
    build_provider,
)
from app.models.detection import ContributionRun, DetectionRun

# Words that mean the user is asking about a figure on screen. Matching any of
# them makes the turn go and look for the detection result behind that figure, so
# an answer touching a displayed number is built on the measurement rather than on
# the model's impression of one.
_FIGURE_TRIGGERS = (
    "value",
    "number",
    "figure",
    "tile",
    "dashboard",
    "today",
    "yesterday",
    "trend",
    "increase",
    "decrease",
    "drop",
    "spike",
    "up",
    "down",
    "why",
    "anomaly",
    "anomalous",
    "expected",
    "baseline",
    "forecast",
    "deviation",
    "variance",
    "change",
    "higher",
    "lower",
)

#: Panels whose whole purpose is a figure. A question asked from one of these is
#: about a measurement even when it is phrased as "explain this" -- so the stored
#: result is fetched for the turn without waiting for a trigger word. The panels
#: about definitions and setup are deliberately absent: fetching a detection run
#: for "what does this KPI mean" would attach a placeholder disclosure to an answer
#: that never needed a number.
_FIGURE_PANELS = frozenset(
    {"stage_performance", "detection_detail", "historical_run", "investigation", "future_action"}
)

# The system prompt asks for plain text, but this final presentation guard keeps
# a non-compliant model response from leaking Markdown chrome into the product.
# Evidence citations (for example ``[E1]``) are deliberately not touched.
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MARKDOWN_QUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_MARKDOWN_LIST = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKDOWN_EMPHASIS = re.compile(r"(?<!\w)(?:\*\*|__|\*|_|`)(.*?)(?:\*\*|__|\*|_|`)(?!\w)")
#: Kept in step with the word limit the system prompt states. It also has to leave
#: room for the three-part shape a figure question is answered in -- if the guard
#: cut before the prompt's own limit, the part it would cut is "Confidence", which
#: is the part that qualifies the answer and the last one worth losing.
_MAX_ANSWER_WORDS = 150

# A locally hosted model is often run with a small context window (4k is common).
# Supplying every possible tool schema is unnecessary for a focused question and
# can crowd out the answer. These are relevance groups, not permission grants: the
# registry remains the sole authorisation gate and every tool is still reachable
# when its subject is asked about.
_TOOL_KEYWORDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("join", "joined", "relationship", "aggregate", "aggregation", "fan out"), ("get_join_safety_summary", "get_relationship_summary")),
    (("table", "profile", "freshness", "grain", "column"), ("get_table_profile", "get_column_profile")),
    (("validation", "validate", "approval", "ready", "blocked", "failed", "passed"), ("get_kpi_validation_summary", "get_kpi_version")),
    (("lineage", "formula", "calculated", "calculation"), ("get_kpi_definition", "get_kpi_lineage")),
    (("dimension", "slice", "segment"), ("get_kpi_dimensions",)),
    (("driver", "factor", "cause"), ("get_kpi_drivers",)),
    (("document", "policy", "handbook", "refund"), ("get_document_context",)),
    (("telemetry", "latency", "error", "request", "usage"), ("get_execution_summary",)),
    (("anomaly", "forecast", "baseline", "expected"), ("get_platform_capabilities",)),
)


def _plain_short_answer(text: str) -> str:
    """Present a compact, readable answer without changing its facts or citations."""
    cleaned = _MARKDOWN_LINK.sub(r"\1", text.strip())
    cleaned = _MARKDOWN_HEADING.sub("", cleaned)
    cleaned = _MARKDOWN_QUOTE.sub("", cleaned)
    cleaned = _MARKDOWN_LIST.sub("", cleaned)
    cleaned = _MARKDOWN_EMPHASIS.sub(r"\1", cleaned)
    # Preserve paragraph breaks but remove empty runs and model-added padding.
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())
    if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] == cleaned[0]:
        cleaned = cleaned[1:-1].strip()
    return _capped(cleaned)


def _capped(text: str) -> str:
    """Trim an over-long answer without flattening it into one paragraph.

    Whole lines are kept whole for as long as the budget allows, because a figure
    answer is three labelled paragraphs and running them together would misattribute
    the context to the measurement. Only the line that crosses the limit is cut.
    """
    kept: list[str] = []
    budget = _MAX_ANSWER_WORDS
    for line in text.splitlines():
        words = line.split()
        if len(words) <= budget:
            kept.append(line)
            budget -= len(words)
            continue
        if budget > 0:
            kept.append(" ".join(words[:budget]).rstrip(".,;:") + "…")
        elif kept:
            kept[-1] = kept[-1].rstrip(".,;:") + "…"
        break
    return "\n".join(kept)


def _relevant_tools(context: CopilotContext, question: str):
    """Offer a small, subject-matched tool set that fits local-model context."""
    available = {spec.name: spec for spec in REGISTRY.llm_specs(context)}
    lowered = question.lower()
    wanted: list[str] = []
    for keywords, names in _TOOL_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            wanted.extend(names)
    if not wanted and context.kpi_definition is not None:
        wanted.append("get_kpi_definition")
    selected = []
    for name in wanted:
        spec = available.get(name)
        if spec is not None and spec not in selected:
            selected.append(spec)
        if len(selected) == 2:
            break
    return selected


@dataclass(slots=True)
class ToolInvocation:
    """One executed tool call, for the response's audit trail."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    error: str | None = None
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "error": self.error,
            "caveats": list(self.caveats),
        }


@dataclass(slots=True)
class CopilotAnswer:
    """The complete result of one turn."""

    answer: str
    evidence: list[dict[str, Any]]
    context: dict[str, Any]
    llm_available: bool
    model: str | None = None
    tool_calls: list[ToolInvocation] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    iterations: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    # Set when the turn could not use a model. Reported as a normal field so the
    # frontend can render an honest unavailable state instead of an error.
    unavailable_reason: str | None = None
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "evidence": self.evidence,
            "context": self.context,
            "llm_available": self.llm_available,
            "model": self.model,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "caveats": list(self.caveats),
            "iterations": self.iterations,
            "usage": dict(self.usage),
            "unavailable_reason": self.unavailable_reason,
            "truncated": self.truncated,
        }


def _mentions_a_figure(question: str) -> bool:
    words = {word.strip(".,;:!?()[]\"'").lower() for word in question.split()}
    return any(trigger in words for trigger in _FIGURE_TRIGGERS)


def _stored_detection_run(context: CopilotContext) -> DetectionRun | None:
    """The detection result behind the figure on screen, if one was stored.

    Scoped by the resolved context and nothing else: this company, this KPI
    version's definition, this date. The most recent run wins, because re-running
    a date supersedes the earlier answer for it.
    """

    if context.kpi_definition is None or context.selected_date is None:
        return None
    return context.session.scalars(
        select(DetectionRun)
        .where(
            DetectionRun.company_id == context.company_id,
            DetectionRun.kpi_definition_id == context.kpi_definition.id,
            DetectionRun.target_date == context.selected_date,
        )
        .order_by(DetectionRun.executed_at.desc())
        .limit(1)
    ).first()


def _stored_contribution_run(context: CopilotContext) -> ContributionRun | None:
    """The breakdown behind the investigation screen, if one was stored.

    Scoped exactly as the detection lookup is -- this company, this KPI, this date
    -- and narrowed to the dimension in view when there is one, because a reader
    looking at a breakdown by one dimension should not be answered about another.
    The most recent matching run wins, since re-running a breakdown supersedes the
    earlier answer for the same view.
    """

    if context.kpi_definition is None or context.selected_date is None:
        return None
    stmt = (
        select(ContributionRun)
        .where(
            ContributionRun.company_id == context.company_id,
            ContributionRun.kpi_definition_id == context.kpi_definition.id,
            ContributionRun.target_date == context.selected_date,
        )
        .order_by(ContributionRun.executed_at.desc())
        .limit(1)
    )
    if context.dimension is not None:
        stmt = stmt.where(ContributionRun.dimension == context.dimension.dimension_name)
    return context.session.scalars(stmt).first()


def _gather_evidence(context: CopilotContext, question: str) -> tuple[EvidenceBundle, list[str]]:
    """Retrieve this company's governed knowledge for one question.

    Returns the bundle and any caveats the user should see regardless of what the
    model says -- retrieval facts, not model output.
    """
    bundle = EvidenceBundle(company_id=context.company_id)
    caveats: list[str] = []

    for scored in retrieve(context, question):
        bundle.add(**scored.passage.as_evidence())

    # A question about a displayed number gets the measurement behind it whether
    # or not the model asks for it -- and, when there is none, gets told so. This
    # is the one piece of evidence the platform inserts on its own initiative,
    # because the failure it prevents -- narrating a figure nobody computed as a
    # business result -- is the most damaging one available.
    #
    # A panel that shows figures counts as asking about them. The panels this
    # Copilot opens from are showing an actual, an expected value and a deviation
    # already, so the verified result is fetched for those turns regardless of how
    # the question was worded: the request never carried those numbers, and this is
    # where the real ones come from.
    if (
        _mentions_a_figure(question)
        or context.selected_date is not None
        or context.panel in _FIGURE_PANELS
    ):
        run = _stored_detection_run(context)
        if run is not None:
            bundle.add(**detection_run_evidence(run))
        else:
            bundle.add(
                **no_detection_run_notice(
                    context.company_id,
                    kpi_name=context.kpi_definition.name if context.kpi_definition else None,
                    selected_date=(
                        context.selected_date.isoformat() if context.selected_date else None
                    ),
                )
            )

    # The investigation panel is showing a breakdown, so the same reasoning applies
    # to the shares as to the figures above: the request never carried them, and a
    # model that can see a total but no parts will estimate parts. The stored
    # analysis is fetched for that panel whatever the question was, and when none
    # exists the turn is told so explicitly -- "nobody has run this yet" is the
    # normal state for an on-demand analysis, and it has to be sayable.
    if context.panel == "investigation" and context.access.has("investigation.read"):
        breakdown = _stored_contribution_run(context)
        if breakdown is not None:
            bundle.add(**contribution_run_evidence(breakdown))
        else:
            bundle.add(
                **no_contribution_notice(
                    context.company_id,
                    kpi_name=context.kpi_definition.name if context.kpi_definition else None,
                    selected_date=(
                        context.selected_date.isoformat() if context.selected_date else None
                    ),
                    dimension=context.dimension_name,
                )
            )

    if context.notes:
        caveats.extend(context.notes)
    return bundle, caveats


def _execute_tool_calls(
    context: CopilotContext,
    bundle: EvidenceBundle,
    calls: tuple[LLMToolCall, ...],
) -> tuple[list[LLMMessage], list[ToolInvocation], list[str]]:
    """Run the tools the model asked for, through the governed registry.

    Nothing here decides whether a call is allowed; ``REGISTRY.invoke`` does,
    against the caller's own permissions. A malformed or refused call comes back
    as a tool message the model can read, which keeps a bad call from ending the
    conversation.
    """
    messages: list[LLMMessage] = []
    invocations: list[ToolInvocation] = []
    notes: list[str] = []

    for call in calls:
        if call.argument_error:
            result_payload = {
                "ok": False,
                "error": (
                    f"Your arguments for {call.name} were not valid JSON "
                    f"({call.argument_error}). Call it again with well-formed arguments."
                ),
            }
            invocations.append(
                ToolInvocation(
                    name=call.name, arguments={}, ok=False, error=call.argument_error
                )
            )
            messages.append(
                LLMMessage.tool_result(
                    call_id=call.call_id, name=call.name, payload=result_payload
                )
            )
            continue

        result = REGISTRY.invoke(context, call.name, call.arguments)
        for payload in result.evidence:
            bundle.add(**payload)
        invocations.append(
            ToolInvocation(
                name=call.name,
                arguments=dict(call.arguments),
                ok=result.ok,
                error=result.error,
                caveats=list(result.caveats),
            )
        )
        notes.extend(result.caveats)
        messages.append(
            LLMMessage.tool_result(
                call_id=call.call_id, name=call.name, payload=result.as_dict()
            )
        )

    return messages, invocations, notes


def _record(usage_sink: LlmUsage | None, config: LLMConfig, response: LLMResponse) -> None:
    if usage_sink is None:
        return
    usage_sink.record(
        model=response.model or config.model,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        cost_usd=config.estimate_cost_usd(
            response.usage.prompt_tokens, response.usage.completion_tokens
        ),
    )


async def answer_question(
    context: CopilotContext,
    question: str,
    *,
    provider: LLMProvider | None = None,
    config: LLMConfig | None = None,
    usage_sink: LlmUsage | None = None,
) -> CopilotAnswer:
    """Answer one question inside one company, from governed evidence only.

    ``provider`` and ``config`` are injectable so tests can drive the loop with a
    scripted model. Production passes neither and gets whatever the environment
    configures -- including ``NullProvider`` when that is nothing.
    """
    cfg = config or get_llm_config()
    described = context.describe()
    question = (question or "").strip()

    bundle, caveats = _gather_evidence(context, question)

    # --- No model: still answer with what the platform retrieved --------
    if not cfg.is_available:
        reason = cfg.unavailable_reason or "No language model is configured."
        return CopilotAnswer(
            answer=unavailable_answer(reason),
            evidence=bundle.as_list(),
            context=described,
            llm_available=False,
            caveats=caveats,
            unavailable_reason=reason,
        )

    # --- Nothing retrieved: do not ask a model to fill the gap ----------
    if bundle.is_empty:
        return CopilotAnswer(
            answer=no_evidence_answer(question),
            evidence=[],
            context=described,
            llm_available=True,
            model=cfg.model,
            caveats=caveats,
        )

    active_provider = provider or build_provider(cfg)
    owns_provider = provider is None
    tools = _relevant_tools(context, question) if cfg.tool_calling_enabled else []

    messages: list[LLMMessage] = [
        LLMMessage.system(system_prompt(context)),
        LLMMessage.user(
            user_prompt(question, bundle.as_prompt(max_items=4, max_chars_per_item=650))
        ),
    ]

    invocations: list[ToolInvocation] = []
    tool_notes: list[str] = []
    iterations = 0
    truncated = False
    text = ""

    try:
        # One iteration is one model turn. The bound matters: a model that keeps
        # asking for tools would otherwise loop until a timeout, and an answer
        # from a truncated loop must be labelled rather than presented as final.
        for iteration in range(1, cfg.max_tool_iterations + 1):
            iterations = iteration
            response = await active_provider.generate(messages, tools=tools)
            _record(usage_sink, cfg, response)

            if not response.wants_tools:
                text = response.text
                break

            messages.append(LLMMessage.assistant(response.text or None, response.tool_calls))
            results, executed, notes = _execute_tool_calls(context, bundle, response.tool_calls)
            messages.extend(results)
            invocations.extend(executed)
            for note in notes:
                if note not in tool_notes:
                    tool_notes.append(note)
        else:
            # The loop ran out without a plain answer. Ask once more with tools
            # withdrawn, so the model must respond from what it already gathered.
            truncated = True
            messages.append(
                LLMMessage.user(
                    "Answer now from the evidence you already have. Do not request "
                    "further tools. If it is not enough, say which part of the "
                    "question you cannot answer."
                )
            )
            final = await active_provider.generate(messages, tools=None)
            _record(usage_sink, cfg, final)
            iterations += 1
            text = final.text
    except LLMUnavailable as exc:
        return CopilotAnswer(
            answer=unavailable_answer(str(exc)),
            evidence=bundle.as_list(),
            context=described,
            llm_available=False,
            caveats=caveats,
            unavailable_reason=str(exc),
            tool_calls=invocations,
            iterations=iterations,
        )
    except LLMProviderError as exc:
        # The endpoint failed. Retrieved evidence is still the platform's own
        # work and is still worth returning, so the user gets the governed
        # material even though the prose could not be written.
        return CopilotAnswer(
            answer=(
                "The language model could not be reached, so I cannot write an answer "
                "for this question. The governed evidence retrieved for it is included "
                "below, and every other part of the platform is unaffected."
            ),
            evidence=bundle.as_list(),
            context=described,
            llm_available=True,
            model=cfg.model,
            caveats=[*caveats, f"Model request failed: {exc}"],
            tool_calls=invocations,
            iterations=iterations,
        )
    finally:
        if owns_provider:
            await active_provider.aclose()

    # A reasoning model can consume its generation budget without producing
    # visible content.  Never send a successful-but-blank Copilot response to
    # the UI: it is indistinguishable from a hung request to the person asking.
    if not text.strip():
        caveats.append(
            "The model completed without a visible answer. Its reasoning budget may have "
            "been exhausted; try again after reducing the question scope."
        )
        text = (
            "I could not produce a written answer from the model for this question. "
            "The governed evidence retrieved below is available to review."
        )

    text = _plain_short_answer(text)

    if bundle.has_placeholder:
        caveats.append(
            "No detection run is stored for the date in view, so no actual, expected or "
            "deviation figure was available to this answer. Running the agent for that "
            "date produces them."
        )
    caveats.extend(note for note in tool_notes if note not in caveats)
    if truncated:
        caveats.append(
            "The assistant reached its tool-call limit for this question, so the answer "
            "may be based on incomplete evidence."
        )

    return CopilotAnswer(
        # An empty completion is possible -- a truncated generation, a model that
        # returned only reasoning. Saying so is better than returning a blank.
        answer=text.strip()
        or (
            "The model returned an empty answer for this question. The evidence "
            "retrieved for it is included below."
        ),
        evidence=bundle.as_list(),
        context=described,
        llm_available=True,
        model=cfg.model,
        tool_calls=invocations,
        caveats=caveats,
        iterations=iterations,
        usage=(
            {
                "prompt_tokens": usage_sink.prompt_tokens,
                "completion_tokens": usage_sink.completion_tokens,
                "calls": usage_sink.calls,
            }
            if usage_sink is not None
            else {}
        ),
        truncated=truncated,
    )
