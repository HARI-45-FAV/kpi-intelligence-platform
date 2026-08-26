"""KPI governance API: registry, discovery proposals, validation, approval,
versioning, lineage and the contract Sprint 2 consumes.

The route set mirrors the lifecycle rather than CRUD, because the lifecycle is
the product: a KPI is created, reviewed, validated, approved, activated and
eventually superseded, and each of those is an auditable act with a distinct
permission.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.registry import build_connector
from app.connectors.sql import SqlConnector
from app.core.deps import AccessContext, SessionDep, load_scoped, require_permissions
from app.core.errors import Conflict, NotFound, ValidationFailure
from app.core.telemetry import usage_of
from app.models.base import KPI_TRANSITIONS, KpiStatus, ValidationStatus
from app.models.kpi import KpiDefinition, KpiValidationRun, KpiVersion
from app.models.source import DataSource, SourceTable
from app.schemas import (
    CompanyDefinitionImport,
    KpiDefinitionOut,
    KpiPreviewRequest,
    KpiProposalAccept,
    KpiRejectRequest,
    KpiTransitionRequest,
    KpiVersionInput,
    KpiVersionSummary,
)
from app.services import audit
from app.services.kpi_discovery import propose_kpis
from app.services.kpi_formula import spec_from_stored
from app.services.kpi_governance import (
    KpiWritePayload,
    create_from_company_definition,
    create_from_proposal,
    create_kpi,
    create_new_version,
    export_contract,
    transition,
    update_version,
)
from app.services.kpi_source_definitions import (
    CompanyKpiDefinition,
    DefinitionTable,
    find_definition_tables,
    read_company_definitions,
)
from app.services.kpi_sql import execute_kpi
from app.services.kpi_validation import validate_kpi_version

router = APIRouter(tags=["kpis"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _payload_from_input(data: KpiVersionInput) -> KpiWritePayload:
    return KpiWritePayload(
        name=data.name,
        business_definition=data.business_definition,
        formula_expression=data.formula_expression,
        source_table_id=data.source_table_id,
        time_field=data.time_field,
        time_grain=data.time_grain,
        kpi_key=data.kpi_key,
        purpose=data.purpose,
        unit=data.unit,
        currency=data.currency,
        direction=data.direction,
        null_handling=data.null_handling,
        filters=[f.model_dump() for f in data.filters],
        calendar_id=data.calendar_id,
        timezone=data.timezone,
        dimensions=[d.model_dump() for d in data.dimensions],
        drivers=[d.model_dump() for d in data.drivers],
        materiality=data.materiality.model_dump() if data.materiality else None,
        access_policies=[p.model_dump() for p in data.access_policies] or None,
        expected_baseline_method=data.expected_baseline_method,
        seasonality_expectation=data.seasonality_expectation,
        sparse_history_strategy=data.sparse_history_strategy,
        min_history_days=data.min_history_days,
        definition_document_id=data.definition_document_id,
        definition_document_version=data.definition_document_version,
        definition_source=data.definition_source,
        owner_user_id=data.owner_user_id,
    )


def _version_summary(version: KpiVersion) -> KpiVersionSummary:
    return KpiVersionSummary(
        id=version.id,
        version=version.version,
        status=version.status,
        formula_expression=version.formula_expression,
        time_grain=version.time_grain,
        last_validation_status=version.last_validation_status,
        last_validated_at=version.last_validated_at,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        activated_at=version.activated_at,
        deprecated_at=version.deprecated_at,
        created_by=version.created_by,
        created_at=version.created_at,
        proposal_origin=version.proposal_origin,
    )


def _definition_out(definition: KpiDefinition) -> KpiDefinitionOut:
    return KpiDefinitionOut(
        id=definition.id,
        kpi_key=definition.kpi_key,
        name=definition.name,
        short_description=definition.short_description,
        status=definition.status,
        current_version=definition.current_version,
        current_version_id=definition.current_version_id,
        owner_user_id=definition.owner_user_id,
        created_at=definition.created_at,
        updated_at=definition.updated_at,
        versions=[
            _version_summary(v) for v in sorted(definition.versions, key=lambda x: x.version)
        ],
    )


def _load_version(session: Session, version_id: str, access: AccessContext) -> KpiVersion:
    return load_scoped(session, KpiVersion, version_id, access)


def _latest_validation(session: Session, version: KpiVersion) -> dict | None:
    run = session.scalar(
        select(KpiValidationRun)
        .where(KpiValidationRun.kpi_version_id == version.id)
        .order_by(KpiValidationRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return None
    return {
        "run_id": run.id,
        "overall_status": run.overall_status,
        "ready_for_approval": run.overall_status
        in {ValidationStatus.PASS, ValidationStatus.WARN},
        "summary": run.summary,
        "duration_ms": run.duration_ms,
        "started_at": run.started_at,
        "passed": run.passed_count,
        "failed": run.failed_count,
        "warned": run.warned_count,
        "checks": [
            {
                "test_type": check.test_type,
                "label": check.label,
                "status": check.status,
                "expected": check.expected,
                "actual": check.actual,
                "message": check.message,
                "is_blocking": check.is_blocking,
                "runtime_ms": check.runtime_ms,
                "evidence": check.evidence,
            }
            for check in sorted(run.checks, key=lambda c: c.created_at)
        ],
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@router.get("/companies/{company_id}/kpis", response_model=list[KpiDefinitionOut])
def list_kpis(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> list[KpiDefinitionOut]:
    definitions = session.scalars(
        select(KpiDefinition)
        .where(KpiDefinition.company_id == access.company.id)
        .order_by(KpiDefinition.name)
    )
    return [_definition_out(definition) for definition in definitions]


@router.post(
    "/companies/{company_id}/kpis",
    response_model=KpiDefinitionOut,
    status_code=status.HTTP_201_CREATED,
)
def register_kpi(
    payload: KpiVersionInput,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.create")),
) -> KpiDefinitionOut:
    """Manual registration. Lands in DRAFT — never ACTIVE."""
    definition, version = create_kpi(session, access, _payload_from_input(payload))
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_CREATED,
        resource_type="kpi",
        resource_id=definition.id,
        resource_label=definition.name,
        summary=f"Created {definition.name} v{version.version} as {version.status}.",
        new_version=str(version.version),
        details={
            "kpi_key": definition.kpi_key,
            "formula": version.formula_expression,
            "source": version.source_definition,
            "origin": version.proposal_origin,
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="KPI",
        title="KPI drafted",
        message=f"{definition.name} v{version.version} created.",
    )
    return _definition_out(definition)


@router.get("/companies/{company_id}/kpis/{kpi_id}")
def get_kpi(
    kpi_id: str,
    session: SessionDep,
    version: int | None = None,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    """Everything the KPI detail panel shows, in one response."""
    definition: KpiDefinition = load_scoped(session, KpiDefinition, kpi_id, access)
    if version is not None:
        target = next((v for v in definition.versions if v.version == version), None)
        if target is None:
            raise NotFound(f"{definition.name} has no version {version}.")
    else:
        target = next(
            (v for v in definition.versions if v.status == KpiStatus.ACTIVE),
            max(definition.versions, key=lambda v: v.version) if definition.versions else None,
        )
    if target is None:
        raise NotFound(f"{definition.name} has no versions.")

    return {
        "definition": _definition_out(definition).model_dump(),
        "version": {
            **export_contract(session, target),
            "is_editable": target.is_editable,
            "allowed_transitions": sorted(KPI_TRANSITIONS.get(target.status, set())),
        },
        "validation": _latest_validation(session, target),
    }


@router.patch("/companies/{company_id}/kpi-versions/{version_id}")
def edit_version(
    version_id: str,
    payload: KpiVersionInput,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.edit")),
) -> dict:
    """Edit an in-flight draft. An ACTIVE version is immutable by design."""
    version = _load_version(session, version_id, access)
    update_version(session, access, version, _payload_from_input(payload))
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_UPDATED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{version.definition.name} v{version.version}",
        summary=f"Edited {version.definition.name} v{version.version}; validation reset.",
        details={"formula": version.formula_expression},
        request=request,
    )
    return {
        "version": export_contract(session, version),
        "validation": None,
        "note": "The contract changed, so any previous validation result is void.",
    }


@router.post("/companies/{company_id}/kpis/{kpi_id}/versions")
def new_version(
    kpi_id: str,
    payload: KpiVersionInput,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.edit")),
) -> dict:
    """Revise a KPI. Creates v(n+1) in DRAFT; the live version keeps serving."""
    definition: KpiDefinition = load_scoped(session, KpiDefinition, kpi_id, access)
    version = create_new_version(session, access, definition, _payload_from_input(payload))
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_VERSION_CREATED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{definition.name} v{version.version}",
        summary=f"Started {definition.name} v{version.version} (supersedes v{version.supersedes_version}).",
        old_version=str(version.supersedes_version or ""),
        new_version=str(version.version),
        request=request,
    )
    return {"version": export_contract(session, version)}


# ---------------------------------------------------------------------------
# Company-provided KPI definitions (primary configuration path)
# ---------------------------------------------------------------------------
def _company_definitions(
    session: Session, access: AccessContext, request: Request
) -> tuple[DefinitionTable | None, list[DefinitionTable], list[CompanyKpiDefinition]]:
    """Read the company's own KPI registry from the connected source."""
    candidates = find_definition_tables(session, access.company.id)
    if not candidates:
        return (None, [], [])

    authoritative = candidates[0]
    source = session.get(DataSource, authoritative.data_source_id)
    if source is None:
        raise NotFound("The data source holding the KPI definitions is no longer registered.")

    connector = build_connector(source)
    try:
        definitions = read_company_definitions(
            session, access.company.id, authoritative, connector
        )
    finally:
        usage_of(request).absorb(connector)
        connector.close()

    registered = {
        row.kpi_key: row.id
        for row in session.scalars(
            select(KpiDefinition).where(KpiDefinition.company_id == access.company.id)
        )
    }
    for definition in definitions:
        definition.registered_kpi_id = registered.get(definition.kpi_key)
        definition.already_registered = definition.registered_kpi_id is not None

    return (authoritative, candidates[1:], definitions)


