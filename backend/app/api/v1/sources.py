"""Data source registry, connection testing, discovery and the analytical scope."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select

from urllib.parse import urlparse

from app.connectors.postgres import parse_connection_uri
from app.connectors.supabase_rest import normalise_supabase_url, project_ref_of
from app.connectors.registry import (
    CONNECTOR_CATALOG,
    build_connector,
    descriptor_for,
)
from app.core.clock import utcnow
from app.core.deps import (
    AccessContext,
    SessionDep,
    load_scoped,
    load_selected_table,
    require_permissions,
)
from app.core.errors import Conflict, NotFound, ValidationFailure
from app.core.security import encrypt_secret
from app.core.telemetry import usage_of
from app.models.base import (
    ConnectionStatus,
    DataSourceType,
    GrainStatus,
    MetadataStatus,
    SourceHealthStatus,
)
from app.models.profiling import TableGrain, TableProfile
from app.models.source import (
    DataSource,
    SelectedTable,
    SourceColumn,
    SourceHealth,
    SourceTable,
)
from app.models.tenant import CompanyCalendar
from app.schemas import (
    ColumnClassificationUpdate,
    ColumnGovernanceUpdate,
    DataScopeUpdate,
    DataSourceCreate,
    DataSourceOut,
    DataSourceUpdate,
    SourceColumnOut,
    SourceHealthOut,
    SourceTableDetailOut,
    SourceTableOut,
    TableGovernanceUpdate,
)
from app.services import audit
from app.services.discovery import discover_source
from app.services.freshness import check_freshness
from app.services.profiling import profile_table
from app.services.source_governance import (
    assess_source_health,
    persist_source_health,
)

router = APIRouter(tags=["data-sources"])


# ---------------------------------------------------------------------------
# Connector catalogue
# ---------------------------------------------------------------------------
@router.get("/connectors")
def list_connectors() -> dict:
    """What the platform can connect to, and what each type needs.

    The UI builds its registration form from this, so adding a connector does not
    require a matching frontend change.
    """
    return {
        "connectors": [
            {
                "source_type": descriptor.source_type,
                "label": descriptor.label,
                "implemented": descriptor.implemented,
                "supports_profiling": descriptor.supports_profiling,
                "accepts_connection_uri": descriptor.accepts_connection_uri,
                "notes": descriptor.notes,
                "fields": [
                    {
                        "name": field.name,
                        "label": field.label,
                        "required": field.required,
                        "kind": field.kind,
                        "placeholder": field.placeholder,
                        "help_text": field.help_text,
                        "secret": field.secret,
                    }
                    for field in descriptor.fields
                ],
            }
            for descriptor in CONNECTOR_CATALOG
        ]
    }


# ---------------------------------------------------------------------------
# Source CRUD
# ---------------------------------------------------------------------------
def _source_out(session, source: DataSource) -> DataSourceOut:
    discovered = session.scalar(
        select(func.count(SourceTable.id)).where(SourceTable.data_source_id == source.id)
    )
    selected = session.scalar(
        select(func.count(SelectedTable.id)).where(
            SelectedTable.data_source_id == source.id, SelectedTable.enabled.is_(True)
        )
    )
    return DataSourceOut(
        id=source.id,
        name=source.name,
        source_type=source.source_type,
        description=source.description,
        host=source.host,
        port=source.port,
        database_name=source.database_name,
        schema_name=source.schema_name,
        username=source.username,
        # The credential itself is never serialised — only whether one is held.
        has_credentials=bool(source.encrypted_credentials),
        connection_reference=source.connection_reference,
        connection_status=source.connection_status,
        last_tested_at=source.last_tested_at,
        last_test_error=source.last_test_error,
        refresh_frequency=source.refresh_frequency,
        timezone=source.timezone,
        known_limitations=source.known_limitations,
        business_calendar_id=source.business_calendar_id,
        last_discovered_at=source.last_discovered_at,
        discovered_table_count=int(discovered or 0),
        selected_table_count=int(selected or 0),
        # Last measured values, replayed as stored. A read never re-measures, so
        # health_checked_at is what tells the reader how much this is still worth.
        grain=source.grain,
        last_refresh_at=source.last_refresh_at,
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
        completeness_pct=source.completeness_pct,
        quality_score=source.quality_score,
        health_status=source.health_status,
        health_checked_at=source.health_checked_at,
        health_reason=source.health_reason,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


_METADATA_ONLY_TYPES = (DataSourceType.API, DataSourceType.CSV, DataSourceType.FILE)


def _resolve_connection(payload: DataSourceCreate) -> dict:
    """Turn whichever input the administrator supplied into coordinates.

    Supabase is the one type that is *not* a Postgres DSN: it takes a project URL
    and a secret key, because those are the two values the dashboard hands you.
    The secret key is a REST credential, so that source is reached over the
    project API rather than a database session.
    """
    source_type = payload.source_type
    resolved: dict = {
        "host": payload.host,
        "port": payload.port,
        "database_name": payload.database_name,
        "schema_name": payload.schema_name,
        "username": payload.username,
        "password": payload.password,
        "options": {},
    }

    if source_type in _METADATA_ONLY_TYPES:
        # No driver exists for these, so there are no coordinates to resolve. The
        # source is registered for governance: what it contains, at what grain, how
        # often it lands, and what is known to be wrong with it. A reference is
        # required because a source nobody can locate cannot be governed either.
        if not payload.connection_reference:
            raise ValidationFailure(
                "A location reference is required so the source can be identified "
                "(an endpoint, bucket path or file share). Credentials must not be "
                "included — this field is stored unencrypted."
            )
        resolved.update(host=None, port=None, username=None, password=None)
        resolved["options"] = {"transport": "none", "governed_metadata_only": True}
        return resolved

    if source_type == DataSourceType.SQLITE:
        path = payload.path or payload.database_name
        if not path:
            raise ValidationFailure("A SQLite source requires a file path.")
        resolved["options"] = {"path": path}
        resolved["database_name"] = path
        resolved["schema_name"] = payload.schema_name or "main"
        return resolved

    if source_type == DataSourceType.SUPABASE:
        url = payload.supabase_url or payload.project_url
        key = payload.secret_key or payload.service_role_key
        if not url:
            raise ValidationFailure("Supabase URL is required.")
        if not key:
            raise ValidationFailure("Supabase secret key is required.")
        normalised = normalise_supabase_url(url)
        resolved.update(
            host=urlparse(normalised).hostname,
            port=None,
            database_name=project_ref_of(normalised) or "supabase",
            schema_name=payload.schema_name or "public",
            username=None,
            # The secret key is the credential for this source type.
            password=key,
        )
        resolved["options"] = {
            "supabase_url": normalised,
            "project_ref": project_ref_of(normalised),
            "transport": "rest",
        }
        return resolved

    if payload.connection_uri:
        parsed = parse_connection_uri(
            payload.connection_uri, default_schema=payload.schema_name or "public"
        )
        resolved.update(
            host=parsed.host,
            port=parsed.port,
            database_name=parsed.database,
            schema_name=payload.schema_name or parsed.schema,
            username=parsed.username,
            password=payload.password or parsed.password,
        )
        if parsed.sslmode:
            resolved["options"]["sslmode"] = parsed.sslmode
        if parsed.project_ref:
            resolved["options"]["project_ref"] = parsed.project_ref

    if source_type == DataSourceType.POSTGRESQL:
        if not resolved["host"] or not resolved["database_name"]:
            raise ValidationFailure(
                "A host and database name are required. Paste the connection string "
                "or fill the fields individually."
            )
        if not resolved["password"]:
            raise ValidationFailure(
                "A database password is required to read from this source."
            )
        resolved["schema_name"] = resolved["schema_name"] or "public"
        if payload.sslmode:
            resolved["options"]["sslmode"] = payload.sslmode

    return resolved


@router.get("/companies/{company_id}/data-sources", response_model=list[DataSourceOut])
def list_sources(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> list[DataSourceOut]:
    rows = session.scalars(
        select(DataSource)
        .where(DataSource.company_id == access.company.id)
        .order_by(DataSource.name)
    )
    return [_source_out(session, row) for row in rows]


def _validated_calendar_id(
    session, calendar_id: str | None, access: AccessContext
) -> str | None:
    """Resolve a business calendar reference, refusing one owned by another company.

    The id arrives from the client, so it is checked against the caller's company
    rather than trusted: without this a source could be pinned to a calendar it is
    not entitled to read, and every later freshness window would be judged against
    another tenant's working days.
    """
    if calendar_id is None:
        return None
    trimmed = calendar_id.strip()
    if not trimmed:
        # An explicit blank clears the reference; it is not a lookup miss.
        return None
    calendar = session.scalar(
        select(CompanyCalendar).where(
            CompanyCalendar.id == trimmed,
            CompanyCalendar.company_id == access.company.id,
        )
    )
    if calendar is None:
        raise ValidationFailure(
            "Unknown business calendar for this company.",
            details={"business_calendar_id": trimmed},
        )
    return calendar.id


@router.post(
    "/companies/{company_id}/data-sources",
    response_model=DataSourceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_source(
    payload: DataSourceCreate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> DataSourceOut:
    descriptor = descriptor_for(payload.source_type)
    if descriptor is None:
        raise ValidationFailure(f"Unknown source type: {payload.source_type}")

    existing = session.scalar(
        select(DataSource).where(
            DataSource.company_id == access.company.id, DataSource.name == payload.name.strip()
        )
    )
    if existing is not None:
        raise Conflict(f"A data source named '{payload.name}' already exists.")

    resolved = _resolve_connection(payload)
    options = dict(resolved["options"])
    if payload.service_role_key:
        # Kept for later non-SQL use (storage, auth admin). Encrypted, and never
        # returned by any endpoint.
        options["has_service_role_key"] = True

    calendar_id = _validated_calendar_id(session, payload.business_calendar_id, access)

    source = DataSource(
        company_id=access.company.id,
        name=payload.name.strip(),
        source_type=payload.source_type,
        description=payload.description,
        host=resolved["host"],
        port=resolved["port"],
        database_name=resolved["database_name"],
        schema_name=resolved["schema_name"],
        username=resolved["username"],
        encrypted_credentials=(
            encrypt_secret(resolved["password"]) if resolved["password"] else None
        ),
        connection_reference=payload.connection_reference,
        options=options,
        connection_status=ConnectionStatus.UNTESTED,
        refresh_frequency=payload.refresh_frequency,
        timezone=payload.timezone,
        known_limitations=payload.known_limitations,
        business_calendar_id=calendar_id,
    )
    if payload.service_role_key:
        source.options["service_role_key_encrypted"] = encrypt_secret(payload.service_role_key)
    session.add(source)
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.SOURCE_CREATED,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=f"Registered {source.source_type} source '{source.name}'.",
        details={
            "source_type": source.source_type,
            "host": source.host,
            "database": source.database_name,
            "schema": source.schema_name,
            "refresh_frequency": source.refresh_frequency,
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="SOURCE",
        title="Data source registered",
        message=f"{source.name} ({source.source_type})",
    )
    return _source_out(session, source)


@router.get(
    "/companies/{company_id}/data-sources/{source_id}", response_model=DataSourceOut
)
def get_source(
    source_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> DataSourceOut:
    """One source's registry record and its last measured governance rollup.

    A pure read: it opens no connection, profiles nothing and re-measures nothing.
    ``health_checked_at`` says when the rollup was taken; running a fresh check is
    an explicit POST.
    """
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    return _source_out(session, source)


@router.patch(
    "/companies/{company_id}/data-sources/{source_id}", response_model=DataSourceOut
)
def update_source(
    source_id: str,
    payload: DataSourceUpdate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> DataSourceOut:
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    changes: dict[str, object] = {}

    for field in (
        "name",
        "description",
        "refresh_frequency",
        "timezone",
        "known_limitations",
        "schema_name",
        "connection_reference",
    ):
        value = getattr(payload, field)
        if value is not None and getattr(source, field) != value:
            changes[field] = value
            setattr(source, field, value)

    if payload.business_calendar_id is not None:
        calendar_id = _validated_calendar_id(session, payload.business_calendar_id, access)
        if calendar_id != source.business_calendar_id:
            changes["business_calendar_id"] = calendar_id
            source.business_calendar_id = calendar_id

    if payload.refresh_frequency is not None and "refresh_frequency" in changes:
        # Freshness is judged against the declared cadence, so changing it makes
        # the stored verdict answer a question nobody asked. Cleared rather than
        # recomputed: recomputing here would be a write hidden inside an edit.
        source.health_status = SourceHealthStatus.UNKNOWN
        source.health_reason = (
            "Refresh cadence changed; the previous health verdict was measured "
            "against the old cadence. Run a health check."
        )
        source.health_checked_at = None

    if payload.password:
        source.encrypted_credentials = encrypt_secret(payload.password)
        # A new credential invalidates the previous test result.
        source.connection_status = ConnectionStatus.UNTESTED
        source.last_test_error = None
        changes["password"] = "[rotated]"
    if payload.service_role_key:
        options = dict(source.options or {})
        options["service_role_key_encrypted"] = encrypt_secret(payload.service_role_key)
        options["has_service_role_key"] = True
        source.options = options
        changes["service_role_key"] = "[rotated]"

    if changes:
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.SOURCE_UPDATED,
            resource_type="data_source",
            resource_id=source.id,
            resource_label=source.name,
            summary=f"Updated {', '.join(sorted(changes))}.",
            details={"changes": changes},
            request=request,
        )
    return _source_out(session, source)


@router.delete(
    "/companies/{company_id}/data-sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_source(
    source_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> None:
    from app.models.kpi import KpiVersion

    source: DataSource = load_scoped(session, DataSource, source_id, access)
    bound = session.scalar(
        select(func.count(KpiVersion.id)).where(KpiVersion.primary_data_source_id == source.id)
    )
    if bound:
        raise Conflict(
            f"{bound} KPI version(s) are bound to '{source.name}'. Deprecate them "
            "first — deleting the source would break their lineage.",
            details={"bound_kpi_versions": int(bound)},
        )

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.SOURCE_DELETED,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=f"Deleted data source '{source.name}'.",
        request=request,
    )
    session.delete(source)


# ---------------------------------------------------------------------------
# Source health and source-level profiling
# ---------------------------------------------------------------------------
def _selected_tables_of(session, source: DataSource) -> list[SourceTable]:
    """The source's tables that are inside the company's approved analytical scope."""
    return list(
        session.scalars(
            select(SourceTable)
            .join(SelectedTable, SelectedTable.source_table_id == SourceTable.id)
            .where(
                SourceTable.data_source_id == source.id,
                SourceTable.company_id == source.company_id,
                SelectedTable.enabled.is_(True),
            )
            .order_by(SourceTable.schema_name, SourceTable.table_name)
        )
    )


@router.get(
    "/companies/{company_id}/data-sources/{source_id}/health",
    response_model=SourceHealthOut,
)
def get_source_health(
    source_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> SourceHealthOut:
    """The deterministic health verdict, projected from stored measurements.

    Reads only: no connector is opened and no measurement is taken or written.
    The rollup arithmetic is replayed over the freshness observations and profiles
    already on record, so ``measured_at`` — not ``checked_at`` — is what says how
    old the underlying evidence is. To take fresh measurements, POST to this path.
    """
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    verdict = assess_source_health(session, source, _selected_tables_of(session, source))
    return SourceHealthOut(**verdict.as_dict())


@router.post(
    "/companies/{company_id}/data-sources/{source_id}/health",
    response_model=SourceHealthOut,
)
def run_source_health_check(
    source_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> SourceHealthOut:
    """Measure freshness across the source's tables, then roll it up. No LLM.

    The inputs are the declared cadence, the measured lag of each table's time
    column, and the completeness and quality already computed by profiling. There
    is no model in this path and there must not be one: a source's trustworthiness
    is the foundation the confidence engine later stands on, and a generated
    verdict would make that foundation unverifiable.
    """
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    tables = _selected_tables_of(session, source)

    if tables:
        connector = build_connector(source)
        try:
            check_freshness(session, source, tables, connector)
        finally:
            usage_of(request).absorb(connector)
            connector.close()
        session.flush()

    verdict = assess_source_health(session, source, tables)
    persist_source_health(source, verdict)

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.FRESHNESS_CHECKED,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=f"Health check: {verdict.status}. {verdict.reason}",
        details={
            "status": verdict.status,
            "reason": verdict.reason,
            "fresh_tables": verdict.fresh_tables,
            "stale_tables": verdict.stale_tables,
            "unknown_tables": verdict.unknown_tables,
            "quality_score": verdict.quality_score,
            "completeness_pct": verdict.completeness_pct,
        },
        request=request,
    )
    return SourceHealthOut(**verdict.as_dict())


@router.post("/companies/{company_id}/data-sources/{source_id}/profile")
def profile_source(
    source_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("profiling.run")),
) -> dict:
    """Profile every in-scope table of this source, then refresh its health.

    Explicit by design: nothing here runs on a read. Profiling issues aggregate
    queries against the live source, and doing that implicitly would put a real
    cost on somebody's dashboard load.

    Runs under the caller's own entitlement, so a column this user may not read
    is never queried — the profile records it as withheld instead of quietly
    presenting a partial picture as complete.
    """
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    tables = _selected_tables_of(session, source)
    if not tables:
        raise ValidationFailure(
            f"No table of '{source.name}' is in this company's approved data scope. "
            "Select tables under Data Scope before profiling.",
            details={"source_id": source.id},
        )

    profiled: list[dict] = []
    connector = build_connector(source)
    try:
        for table in tables:
            outcome = profile_table(session, table, connector, access)
            profiled.append(outcome.as_dict())
        session.flush()
        check_freshness(session, source, tables, connector)
    finally:
        usage_of(request).absorb(connector)
        connector.close()
    session.flush()

    verdict = assess_source_health(session, source, tables)
    persist_source_health(source, verdict)

    withheld = sum(int(item.get("withheld_columns") or 0) for item in profiled)
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.PROFILE_RUN,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=(
            f"Profiled {len(profiled)} table(s) of '{source.name}'; "
            f"{withheld} column(s) withheld by access policy. Health: {verdict.status}."
        ),
        details={
            "tables": [item.get("table") for item in profiled],
            "withheld_columns": withheld,
            "health_status": verdict.status,
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="SOURCE",
        title="Source profiled",
        message=f"{source.name}: {len(profiled)} table(s), health {verdict.status}.",
    )
    return {
        "source_id": source.id,
        "profiled_table_count": len(profiled),
        "withheld_column_count": withheld,
        "tables": profiled,
        "health": verdict.as_dict(),
        "note": (
            "Column roles and table candidates were re-proposed from the measured "
            "statistics. Anything a reviewer had confirmed was left untouched."
        ),
    }


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/data-sources/{source_id}/test")
def test_connection(
    source_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> dict:
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    connector = build_connector(source)
    try:
        result = connector.test_connection()
    finally:
        usage_of(request).absorb(connector)
        connector.close()

    source.last_tested_at = utcnow()
    source.connection_status = (
        ConnectionStatus.CONNECTED if result.ok else ConnectionStatus.FAILED
    )
    source.last_test_error = None if result.ok else result.error

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.SOURCE_TESTED,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=f"Connection test: {'passed' if result.ok else 'failed'}.",
        outcome="SUCCESS" if result.ok else "FAILURE",
        details={"checks": result.checks, "duration_ms": result.duration_ms},
        request=request,
    )
    return {
        "ok": result.ok,
        "message": result.message,
        "checks": result.checks,
        "server_version": result.server_version,
        "table_count": result.table_count,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "connection_status": source.connection_status,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@router.post("/companies/{company_id}/data-sources/{source_id}/discover")
def discover(
    source_id: str,
    session: SessionDep,
    request: Request,
    schema: str | None = None,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> dict:
    """List what the source contains. Grants no analytical access on its own."""
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    connector = build_connector(source)
    try:
        result = discover_source(session, source, connector, schema=schema)
    finally:
        usage_of(request).absorb(connector)
        connector.close()

    source.connection_status = ConnectionStatus.CONNECTED
    source.last_tested_at = utcnow()
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.TABLES_DISCOVERED,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=(
            f"Discovered {result.tables_found} table(s) in {result.schema_name} "
            f"({result.tables_created} new)."
        ),
        details=result.as_dict(),
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="SOURCE",
        title="Tables discovered",
        message=f"{result.tables_found} table(s) in {source.name}.{result.schema_name}",
        details=result.as_dict(),
    )
    return {
        **result.as_dict(),
        "note": (
            "Discovery is metadata only. Select tables under Data Scope before "
            "profiling or KPI registration can use them."
        ),
    }


@router.get("/companies/{company_id}/data-sources/{source_id}/schemas")
def list_schemas(
    source_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> dict:
    source: DataSource = load_scoped(session, DataSource, source_id, access)
    connector = build_connector(source)
    try:
        schemas = connector.list_schemas()
    finally:
        usage_of(request).absorb(connector)
        connector.close()
    return {"schemas": schemas, "current": source.schema_name}


# ---------------------------------------------------------------------------
# Discovered tables and columns
# ---------------------------------------------------------------------------
def _table_out(
    table: SourceTable,
    *,
    profile: TableProfile | None,
    grain: TableGrain | None,
    observation: SourceHealth | None,
) -> SourceTableOut:
    """One table's registry row, with proposed and confirmed metadata kept apart."""
    selection = table.selection
    return SourceTableOut(
        id=table.id,
        data_source_id=table.data_source_id,
        schema_name=table.schema_name,
        table_name=table.table_name,
        qualified_name=table.qualified_name,
        table_type=table.table_type,
        approx_row_count=table.approx_row_count,
        column_count=table.column_count,
        discovered_at=table.discovered_at,
        selected=bool(selection and selection.enabled),
        business_alias=selection.business_alias if selection else None,
        declared_grain=selection.declared_grain if selection else None,
        primary_time_column=selection.primary_time_column if selection else None,
        inferred_grain=grain.inferred_grain if grain else None,
        quality_status=profile.quality_status if profile else None,
        freshness_status=observation.freshness_status if observation else None,
        profiled_at=table.profiled_at or (profile.profiled_at if profile else None),
        display_name=table.display_name,
        description=table.description,
        primary_identifier_candidates=list(table.primary_identifier_candidates or []),
        time_field_candidates=list(table.time_field_candidates or []),
        company_field_candidates=list(table.company_field_candidates or []),
        candidates_status=table.candidates_status,
        confirmed_grain=grain.confirmed_grain if grain else None,
        effective_grain=grain.effective_grain if grain else None,
        grain_status=grain.grain_status if grain else GrainStatus.PROPOSED,
    )


