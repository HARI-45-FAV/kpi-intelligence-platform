"""The evidence model every Copilot answer is built from.

The platform is the source of truth, so an answer is only as good as what it can
point at. Each ``EvidenceItem`` is one piece of governed material -- a KPI
contract, a validation run, a table profile, a passage from an approved document
-- carrying where it came from and whether it is a real measurement.

Three properties matter:

* **Company-stamped.** Every item records the company it was read from, and
  ``EvidenceBundle.add`` refuses an item stamped with a different company than
  the bundle. Cross-tenant material cannot enter a bundle even by a coding
  mistake, and the check is one line rather than a convention.
* **Measurement-aware.** ``is_placeholder`` is not decoration. It marks an item
  that describes the *absence* of a measurement rather than one -- most often
  "the screen is showing a date no detection run was stored for". A real
  detection run carries its own numbers and is never marked, so the prompt rule
  "say so whenever you refer to marked evidence" keeps the model from narrating a
  gap as a result.
* **Citable.** ``evidence_id`` is short and stable within one turn so the answer
  can reference ``[E1]`` and the UI can resolve it to a source the user can open.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import-light
    from app.models.detection import ContributionRun, DetectionRun

# Where a piece of evidence came from. Every value here is governed platform
# metadata or an authorised document -- there is deliberately no source type for
# tenant business rows, because none are ever loaded into evidence.
SOURCE_TYPES: tuple[str, ...] = (
    "kpi_contract",
    "kpi_validation",
    "kpi_lineage",
    "kpi_dimension",
    "kpi_driver",
    "document",
    "data_source",
    "table_profile",
    "column_profile",
    "relationship",
    "join_safety",
    "reconciliation",
    "execution_telemetry",
    "platform_capability",
    "detection_run",
    # A contribution result: one KPI movement apportioned across one approved
    # dimension by deterministic arithmetic. It is measured platform output like a
    # detection run, and it enters evidence for the same reason -- so an
    # explanation can quote the shares rather than estimate them. What it is *not*
    # is a verdict about any contributor, and nothing downstream may read it as
    # one.
    "contribution_analysis",
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    source_type: str
    source_id: str | None
    company_id: str
    title: str
    content: str
    is_placeholder: bool = False
    # Free-form provenance: document version, KPI version, table name, run id.
    # Rendered to the model and returned to the client, so nothing secret goes
    # in here -- credentials are never part of any evidence path.
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "title": self.title,
            "content": self.content,
            "is_placeholder": self.is_placeholder,
            "metadata": self.metadata,
        }

    def as_prompt_block(self) -> str:
        """How the model sees one item: labelled, attributed, and honest."""
        lines = [f"[{self.evidence_id}] {self.title}", f"source_type: {self.source_type}"]
        if self.metadata:
            detail = ", ".join(f"{key}={value}" for key, value in sorted(self.metadata.items()))
            lines.append(f"provenance: {detail}")
        if self.is_placeholder:
            lines.append(
                "NOT A MEASUREMENT: this records that no measured result is available, "
                "not a result."
            )
        lines.append(self.content.strip())
        return "\n".join(lines)


class EvidenceBundle:
    """The evidence gathered for one turn, in the order it was gathered."""

    __slots__ = ("company_id", "_items", "_seen")

    def __init__(self, company_id: str) -> None:
        self.company_id = company_id
        self._items: list[EvidenceItem] = []
        self._seen: set[tuple[str, str | None, str]] = set()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        return tuple(self._items)

    @property
    def is_empty(self) -> bool:
        return not self._items

    @property
    def has_placeholder(self) -> bool:
        return any(item.is_placeholder for item in self._items)

    def add(
        self,
        *,
        source_type: str,
        title: str,
        content: str,
        source_id: str | None = None,
        company_id: str | None = None,
        is_placeholder: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceItem | None:
        """Append one item, or return ``None`` if it is a duplicate.

        Raises when the item belongs to another company. That is a programming
        error, not a user error, and it must fail loudly rather than produce a
        cross-tenant answer.
        """
        owner = company_id or self.company_id
        if owner != self.company_id:
            raise ValueError(
                "Refusing to add evidence from another company to this bundle."
            )
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"Unknown evidence source_type {source_type!r}.")

        fingerprint = (source_type, source_id, content[:200])
        if fingerprint in self._seen:
            return None
        self._seen.add(fingerprint)

        item = EvidenceItem(
            evidence_id=f"E{len(self._items) + 1}",
            source_type=source_type,
            source_id=source_id,
            company_id=self.company_id,
            title=title,
            content=content,
            is_placeholder=is_placeholder,
            metadata=metadata or {},
        )
        self._items.append(item)
        return item

    def extend(self, items) -> None:
        for item in items:
            self.add(
                source_type=item.source_type,
                title=item.title,
                content=item.content,
                source_id=item.source_id,
                company_id=item.company_id,
                is_placeholder=item.is_placeholder,
                metadata=item.metadata,
            )

    # -- rendering -------------------------------------------------------
    def as_prompt(
        self, *, max_items: int | None = None, max_chars_per_item: int | None = None
    ) -> str:
        """Render evidence for the model, optionally within a prompt budget.

        The response still returns every evidence item to the user.  This only
        prevents a local model with a small context window from timing out while
        trying to read more text than it can process in one turn.
        """
        if not self._items:
            return (
                "NO EVIDENCE AVAILABLE. Nothing in this company's governed metadata or "
                "authorised documents matched the question. Say so plainly."
            )
        items = self._select_within(max_items)
        blocks: list[str] = []
        for item in items:
            if max_chars_per_item is None or len(item.content) <= max_chars_per_item:
                blocks.append(item.as_prompt_block())
                continue
            shortened = EvidenceItem(
                evidence_id=item.evidence_id,
                source_type=item.source_type,
                source_id=item.source_id,
                company_id=item.company_id,
                title=item.title,
                content=item.content[:max_chars_per_item].rsplit(" ", 1)[0] + "…",
                is_placeholder=item.is_placeholder,
                metadata=item.metadata,
            )
            blocks.append(shortened.as_prompt_block())
        return "\n\n".join(blocks)

    def as_list(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._items]

    def _select_within(self, max_items: int | None) -> list[EvidenceItem]:
        """Apply the prompt budget without ever dropping a disclosure.

        Retrieved passages are ranked, so trimming to the first ``max_items`` is
        the right call for them. A not-a-measurement notice is not ranked and is
        not retrieved -- the platform inserts it on its own initiative, last, after
        the passages. Budgeting by position alone therefore silently discarded the
        one item whose absence is dangerous: the model would be asked about a
        figure with nothing telling it that no measured figure exists, and would
        narrate something as a business result. So the notices are held back from
        the budget and always rendered, displacing the lowest-ranked passage
        instead.
        """
        if max_items is None:
            return list(self._items)

        notices = [item for item in self._items if item.is_placeholder]
        if not notices:
            return self._items[:max_items]

        ranked = [item for item in self._items if not item.is_placeholder]
        room = max(0, max_items - len(notices))
        # Original order is preserved so evidence ids still read in sequence.
        kept = set(id(item) for item in ranked[:room]) | set(id(item) for item in notices)
        return [item for item in self._items if id(item) in kept]


def detection_run_evidence(run: DetectionRun) -> dict[str, Any]:
    """One stored detection result, as the evidence behind a displayed figure.

    This is what the screen is showing: the actual the engine measured at the
    KPI's registered source, the expected value it derived from that company's own
    comparable history, the deviation between them and the verdict that followed.
    It is a measurement, so it is *not* marked -- the model may quote these numbers,
    and only these.

    The statistics behind the verdict (median, MAD, modified z-score, reference
    dates and values) are deliberately absent. They are the approver's evidence,
    exposed on the detection API to callers holding ``kpi.read``; a Copilot answer
    is a business explanation, and putting them here would leak the technical
    surface into every reply.
    """

    unit = f" {run.currency}" if run.currency else ""

    def figure(value: float | None) -> str:
        return "not available" if value is None else f"{value:,.2f}{unit}"

    deviation = (
        "not available"
        if run.deviation_pct is None
        else f"{run.deviation_pct:+.2f}% ({figure(run.deviation_absolute)})"
    )
    return {
        "source_type": "detection_run",
        "source_id": run.id,
        "company_id": run.company_id,
        "title": (
            f"Detection result for {run.kpi_name} on {run.target_date.isoformat()}: "
            f"{run.status}"
        ),
        "is_placeholder": False,
        "content": (
            f"On {run.target_date.isoformat()}, {run.kpi_name} (v{run.kpi_version}) was "
            f"measured at {figure(run.actual_value)} against an expected "
            f"{figure(run.expected_value)}, a deviation of {deviation}. The engine "
            f"classified this as {run.status}. "
            + (f"{run.comparison_label} were used as the comparable history. " if run.comparison_label else "")
            + (run.headline or "")
        ).strip(),
        "metadata": {
            "kpi_key": run.kpi_key,
            "kpi_version": run.kpi_version,
            "target_date": run.target_date.isoformat(),
            "status": run.status,
            "run_id": run.id,
            "executed_at": run.executed_at.isoformat() if run.executed_at else None,
        },
    }


def contribution_run_evidence(run: ContributionRun) -> dict[str, Any]:
    """One stored contribution analysis, as the evidence behind a breakdown.

    This is the arithmetic the investigation screen showed: the movement detection
    measured, split across one approved dimension, with each part's own actual,
    expected value and signed share. Like a detection run it is a measurement, so
    it is not marked -- the model may quote these shares, and only these.

    The content is written to be hard to misread, because the misreading is
    predictable: a large share is a large part of the business, not a finding about
    it. So the text says which part accounts for most of the movement, states in
    the same breath that this is not a judgement about that part, and offers the
    associative vocabulary the prompt rules already require in place of causal
    language. There is no per-contributor status here because there is none in the
    table -- entity-level anomaly detection is a separate, on-demand analysis.

    ``unexplained_pct`` travels with the shares deliberately. A breakdown that
    reconciles to 78% of a movement is a different fact from one that reconciles to
    all of it, and an explanation that quotes the leader without that number is
    overstating what the arithmetic supports.
    """

    unit = f" {run.currency}" if run.currency else ""

    def figure(value: float | None) -> str:
        return "not available" if value is None else f"{value:,.2f}{unit}"

    def share(value: float | None) -> str:
        return "share not available" if value is None else f"{value:+.1f}% of the movement"

    where = (
        " within " + ", ".join(f"{step['dimension']} = {step['value']}" for step in run.path)
        if run.path
        else ""
    )
    ranked = [
        f"{row.get('label')}: {figure(row.get('change'))} ({share(row.get('share_pct'))}), "
        f"measured {figure(row.get('actual'))} against an expected "
        f"{figure(row.get('expected'))}"
        for row in (run.contributors or [])
    ]
    lines = [
        f"On {run.target_date.isoformat()}, {run.kpi_name} (v{run.kpi_version}) moved "
        f"{figure(run.kpi_movement)} against its expected {figure(run.kpi_expected)}"
        f"{where}. Broken down by {run.dimension}, the parts are:",
        *(f"- {line}" for line in ranked),
    ]
    if run.leader_entity is not None:
        lines.append(
            f"{run.leader_entity} accounts for the largest share of the movement. That is "
            "a share of an amount, not a verdict about that part of the business: no "
            "anomaly detection has been run on it, and it carries no status. Describe it "
            "as accounting for, or being associated with, the movement -- never as its "
            "cause."
        )
    if run.explained_pct is not None:
        lines.append(
            f"The listed parts account for {run.explained_pct:.1f}% of the movement"
            + (
                f", leaving {run.unexplained_pct:.1f}% the breakdown does not reconcile."
                if run.unexplained_pct
                else "."
            )
        )
    if not run.shares_available:
        lines.append(
            "Shares are unavailable for this KPI, so the parts are reported as amounts "
            "only and no percentage of the movement may be stated."
        )
    if run.withheld_count:
        lines.append(
            f"{run.withheld_count} value(s) of {run.dimension} are outside the reader's "
            "data scope and are not listed, which is part of why the shares may not sum "
            "to the whole."
        )
    for warning in run.warnings or ():
        lines.append(str(warning))

    return {
        "source_type": "contribution_analysis",
        "source_id": run.id,
        "company_id": run.company_id,
        "title": (
            f"Contribution of {run.dimension} to {run.kpi_name} on "
            f"{run.target_date.isoformat()}"
        ),
        "is_placeholder": False,
        "content": "\n".join(lines),
        "metadata": {
            "kpi_key": run.kpi_key,
            "kpi_version": run.kpi_version,
            "target_date": run.target_date.isoformat(),
            "dimension": run.dimension,
            "path": run.path,
            "kpi_status": run.kpi_status,
            "ranked_count": run.ranked_count,
            "top_k": run.top_k,
            "leader_entity": run.leader_entity,
            "leader_share_pct": run.leader_share_pct,
            "explained_pct": run.explained_pct,
            "unexplained_pct": run.unexplained_pct,
            "withheld_by_scope": run.withheld_count,
            "run_id": run.id,
            "executed_at": run.executed_at.isoformat() if run.executed_at else None,
        },
    }


def no_contribution_notice(
    company_id: str,
    *,
    kpi_name: str | None,
    selected_date,
    dimension: str | None = None,
) -> dict[str, Any]:
    """The honest answer when no breakdown has been run for what is on screen.

    Contribution analysis is on demand, by design -- nothing apportions every KPI
    across every dimension on a schedule. So "no breakdown exists yet" is the
    normal state, not a fault, and the model has to say it rather than estimate
    shares from a total it can see. Marked as not-a-measurement for exactly that
    reason.
    """

    subject = kpi_name or "the KPI in view"
    when = f" on {selected_date}" if selected_date else ""
    by = f" by {dimension}" if dimension else ""
    return {
        "source_type": "contribution_analysis",
        "source_id": None,
        "company_id": company_id,
        "title": "No contribution breakdown is stored for this view",
        "is_placeholder": True,
        "content": (
            f"The platform holds no stored contribution analysis for {subject}{by}{when}, "
            "so there are no measured shares for any part of the business. Contribution "
            "analysis runs on request rather than continuously, so this is expected until "
            "someone runs it from the investigation screen. Do not state, rank or "
            "estimate any part's share of this movement."
        ),
        "metadata": {
            "applies_to": subject,
            "dimension": dimension,
            "date": str(selected_date) if selected_date else None,
        },
    }


def no_detection_run_notice(
    company_id: str, *, kpi_name: str | None, selected_date
) -> dict[str, Any]:
    """The honest answer when the screen shows a date nothing was evaluated for.

    A question about a figure has to be answerable or refused, and those are the
    only two options: the platform either stored a detection result for that KPI
    and date or it did not. Marked as not-a-measurement so the prompt rules force
    the model to say the number is unavailable rather than reach for the nearest
    thing that looks like one.
    """

    subject = kpi_name or "the KPIs in view"
    when = f" on {selected_date}" if selected_date else ""
    return {
        "source_type": "detection_run",
        "source_id": None,
        "company_id": company_id,
        "title": "No detection result is stored for this date",
        "is_placeholder": True,
        "content": (
            f"The platform holds no stored detection run for {subject}{when}, so no "
            "actual, expected, deviation or status figure is available for it. Values "
            "of that kind exist only where the detection engine evaluated the KPI at "
            "its registered source -- running the agent for that date would produce "
            "them. Do not state or estimate any figure for this date."
        ),
        "metadata": {"applies_to": subject, "date": str(selected_date) if selected_date else None},
    }