@router.get("/companies/{company_id}/kpi-source-definitions")
def list_company_definitions(
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    """The company's own KPI definitions, read from the connected source.

    These are the primary KPI configuration: the business already decided what
    its metrics mean, and the platform's job is to verify those definitions
    against the actual data, not to invent alternatives. Discovery proposals are
    a separate, optional endpoint.
    """
    authoritative, others, definitions = _company_definitions(session, access, request)
    resolved = [d for d in definitions if d.resolution_status == "RESOLVED"]
    return {
        "definition_table": authoritative.as_dict() if authoritative else None,
        "other_candidate_tables": [table.as_dict() for table in others],
        "definitions": [definition.as_dict() for definition in definitions],
        "counts": {
            "total": len(definitions),
            "active": sum(1 for d in definitions if d.is_active),
            "resolved": len(resolved),
            "needs_mapping": len(definitions) - len(resolved),
            "registered": sum(1 for d in definitions if d.already_registered),
            "importable": sum(1 for d in definitions if d.importable),
        },
        "note": (
            "Definitions are read verbatim from the company's KPI registry in the "
            "connected source and bound to real columns by the governed formula "
            "parser. No language model is involved, and no definition is rewritten."
            if authoritative
            else "No KPI-definition table was found in the discovered schema. A table "
            "qualifies when it has both a metric-name column and a formula column."
        ),
    }


@router.post(
    "/companies/{company_id}/kpi-source-definitions/import",
    status_code=status.HTTP_201_CREATED,
)
def import_company_definitions(
    payload: CompanyDefinitionImport,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.create")),
) -> dict:
    """Register company-defined KPIs as governed contracts in PROPOSED.

    Idempotent by KPI key: re-importing skips what is already registered rather
    than raising, so the button stays usable after a partial import.
    """
    _authoritative, _others, definitions = _company_definitions(session, access, request)
    if not definitions:
        raise NotFound(
            "No company KPI definitions are available to import. Discover the "
            "source that holds the KPI registry first."
        )

    wanted = set(payload.kpi_keys or [])
    selected = [d for d in definitions if not wanted or d.kpi_key in wanted]
    unknown = sorted(wanted - {d.kpi_key for d in definitions})
    if unknown:
        raise NotFound(
            f"No company KPI definition with key(s): {', '.join(unknown)}.",
            details={"unknown_keys": unknown},
        )

    imported: list[KpiDefinitionOut] = []
    skipped: list[dict] = []
    for definition in selected:
        if definition.already_registered:
            skipped.append(
                {
                    "kpi_key": definition.kpi_key,
                    "reason": "Already registered.",
                    "kpi_id": definition.registered_kpi_id,
                }
            )
            continue
        if definition.resolution_status != "RESOLVED":
            skipped.append(
                {
                    "kpi_key": definition.kpi_key,
                    "reason": "Definition is not bound to the discovered schema.",
                    "issues": definition.issues,
                }
            )
            continue

        created, version = create_from_company_definition(
            session, access, definition, overrides=payload.overrides.get(definition.kpi_key)
        )
        session.flush()
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.KPI_IMPORTED,
            resource_type="kpi",
            resource_id=created.id,
            resource_label=created.name,
            summary=(
                f"Imported company-defined KPI {created.name} from the source KPI "
                f"registry as {version.status}."
            ),
            new_version=str(version.version),
            details={
                "kpi_key": created.kpi_key,
                "source_formula": definition.source_formula,
                "bound_formula": version.formula_expression,
                "definition_origin": "company_kpi_registry",
            },
            request=request,
        )
        imported.append(_definition_out(created))

    if imported:
        audit.event(
            session,
            company_id=access.company.id,
            category="KPI",
            title="Company KPI definitions imported",
            message=f"{len(imported)} company-defined KPI(s) registered as PROPOSED.",
        )
    return {
        "imported": imported,
        "skipped": skipped,
        "counts": {"imported": len(imported), "skipped": len(skipped)},
    }