def _column_out(
    column: SourceColumn, table: SourceTable | None, access: AccessContext
) -> SourceColumnOut:
    readable = access.can_read_column(
        column, table_name=table.table_name if table else None
    )
    return SourceColumnOut(
        id=column.id,
        column_name=column.column_name,
        ordinal_position=column.ordinal_position,
        data_type=column.data_type,
        is_nullable=column.is_nullable,
        is_primary_key=column.is_primary_key,
        is_foreign_key=column.is_foreign_key,
        references_table=column.references_table,
        references_column=column.references_column,
        semantic_type=column.semantic_type,
        candidate_role=column.candidate_role,
        confirmed_role=column.confirmed_role,
        effective_role=column.effective_role,
        role_status=column.role_status,
        description=column.description,
        classification=column.classification,
        is_pii=column.is_pii,
        is_sensitive=column.is_sensitive,
        is_restricted=column.is_restricted,
        readable=readable,
        withheld_reason=None if readable else access.withheld_reason(column),
    )


@router.get("/companies/{company_id}/tables", response_model=list[SourceTableOut])
def list_tables(
    session: SessionDep,
    data_source_id: str | None = None,
    selected_only: bool = False,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> list[SourceTableOut]:
    query = select(SourceTable).where(SourceTable.company_id == access.company.id)
    if data_source_id:
        query = query.where(SourceTable.data_source_id == data_source_id)
    tables = list(session.scalars(query.order_by(SourceTable.table_name)))

    table_ids = [table.id for table in tables]
    profiles = _index(session, TableProfile, table_ids)
    grains = _index(session, TableGrain, table_ids)
    health = _latest_health(session, table_ids)

    results: list[SourceTableOut] = []
    for table in tables:
        if selected_only and not (table.selection and table.selection.enabled):
            continue
        results.append(
            _table_out(
                table,
                profile=profiles.get(table.id),
                grain=grains.get(table.id),
                observation=health.get(table.id),
            )
        )
    return results


@router.get(
    "/companies/{company_id}/tables/{table_id}", response_model=SourceTableDetailOut
)
def get_table(
    table_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> SourceTableDetailOut:
    """One table with its columns, its grain evidence and its last profile.

    A pure read. Every governed field arrives with the status that says who put it
    there — ``candidates_status``, ``grain_status``, ``role_status`` — so a screen
    can show a proposal as a proposal instead of dressing inference up as fact.
    """
    table: SourceTable = load_scoped(session, SourceTable, table_id, access)
    profile = session.scalar(
        select(TableProfile).where(TableProfile.source_table_id == table.id)
    )
    grain = session.scalar(select(TableGrain).where(TableGrain.source_table_id == table.id))
    observation = _latest_health(session, [table.id]).get(table.id)
    selection = table.selection

    base = _table_out(table, profile=profile, grain=grain, observation=observation)
    return SourceTableDetailOut(
        **base.model_dump(),
        database_name=table.database_name,
        comment=table.comment,
        notes=selection.notes if selection else None,
        grain_columns=list(grain.grain_columns or []) if grain else [],
        grain_confidence=grain.confidence if grain else None,
        grain_method=grain.method if grain else None,
        grain_evidence=dict(grain.evidence or {}) if grain else {},
        grain_confirmed_by=grain.confirmed_by if grain else None,
        grain_confirmed_at=grain.confirmed_at if grain else None,
        time_grain=grain.time_grain if grain else None,
        row_count=profile.row_count if profile else None,
        completeness_pct=profile.completeness_pct if profile else None,
        quality_score=profile.quality_score if profile else None,
        withheld_column_count=profile.withheld_column_count if profile else 0,
        quality_warnings=list(profile.warnings or []) if profile else [],
        columns=[
            _column_out(column, table, access)
            for column in sorted(table.columns, key=lambda c: c.ordinal_position)
        ],
    )


@router.patch(
    "/companies/{company_id}/tables/{table_id}", response_model=SourceTableDetailOut
)
def review_table_metadata(
    table_id: str,
    payload: TableGovernanceUpdate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> SourceTableDetailOut:
    """Record a reviewer's decision about this table's governed metadata.

    This is the only path to CONFIRMED. Profiling and discovery may write
    proposals and may rewrite their own proposals, but they stop at a confirmed
    value — so a confirmation survives every later automated pass, which is what
    makes it worth making.
    """
    table: SourceTable = load_scoped(session, SourceTable, table_id, access)
    changes: dict[str, object] = {}

    for field in ("display_name", "description"):
        value = getattr(payload, field)
        if value is not None and getattr(table, field) != value:
            changes[field] = value
            setattr(table, field, value)

    for field in (
        "primary_identifier_candidates",
        "time_field_candidates",
        "company_field_candidates",
    ):
        value = getattr(payload, field)
        if value is None:
            continue
        cleaned = _validated_column_names(table, value, field)
        if list(getattr(table, field) or []) != cleaned:
            changes[field] = cleaned
            setattr(table, field, cleaned)

    if payload.confirm_candidates is not None:
        target = (
            MetadataStatus.CONFIRMED if payload.confirm_candidates else MetadataStatus.PROPOSED
        )
        if table.candidates_status != target:
            changes["candidates_status"] = target
            table.candidates_status = target

    grain_changed = _apply_grain_review(session, table, payload, access, changes)

    if changes:
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.SCOPE_UPDATED,
            resource_type="source_table",
            resource_id=table.id,
            resource_label=table.qualified_name,
            summary=f"Reviewed governed metadata: {', '.join(sorted(changes))}.",
            details={"changes": changes, "grain_reviewed": grain_changed},
            request=request,
        )
    session.flush()
    return get_table(table.id, session, access)


def _apply_grain_review(
    session,
    table: SourceTable,
    payload: TableGovernanceUpdate,
    access: AccessContext,
    changes: dict[str, object],
) -> bool:
    """Confirm or withdraw the grain, recording who decided and when."""
    if payload.confirmed_grain is None and payload.confirm_grain is None:
        return False

    grain = session.scalar(select(TableGrain).where(TableGrain.source_table_id == table.id))
    if grain is None:
        raise ValidationFailure(
            f"{table.qualified_name} has no detected grain to review. "
            "Run grain detection first.",
            details={"source_table_id": table.id},
        )

    if payload.confirmed_grain is not None:
        grain.confirmed_grain = payload.confirmed_grain
        changes["confirmed_grain"] = payload.confirmed_grain

    if payload.confirm_grain is False:
        # Withdrawing a confirmation returns the field to whatever authority the
        # underlying evidence carries on its own — never silently to CONFIRMED.
        grain.confirmed_grain = None
        grain.confirmed_by = None
        grain.confirmed_at = None
        grain.grain_status = (
            GrainStatus.DECLARED if grain.declared_grain else GrainStatus.PROPOSED
        )
        changes["grain_status"] = grain.grain_status
        return True

    if payload.confirm_grain or payload.confirmed_grain is not None:
        if not (grain.confirmed_grain or grain.declared_grain or grain.inferred_grain):
            raise ValidationFailure(
                "There is nothing to confirm: no grain has been declared or inferred "
                "for this table.",
                details={"source_table_id": table.id},
            )
        grain.confirmed_grain = (
            grain.confirmed_grain or grain.declared_grain or grain.inferred_grain
        )
        grain.grain_status = GrainStatus.CONFIRMED
        grain.confirmed_by = access.user.id
        grain.confirmed_at = utcnow()
        changes["grain_status"] = GrainStatus.CONFIRMED
    return True


def _validated_column_names(
    table: SourceTable, names: list[str], field: str
) -> list[str]:
    """Candidates must name columns that actually exist on the table.

    A governed identifier pointing at a column that was renamed away is worse than
    an empty list: the next engine to read it fails somewhere far from here.
    """
    known = {column.column_name for column in table.columns}
    cleaned: list[str] = []
    for name in names:
        stripped = (name or "").strip()
        if not stripped or stripped in cleaned:
            continue
        if stripped not in known:
            raise ValidationFailure(
                f"{table.qualified_name} has no column '{stripped}'.",
                details={"field": field, "known_columns": sorted(known)},
            )
        cleaned.append(stripped)
    return cleaned


@router.get(
    "/companies/{company_id}/tables/{table_id}/columns", response_model=list[SourceColumnOut]
)
def list_columns(
    table_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> list[SourceColumnOut]:
    table: SourceTable = load_scoped(session, SourceTable, table_id, access)
    return [
        _column_out(column, table, access)
        for column in sorted(table.columns, key=lambda c: c.ordinal_position)
    ]


@router.patch("/companies/{company_id}/columns/{column_id}", response_model=SourceColumnOut)
def reclassify_column(
    column_id: str,
    payload: ColumnClassificationUpdate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> SourceColumnOut:
    """Override the automatic sensitivity classification.

    Auto-classification is a name-based first pass; the administrator's decision
    is authoritative and is what the access checks read from then on.
    """
    column: SourceColumn = load_scoped(session, SourceColumn, column_id, access)
    table = session.get(SourceTable, column.source_table_id)
    changes: dict[str, object] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None and getattr(column, field) != value:
            changes[field] = value
            setattr(column, field, value)

    if changes:
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.COLUMN_CLASSIFIED,
            resource_type="source_column",
            resource_id=column.id,
            resource_label=f"{table.table_name if table else '?'}.{column.column_name}",
            summary=f"Reclassified {', '.join(sorted(changes))}.",
            details={"changes": changes},
            request=request,
        )

    return _column_out(column, table, access)


@router.patch(
    "/companies/{company_id}/columns/{column_id}/role", response_model=SourceColumnOut
)
def review_column_role(
    column_id: str,
    payload: ColumnGovernanceUpdate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> SourceColumnOut:
    """Confirm what a column means in business terms.

    Deliberately a different endpoint from reclassification: sensitivity decides
    *who may read* a column and role decides *what it means*. Sharing one payload
    would let a meaning correction quietly widen access.

    A confirmed role outranks the profiler's proposal and survives every later
    profiling pass. Clearing it hands the column back to the proposer.
    """
    column: SourceColumn = load_scoped(session, SourceColumn, column_id, access)
    table = session.get(SourceTable, column.source_table_id)
    changes: dict[str, object] = {}

    if payload.clear_confirmed_role:
        if column.confirmed_role is not None:
            changes["confirmed_role"] = None
            column.confirmed_role = None
        # Back to whatever the last automated pass proposed. Left as PROPOSED even
        # when no proposal exists — the alternative would assert a role nobody set.
        if column.role_status != MetadataStatus.PROPOSED:
            changes["role_status"] = MetadataStatus.PROPOSED
            column.role_status = MetadataStatus.PROPOSED
    elif payload.confirmed_role is not None:
        if column.confirmed_role != payload.confirmed_role:
            changes["confirmed_role"] = payload.confirmed_role
            column.confirmed_role = payload.confirmed_role
        if column.role_status != MetadataStatus.CONFIRMED:
            changes["role_status"] = MetadataStatus.CONFIRMED
            column.role_status = MetadataStatus.CONFIRMED

    if payload.description is not None and column.description != payload.description:
        changes["description"] = payload.description
        column.description = payload.description

    if changes:
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.COLUMN_CLASSIFIED,
            resource_type="source_column",
            resource_id=column.id,
            resource_label=f"{table.table_name if table else '?'}.{column.column_name}",
            summary=f"Reviewed column role: {', '.join(sorted(changes))}.",
            details={"changes": changes, "review": "role"},
            request=request,
        )
    return _column_out(column, table, access)


# ---------------------------------------------------------------------------
# Data scope
# ---------------------------------------------------------------------------
@router.get("/companies/{company_id}/data-scope")
def get_data_scope(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> dict:
    rows = session.execute(
        select(SelectedTable, SourceTable)
        .join(SourceTable, SourceTable.id == SelectedTable.source_table_id)
        .where(SelectedTable.company_id == access.company.id)
        .order_by(SourceTable.table_name)
    ).all()
    return {
        "boundary": (
            "These tables are the maximum analytical scope for this company. "
            "Profiling, the semantic catalog and KPI registration refuse anything else."
        ),
        "tables": [
            {
                "source_table_id": table.id,
                "data_source_id": table.data_source_id,
                "qualified_name": table.qualified_name,
                "enabled": selection.enabled,
                "business_alias": selection.business_alias,
                "declared_grain": selection.declared_grain,
                "primary_time_column": selection.primary_time_column,
                "notes": selection.notes,
            }
            for selection, table in rows
        ],
    }


@router.put("/companies/{company_id}/data-scope")
def update_data_scope(
    payload: DataScopeUpdate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> dict:
    """Set which tables may enter semantic and KPI processing."""
    from app.models.kpi import KpiStatus, KpiVersion

    requested = {item.source_table_id: item for item in payload.tables}
    tables = {
        table.id: table
        for table in session.scalars(
            select(SourceTable).where(
                SourceTable.company_id == access.company.id,
                SourceTable.id.in_(list(requested)) if requested else False,
            )
        )
    }
    missing = sorted(set(requested) - set(tables))
    if missing:
        raise NotFound(f"Unknown table id(s): {', '.join(missing)}")

    existing = {
        selection.source_table_id: selection
        for selection in session.scalars(
            select(SelectedTable).where(SelectedTable.company_id == access.company.id)
        )
    }

    enabled_names: list[str] = []
    disabled_names: list[str] = []

    for table_id, item in requested.items():
        table = tables[table_id]
        selection = existing.get(table_id)
        if selection is None:
            selection = SelectedTable(
                company_id=access.company.id,
                data_source_id=table.data_source_id,
                source_table_id=table.id,
            )
            session.add(selection)
        selection.enabled = item.enabled
        selection.business_alias = item.business_alias
        selection.declared_grain = item.declared_grain
        selection.primary_time_column = item.primary_time_column
        selection.notes = item.notes
        selection.selected_by = access.user.id
        (enabled_names if item.enabled else disabled_names).append(table.qualified_name)

    if payload.replace:
        for table_id, selection in existing.items():
            if table_id in requested or not selection.enabled:
                continue
            # Removing a table a live KPI depends on would break it silently.
            bound = session.scalar(
                select(func.count(KpiVersion.id)).where(
                    KpiVersion.primary_source_table_id == table_id,
                    KpiVersion.status.in_([KpiStatus.ACTIVE, KpiStatus.APPROVED]),
                )
            )
            if bound:
                table = session.get(SourceTable, table_id)
                raise Conflict(
                    f"{table.qualified_name if table else table_id} is used by "
                    f"{bound} active/approved KPI version(s) and cannot be removed "
                    "from scope. Deprecate them first.",
                    details={"source_table_id": table_id, "bound_kpi_versions": int(bound)},
                )
            selection.enabled = False
            table = session.get(SourceTable, table_id)
            disabled_names.append(table.qualified_name if table else table_id)

    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.SCOPE_UPDATED,
        resource_type="data_scope",
        resource_id=access.company.id,
        resource_label=access.company.company_name,
        summary=f"Enabled {len(enabled_names)}, disabled {len(disabled_names)} table(s).",
        details={"enabled": sorted(enabled_names), "disabled": sorted(disabled_names)},
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="SOURCE",
        title="Data scope updated",
        message=f"{len(enabled_names)} table(s) in analytical scope.",
    )
    return {
        "enabled": sorted(enabled_names),
        "disabled": sorted(disabled_names),
        "enabled_count": len(enabled_names),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _index(session, model: type, table_ids: list[str]) -> dict:
    if not table_ids:
        return {}
    rows = session.scalars(select(model).where(model.source_table_id.in_(table_ids)))
    return {row.source_table_id: row for row in rows}


def _latest_health(session, table_ids: list[str]) -> dict[str, SourceHealth]:
    if not table_ids:
        return {}
    rows = session.scalars(
        select(SourceHealth)
        .where(SourceHealth.source_table_id.in_(table_ids))
        .order_by(SourceHealth.checked_at.asc())
    )
    return {row.source_table_id: row for row in rows if row.source_table_id}
