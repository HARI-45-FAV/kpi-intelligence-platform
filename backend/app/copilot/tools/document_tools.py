"""Governed tool over the company document store.

Documents are the one place the Copilot touches free text a human wrote, which
makes the entitlement path the important part. Every read goes through the same
gates a retrieval read does:

1. ``document.read`` -- the permission. A VIEWER does not hold it, so the tool is
   not even advertised to a VIEWER's turn.
2. ``documents.is_retrievable`` -- the per-document role scope, the membership's
   document scope, and the membership's row scope against the business
   coordinates in the document's tags. A document failing any of them is skipped
   silently: telling the user a restricted document exists is itself a
   disclosure. It is the identical gate ``copilot.retrieval`` applies, so a
   document the caller cannot reach through the corpus cannot be reached by
   naming it here either.

Content is served from the version the caller asked for, so an answer can cite
"Refund Policy v2" and still be checkable after v3 lands. Formats needing a
parser this deployment does not install are reported as unreadable rather than
guessed at -- see ``app.copilot.text``.

Each returned passage carries its ``retrieval_purpose``: a REFERENCE document is
being read for what a thing *means*, an EVENT document for what *happened*. The
label is stated rather than left to the model to infer, because a definition
quoted as evidence of an event reads convincingly and is wrong.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.copilot.context import CopilotContext
from app.copilot.text import chunk_text, extract_text, unreadable_reason
from app.copilot.tools.base import ToolResult, ToolSpec, refuse
from app.core.config import settings
from app.core.errors import NotFound
from app.models.base import DocumentStatus
from app.models.document import CompanyDocument
from app.services import documents as document_service

DOCUMENT_READ = ("document.read",)


def _readable_documents(context: CopilotContext) -> list[CompanyDocument]:
    """Documents in this company the caller is entitled to read."""
    rows = context.session.scalars(
        select(CompanyDocument)
        .where(CompanyDocument.company_id == context.company_id)
        .order_by(CompanyDocument.title)
    )
    return [
        document
        for document in rows
        if document_service.is_retrievable(document, context.access)
    ]


def _score(haystack: str, terms: list[str]) -> int:
    lowered = haystack.lower()
    return sum(lowered.count(term) for term in terms)


def _terms(query: str) -> list[str]:
    words = [w.strip(".,;:!?()[]\"'").lower() for w in query.split()]
    return [w for w in words if len(w) > 2]


def get_document_context(context: CopilotContext, arguments: dict[str, Any]) -> ToolResult:
    """Find passages in this company's authorised documents that mention a topic.

    The parameter is ``topic`` rather than ``query`` because ``query`` is a
    forbidden parameter name across the whole tool layer -- see
    ``base.FORBIDDEN_PARAMETERS``. Keeping that name unusable means no tool can
    ever quietly grow into one that accepts a query language.
    """
    topic = str(arguments.get("topic") or "").strip()
    if not topic:
        return refuse("Provide a topic or phrase to look for in the company's documents.")

    documents = _readable_documents(context)
    if not documents:
        return refuse(
            f"{context.company_name} has no documents you are entitled to read, so no "
            "document evidence is available."
        )

    wanted_key = (arguments.get("document") or "").strip().lower()
    if wanted_key:
        documents = [
            d
            for d in documents
            if wanted_key in (d.document_key.lower(), d.id.lower())
            or wanted_key in d.title.lower()
        ]
        if not documents:
            return refuse(
                f"No document matching '{arguments['document']}' is readable in "
                f"{context.company_name}."
            )

    document_class = (arguments.get("document_class") or "").strip().upper()
    if document_class:
        documents = [d for d in documents if d.document_class == document_class]
        if not documents:
            return refuse(f"No readable {document_class} documents exist in this company.")

    terms = _terms(topic) or [topic.lower()]
    limit = int(arguments.get("max_passages") or 4)

    passages: list[dict[str, Any]] = []
    unreadable: list[str] = []
    scanned = 0

    for document in documents:
        try:
            version = document_service.resolve_version(
                document, arguments.get("document_version")
            )
        except NotFound:
            continue
        try:
            data, content_type = document_service.read_content(version)
        except NotFound:
            unreadable.append(f"{document.title} v{version.version}: content is missing.")
            continue

        # A policy document is a few pages. The cap exists so a data dump
        # uploaded by mistake cannot turn one question into a huge scan.
        if len(data) > settings.copilot_max_document_bytes_scanned:
            data = data[: settings.copilot_max_document_bytes_scanned]
        scanned += len(data)

        text = extract_text(data, content_type, version.original_filename)
        if text is None:
            unreadable.append(
                f"{document.title} v{version.version}: "
                + unreadable_reason(content_type, version.original_filename)
            )
            continue

        for chunk in chunk_text(text, target_chars=settings.copilot_chunk_chars):
            hits = _score(chunk.text, terms)
            if hits:
                passages.append(
                    {
                        "document_id": document.id,
                        "document_key": document.document_key,
                        "title": document.title,
                        "document_type": document.document_type,
                        "document_class": document.document_class,
                        "document_status": document.status,
                        "version": version.version,
                        "is_current_version": version.is_current,
                        "effective_from": version.effective_from,
                        "effective_to": version.effective_to,
                        "passage_ordinal": chunk.ordinal,
                        "text": chunk.text,
                        "_hits": hits,
                    }
                )

    passages.sort(key=lambda p: (-p["_hits"], p["title"], p["passage_ordinal"]))
    selected = passages[:limit]
    for passage in selected:
        passage.pop("_hits", None)

    caveats: list[str] = list(unreadable)
    if not selected:
        return ToolResult(
            data={"topic": topic, "passages": [], "documents_searched": len(documents)},
            evidence=[],
            caveats=caveats,
            error=(
                f"Nothing in the {len(documents)} document(s) you can read mentions "
                f"'{topic}'."
                + (f" Note: {' '.join(unreadable)}" if unreadable else "")
            ),
        )

    archived = {p["title"] for p in selected if p["document_status"] != DocumentStatus.ACTIVE}
    if archived:
        caveats.append(
            "Some passages come from archived documents: " + ", ".join(sorted(archived)) + "."
        )
    superseded = [p for p in selected if not p["is_current_version"]]
    if superseded:
        caveats.append(
            "Some passages come from a superseded version, which may no longer reflect "
            "current policy."
        )

    evidence = [
        {
            "source_type": "document",
            "source_id": p["document_id"],
            "company_id": context.company_id,
            "title": f"{p['title']} v{p['version']} (passage {p['passage_ordinal']})",
            "content": p["text"],
            "metadata": {
                "document_id": p["document_id"],
                "document_key": p["document_key"],
                "document_version": p["version"],
                "document_type": p["document_type"],
                "document_class": p["document_class"],
                "document_status": p["document_status"],
                "is_current_version": p["is_current_version"],
                # Why this passage is being read: a definition, or a record of
                # something that happened. Stated, not left to inference.
                "retrieval_purpose": document_service.retrieval_purpose(
                    p["document_class"]
                ),
                "passage_ordinal": p["passage_ordinal"],
                "effective_from": str(p["effective_from"]) if p["effective_from"] else None,
                "effective_to": str(p["effective_to"]) if p["effective_to"] else None,
            },
        }
        for p in selected
    ]

    return ToolResult(
        data={
            "topic": topic,
            "documents_searched": len(documents),
            "bytes_scanned": scanned,
            "passages": selected,
        },
        evidence=evidence,
        caveats=caveats,
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_document_context",
        description=(
            "Search this company's approved documents (policies, KPI handbooks, "
            "definitions, incident notes) for passages about a topic, and return the "
            "matching text with its document title, version and effective dates. Use it "
            "for questions about how the business defines or handles something. Only "
            "documents the caller is entitled to read are searched."
        ),
        permissions=DOCUMENT_READ,
        parameters={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Topic or phrase to look for, e.g. 'refund eligibility'.",
                },
                "document": {
                    "type": "string",
                    "description": (
                        "Optional: restrict the search to one document by its key, id or "
                        "title."
                    ),
                },
                "document_version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Optional: read a specific version instead of the current one. "
                        "Use when a KPI contract cites a particular version."
                    ),
                },
                "document_class": {
                    "type": "string",
                    "enum": ["REFERENCE", "EVENT"],
                    "description": (
                        "Optional: REFERENCE describes how the company operates; EVENT "
                        "describes what happened."
                    ),
                },
                "max_passages": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "How many passages to return. Default 4.",
                },
            },
            "required": ["topic"],
        },
        handler=get_document_context,
    ),
)
