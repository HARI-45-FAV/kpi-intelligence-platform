"""Turning a stored result into an explanation, with or without a model.

This is the bridge between :mod:`app.services.explanation` — which assembles six
labelled sections from stored evidence, deterministically — and the Copilot's
governed language model. The order is the point:

1. resolve the caller's context against the database (never against the request);
2. retrieve the approved documents this caller is entitled to read;
3. assemble the explanation from stored measurements;
4. *only then*, if a model is configured, ask it to re-narrate those same
   sections from those same facts.

Step 4 is optional and always last, so the feature works with ``LLM_ENABLED``
false and the model can never be the source of a figure. If narration fails,
returns nothing, or comes back missing sections, the deterministic version is what
the reader gets — a degraded model is a prose regression here, never a factual one.

The reader is told which of the two they are looking at, via ``model_written``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.copilot.context import CopilotContext, build_context
from app.copilot.orchestrator import _capped, _record, plain_text
from app.copilot.prompts import EXPLANATION_RULES, explanation_prompt, system_prompt
from app.copilot.retrieval import retrieve
from app.core.deps import AccessContext
from app.core.telemetry import LlmUsage
from app.llm.config import LLMConfig, get_llm_config
from app.llm.provider import LLMMessage, LLMProvider, build_provider
from app.models.detection import DetectionRun
from app.services import explanation as explanation_service
from app.services.explanation import StructuredExplanation

#: A full structured explanation needs considerably more room than a chat reply. Still bounded --
#: an unbounded answer from a misconfigured model is a denial-of-service on the
#: reader's attention -- and generous enough that the sections that qualify the
#: answer, limitations and confidence, are never the ones trimmed away.
_MAX_EXPLANATION_WORDS = 460

#: Passage source types that count as business context. Everything else the
#: retriever returns (KPI contracts, profiles, catalog entries) is governance
#: metadata: useful to the Copilot's chat, but not "supporting business context",
#: and presenting a column profile under that heading would pad the section with
#: something that explains nothing.
_DOCUMENT_SOURCES = frozenset({"document"})

#: How many documents may be cited. A citation the reader will not open is noise,
#: and the retrieval ranker's tail is where its precision falls off.
_MAX_CITATIONS = 4


def _citations(context: CopilotContext, query: str) -> list[dict[str, Any]]:
    """Approved documents bearing on this result, permission-filtered.

    Delegates entirely to the Copilot's retriever, which applies
    ``document.read``, the document's own access scope, the membership's document
    scope and the row scope before any content is read. Reusing it rather than
    querying documents here means there is exactly one definition of what a
    caller may be shown, and a fix to that definition fixes both surfaces.
    """
    if not context.access.has("document.read"):
        return []

    cites: list[dict[str, Any]] = []
    for scored in retrieve(context, query, top_k=12):
        passage = scored.passage
        if passage.source_type not in _DOCUMENT_SOURCES or passage.is_placeholder:
            continue
        metadata = passage.metadata or {}
        cites.append(
            {
                "label": passage.title,
                "title": passage.title,
                "snippet": passage.content,
                "document_id": metadata.get("document_id"),
                "document_key": metadata.get("document_key"),
                "document_version": metadata.get("document_version"),
                "document_status": metadata.get("document_status"),
                "standing": metadata.get("standing"),
                "effective_from": metadata.get("effective_from"),
                "effective_to": metadata.get("effective_to"),
                "score": round(scored.score, 4),
            }
        )
        if len(cites) >= _MAX_CITATIONS:
            break
    return cites


def _retrieval_query(run: DetectionRun, entity: str | None, dimension: str | None) -> str:
    """What to look for in the documents.

    The KPI name, the date and the selected node — the coordinates of the thing
    being explained. Deliberately not a question: nobody typed one, and inventing
    a question to retrieve against would bias the result toward whatever the
    invented wording happened to say.
    """
    parts = [run.kpi_name, run.kpi_key, run.target_date.isoformat()]
    if dimension:
        parts.append(dimension)
    if entity:
        parts.append(entity)
    return " ".join(str(part) for part in parts if part)


def _evidence_block(citations: list[dict[str, Any]]) -> str | None:
    if not citations:
        return None
    lines: list[str] = []
    for index, item in enumerate(citations, start=1):
        content = (item.get("snippet") or "").strip()
        if len(content) > 900:
            content = content[:900].rstrip() + "…"
        lines.append(f"[E{index}] {item.get('label')}\n{content}")
    return "\n\n".join(lines)


def _split_sections(text: str, order: tuple[str, ...]) -> dict[str, str] | None:
    """Recover the model's sections by their headings.

    Returns ``None`` unless every heading was found and every one has a body: a
    partial narration is not merged with the draft, because a document that is
    half model prose and half platform prose reads as one voice and would let a
    dropped limitation pass as deliberate brevity. All or nothing, and nothing
    means the deterministic version stands.
    """
    upper = text.upper()
    positions: list[tuple[int, str]] = []
    for heading in order:
        index = upper.find(heading)
        if index < 0:
            return None
        positions.append((index, heading))
    positions.sort()
    if [heading for _, heading in positions] != list(order):
        return None  # Reordered sections are not the document that was asked for.

    sections: dict[str, str] = {}
    for slot, (index, heading) in enumerate(positions):
        start = index + len(heading)
        end = positions[slot + 1][0] if slot + 1 < len(positions) else len(text)
        body = text[start:end].strip().lstrip(":").strip()
        if not body:
            return None
        sections[heading] = body
    return sections


async def narrate(
    draft: StructuredExplanation,
    context: CopilotContext,
    *,
    provider: LLMProvider | None = None,
    config: LLMConfig | None = None,
    usage_sink: LlmUsage | None = None,
) -> StructuredExplanation:
    """Re-narrate an assembled explanation, keeping every figure it already has.

    Mutates and returns ``draft``. On any failure the draft is returned untouched
    and still says ``model_written = False``, which is the honest label: the
    reader is looking at the platform's own prose.
    """
    cfg = config or get_llm_config()
    if not cfg.is_available:
        return draft

    active = provider or build_provider(cfg)
    owns = provider is None
    messages = [
        # Both prompts, in this order. The Copilot's standing rules first -- the
        # ones about never inventing a figure and never claiming a cause -- then
        # the narrower instruction that this particular turn is a rewrite, not an
        # analysis. Narrowing after the general rules cannot loosen them.
        LLMMessage.system(system_prompt(context) + "\n\n" + EXPLANATION_RULES),
        LLMMessage.user(
            explanation_prompt(
                draft.subject,
                draft.scope,
                draft.order,
                draft.facts,
                draft.sections,
                _evidence_block(draft.citations),
            )
        ),
    ]

    try:
        # No tools. Every fact is already in the prompt, and a tool call here could
        # only fetch something the deterministic pass decided not to include.
        response = await active.generate(messages, tools=None)
        _record(usage_sink, cfg, response)
        parsed = _split_sections(
            _capped(plain_text(response.text or ""), _MAX_EXPLANATION_WORDS), draft.order
        )
    except Exception:  # noqa: BLE001 - a model failure must not fail the explanation
        parsed = None
    finally:
        if owns:
            await active.aclose()

    if parsed is None:
        draft.limitations.append(
            "A language model is configured but did not return a usable narration, "
            "so these sections are the platform's own wording of the same evidence."
        )
        return draft

    draft.sections = parsed
    draft.model_written = True
    draft.model = cfg.model
    return draft


async def explain_result(
    session: Session,
    access: AccessContext,
    run: DetectionRun,
    *,
    request_id: str | None = None,
    provider: LLMProvider | None = None,
    config: LLMConfig | None = None,
    usage_sink: LlmUsage | None = None,
    narrate_with_model: bool = True,
) -> StructuredExplanation:
    """Explain one KPI result end to end."""
    context = build_context(
        session,
        access,
        request_id=request_id,
        panel="kpi_result",
        kpi_id=run.kpi_definition_id,
        selected_date=run.target_date,
    )
    citations = _citations(context, _retrieval_query(run, None, None))
    draft = explanation_service.build_result_explanation(
        session, access, run, citations=citations
    )
    if not narrate_with_model:
        return draft
    return await narrate(
        draft, context, provider=provider, config=config, usage_sink=usage_sink
    )


async def explain_node(
    session: Session,
    access: AccessContext,
    run: DetectionRun,
    *,
    dimension: str | None = None,
    entity: str | None = None,
    path: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
    provider: LLMProvider | None = None,
    config: LLMConfig | None = None,
    usage_sink: LlmUsage | None = None,
    narrate_with_model: bool = True,
) -> StructuredExplanation:
    """Explain one node of an investigation end to end.

    ``dimension`` and ``entity`` are resolved by ``build_context`` against the
    KPI's approved dimensions before they reach the explanation, so a client
    naming a dimension this KPI is not registered to be split by gets a noted
    context rather than an answer about it.
    """
    context = build_context(
        session,
        access,
        request_id=request_id,
        panel="investigation_node",
        kpi_id=run.kpi_definition_id,
        selected_date=run.target_date,
        dimension=dimension,
        selected_entity=entity,
    )
    citations = _citations(context, _retrieval_query(run, entity, dimension))
    draft = explanation_service.build_node_explanation(
        session,
        access,
        run,
        dimension=dimension,
        entity=entity,
        path=path,
        citations=citations,
    )
    if not narrate_with_model:
        return draft
    return await narrate(
        draft, context, provider=provider, config=config, usage_sink=usage_sink
    )
