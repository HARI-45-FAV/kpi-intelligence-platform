"""Company-scoped retrieval over governed knowledge.

The Copilot needs to find relevant material before it can answer, and the model
must not be the thing that decides what it may read. So retrieval is a plain
function of the caller's ``AccessContext``:

**One company, always.** Every corpus builder filters on
``company_id == context.company_id`` in the SQL, and every passage is stamped with
that company before it becomes evidence -- where ``EvidenceBundle.add`` checks it
again. There is no index shared between tenants to leak from, because the corpus
is assembled per request from rows the caller can already read. Cross-company
retrieval is not blocked by a filter that could be forgotten; there is no query
that would return another company's rows.

**Only governed knowledge.** KPI contracts, versions, lineage, dimensions,
drivers, validation runs, catalog snapshots, table and column profiles,
relationship and join-safety verdicts, and authorised documents. No tenant
business rows are indexed. Profile ``sample_values`` -- the one place stored data
appears -- are excluded, so a passage can say a column is 4% null but never what
is in it.

**Entitlement before content.** Documents pass ``document.read`` and
``assert_readable``; profiles pass ``analytics.read`` and the same per-column
readability check the analysis API uses; sources pass ``source.read``. A caller
who cannot read something through the REST API cannot retrieve it here.

**The question never selects the documents.** The order is fixed and one-way:
caller -> company -> role and membership scope -> the documents that scope
permits -> lexical relevance within *those* -> the model. The question is applied
last, to rank what the caller was already entitled to, so a cleverly worded
question cannot widen the set. Four membership axes narrow it before any word of
the question is read: the document's role ``access_scope``, the membership's
``allowed_document_scopes``, the membership's row scope against the business
coordinates in ``tags`` (region, sector, channel and the like), and -- for event
material, when a date is in context -- the version's effective window.

**Two document purposes, kept apart.** A KPI handbook or a policy says what a
measure *means*; an incident, campaign or management note records what
*happened*. Those answer different questions, and an answer that quotes a
definition as though it were evidence of an event is wrong in a way that reads
convincingly. So the two are assembled by separate builders and every document
passage carries its ``retrieval_purpose``, which reaches the model in the
evidence provenance line rather than being inferred from the prose.

Ranking is lexical: IDF-weighted term overlap with a light field boost. That is a
deliberate choice, not a placeholder. Embeddings would need a model server, a
vector store and a re-embedding path for every KPI edit -- and the corpus here is
small, dense with proper nouns, and already labelled. Exact-term matching on
"gross margin" or "orders.customer_id" beats semantic similarity on a corpus of a
few hundred short, highly specific passages. The interface returns scored
passages, so swapping in embeddings later is a change to ``_score`` and nothing
else.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from app.copilot.context import CopilotContext
from app.copilot.text import chunk_text, extract_text
from app.core.config import settings
from app.core.errors import PlatformError
from app.models.base import DocumentClass, DocumentStatus, KpiStatus
from app.models.catalog import CatalogVersion
from app.models.document import CompanyDocument, CompanyDocumentVersion
from app.models.kpi import KpiDefinition
from app.models.profiling import ColumnProfile, TableGrain, TableProfile
from app.services import documents as document_service
from app.services.analysis_views import (
    latest_health,
    relationship_payload,
    relationship_summary,
    scoped_tables,
)

_WORD = re.compile(r"[a-z0-9_]+")

# Words that match everything and therefore discriminate nothing. Kept short --
# an aggressive stop list would strip "revenue" style domain words from a
# question like "what is the definition of revenue".
_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those there here what which who whom
    whose when where why how is are was were be been being do does did doing have has had
    having i you he she it we they me him her us them my your his its our their of to in
    on at by for with from about into over after before between as not no nor so too very
    can will just should now also please tell show me explain does
    """.split()
)


