"""The authenticated, company-scoped context every Copilot operation runs inside.

This is the security boundary of the Copilot. Two rules it exists to enforce:

**Company scope arrives already proven.** ``access`` was built by
``app.core.deps``, which walked JWT -> user -> membership -> role -> permissions
against the database. The ``company_id`` in the URL was an assertion by the
caller and has already been checked against a real membership row. Nothing here
re-reads it, and no tool takes a company argument, so the model has no way to
name a company at all -- let alone a different one.

**Client-supplied context is a hint, not a fact.** The frontend passes what the
user is looking at: a panel, a KPI, a version, a selected date, a dimension, a
selected entity, a run. Each one is re-resolved through ``load_scoped`` or the
governed registry it belongs to, so an id belonging to another company resolves to
nothing exactly as a deleted id would. When a hint cannot be resolved the context
records a note and continues; the answer then says the reference was not found
rather than quietly answering about something else.

**No measurement arrives from the client.** One panel context reaches this
platform from five different screens, and each of them is displaying figures --
an actual, an expected value, a deviation, a status. None of those are accepted
here. A client that could state the actual could state a false one and have it
explained as fact, so what the request carries is *coordinates* (which KPI, which
date, which dimension, which entity, which run) and the numbers are re-read from
the run the platform itself stored. This is why the context has no field for a
value: adding one would be the whole vulnerability.

**A dimension and an entity are entitlement decisions, not labels.** A user may
type a dimension name into the manual analysis screen, so the dimension is checked
against the KPI version's own approved dimensions, and the entity against the
membership's row scope, before either is allowed to shape a turn. Typing a
coordinate by hand must buy exactly as much as clicking one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import AccessContext, load_scoped
from app.core.errors import NotFound
from app.models.base import KpiStatus
from app.models.detection import AgentRun
from app.models.kpi import KpiDefinition, KpiDimension, KpiVersion

#: The panels one Copilot serves. A panel is not decoration: it says which
#: verified result the turn is about and therefore what the answer is allowed to
#: be, so an unrecognised value is dropped rather than passed through to the
#: model as though the platform had a view on it.
#:
#: There is one Copilot behind all of these. Separate assistants per screen would
#: each drift into their own rules about what may be asserted, which is the
#: failure this set exists to prevent.
PANELS: frozenset[str] = frozenset(
    {
        "stage_performance",
        "detection_detail",
        "historical_run",
        "investigation",
        "future_action",
        "kpi_setup",
        "monitoring",
        "dashboard",
        # The two explainability surfaces. Both are anchored on a stored result, so
        # both belong to the set that auto-attaches one as evidence.
        "kpi_result",
        "investigation_node",
    }
)


@dataclass(slots=True)
class CopilotContext:
    """Everything one Copilot turn is allowed to see, resolved from the database."""

    session: Session
    access: AccessContext
    request_id: str | None = None
    # Which panel asked. Resolved against ``PANELS``; an unknown value becomes
    # ``None`` and a note, never a claim about what the user is looking at.
    panel: str | None = None
    # Resolved from the client hints below. ``None`` means "not resolvable in
    # this company", which is reported, not worked around.
    kpi_definition: KpiDefinition | None = None
    kpi_version: KpiVersion | None = None
    selected_date: date | None = None
    # An approved dimension of this KPI version, and a value within it. The
    # dimension is a governed row, not a string: what survives here has passed
    # the KPI's own dimension registry. The entity stays a string because only
    # the company's database knows its values -- but it has passed row scope.
    dimension: KpiDimension | None = None
    selected_entity: str | None = None
    # The agent run whose stored results are on screen, company-scoped.
    agent_run: AgentRun | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def company_id(self) -> str:
        return self.access.company.id

    @property
    def company_name(self) -> str:
        return self.access.company.company_name

    @property
    def role_key(self) -> str:
        return self.access.role.role_key

    @property
    def agent_run_id(self) -> str | None:
        """The resolved run's id -- absent when the hint resolved to nothing."""
        return self.agent_run.id if self.agent_run else None

    @property
    def dimension_name(self) -> str | None:
        return self.dimension.dimension_name if self.dimension else None

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def describe(self) -> dict[str, object]:
        """The resolved context, for the response body and the model prompt.

        Only resolved values appear. An unresolved hint shows up in ``notes``,
        so a stale KPI id from the frontend cannot be mistaken for a real one.
        """
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "role": self.role_key,
            "panel": self.panel,
            "kpi_definition_id": self.kpi_definition.id if self.kpi_definition else None,
            "kpi_key": self.kpi_definition.kpi_key if self.kpi_definition else None,
            "kpi_name": self.kpi_definition.name if self.kpi_definition else None,
            "kpi_version_id": self.kpi_version.id if self.kpi_version else None,
            "kpi_version": self.kpi_version.version if self.kpi_version else None,
            "selected_date": self.selected_date.isoformat() if self.selected_date else None,
            "dimension": self.dimension_name,
            "selected_entity": self.selected_entity,
            "agent_run_id": self.agent_run_id,
            "notes": list(self.notes),
        }


def _resolve_kpi(
    session: Session, access: AccessContext, kpi_id: str
) -> KpiDefinition | None:
    """Accept either a definition id or a business KPI key, company-scoped.

    ``load_scoped`` raises the same ``NotFound`` for "does not exist" and
    "belongs to another company", which is what keeps an id from a neighbouring
    tenant from confirming that tenant's existence.
    """
    try:
        return load_scoped(session, KpiDefinition, kpi_id, access)
    except NotFound:
        pass
    return session.scalar(
        select(KpiDefinition).where(
            KpiDefinition.company_id == access.company.id,
            KpiDefinition.kpi_key == kpi_id,
        )
    )