# ---------------------------------------------------------------------------
# Discovery proposals
# ---------------------------------------------------------------------------
@router.get("/companies/{company_id}/kpi-proposals")
def list_proposals(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    """*Optional additional* candidate KPIs derived from the profiled catalog.

    Secondary to the company's own definitions by design: this is a suggestion
    engine, not the configuration of record. The platform proposes; the
    administrator decides. Each candidate carries the evidence that produced it
    so the decision is informed rather than blind.
    """
    proposals = propose_kpis(session, access.company.id)
    existing = {
        definition.kpi_key
        for definition in session.scalars(
            select(KpiDefinition).where(KpiDefinition.company_id == access.company.id)
        )
    }
    return {
        "proposals": [
            {**proposal.as_dict(), "already_registered": proposal.kpi_key in existing}
            for proposal in proposals
        ],
        "note": (
            "Optional suggestions, generated deterministically from column profiles "
            "and grain, not by a language model. They supplement the company's own "
            "KPI definitions and never replace them. Naming hints affect the "
            "suggested label only, never the formula."
        ),
    }


@router.post(
    "/companies/{company_id}/kpi-proposals/accept",
    response_model=KpiDefinitionOut,
    status_code=status.HTTP_201_CREATED,
)
def accept_proposal(
    payload: KpiProposalAccept,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.create")),
) -> KpiDefinitionOut:
    """Accept a proposal — with edits if the administrator disagrees."""
    proposals = propose_kpis(session, access.company.id)
    proposal = next((p for p in proposals if p.kpi_key == payload.kpi_key), None)
    if proposal is None:
        raise NotFound(
            f"No current proposal with key '{payload.kpi_key}'. "
            "Re-run profiling if the catalog has changed."
        )

    definition, version = create_from_proposal(
        session, access, proposal, overrides=payload.overrides
    )
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_PROPOSED,
        resource_type="kpi",
        resource_id=definition.id,
        resource_label=definition.name,
        summary=(
            f"Accepted discovery proposal for {definition.name} "
            f"({'edited' if payload.overrides else 'unmodified'})."
        ),
        new_version=str(version.version),
        details={
            "formula": version.formula_expression,
            "discovery_confidence": proposal.confidence,
            "overrides": sorted(payload.overrides) if payload.overrides else [],
        },
        request=request,
    )
    return _definition_out(definition)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/kpi-versions/{version_id}/validate")
