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
from app.core.deps import AccessContext, SessionDep, load_scoped, require_permissions
from app.core.errors import Conflict, NotFound, ValidationFailure
from app.core.security import encrypt_secret
from app.core.telemetry import usage_of
from app.models.base import ConnectionStatus, DataSourceType
from app.models.profiling import TableGrain, TableProfile
from app.models.source import (
    DataSource,
    SelectedTable,
    SourceColumn,
    SourceHealth,
    SourceTable,
)
from app.schemas import (
    ColumnClassificationUpdate,
    DataScopeUpdate,
    DataSourceCreate,
    DataSourceOut,
    DataSourceUpdate,
    SourceColumnOut,
    SourceTableOut,
)
from app.services import audit
from app.services.discovery import discover_source

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
        connection_status=source.connection_status,
        last_tested_at=source.last_tested_at,
        last_test_error=source.last_test_error,
        refresh_frequency=source.refresh_frequency,
        timezone=source.timezone,
        known_limitations=source.known_limitations,
        last_discovered_at=source.last_discovered_at,
        discovered_table_count=int(discovered or 0),
        selected_table_count=int(selected or 0),
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


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
        options=options,
        connection_status=ConnectionStatus.UNTESTED,
        refresh_frequency=payload.refresh_frequency,
        timezone=payload.timezone,
        known_limitations=payload.known_limitations,
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

    for field in ("name", "description", "refresh_frequency", "timezone", "known_limitations", "schema_name"):
        value = getattr(payload, field)
        if value is not None and getattr(source, field) != value:
            changes[field] = value
            setattr(source, field, value)

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
        selection = table.selection
        is_selected = bool(selection and selection.enabled)
        if selected_only and not is_selected:
            continue
        profile = profiles.get(table.id)
        grain = grains.get(table.id)
        observation = health.get(table.id)
        results.append(
            SourceTableOut(
                id=table.id,
                data_source_id=table.data_source_id,
                schema_name=table.schema_name,
                table_name=table.table_name,
                qualified_name=table.qualified_name,
                table_type=table.table_type,
                approx_row_count=table.approx_row_count,
                column_count=table.column_count,
                discovered_at=table.discovered_at,
                selected=is_selected,
                business_alias=selection.business_alias if selection else None,
                declared_grain=selection.declared_grain if selection else None,
                primary_time_column=selection.primary_time_column if selection else None,
                inferred_grain=grain.inferred_grain if grain else None,
                quality_status=profile.quality_status if profile else None,
                freshness_status=observation.freshness_status if observation else None,
                profiled_at=profile.profiled_at if profile else None,
            )
        )
    return results


@router.get(
    "/companies/{company_id}/tables/{table_id}/columns", response_model=list[SourceColumnOut]
)
def list_columns(
    table_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("source.read")),
) -> list[SourceColumnOut]:
    table: SourceTable = load_scoped(session, SourceTable, table_id, access)
    results: list[SourceColumnOut] = []
    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        readable = access.can_read_column(column, table_name=table.table_name)
        results.append(
            SourceColumnOut(
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
                classification=column.classification,
                is_pii=column.is_pii,
                is_sensitive=column.is_sensitive,
                is_restricted=column.is_restricted,
                readable=readable,
                withheld_reason=None if readable else access.withheld_reason(column),
            )
        )
    return results


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

    readable = access.can_read_column(column, table_name=table.table_name if table else None)
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
        classification=column.classification,
        is_pii=column.is_pii,
        is_sensitive=column.is_sensitive,
        is_restricted=column.is_restricted,
        readable=readable,
        withheld_reason=None if readable else access.withheld_reason(column),
    )


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