def _resolve_version(
    definition: KpiDefinition, requested: int | str | None
) -> KpiVersion | None:
    """Pick the version being discussed: the requested one, else what is live."""
    versions = list(definition.versions)
    if not versions:
        return None
    if requested is not None:
        try:
            number = int(requested)
        except (TypeError, ValueError):
            return None
        return next((v for v in versions if v.version == number), None)
    return next(
        (v for v in versions if v.status == KpiStatus.ACTIVE),
        max(versions, key=lambda v: v.version),
    )


def _resolve_panel(context: CopilotContext, panel: str | None) -> None:
    """Normalise the panel the question came from, or record that it was unknown."""
    if not panel:
        return
    key = str(panel).strip().lower().replace("-", "_").replace(" ", "_")
    if key in PANELS:
        context.panel = key
        return
    context.note(
        f"The screen identified itself as '{panel}', which this platform does not "
        "recognise, so the answer is not shaped to any particular panel."
    )


def _resolve_dimension(
    context: CopilotContext, dimension: str | None
) -> None:
    """Match a dimension hint against this KPI version's approved dimensions.

    Three ways this can fail, and each is reported rather than ignored, because a
    question about a slice the platform does not govern must not be answered as
    though it did: no KPI version in context, no dimension of that name declared
    on it, or one declared but not approved for analysis.
    """
    if not dimension:
        return
    wanted = str(dimension).strip().lower()
    if not wanted:
        return
    if context.kpi_version is None:
        context.note(
            f"A '{dimension}' breakdown was referenced, but no KPI version is in "
            "context to approve a dimension against."
        )
        return

    declared = context.session.scalars(
        select(KpiDimension).where(KpiDimension.kpi_version_id == context.kpi_version.id)
    ).all()
    match = next(
        (d for d in declared if d.dimension_name.strip().lower() == wanted), None
    )
    if match is None:
        context.note(
            f"'{dimension}' is not a declared dimension of this KPI version, so it "
            "is not a slice this platform can analyse."
        )
        return
    if not match.allowed:
        context.note(
            f"The '{match.dimension_name}' dimension is declared on this KPI but not "
            "approved for analysis, so it was not used."
        )
        return
    context.dimension = match


def _resolve_entity(context: CopilotContext, entity: str | None) -> None:
    """Admit a selected entity only within an approved dimension and the row scope.

    The entity is the one piece of context the platform cannot verify -- its
    values live in the company's own database, and confirming one would mean
    running a query to answer a question nobody asked. So it is carried as *what
    the user selected*, never as a fact about the data, and it is still gated:
    ``permits_scope_value`` is the same check a grouped read applies, so a user
    who types a region they are not entitled to gets the same refusal whether they
    typed it or clicked it.
    """
    if not entity:
        return
    value = str(entity).strip()
    if not value:
        return
    if context.dimension is None:
        context.note(
            f"A selected value ('{value}') was supplied without an approved "
            "dimension to interpret it in, so it was not used."
        )
        return
    if not context.access.permits_scope_value(context.dimension.dimension_name, value):
        context.note(
            f"Your access does not extend to '{value}' within "
            f"{context.dimension.dimension_name}, so the answer does not cover it."
        )
        return
    context.selected_entity = value


def _resolve_agent_run(context: CopilotContext, agent_run_id: str | None) -> None:
    """Resolve the run whose stored results are on screen, inside this company.

    Historical runs are how this platform stays reproducible: an answer about a
    past date must come from the run that produced it, not from a fresh
    calculation. ``load_scoped`` gives a run from another company the same
    ``NotFound`` as a deleted one.
    """
    if not agent_run_id:
        return
    try:
        context.agent_run = load_scoped(context.session, AgentRun, str(agent_run_id), context.access)
    except NotFound:
        context.note(
            "The run referenced by the current screen was not found in this company, "
            "so the answer does not rely on it."
        )


def build_context(
    session: Session,
    access: AccessContext,
    *,
    request_id: str | None = None,
    panel: str | None = None,
    kpi_id: str | None = None,
    kpi_version: int | str | None = None,
    selected_date: str | date | None = None,
    dimension: str | None = None,
    selected_entity: str | None = None,
    agent_run_id: str | None = None,
) -> CopilotContext:
    """Resolve the client's context hints inside the caller's own company.

    Order matters: the KPI resolves before the dimension, because a dimension is
    only approved relative to a KPI version, and the dimension resolves before the
    entity, because an entity means nothing without the coordinate it belongs to.
    A hint that fails leaves the ones that depended on it unresolved and noted,
    rather than half-applied.
    """
    context = CopilotContext(session=session, access=access, request_id=request_id)
    _resolve_panel(context, panel)

    if kpi_id:
        definition = _resolve_kpi(session, access, kpi_id)
        if definition is None:
            context.note(
                "The KPI referenced by the current screen was not found in this "
                "company, so the answer does not assume one."
            )
        else:
            context.kpi_definition = definition
            version = _resolve_version(definition, kpi_version)
            if version is None:
                context.note(
                    f"{definition.name} has no version "
                    f"{kpi_version if kpi_version is not None else 'on record'}."
                )
            else:
                context.kpi_version = version

    if selected_date:
        if isinstance(selected_date, date):
            context.selected_date = selected_date
        else:
            try:
                context.selected_date = date.fromisoformat(str(selected_date)[:10])
            except ValueError:
                context.note(f"Ignored an unreadable selected date: {selected_date!r}.")

    _resolve_dimension(context, dimension)
    _resolve_entity(context, selected_entity)
    _resolve_agent_run(context, agent_run_id)

    return context