def validate_version(
    version_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.validate")),
) -> dict:
    """Run the nine governance checks, including executing the KPI."""
    version = _load_version(session, version_id, access)
    source = (
        session.get(DataSource, version.primary_data_source_id)
        if version.primary_data_source_id
        else None
    )
    connector = build_connector(source) if source is not None else None
    try:
        run, report = validate_kpi_version(session, version, access, connector)
    finally:
        if connector is not None:
            usage_of(request).absorb(connector)
            connector.close()
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_VALIDATED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{version.definition.name} v{version.version}",
        summary=f"Validation {report.overall_status}: {report.summary}",
        outcome="SUCCESS" if report.ready_for_approval else "FAILURE",
        details={
            "overall_status": report.overall_status,
            "passed": report.passed_count,
            "failed": report.failed_count,
            "warned": report.warned_count,
            "run_id": run.id,
        },
        request=request,
    )
    return report.as_dict()


@router.get("/companies/{company_id}/kpi-versions/{version_id}/validation")
def get_validation(
    version_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    version = _load_version(session, version_id, access)
    result = _latest_validation(session, version)
    if result is None:
        return {"overall_status": None, "checks": [], "note": "Not yet validated."}
    return result


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/kpi-versions/{version_id}/submit")
def submit_for_review(
    version_id: str,
    payload: KpiTransitionRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.edit")),
) -> dict:
    version = _load_version(session, version_id, access)
    target = (
        KpiStatus.PROPOSED if version.status == KpiStatus.DRAFT else KpiStatus.UNDER_REVIEW
    )
    transition(session, version, target, access=access, reason=payload.reason)
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_SUBMITTED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{version.definition.name} v{version.version}",
        summary=f"Moved to {target}.",
        new_version=str(version.version),
        details={"reason": payload.reason},
        request=request,
    )
    return {"status": version.status, "version": export_contract(session, version)}


