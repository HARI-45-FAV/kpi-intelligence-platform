"""KPI governance: lifecycle, versioning, lineage and approval.

The lifecycle is enforced by a transition table, not by trust:

    DRAFT -> PROPOSED -> UNDER_REVIEW -> APPROVED -> ACTIVE -> DEPRECATED

Two invariants matter more than the rest:

* **An ACTIVE version is never edited.** Editing produces v(n+1) in DRAFT. An
  insight emitted last month keeps pointing at the exact definition that produced
  it, which is the only way "why did this number change?" stays answerable.
* **Approval requires a passing validation run.** The approver can override
  advisory warnings — that is their job — but cannot activate a KPI whose
  blocking checks fail.

Lineage is regenerated from the formula contract on every save, so it cannot
drift away from the calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.deps import AccessContext
from app.core.errors import Conflict, NotFound, ValidationFailure
from app.core.permissions import ADMIN_ROLE_KEY
from app.models.base import (
    KPI_TRANSITIONS,
    DriverType,
    KpiKind,
    KpiStatus,
    TimeGrain,
    ValidationStatus,
)
from app.models.kpi import (
    KpiAccessPolicy,
    KpiDefinition,
    KpiDimension,
    KpiDriver,
    KpiLineage,
    KpiMaterialityRule,
    KpiVersion,
)
from app.models.source import DataSource, SourceTable
from app.models.tenant import CompanyCalendar
from app.services.kpi_discovery import KpiProposal
from app.services.kpi_formula import FormulaSpec, lineage_entries, parse_formula
from app.services.kpi_source_definitions import CompanyKpiDefinition

# Default entitlement applied when an administrator does not specify one.
# Deliberately conservative: everything below ADMIN starts scoped or aggregate-only.
DEFAULT_ACCESS_POLICIES: tuple[dict[str, Any], ...] = (
    {"role_key": "ADMIN", "allowed": True},
    {"role_key": "EXECUTIVE", "allowed": True},
    {"role_key": "ANALYST", "allowed": True},
    {"role_key": "MANAGER", "allowed": True},
    {"role_key": "REGIONAL_MANAGER", "allowed": True, "row_scope": {"mode": "SELF_SCOPE"}},
    {"role_key": "VIEWER", "allowed": True, "aggregate_only": True},
)

DEFAULT_MATERIALITY: dict[str, Any] = {
    "relative_threshold_pct": 5.0,
    "statistical_rule": "abs_z_score>2",
    "business_criticality": "MEDIUM",
    "persistence_periods": 1,
    "priority_policy": "relative_and_absolute_both_required",
}


@dataclass(slots=True)
class KpiWritePayload:
    """Everything needed to author or revise a KPI version."""

    name: str
    business_definition: str
    formula_expression: str
    source_table_id: str
    time_field: str | None = None
    time_grain: str = TimeGrain.DAY
    kpi_key: str | None = None
    purpose: str | None = None
    unit: str | None = None
    currency: str | None = None
    direction: str = "HIGHER_IS_BETTER"
    null_handling: str = "TREAT_AS_ZERO"
    filters: list[dict[str, Any]] | None = None
    calendar_id: str | None = None
    timezone: str | None = None
    dimensions: list[dict[str, Any]] | None = None
    drivers: list[dict[str, Any]] | None = None
    materiality: dict[str, Any] | None = None
    access_policies: list[dict[str, Any]] | None = None
    expected_baseline_method: str = "NOT_CONFIGURED"
    seasonality_expectation: str | None = None
    sparse_history_strategy: str = "PEER_BASELINE"
    min_history_days: int | None = None
    definition_document_id: str | None = None
    definition_document_version: int | None = None
    definition_source: str | None = None
    owner_user_id: str | None = None


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def create_kpi(
    session: Session,
    access: AccessContext,
    payload: KpiWritePayload,
    *,
    origin: str = "MANUAL",
    discovery_evidence: dict[str, Any] | None = None,
    initial_status: str = KpiStatus.DRAFT,
) -> tuple[KpiDefinition, KpiVersion]:
    key = _slug(payload.kpi_key or payload.name)
    existing = session.scalar(
        select(KpiDefinition).where(
            KpiDefinition.company_id == access.company.id, KpiDefinition.kpi_key == key
        )
    )
    if existing is not None:
        raise Conflict(
            f"A KPI with key '{key}' already exists in this company. "
            "Create a new version of it instead.",
            details={"kpi_id": existing.id, "kpi_key": key},
        )

    definition = KpiDefinition(
        company_id=access.company.id,
        kpi_key=key,
        name=payload.name,
        short_description=payload.business_definition[:400],
        status=initial_status,
        current_version=0,
        owner_user_id=payload.owner_user_id or access.user.id,
    )
    session.add(definition)
    session.flush()

    version = _build_version(
        session,
        access,
        definition=definition,
        payload=payload,
        version_number=1,
        status=initial_status,
        origin=origin,
        discovery_evidence=discovery_evidence,
    )
    definition.current_version = version.version
    definition.current_version_id = version.id
    return (definition, version)


def create_from_proposal(
    session: Session,
    access: AccessContext,
    proposal: KpiProposal,
    *,
    overrides: dict[str, Any] | None = None,
) -> tuple[KpiDefinition, KpiVersion]:
    """Accept a discovery proposal as a PROPOSED version awaiting review.

    Discovery output never lands as ACTIVE — an administrator still owns the
    business meaning.
    """
    merged = {
        "name": proposal.name,
        "kpi_key": proposal.kpi_key,
        "business_definition": proposal.business_definition,
        "formula_expression": proposal.formula_expression,
        "source_table_id": proposal.source_table_id,
        "time_field": proposal.time_field,
        "time_grain": proposal.time_grain,
        "unit": proposal.unit,
        "direction": proposal.direction,
        "dimensions": [d.as_dict() for d in proposal.dimensions],
        "drivers": [d.as_dict() for d in proposal.drivers],
        "materiality": {
            **DEFAULT_MATERIALITY,
            "business_criticality": proposal.business_criticality,
        },
        "definition_source": "Discovery proposal (platform-generated, admin reviewed)",
        **(overrides or {}),
    }
    payload = KpiWritePayload(**{k: v for k, v in merged.items() if k in KpiWritePayload.__annotations__})
    return create_kpi(
        session,
        access,
        payload,
        origin="DISCOVERY",
        discovery_evidence={
            "confidence": proposal.confidence,
            "warnings": proposal.warnings,
            **proposal.evidence,
        },
        initial_status=KpiStatus.PROPOSED,
    )


def create_from_company_definition(
    session: Session,
    access: AccessContext,
    definition: CompanyKpiDefinition,
    *,
    overrides: dict[str, Any] | None = None,
) -> tuple[KpiDefinition, KpiVersion]:
    """Turn a company-authored KPI definition into a governed contract.

    The company's registry is the authority on business meaning, so the imported
    version carries the company's own name, definition and formula verbatim. It
    still lands in PROPOSED rather than ACTIVE: the deterministic engine has to
    prove the definition actually works against the connected data before anyone
    can rely on the number, and that proof is the validation run.
    """
    if definition.source_table_id is None:
        raise ValidationFailure(
            f"'{definition.name}' has not been bound to a discovered table, so it "
            "cannot become a contract yet.",
            details={"kpi_key": definition.kpi_key, "issues": definition.issues},
        )

    materiality = dict(DEFAULT_MATERIALITY)
    if definition.materiality_threshold_pct is not None:
        materiality["relative_threshold_pct"] = definition.materiality_threshold_pct

    merged = {
        "name": definition.name,
        "kpi_key": definition.kpi_key,
        "business_definition": definition.business_definition,
        "formula_expression": definition.formula_expression,
        "source_table_id": definition.source_table_id,
        "time_field": definition.time_field,
        "time_grain": definition.time_grain,
        "unit": definition.unit,
        "direction": definition.direction,
        "dimensions": definition.dimensions or None,
        "materiality": materiality,
        "definition_source": (f"Company KPI registry: {definition.source_formula}")[:200],
        **(overrides or {}),
    }
    payload = KpiWritePayload(
        **{k: v for k, v in merged.items() if k in KpiWritePayload.__annotations__}
    )
    return create_kpi(
        session,
        access,
        payload,
        origin="COMPANY",
        discovery_evidence={
            "definition_origin": "company_kpi_registry",
            "source_formula": definition.source_formula,
            "declared_grain": definition.declared_grain,
            "declared_source": definition.declared_source,
            "owner": definition.owner,
            "is_active_in_source": definition.is_active,
            "issues": definition.issues,
            "method": "deterministic read of the company KPI-definition table",
        },
        initial_status=KpiStatus.PROPOSED,
    )


def create_new_version(
    session: Session,
    access: AccessContext,
    definition: KpiDefinition,
    payload: KpiWritePayload,
) -> KpiVersion:
    """Revise a KPI by creating the next version in DRAFT.

    The current ACTIVE version keeps serving until the new one is approved.
    """
    latest = max((v.version for v in definition.versions), default=0)
    open_draft = next(
        (v for v in definition.versions if v.status in {KpiStatus.DRAFT, KpiStatus.PROPOSED, KpiStatus.UNDER_REVIEW}),
        None,
    )
    if open_draft is not None:
        raise Conflict(
            f"{definition.name} already has an open v{open_draft.version} in "
            f"{open_draft.status}. Finish or reject it before starting another.",
            details={"kpi_version_id": open_draft.id, "status": open_draft.status},
        )

    version = _build_version(
        session,
        access,
        definition=definition,
        payload=payload,
        version_number=latest + 1,
        status=KpiStatus.DRAFT,
        origin="MANUAL",
        discovery_evidence=None,
        supersedes=definition.current_version or None,
    )
    if payload.name and payload.name != definition.name:
        definition.name = payload.name
    return version


def update_version(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    payload: KpiWritePayload,
) -> KpiVersion:
    if not version.is_editable:
        raise Conflict(
            f"v{version.version} is {version.status} and cannot be edited. "
            "Create a new version instead.",
            details={"status": version.status},
        )
    _apply_payload(session, access, version, payload)
    # Any prior verdict is void once the contract changes.
    version.last_validation_status = None
    version.last_validated_at = None
    version.last_validation_run_id = None
    return version


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------
def transition(
    session: Session,
    version: KpiVersion,
    target: str,
    *,
    access: AccessContext,
    reason: str | None = None,
) -> KpiVersion:
    current = version.status
    allowed = KPI_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise Conflict(
            f"A KPI in {current} cannot move to {target}. "
            f"Permitted from {current}: {', '.join(sorted(allowed)) or 'nothing'}.",
            details={"from": current, "to": target, "allowed": sorted(allowed)},
        )

    now = utcnow()
    definition = version.definition

    if target == KpiStatus.PROPOSED:
        version.submitted_at = now

    elif target == KpiStatus.UNDER_REVIEW:
        version.reviewed_by = access.user.id
        version.reviewed_at = now

    elif target == KpiStatus.APPROVED:
        if version.last_validation_status not in {ValidationStatus.PASS, ValidationStatus.WARN}:
            raise ValidationFailure(
                "This version has not passed validation. Run validation before approving.",
                details={"last_validation_status": version.last_validation_status},
            )
        if version.created_by == access.user.id and not access.is_admin:
            # Separation of duties: an analyst may author but not self-approve.
            raise Conflict(
                "You authored this version, so it must be approved by someone else.",
                details={"author": version.created_by},
            )
        version.approved_by = access.user.id
        version.approved_at = now
        version.approval_reason = reason

    elif target == KpiStatus.ACTIVE:
        if version.approved_at is None:
            raise Conflict("A version must be APPROVED before it can be activated.")
        # Exactly one ACTIVE version per KPI.
        for sibling in definition.versions:
            if sibling.id != version.id and sibling.status == KpiStatus.ACTIVE:
                sibling.status = KpiStatus.DEPRECATED
                sibling.deprecated_at = now
        version.activated_at = now
        definition.current_version = version.version
        definition.current_version_id = version.id

    elif target == KpiStatus.REJECTED:
        if not reason:
            raise ValidationFailure("A rejection reason is required.")
        version.rejection_reason = reason

    elif target == KpiStatus.DEPRECATED:
        version.deprecated_at = now

    version.status = target
    definition.status = _definition_status(definition, version, target)
    return version


def _definition_status(
    definition: KpiDefinition, version: KpiVersion, target: str
) -> str:
    """The definition reflects the most advanced state among its versions."""
    if target == KpiStatus.ACTIVE:
        return KpiStatus.ACTIVE
    if any(v.status == KpiStatus.ACTIVE for v in definition.versions if v.id != version.id):
        # An older version is still serving, so the KPI as a whole stays ACTIVE.
        return KpiStatus.ACTIVE
    return target


# ---------------------------------------------------------------------------
# Version construction
# ---------------------------------------------------------------------------
def _build_version(
    session: Session,
    access: AccessContext,
    *,
    definition: KpiDefinition,
    payload: KpiWritePayload,
    version_number: int,
    status: str,
    origin: str,
    discovery_evidence: dict[str, Any] | None,
    supersedes: int | None = None,
) -> KpiVersion:
    version = KpiVersion(
        company_id=access.company.id,
        kpi_id=definition.id,
        version=version_number,
        status=status,
        business_definition=payload.business_definition,
        formula_expression=payload.formula_expression,
        proposal_origin=origin,
        discovery_evidence=discovery_evidence or {},
        created_by=access.user.id,
        supersedes_version=supersedes,
    )
    session.add(version)
    session.flush()
    _apply_payload(session, access, version, payload)
    return version


def _apply_payload(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    payload: KpiWritePayload,
) -> None:
    table = session.get(SourceTable, payload.source_table_id)
    if table is None or table.company_id != access.company.id:
        raise NotFound(f"Source table {payload.source_table_id} was not found.")
    if table.selection is None or not table.selection.enabled:
        raise ValidationFailure(
            f"{table.qualified_name} is not in the approved data scope, so a KPI "
            "cannot be bound to it.",
            details={"source_table_id": table.id},
        )

    spec = parse_formula(
        payload.formula_expression,
        default_table=table.table_name,
        filters=payload.filters,
        null_handling=payload.null_handling,
    )

    source = session.get(DataSource, table.data_source_id)
    calendar = _resolve_calendar(session, access, payload.calendar_id)

    version.business_definition = payload.business_definition
    version.purpose = payload.purpose
    version.unit = payload.unit
    version.currency = payload.currency or (access.company.currency if payload.unit == "currency" else None)
    version.direction = payload.direction
    version.kind = spec.kind
    # Canonical rendering, so the display string always matches what executes.
    version.formula_expression = spec.render()
    version.formula_spec = spec.as_dict()
    version.aggregation = spec.numerator.effective_aggregation
    version.numerator = spec.numerator.as_dict()
    version.denominator = spec.denominator.as_dict() if spec.denominator else None
    version.filters = [f.as_dict() for f in spec.filters]
    version.null_handling = payload.null_handling

    version.primary_data_source_id = table.data_source_id
    version.primary_source_table_id = table.id
    version.source_definition = {
        "data_source": source.name if source else None,
        "data_source_type": source.source_type if source else None,
        "schema": table.schema_name,
        "table": table.table_name,
        "qualified_name": table.qualified_name,
    }

    version.time_field = payload.time_field
    version.time_grain = payload.time_grain or TimeGrain.DAY
    version.calendar_id = calendar.id if calendar else None
    version.timezone = payload.timezone or (calendar.timezone if calendar else access.company.timezone)

    version.expected_baseline_method = payload.expected_baseline_method
    version.seasonality_expectation = payload.seasonality_expectation
    version.sparse_history_strategy = payload.sparse_history_strategy
    version.min_history_days = payload.min_history_days

    version.definition_document_id = payload.definition_document_id
    version.definition_document_version = payload.definition_document_version
    version.definition_source = payload.definition_source

    _replace_dimensions(session, access, version, payload.dimensions, default_table=table)
    _replace_drivers(session, access, version, payload.drivers, default_table=table)
    _replace_materiality(session, access, version, payload.materiality)
    _replace_access_policies(session, access, version, payload.access_policies)
    rebuild_lineage(session, version, spec=spec, table=table, source=source)


def _resolve_calendar(
    session: Session, access: AccessContext, calendar_id: str | None
) -> CompanyCalendar | None:
    if calendar_id:
        calendar = session.get(CompanyCalendar, calendar_id)
        if calendar is None or calendar.company_id != access.company.id:
            raise NotFound(f"Calendar {calendar_id} was not found.")
        return calendar
    return session.scalar(
        select(CompanyCalendar).where(
            CompanyCalendar.company_id == access.company.id,
            CompanyCalendar.is_default.is_(True),
        )
    )


def _replace_dimensions(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    payloads: list[dict[str, Any]] | None,
    *,
    default_table: SourceTable,
) -> None:
    for existing in list(version.dimensions):
        session.delete(existing)
    version.dimensions.clear()
    session.flush()

    for item in payloads or []:
        table_id = item.get("source_table_id") or default_table.id
        table = session.get(SourceTable, table_id)
        if table is None or table.company_id != access.company.id:
            raise NotFound(f"Dimension source table {table_id} was not found.")
        column = item.get("source_column")
        if not column:
            raise ValidationFailure(
                f"Dimension '{item.get('dimension_name')}' needs a source column."
            )
        # Appended through the relationship so the in-memory collection is
        # correct for the lineage rebuild that follows in the same call.
        version.dimensions.append(
            KpiDimension(
                company_id=access.company.id,
                dimension_name=str(item.get("dimension_name") or column).lower(),
                source_table_id=table.id,
                source_table=table.table_name,
                source_column=column,
                hierarchy=item.get("hierarchy") or [],
                allowed=bool(item.get("allowed", True)),
                is_default_breakdown=bool(item.get("is_default_breakdown", False)),
                approx_cardinality=item.get("approx_cardinality"),
                notes=item.get("notes"),
            )
        )
    session.flush()


def _replace_drivers(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    payloads: list[dict[str, Any]] | None,
    *,
    default_table: SourceTable,
) -> None:
    for existing in list(version.drivers):
        session.delete(existing)
    version.drivers.clear()
    session.flush()

    seen: set[str] = set()
    for item in payloads or []:
        name = str(item.get("driver_name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        table_id = item.get("source_table_id")
        table = session.get(SourceTable, table_id) if table_id else None
        if table is not None and table.company_id != access.company.id:
            raise NotFound(f"Driver source table {table_id} was not found.")
        version.drivers.append(
            KpiDriver(
                company_id=access.company.id,
                driver_name=name,
                driver_type=item.get("driver_type") or DriverType.OTHER,
                source_table_id=table.id if table else None,
                source_table=table.table_name if table else item.get("source_table"),
                source_column=item.get("source_column"),
                controllable=bool(item.get("controllable", False)),
                measurement_method=item.get("measurement_method"),
                notes=item.get("notes"),
            )
        )
    session.flush()


def _replace_materiality(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    payload: dict[str, Any] | None,
) -> None:
    values = {**DEFAULT_MATERIALITY, **(payload or {})}
    rule = version.materiality
    if rule is None:
        rule = KpiMaterialityRule(company_id=access.company.id, kpi_version_id=version.id)
        session.add(rule)
        session.flush()
        version.materiality = rule

    relative = values.get("relative_threshold_pct")
    absolute = values.get("absolute_threshold")
    if relative is not None and float(relative) <= 0:
        raise ValidationFailure("Relative materiality threshold must be positive.")
    if absolute is not None and float(absolute) < 0:
        raise ValidationFailure("Absolute materiality threshold cannot be negative.")

    rule.relative_threshold_pct = relative
    rule.absolute_threshold = absolute
    rule.statistical_rule = values.get("statistical_rule")
    rule.business_criticality = values.get("business_criticality") or "MEDIUM"
    rule.priority_policy = values.get("priority_policy")
    rule.persistence_periods = int(values.get("persistence_periods") or 1)
    rule.notes = values.get("notes")


def _replace_access_policies(
    session: Session,
    access: AccessContext,
    version: KpiVersion,
    payloads: list[dict[str, Any]] | None,
) -> None:
    for existing in list(version.access_policies):
        session.delete(existing)
    version.access_policies.clear()
    session.flush()

    items = list(payloads) if payloads else [dict(p) for p in DEFAULT_ACCESS_POLICIES]
    # ADMIN must always retain access or the KPI becomes ungovernable.
    if not any(item.get("role_key") == ADMIN_ROLE_KEY for item in items):
        items.insert(0, {"role_key": ADMIN_ROLE_KEY, "allowed": True})

    seen: set[str] = set()
    for item in items:
        role_key = str(item.get("role_key") or "").upper()
        if not role_key or role_key in seen:
            continue
        seen.add(role_key)
        version.access_policies.append(
            KpiAccessPolicy(
                company_id=access.company.id,
                role_key=role_key,
                allowed=bool(item.get("allowed", True)),
                row_scope=item.get("row_scope") or {},
                column_scope=item.get("column_scope") or [],
                domain_scope=item.get("domain_scope") or [],
                aggregate_only=bool(item.get("aggregate_only", False)),
                notes=item.get("notes"),
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------
def rebuild_lineage(
    session: Session,
    version: KpiVersion,
    *,
    spec: FormulaSpec | None = None,
    table: SourceTable | None = None,
    source: DataSource | None = None,
) -> list[KpiLineage]:
    """Regenerate lineage from the formula contract."""
    for existing in list(version.lineage):
        session.delete(existing)
    version.lineage.clear()
    session.flush()

    table = table or (
        session.get(SourceTable, version.primary_source_table_id)
        if version.primary_source_table_id
        else None
    )
    if table is None:
        return []
    source = source or session.get(DataSource, table.data_source_id)
    spec = spec or FormulaSpec.from_dict(version.formula_spec)

    entries = lineage_entries(
        spec,
        default_table=table.table_name,
        time_field=version.time_field,
        dimensions=[
            (d.dimension_name, d.source_table, d.source_column) for d in version.dimensions
        ],
        drivers=[(d.driver_name, d.source_table, d.source_column) for d in version.drivers],
    )

    by_table = {table.table_name.lower(): table}
    for dimension in version.dimensions:
        if dimension.source_table:
            resolved = session.get(SourceTable, dimension.source_table_id) if dimension.source_table_id else None
            if resolved is not None:
                by_table[dimension.source_table.lower()] = resolved

    records: list[KpiLineage] = []
    for entry in entries:
        resolved_table = by_table.get((entry.table or "").lower(), table)
        record = KpiLineage(
            company_id=version.company_id,
            role=entry.role,
            data_source_id=resolved_table.data_source_id,
            data_source_name=source.name if source else None,
            source_table_id=resolved_table.id,
            schema_name=resolved_table.schema_name,
            table_name=resolved_table.table_name,
            column_name=entry.column,
            transformation=entry.transformation,
            notes=entry.notes,
        )
        # Through the relationship: export_contract reads version.lineage in the
        # same unit of work, so the collection has to reflect these rows.
        version.lineage.append(record)
        records.append(record)
    session.flush()
    return records


# ---------------------------------------------------------------------------
# Contract export (what Sprint 2 consumes)
# ---------------------------------------------------------------------------
def export_contract(session: Session, version: KpiVersion) -> dict[str, Any]:
    """The complete governed contract for one KPI version.

    Sprint 2 should never have to ask "what is Revenue?" — it reads this.
    """
    definition = version.definition
    materiality = version.materiality
    calendar = session.get(CompanyCalendar, version.calendar_id) if version.calendar_id else None

    return {
        "company_id": version.company_id,
        "kpi_id": definition.kpi_key,
        "kpi_definition_id": definition.id,
        "kpi_version_id": version.id,
        "name": definition.name,
        "version": version.version,
        "status": version.status,
        "business_definition": version.business_definition,
        "purpose": version.purpose,
        "kind": version.kind,
        "formula": version.formula_expression,
        "formula_spec": version.formula_spec,
        "aggregation": version.aggregation,
        "numerator": version.numerator,
        "denominator": version.denominator,
        "filters": version.filters,
        # Stated explicitly because getting it wrong is a silent error: a ratio
        # summed across periods, or an average of averages, produces a number
        # that looks plausible and is wrong. Sprint 2 must recompute these from
        # their components at every level of aggregation.
        "is_additive": _is_additive(version),
        "additivity_note": (
            "Additive: safe to sum across periods and dimensions."
            if _is_additive(version)
            else "NOT additive: recompute from numerator and denominator at each "
            "level of aggregation. Never sum or average this value."
        ),
        "unit": version.unit,
        "currency": version.currency,
        "direction": version.direction,
        "null_handling": version.null_handling,
        "time_field": version.time_field,
        "time_grain": version.time_grain,
        "timezone": version.timezone,
        "calendar": (
            {
                "calendar_key": calendar.calendar_key,
                "timezone": calendar.timezone,
                "week_start_day": calendar.week_start_day,
                "fiscal_year_start_month": calendar.fiscal_year_start_month,
            }
            if calendar
            else None
        ),
        "source": version.source_definition,
        "dimensions": [
            {
                "name": d.dimension_name,
                "table": d.source_table,
                "column": d.source_column,
                "allowed": d.allowed,
                "is_default_breakdown": d.is_default_breakdown,
                "approx_cardinality": d.approx_cardinality,
                # Stated explicitly: declaring a dimension authorises a
                # breakdown, it does not schedule per-entity monitoring.
                "monitoring_note": "valid breakdown; not a per-entity monitoring instruction",
            }
            for d in sorted(version.dimensions, key=lambda x: x.dimension_name)
        ],
        "drivers": [
            {
                "name": d.driver_name,
                "type": d.driver_type,
                "table": d.source_table,
                "column": d.source_column,
                "controllable": d.controllable,
                "measurement_method": d.measurement_method,
            }
            for d in sorted(version.drivers, key=lambda x: x.driver_name)
        ],
        "materiality": (
            {
                "relative_threshold_pct": materiality.relative_threshold_pct,
                "absolute_threshold": materiality.absolute_threshold,
                "statistical_rule": materiality.statistical_rule,
                "business_criticality": materiality.business_criticality,
                "priority_policy": materiality.priority_policy,
                "persistence_periods": materiality.persistence_periods,
            }
            if materiality
            else None
        ),
        "behaviour": {
            "expected_baseline_method": version.expected_baseline_method,
            "seasonality_expectation": version.seasonality_expectation,
            "sparse_history_strategy": version.sparse_history_strategy,
            "min_history_days": version.min_history_days,
        },
        "access_policy": [
            {
                "role": p.role_key,
                "allowed": p.allowed,
                "row_scope": p.row_scope,
                "column_scope": p.column_scope,
                "domain_scope": p.domain_scope,
                "aggregate_only": p.aggregate_only,
            }
            for p in sorted(version.access_policies, key=lambda x: x.role_key)
        ],
        "lineage": [
            {
                "role": item.role,
                "data_source": item.data_source_name,
                "schema": item.schema_name,
                "table": item.table_name,
                "column": item.column_name,
                "transformation": item.transformation,
            }
            for item in sorted(version.lineage, key=lambda x: (x.role, x.column_name or ""))
        ],
        "governance": {
            "proposal_origin": version.proposal_origin,
            "discovery_evidence": version.discovery_evidence,
            "definition_document_id": version.definition_document_id,
            "definition_document_version": version.definition_document_version,
            "definition_source": version.definition_source,
            "created_by": version.created_by,
            "approved_by": version.approved_by,
            "approved_at": version.approved_at.isoformat() if version.approved_at else None,
            "approval_reason": version.approval_reason,
            "activated_at": version.activated_at.isoformat() if version.activated_at else None,
            "supersedes_version": version.supersedes_version,
            "last_validation_status": version.last_validation_status,
            "last_validated_at": (
                version.last_validated_at.isoformat() if version.last_validated_at else None
            ),
        },
    }


def _is_additive(version: KpiVersion) -> bool:
    """Can this KPI's value be summed across periods and dimensions?

    Only SUM and COUNT are. A ratio is not (it must be recomputed from its
    components), and neither are AVG, MIN or MAX.
    """
    if version.kind == KpiKind.RATIO:
        return False
    aggregation = (version.aggregation or "").upper()
    return aggregation in {"SUM", "COUNT"}


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in (value or "").lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    slug = cleaned.strip("_")[:80]
    if not slug:
        raise ValidationFailure("A KPI needs a name that contains at least one letter or digit.")
    return slug