@dataclass(frozen=True, slots=True)
class Passage:
    """One retrievable unit of governed knowledge, already company-stamped."""

    source_type: str
    source_id: str | None
    title: str
    content: str
    company_id: str
    metadata: dict[str, Any]
    is_placeholder: bool = False
    # Multiplies the lexical score. Higher for material that is more likely to be
    # the actual answer: a KPI's business definition over a freshness reading.
    weight: float = 1.0

    def as_evidence(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "company_id": self.company_id,
            "title": self.title,
            "content": self.content,
            "is_placeholder": self.is_placeholder,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ScoredPassage:
    passage: Passage
    score: float


# ---------------------------------------------------------------------------
# Corpus builders. Each is company-filtered in SQL and entitlement-checked.
# ---------------------------------------------------------------------------
def _kpi_passages(context: CopilotContext) -> list[Passage]:
    if not context.access.has("kpi.read"):
        return []

    definitions = list(
        context.session.scalars(
            select(KpiDefinition).where(KpiDefinition.company_id == context.company_id)
        )
    )
    passages: list[Passage] = []
    for definition in definitions:
        for version in definition.versions:
            # Retrieval favours what is in force. A DRAFT is still indexed --
            # "why is v3 not live yet" is a fair question -- but it does not
            # outrank the definition the business is actually using.
            in_force = version.status == KpiStatus.ACTIVE
            label = f"{definition.name} v{version.version}"
            base = {
                "kpi_key": definition.kpi_key,
                "kpi_definition_id": definition.id,
                "kpi_version_id": version.id,
                "kpi_version": version.version,
                "status": version.status,
            }

            body = [
                f"KPI {definition.name} (key {definition.kpi_key}), version "
                f"{version.version}, status {version.status}.",
                f"Business definition: {version.business_definition}",
            ]
            if version.purpose:
                body.append(f"Purpose: {version.purpose}")
            if definition.short_description:
                body.append(f"Summary: {definition.short_description}")
            body.append(
                f"Formula: {version.formula_expression}. Kind {version.kind}, aggregation "
                f"{version.aggregation or 'n/a'}, null handling {version.null_handling}."
            )
            body.append(
                f"Unit {version.unit or 'unspecified'}"
                + (f" in {version.currency}" if version.currency else "")
                + f", direction {version.direction}, time field "
                f"{version.time_field or 'unspecified'} at {version.time_grain} grain."
            )
            if version.filters:
                body.append(f"Filters: {version.filters}")
            passages.append(
                Passage(
                    source_type="kpi_contract",
                    source_id=version.id,
                    title=f"{label} governed contract",
                    content="\n".join(body),
                    company_id=context.company_id,
                    metadata=base,
                    weight=1.6 if in_force else 1.0,
                )
            )

            if version.lineage:
                passages.append(
                    Passage(
                        source_type="kpi_lineage",
                        source_id=version.id,
                        title=f"{label} column lineage",
                        content=(
                            f"{label} is computed from: "
                            + "; ".join(
                                f"{item.role} = {item.schema_name}.{item.table_name}."
                                f"{item.column_name}"
                                + (f" via {item.transformation}" if item.transformation else "")
                                for item in version.lineage
                            )
                            + ". Lineage is generated from the formula contract."
                        ),
                        company_id=context.company_id,
                        metadata=base,
                        weight=1.2 if in_force else 0.9,
                    )
                )

            if version.dimensions:
                passages.append(
                    Passage(
                        source_type="kpi_dimension",
                        source_id=version.id,
                        title=f"{label} dimensions",
                        content=(
                            f"{label} may be broken down by: "
                            + "; ".join(
                                f"{d.dimension_name} ({d.source_table}.{d.source_column})"
                                + ("" if d.allowed else ", not allowed")
                                for d in version.dimensions
                            )
                            + ". A declared dimension is an approved way to slice this "
                            "KPI on request, not a monitoring instruction: detection "
                            "runs at the KPI level, and a dimension is analysed only "
                            "when someone investigates."
                        ),
                        company_id=context.company_id,
                        metadata=base,
                    )
                )

            if version.drivers:
                passages.append(
                    Passage(
                        source_type="kpi_driver",
                        source_id=version.id,
                        title=f"{label} registered drivers",
                        content=(
                            f"Candidate explanatory factors registered for {label}: "
                            + "; ".join(
                                f"{d.driver_name} ({d.driver_type}"
                                + (", controllable" if d.controllable else ", not controllable")
                                + ")"
                                for d in version.drivers
                            )
                            + ". These are hypotheses to investigate, not measured causes: "
                            "no attribution or correlation has been computed."
                        ),
                        company_id=context.company_id,
                        metadata=base,
                    )
                )

            if version.last_validation_status:
                passages.append(
                    Passage(
                        source_type="kpi_validation",
                        source_id=version.id,
                        title=f"{label} validation state",
                        content=(
                            f"{label} last validated {version.last_validated_at} with "
                            f"overall status {version.last_validation_status}. Call "
                            "get_kpi_validation_summary for the individual checks."
                        ),
                        company_id=context.company_id,
                        metadata=base,
                    )
                )

            if version.materiality is not None:
                rule = version.materiality
                passages.append(
                    Passage(
                        source_type="kpi_contract",
                        source_id=version.id,
                        title=f"{label} materiality thresholds",
                        content=(
                            f"{label} is declared {rule.business_criticality} criticality "
                            f"with relative threshold {rule.relative_threshold_pct}%, "
                            f"absolute threshold {rule.absolute_threshold}, statistical "
                            f"rule {rule.statistical_rule or 'none'}, persistence "
                            f"{rule.persistence_periods} period(s). The detection engine "
                            "reads these as this KPI's own business tolerance and its "
                            "movement floor, alongside the spread of its comparable "
                            "history. They are thresholds, not a result: whether any "
                            "particular date breached them is recorded on that date's "
                            "detection run, not here."
                        ),
                        company_id=context.company_id,
                        metadata=base,
                    )
                )
    return passages


# ---------------------------------------------------------------------------
# Documents: two categories, two purposes, never mixed
# ---------------------------------------------------------------------------
#: Taken from the document service rather than restated here, so the corpus and
#: the document tool label a passage's purpose with the same words.
DEFINITION_PURPOSE = document_service.DEFINITION_PURPOSE
EVIDENCE_PURPOSE = document_service.EVIDENCE_PURPOSE


def _permitted_documents(
    context: CopilotContext, *, document_class: str
) -> list[CompanyDocument]:
    """One purpose's documents, narrowed by entitlement before any content is read.

    The gate is ``documents.is_retrievable`` -- the same one the Copilot's document
    tool applies -- so there is one definition of what this caller may be shown:
    the document's own role scope, then the membership's document scope, then its
    row scope against the document's business coordinates. A document failing any
    of them is skipped silently, because naming a restricted document is itself a
    disclosure. The question has not been consulted at this point, and will not
    widen the result when it is.
    """
    rows = context.session.scalars(
        select(CompanyDocument).where(
            CompanyDocument.company_id == context.company_id,
            CompanyDocument.document_class == document_class,
        )
    )
    return [
        document
        for document in rows
        if document_service.is_retrievable(document, context.access)
    ]


def _event_standing(
    version: CompanyDocumentVersion, day: date | None
) -> tuple[bool, float, str]:
    """Whether an event note bears on the date in context, and how strongly.

    With no date in context every event stays retrievable: "which incidents were
    logged this quarter" is a fair question. With a date in context, an event
    whose own window is far from it is *dropped* rather than merely ranked low --
    a distant incident retrieved alongside a movement invites precisely the
    association this platform refuses to assert, and the cheapest way not to
    imply it is not to put the two in the same prompt.
    """
    if day is None:
        return (True, 1.0, "no_date_in_context")
    starts = version.effective_from
    if starts is None:
        # Undated event material cannot be placed relative to the date, so it is
        # offered with less weight and labelled, never silently dated.
        return (True, 0.7, "undated")
    ends = version.effective_to or starts
    if starts <= day <= ends:
        return (True, 1.25, "covers_the_date")
    window = timedelta(days=max(settings.copilot_event_relevance_days, 0))
    if (starts - window) <= day <= (ends + window):
        return (True, 0.9, "near_the_date")
    return (False, 0.0, "outside_the_window")


def _reference_standing(
    version: CompanyDocumentVersion, day: date | None
) -> tuple[bool, float, str]:
    """A policy version's standing on the date in context.

    A definition stays retrievable whatever date is on screen -- "what does this
    KPI mean" does not stop being answerable because a window closed. What
    changes is standing: a version that was not yet effective, or had already
    been replaced, is not the policy that governed that date, so it ranks below
    one that was in force and says which it was.
    """
    if day is None or version.effective_from is None:
        return (True, 1.0, "unstated")
    if version.effective_from > day:
        return (True, 0.8, "not_yet_effective_on_the_date")
    if version.effective_to is not None and version.effective_to < day:
        return (True, 0.8, "superseded_by_the_date")
    return (True, 1.15, "in_force_on_the_date")


#: Whether a document version bears on the date in context, how strongly, and the
#: word for why. The two purposes answer this differently, which is the whole
#: reason they are assembled separately.
StandingRule = Callable[[CompanyDocumentVersion, date | None], tuple[bool, float, str]]


def _document_passages(
    context: CopilotContext,
    *,
    document_class: str,
    purpose: str,
    standing: StandingRule,
) -> list[Passage]:
    """Chunk one purpose's permitted documents into company-stamped passages."""
    if not context.access.has("document.read"):
        return []

    day = context.selected_date
    passages: list[Passage] = []
    for document in _permitted_documents(context, document_class=document_class):
        try:
            version = document_service.resolve_version(document, None)
        except PlatformError:
            continue

        relevant, multiplier, standing_label = standing(version, day)
        if not relevant:
            continue
        # An archived document is still citable -- an investigation may have been
        # written against it -- but it is not current company context, so it does
        # not outrank one that is.
        if document.status != DocumentStatus.ACTIVE:
            multiplier *= 0.6

        try:
            data, content_type = document_service.read_content(version)
        except PlatformError:
            continue

        if len(data) > settings.copilot_max_document_bytes_scanned:
            data = data[: settings.copilot_max_document_bytes_scanned]
        text = extract_text(data, content_type, version.original_filename)

        metadata: dict[str, Any] = {
            "document_id": document.id,
            "document_key": document.document_key,
            "document_version": version.version,
            "document_type": document.document_type,
            "document_class": document.document_class,
            "document_status": document.status,
            "access_scope": list(document.access_scope or []),
            "retrieval_purpose": purpose,
            "standing": standing_label,
            "effective_from": (
                str(version.effective_from) if version.effective_from else None
            ),
            "effective_to": (str(version.effective_to) if version.effective_to else None),
        }

        if not text:
            # Binary formats are represented by their metadata only, so the
            # Copilot can say the document exists and cannot be read.
            passages.append(
                Passage(
                    source_type="document",
                    source_id=document.id,
                    title=f"{document.title} v{version.version} (content not extractable)",
                    content=(
                        f"{document.title} is a {document.document_type} document "
                        f"({document.document_class}), version {version.version}"
                        + (f", {document.description}" if document.description else "")
                        + ". Its stored format cannot be read as text by this deployment, "
                        "so its contents were not searched."
                    ),
                    company_id=context.company_id,
                    metadata={**metadata, "extractable": False},
                    weight=0.7 * multiplier,
                )
            )
            continue

        for chunk in chunk_text(text, target_chars=settings.copilot_chunk_chars):
            passages.append(
                Passage(
                    source_type="document",
                    source_id=document.id,
                    title=f"{document.title} v{version.version} (passage {chunk.ordinal})",
                    content=chunk.text,
                    company_id=context.company_id,
                    metadata={**metadata, "passage_ordinal": chunk.ordinal},
                    weight=1.3 * multiplier,
                )
            )
    return passages


def _reference_document_passages(context: CopilotContext) -> list[Passage]:
    """Handbooks, policies and business rules: what the company's measures *mean*."""
    return _document_passages(
        context,
        document_class=DocumentClass.REFERENCE,
        purpose=DEFINITION_PURPOSE,
        standing=_reference_standing,
    )


def _event_document_passages(context: CopilotContext) -> list[Passage]:
    """Incidents, campaigns and management notes: what *happened*, and when."""
    return _document_passages(
        context,
        document_class=DocumentClass.EVENT,
        purpose=EVIDENCE_PURPOSE,
        standing=_event_standing,
    )


def _data_passages(context: CopilotContext) -> list[Passage]:
    if not context.access.has("analytics.read"):
        return []

    session = context.session
    tables = scoped_tables(session, context.access)
    passages: list[Passage] = []

    for table in tables:
        profile = session.scalar(
            select(TableProfile).where(TableProfile.source_table_id == table.id)
        )
        grain = session.scalar(select(TableGrain).where(TableGrain.source_table_id == table.id))
        health = latest_health(session, table)

        lines = [
            f"Table {table.qualified_name} from source "
            f"{table.data_source.name if table.data_source else 'unknown'}, "
            f"approximately {table.approx_row_count} rows, "
            f"{table.column_count} column(s)."
        ]
        if table.comment:
            lines.append(f"Description: {table.comment}")
        if profile is not None:
            lines.append(
                f"Profiled {profile.profiled_at}: {profile.row_count} rows, completeness "
                f"{profile.completeness_pct}%, quality {profile.quality_status} "
                f"(score {profile.quality_score}), {profile.withheld_column_count} "
                "column(s) withheld by access policy."
            )
            if profile.warnings:
                lines.append(f"Quality warnings: {profile.warnings}")
        else:
            lines.append("This table has not been profiled.")
        if grain is not None:
            lines.append(
                f"Grain: {grain.inferred_grain or grain.declared_grain} "
                f"({'unique' if grain.is_unique else 'not unique'}), time column "
                f"{grain.time_column or 'none'} at {grain.time_grain or 'unknown'} grain."
            )
        if health is not None:
            lines.append(
                f"Freshness {health.freshness_status}, coverage {health.coverage_start} to "
                f"{health.coverage_end}, checked {health.checked_at}."
            )

        passages.append(
            Passage(
                source_type="table_profile",
                source_id=table.id,
                title=f"{table.qualified_name} profile",
                content="\n".join(lines),
                company_id=context.company_id,
                metadata={"table": table.qualified_name, "source_table_id": table.id},
                weight=1.1,
            )
        )

        # Columns are summarised in one passage per table: a question naming a
        # column should surface the table it belongs to, and per-column passages
        # would flood the ranking with near-identical text.
        profiles = {
            row.source_column_id: row
            for row in session.scalars(
                select(ColumnProfile).where(ColumnProfile.source_table_id == table.id)
            )
        }
        described: list[str] = []
        withheld: list[str] = []
        for column in sorted(table.columns, key=lambda c: c.ordinal_position):
            if not context.access.can_read_column(column, table_name=table.table_name):
                withheld.append(column.column_name)
                continue
            stat = profiles.get(column.id)
            entry = f"{column.column_name} ({column.data_type}, {column.semantic_type}"
            if column.is_primary_key:
                entry += ", primary key"
            if column.is_foreign_key:
                entry += f", references {column.references_table}.{column.references_column}"
            if column.classification != "INTERNAL":
                entry += f", {column.classification}"
            entry += ")"
            if stat is not None and not stat.access_withheld:
                entry += (
                    f": {stat.null_pct}% null, {stat.distinct_count} distinct"
                    + (", unique" if stat.is_unique else "")
                    + f", quality {stat.quality_status}"
                )
            described.append(entry)

        if described or withheld:
            content = f"Columns of {table.qualified_name}: " + "; ".join(described)
            if withheld:
                content += (
                    f". Withheld from you by access policy: {', '.join(withheld)} -- their "
                    "statistics are not included."
                )
            passages.append(
                Passage(
                    source_type="column_profile",
                    source_id=table.id,
                    title=f"{table.qualified_name} columns",
                    content=content,
                    company_id=context.company_id,
                    metadata={
                        "table": table.qualified_name,
                        "withheld_columns": len(withheld),
                    },
                )
            )

    relationships = relationship_payload(session, context.access)
    if relationships:
        summary = relationship_summary(relationships)
        passages.append(
            Passage(
                source_type="relationship",
                source_id=None,
                title="Table relationships and join safety",
                content=(
                    f"{summary['checked']} relationship(s) between tables in scope: "
                    f"{summary['safe']} safe, {summary['needs_attention']} safe only with "
                    f"aggregation, {summary['unsafe']} risky, {summary['unrated']} unrated. "
                    + "; ".join(
                        f"{r['from_table']}.{r['from_column']} -> {r['to_table']}."
                        f"{r['to_column']} ({r['type']}"
                        + (
                            f", join safety {r['join_safety']['level']}"
                            if r.get("join_safety")
                            else ""
                        )
                        + (
                            f", {r['orphan_count']} orphan rows"
                            if r["orphan_count"]
                            else ""
                        )
                        + ")"
                        for r in relationships
                    )
                ),
                company_id=context.company_id,
                metadata={"relationship_count": summary["checked"]},
            )
        )
        risky = [
            r
            for r in relationships
            if r.get("join_safety") and r["join_safety"]["level"] != "SAFE"
        ]
        if risky:
            passages.append(
                Passage(
                    source_type="join_safety",
                    source_id=None,
                    title="Joins that need care",
                    content=(
                        "These joins can change a number without raising an error: "
                        + "; ".join(
                            f"{r['from_table']} -> {r['to_table']} is "
                            f"{r['join_safety']['level']}"
                            + (
                                f" ({r['join_safety']['reason']})"
                                if r["join_safety"].get("reason")
                                else ""
                            )
                            + (
                                f" Guidance: {r['join_safety']['guidance']}"
                                if r["join_safety"].get("guidance")
                                else ""
                            )
                            for r in risky
                        )
                    ),
                    company_id=context.company_id,
                    metadata={"flagged": len(risky)},
                    weight=1.2,
                )
            )

    return passages


def _catalog_passages(context: CopilotContext) -> list[Passage]:
    if not context.access.has("catalog.read"):
        return []
    version = context.session.scalar(
        select(CatalogVersion)
        .where(CatalogVersion.company_id == context.company_id)
        .order_by(CatalogVersion.version.desc())
        .limit(1)
    )
    if version is None:
        return []
    return [
        Passage(
            source_type="data_source",
            source_id=version.id,
            title=f"Semantic catalog v{version.version}",
            content=(
                f"Catalog version {version.version} published {version.published_at}"
                + (f": {version.note}" if version.note else "")
                + f". It snapshots {version.source_count} source(s), "
                f"{version.selected_table_count} table(s) in scope, "
                f"{version.profiled_table_count} profiled, "
                f"{version.relationship_count} relationship(s), "
                f"{version.document_count} document(s) and "
                f"{version.active_kpi_count} active KPI(s). A catalog version records "
                "what was known about the company's data at that point; it is immutable, "
                "and is not the same thing as a KPI version."
            ),
            company_id=context.company_id,
            metadata={"catalog_version": version.version},
        )
    ]


def _source_passages(context: CopilotContext) -> list[Passage]:
    if not context.access.has("source.read"):
        return []
    tables = scoped_tables(context.session, context.access)
    sources = {t.data_source_id: t.data_source for t in tables if t.data_source}
    passages: list[Passage] = []
    for source in sources.values():
        passages.append(
            Passage(
                source_type="data_source",
                source_id=source.id,
                title=f"Data source {source.name}",
                content=(
                    f"{source.name} is a {source.source_type} source"
                    + (f" ({source.description})" if source.description else "")
                    + f", connection status {source.connection_status}, refresh "
                    f"{source.refresh_frequency}, timezone {source.timezone}."
                    + (
                        f" Known limitations: {source.known_limitations}"
                        if source.known_limitations
                        else ""
                    )
                    + (
                        f" Last connection test failed: {source.last_test_error}"
                        if source.last_test_error
                        else ""
                    )
                ),
                company_id=context.company_id,
                metadata={"source_type": source.source_type, "data_source_id": source.id},
            )
        )
    return passages


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    return [word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


def _score(question_terms: list[str], corpus: list[Passage]) -> list[ScoredPassage]:
    """IDF-weighted overlap between the question and each passage.

    A term appearing in almost every passage (``kpi``, ``version``, ``table``)
    earns little; a term appearing in two passages (``margin``, ``customer_id``)
    earns a lot. Repeated occurrences count with diminishing returns, so a long
    passage cannot win by mentioning the term twenty times.
    """
    if not corpus or not question_terms:
        return []

    documents = [tokenize(f"{p.title}\n{p.content}") for p in corpus]
    total = len(documents)
    frequency: dict[str, int] = {}
    for tokens in documents:
        for term in set(tokens):
            frequency[term] = frequency.get(term, 0) + 1

    scored: list[ScoredPassage] = []
    for passage, tokens in zip(corpus, documents, strict=True):
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        title_terms = set(tokenize(passage.title))

        score = 0.0
        matched = 0
        for term in question_terms:
            hits = counts.get(term, 0)
            if not hits:
                # Prefix match catches plural and possessive forms without a
                # stemmer: "orders" against "order_items", "margins" against
                # "margin". Both words have to be long enough to mean something --
                # without the length floor on the corpus token, any sufficiently
                # long word matches whatever short token happens to begin it, and
                # a question made entirely of nonsense retrieves evidence.
                if len(term) > 3:
                    hits = sum(
                        count
                        for token, count in counts.items()
                        if len(token) > 3
                        and (token.startswith(term) or term.startswith(token))
                    )
                if not hits:
                    continue
                hits *= 0.5
            matched += 1
            idf = math.log((total + 1) / (frequency.get(term, 0) + 1)) + 1.0
            weight = 1.0 + math.log(hits)
            if term in title_terms:
                # A term in the title is what the passage is *about*, not merely
                # something it mentions.
                weight *= 1.8
            score += idf * weight

        if not matched:
            continue
        # Reward covering more of the question rather than hammering one word.
        coverage = matched / len(question_terms)
        scored.append(ScoredPassage(passage, score * passage.weight * (0.5 + coverage)))

    scored.sort(key=lambda item: -item.score)
    return scored


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_corpus(context: CopilotContext) -> list[Passage]:
    """Everything this caller, in this company, is entitled to retrieve.

    Assembled per request. There is no persistent index, which is what makes
    cross-tenant retrieval structurally impossible rather than merely filtered:
    the rows are fetched under the caller's own access context every time.

    The two document builders are listed separately because they serve different
    purposes and are permitted to disagree about what is relevant -- a definition
    holds regardless of the date on screen, an event note does not.
    """
    passages: list[Passage] = []
    for builder in (
        _kpi_passages,
        _reference_document_passages,
        _event_document_passages,
        _data_passages,
        _catalog_passages,
        _source_passages,
    ):
        passages.extend(builder(context))

    # Belt and braces: a passage carrying the wrong company must never reach the
    # ranker. If a builder ever regresses, this drops the row rather than
    # answering from it.
    return [p for p in passages if p.company_id == context.company_id]


def retrieve(
    context: CopilotContext, question: str, *, top_k: int | None = None
) -> list[ScoredPassage]:
    """Rank this company's governed knowledge against one question."""
    limit = top_k or settings.copilot_retrieval_top_k
    terms = tokenize(question)

    # The KPI and date on screen are part of what the user is asking about, so
    # their words join the query. This is why "why is it blocked?" retrieves the
    # right KPI's validation run without the user naming it.
    if context.kpi_definition is not None:
        terms.extend(tokenize(context.kpi_definition.name))
        terms.extend(tokenize(context.kpi_definition.kpi_key))

    if not terms:
        return []

    corpus = build_corpus(context)
    return _score(terms, corpus)[:limit]