@router.post("/companies/{company_id}/kpi-versions/{version_id}/review")
def start_review(
    version_id: str,
    payload: KpiTransitionRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.approve")),
) -> dict:
    version = _load_version(session, version_id, access)
    transition(session, version, KpiStatus.UNDER_REVIEW, access=access, reason=payload.reason)
    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_SUBMITTED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{version.definition.name} v{version.version}",
        summary="Review started.",
        request=request,
    )
    return {"status": version.status}


@router.post("/companies/{company_id}/kpi-versions/{version_id}/approve")
def approve_and_activate(
    version_id: str,
    payload: KpiTransitionRequest,
    session: SessionDep,
    request: Request,
    activate: bool = True,
    access: AccessContext = Depends(require_permissions("kpi.approve")),
) -> dict:
    """Approve and, by default, activate.

    Approval requires a passing validation run. Advisory warnings can be
    overridden by the approver — that is their judgement to make — but a blocking
    failure cannot.
    """
    version = _load_version(session, version_id, access)
    if version.status == KpiStatus.PROPOSED:
        # A proposal must be formally reviewed before it can be approved.
        transition(session, version, KpiStatus.UNDER_REVIEW, access=access)

    transition(session, version, KpiStatus.APPROVED, access=access, reason=payload.reason)
    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_APPROVED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{version.definition.name} v{version.version}",
        summary=f"Approved by {access.user.email}.",
        new_version=str(version.version),
        details={
            "reason": payload.reason,
            "validation_status": version.last_validation_status,
        },
        request=request,
    )

    if activate:
        previous = version.definition.current_version
        transition(session, version, KpiStatus.ACTIVE, access=access, reason=payload.reason)
        session.flush()
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.KPI_ACTIVATED,
            resource_type="kpi_version",
            resource_id=version.id,
            resource_label=f"{version.definition.name} v{version.version}",
            summary=f"{version.definition.name} v{version.version} is now ACTIVE.",
            old_version=str(previous or ""),
            new_version=str(version.version),
            request=request,
        )
        audit.event(
            session,
            company_id=access.company.id,
            category="KPI",
            title="KPI activated",
            message=f"{version.definition.name} v{version.version} is live.",
        )

    return {"status": version.status, "version": export_contract(session, version)}


@router.post("/companies/{company_id}/kpi-versions/{version_id}/reject")
def reject_version(
    version_id: str,
    payload: KpiRejectRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.approve")),
) -> dict:
    version = _load_version(session, version_id, access)
    transition(session, version, KpiStatus.REJECTED, access=access, reason=payload.reason)
    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_REJECTED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{version.definition.name} v{version.version}",
        summary=f"Rejected: {payload.reason}",
        outcome="FAILURE",
        details={"reason": payload.reason},
        request=request,
    )
    return {"status": version.status, "rejection_reason": version.rejection_reason}


@router.post("/companies/{company_id}/kpi-versions/{version_id}/deprecate")
def deprecate_version(
    version_id: str,
    payload: KpiTransitionRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.approve")),
) -> dict:
    version = _load_version(session, version_id, access)
    transition(session, version, KpiStatus.DEPRECATED, access=access, reason=payload.reason)
    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.KPI_DEPRECATED,
        resource_type="kpi_version",
        resource_id=version.id,
        resource_label=f"{version.definition.name} v{version.version}",
        summary=f"Deprecated: {payload.reason or 'no reason given'}",
        old_version=str(version.version),
        request=request,
    )
    return {"status": version.status}


# ---------------------------------------------------------------------------
# Contract export and preview
# ---------------------------------------------------------------------------
@router.get("/companies/{company_id}/kpi-contracts")
def list_contracts(
    session: SessionDep,
    active_only: bool = True,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    """The governed contracts Sprint 2 consumes.

    Sprint 2 should never need to ask "what is Revenue?" — it reads this.
    """
    definitions = session.scalars(
        select(KpiDefinition)
        .where(KpiDefinition.company_id == access.company.id)
        .order_by(KpiDefinition.name)
    )
    contracts = []
    for definition in definitions:
        for version in definition.versions:
            if active_only and version.status != KpiStatus.ACTIVE:
                continue
            contracts.append(export_contract(session, version))
    return {
        "company_id": access.company.id,
        "contracts": contracts,
        "count": len(contracts),
    }


@router.post("/companies/{company_id}/kpi-versions/{version_id}/preview")
def preview_value(
    version_id: str,
    payload: KpiPreviewRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("kpi.read")),
) -> dict:
    """Compute the KPI over a window, to sanity-check the definition.

    This is a governance aid, not the analytics engine — no baselines, no
    anomaly detection, no narratives. The value comes from SQL generated from
    the contract; the returned SQL is shown so an analyst can verify it.
    """
    version = _load_version(session, version_id, access)
    table = (
        session.get(SourceTable, version.primary_source_table_id)
        if version.primary_source_table_id
        else None
    )
    if table is None:
        raise NotFound("This KPI version has no source table binding.")

    # Entitlement is re-checked per breakdown column: a role may read the KPI
    # without being entitled to slice it by every dimension.
    allowed = {d.dimension_name.lower(): d for d in version.dimensions if d.allowed}
    for column in payload.group_by:
        dimension = allowed.get(column.lower())
        if dimension is None:
            raise ValidationFailure(
                f"'{column}' is not a governed dimension of this KPI.",
                details={"available": sorted(allowed)},
            )

    group_columns = [allowed[c.lower()].source_column for c in payload.group_by]
    source = session.get(DataSource, table.data_source_id)
    if source is None:
        raise NotFound("The KPI's data source is no longer registered.")

    connector = build_connector(source)
    if not isinstance(connector, SqlConnector):
        connector.close()
        raise Conflict(f"{source.source_type} cannot execute KPI queries yet.")

    try:
        spec = spec_from_stored(
            version.formula_spec,
            expression=version.formula_expression,
            default_table=table.table_name,
        )
        result = execute_kpi(
            connector,
            spec,
            schema=table.schema_name,
            table=table.table_name,
            time_column=version.time_field,
            start=payload.start,
            end=payload.end,
            group_by=group_columns,
            limit=payload.limit if group_columns else None,
        )
    finally:
        usage_of(request).absorb(connector)
        connector.close()

    return {
        "kpi": version.definition.name,
        "version": version.version,
        "formula": version.formula_expression,
        "unit": version.unit,
        "currency": version.currency,
        "window": {"start": payload.start, "end": payload.end},
        "group_by": payload.group_by,
        **result.as_dict(),
        "method": "SQL aggregate pushed down to the source. No model involved.",
    }
