"""The semantic catalog: the company's governed business world.

This assembles everything Sprint 1 established into one structure, then freezes
it as an immutable version. The point is reproducibility — Sprint 2's monitoring
and Sprint 4's evidence both need to be able to say "this is what we knew about
the company's data at the moment this insight was produced", and that is only
possible if the catalog is versioned rather than continuously mutated.

Publishing never overwrites. Catalog v1 remains readable after v2 exists.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import iso, utcnow
from app.core.deps import AccessContext
from app.models.base import DocumentStatus, KpiStatus
from app.models.catalog import CatalogVersion
from app.models.document import CompanyDocument
from app.models.kpi import KpiDefinition, KpiVersion
from app.models.profiling import (
    ColumnProfile,
    JoinSafety,
    SourceReconciliation,
    TableGrain,
    TableProfile,
    TableRelationship,
)
from app.models.source import DataSource, SelectedTable, SourceColumn, SourceHealth, SourceTable
from app.models.tenant import Company, CompanyCalendar, CompanyUser, Role, User
from app.services.freshness import latest_health
from app.services.kpi_governance import export_contract


def build_catalog(session: Session, access: AccessContext) -> dict[str, Any]:
    """Assemble the current catalog for a company, respecting entitlements."""
    company: Company = access.company
    sources = list(
        session.scalars(
            select(DataSource)
            .where(DataSource.company_id == company.id)
            .order_by(DataSource.name)
        )
    )
    selected = _selected_tables(session, company.id)
    table_ids = [table.id for table in selected]

    table_profiles = _by_table(session, TableProfile, table_ids)
    grains = _by_table(session, TableGrain, table_ids)
    health = latest_health(session, table_ids)
    column_profiles = _column_profiles(session, table_ids)
    relationships = _relationships(session, table_ids)
    reconciliations = _reconciliations(session, table_ids)

    tables_payload = [
        _table_entry(
            table=table,
            profile=table_profiles.get(table.id),
            grain=grains.get(table.id),
            health=health.get(table.id),
            column_profiles=column_profiles,
            access=access,
        )
        for table in selected
    ]

    kpi_payload = _kpi_registry(session, company.id)

    snapshot: dict[str, Any] = {
        "generated_at": iso(utcnow()),
        "company": {
            "id": company.id,
            "name": company.company_name,
            "slug": company.slug,
            "industry": company.industry,
            "country": company.country,
            "timezone": company.timezone,
            "currency": company.currency,
            "fiscal_year_start_month": company.fiscal_year_start_month,
            "week_start_day": company.week_start_day,
            "status": company.status,
        },
        "calendars": _calendars(session, company.id),
        "members": _members(session, company.id) if access.has("user.read") else [],
        "data_sources": [
            {
                "id": source.id,
                "name": source.name,
                "type": source.source_type,
                "schema": source.schema_name,
                "connection_status": source.connection_status,
                "refresh_frequency": source.refresh_frequency,
                "timezone": source.timezone,
                "last_tested_at": iso(source.last_tested_at),
                "last_discovered_at": iso(source.last_discovered_at),
                "known_limitations": source.known_limitations,
                "discovered_table_count": _discovered_count(session, source.id),
                "selected_table_count": sum(
                    1 for table in selected if table.data_source_id == source.id
                ),
            }
            for source in sources
        ],
        "selected_tables": tables_payload,
        "relationships": relationships,
        "cross_source_reconciliation": reconciliations,
        "documents": _documents(session, company.id, access),
        "kpi_registry": kpi_payload,
        "boundaries": {
            "analytical_scope": (
                "Only the tables listed in selected_tables may enter semantic or KPI "
                "processing. Discovery alone grants no analytical access."
            ),
            "monitoring_note": (
                "KPI dimensions declare valid breakdowns. Monitoring happens at the "
                "KPI level; entity-level analysis is selective and on demand."
            ),
            "not_in_sprint_1": [
                "anomaly detection",
                "forecasting / expected-value baselines",
                "contribution analysis",
                "document embeddings and retrieval",
                "LLM reasoning and narratives",
            ],
        },
    }

    snapshot["counts"] = {
        "data_sources": len(sources),
        "selected_tables": len(selected),
        "profiled_tables": sum(1 for entry in tables_payload if entry["profile"]["profiled_at"]),
        "relationships": len(relationships),
        "documents": len(snapshot["documents"]),
        "active_kpis": sum(1 for kpi in kpi_payload if kpi["status"] == KpiStatus.ACTIVE),
        "total_kpis": len(kpi_payload),
    }
    return snapshot


def publish_catalog(
    session: Session, access: AccessContext, *, note: str | None = None
) -> CatalogVersion:
    """Freeze the current catalog as the next immutable version."""
    snapshot = build_catalog(session, access)
    latest = session.scalar(
        select(func.max(CatalogVersion.version)).where(
            CatalogVersion.company_id == access.company.id
        )
    )
    counts = snapshot["counts"]

    # The checksum covers the snapshot minus its own timestamp, so republishing
    # an unchanged catalog is detectable.
    comparable = {k: v for k, v in snapshot.items() if k != "generated_at"}
    checksum = hashlib.sha256(
        json.dumps(comparable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    version = CatalogVersion(
        company_id=access.company.id,
        version=(latest or 0) + 1,
        published_at=utcnow(),
        published_by=access.user.id,
        note=note,
        source_count=counts["data_sources"],
        selected_table_count=counts["selected_tables"],
        profiled_table_count=counts["profiled_tables"],
        relationship_count=counts["relationships"],
        document_count=counts["documents"],
        active_kpi_count=counts["active_kpis"],
        checksum_sha256=checksum,
        snapshot=snapshot,
    )
    session.add(version)
    session.flush()
    return version


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def _table_entry(
    *,
    table: SourceTable,
    profile: TableProfile | None,
    grain: TableGrain | None,
    health: SourceHealth | None,
    column_profiles: dict[str, ColumnProfile],
    access: AccessContext,
) -> dict[str, Any]:
    selection = table.selection
    columns: list[dict[str, Any]] = []
    for column in sorted(table.columns, key=lambda c: c.ordinal_position):
        readable = access.can_read_column(column, table_name=table.table_name)
        column_profile = column_profiles.get(column.id)
        entry: dict[str, Any] = {
            "name": column.column_name,
            "data_type": column.data_type,
            "semantic_type": column.semantic_type,
            "classification": column.classification,
            "is_pii": column.is_pii,
            "is_restricted": column.is_restricted,
            "is_primary_key": column.is_primary_key,
            "is_foreign_key": column.is_foreign_key,
            "references": (
                f"{column.references_table}.{column.references_column}"
                if column.references_table
                else None
            ),
            "readable": readable,
        }
        if readable and column_profile and not column_profile.access_withheld:
            entry["profile"] = {
                "null_pct": column_profile.null_pct,
                "distinct_count": column_profile.distinct_count,
                "min": column_profile.min_value,
                "max": column_profile.max_value,
                "mean": column_profile.mean_value,
                "is_unique": column_profile.is_unique,
                "quality_status": column_profile.quality_status,
                "warnings": column_profile.warnings,
                "sample_values": column_profile.sample_values,
            }
        else:
            # The catalog states that a column exists and was not read, rather
            # than omitting it and implying a complete picture.
            entry["profile"] = None
            entry["withheld_reason"] = (
                None if readable else access.withheld_reason(column)
            )
        columns.append(entry)

    return {
        "source_table_id": table.id,
        "data_source_id": table.data_source_id,
        "schema": table.schema_name,
        "table": table.table_name,
        "qualified_name": table.qualified_name,
        "business_alias": selection.business_alias if selection else None,
        "approx_row_count": table.approx_row_count,
        "primary_time_column": selection.primary_time_column if selection else None,
        "grain": (
            {
                "declared": grain.declared_grain,
                "inferred": grain.inferred_grain,
                "columns": grain.grain_columns,
                "is_unique": grain.is_unique,
                "confidence": grain.confidence,
                "method": grain.method,
                "time_column": grain.time_column,
                "time_grain": grain.time_grain,
            }
            if grain
            else None
        ),
        "profile": {
            "profiled_at": iso(profile.profiled_at) if profile else None,
            "row_count": profile.row_count if profile else None,
            "completeness_pct": profile.completeness_pct if profile else None,
            "quality_score": profile.quality_score if profile else None,
            "quality_status": profile.quality_status if profile else "UNKNOWN",
            "warnings": profile.warnings if profile else [],
            "profiled_column_count": profile.profiled_column_count if profile else 0,
            "withheld_column_count": profile.withheld_column_count if profile else 0,
        },
        "freshness": (
            {
                "status": health.freshness_status,
                "time_column": health.time_column,
                "lag_seconds": health.freshness_lag_seconds,
                "expected_interval_seconds": health.expected_interval_seconds,
                "coverage_start": iso(health.coverage_start),
                "coverage_end": iso(health.coverage_end),
                "checked_at": iso(health.checked_at),
                "note": (health.details or {}).get("note"),
            }
            if health
            else None
        ),
        "columns": columns,
    }


def _relationships(session: Session, table_ids: list[str]) -> list[dict[str, Any]]:
    if not table_ids:
        return []
    rows = session.execute(
        select(TableRelationship, JoinSafety)
        .outerjoin(JoinSafety, JoinSafety.relationship_id == TableRelationship.id)
        .where(
            TableRelationship.source_table_id.in_(table_ids),
            TableRelationship.target_table_id.in_(table_ids),
        )
    ).all()

    names = _table_names(session, table_ids)
    payload: list[dict[str, Any]] = []
    for relationship, safety in rows:
        payload.append(
            {
                "id": relationship.id,
                "from": f"{names.get(relationship.source_table_id)}.{relationship.source_column}",
                "to": f"{names.get(relationship.target_table_id)}.{relationship.target_column}",
                "type": relationship.relationship_type,
                "method": relationship.method,
                "is_declared": relationship.is_declared,
                "confidence": relationship.confidence,
                "orphan_pct": relationship.orphan_pct,
                "join_safety": (
                    {
                        "level": safety.safety_level,
                        "fan_out_factor": safety.fan_out_factor,
                        "max_fan_out": safety.max_fan_out,
                        "duplicate_key_rate": safety.duplicate_key_rate,
                        "reason": safety.reason,
                        "guidance": safety.guidance,
                    }
                    if safety
                    else None
                ),
            }
        )
    return sorted(payload, key=lambda item: (item["from"], item["to"]))


def _reconciliations(session: Session, table_ids: list[str]) -> list[dict[str, Any]]:
    if not table_ids:
        return []
    rows = session.scalars(
        select(SourceReconciliation).where(
            SourceReconciliation.left_table_id.in_(table_ids),
            SourceReconciliation.right_table_id.in_(table_ids),
        )
    )
    names = _table_names(session, table_ids)
    return [
        {
            "left": names.get(row.left_table_id),
            "right": names.get(row.right_table_id),
            "status": row.status,
            "left_grain": row.left_grain,
            "right_grain": row.right_grain,
            "left_time_grain": row.left_time_grain,
            "right_time_grain": row.right_time_grain,
            "shared_dimensions": row.shared_dimensions,
            "unmapped_dimensions": row.unmapped_dimensions,
            "time_overlap_days": row.time_overlap_days,
            "reason": row.reason,
            "guidance": row.guidance,
        }
        for row in rows
    ]


def _kpi_registry(session: Session, company_id: str) -> list[dict[str, Any]]:
    definitions = list(
        session.scalars(
            select(KpiDefinition)
            .where(KpiDefinition.company_id == company_id)
            .order_by(KpiDefinition.name)
        )
    )
    registry: list[dict[str, Any]] = []
    for definition in definitions:
        current = next(
            (v for v in definition.versions if v.status == KpiStatus.ACTIVE),
            next(
                (v for v in sorted(definition.versions, key=lambda x: -x.version)),
                None,
            ),
        )
        if current is None:
            continue
        entry = export_contract(session, current)
        entry["status"] = current.status
        entry["definition_status"] = definition.status
        entry["version_history"] = [
            {
                "version": v.version,
                "status": v.status,
                "approved_at": iso(v.approved_at),
                "activated_at": iso(v.activated_at),
                "deprecated_at": iso(v.deprecated_at),
            }
            for v in sorted(definition.versions, key=lambda x: x.version)
        ]
        registry.append(entry)
    return registry


def _documents(
    session: Session, company_id: str, access: AccessContext
) -> list[dict[str, Any]]:
    if not access.has("document.read"):
        return []
    documents = list(
        session.scalars(
            select(CompanyDocument)
            .where(CompanyDocument.company_id == company_id)
            .order_by(CompanyDocument.title)
        )
    )
    payload: list[dict[str, Any]] = []
    for document in documents:
        if not _document_visible(document, access):
            continue
        current = next((v for v in document.versions if v.is_current), None)
        payload.append(
            {
                "id": document.id,
                "document_key": document.document_key,
                "title": document.title,
                "document_type": document.document_type,
                "document_class": document.document_class,
                "status": document.status,
                "current_version": document.current_version,
                "access_scope": document.access_scope,
                "tags": document.tags,
                "effective_from": (
                    current.effective_from.isoformat()
                    if current and current.effective_from
                    else None
                ),
                "version_count": len(document.versions),
                # Sprint 1 stores and versions documents. Chunking, embedding and
                # retrieval are explicitly out of scope.
                "retrieval_ready": False,
            }
        )
    return payload


def _document_visible(document: CompanyDocument, access: AccessContext) -> bool:
    if access.is_admin or not document.access_scope:
        return True
    return access.role.role_key in document.access_scope


def _calendars(session: Session, company_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(CompanyCalendar)
        .where(CompanyCalendar.company_id == company_id)
        .order_by(CompanyCalendar.calendar_key)
    )
    return [
        {
            "id": row.id,
            "calendar_key": row.calendar_key,
            "name": row.name,
            "timezone": row.timezone,
            "week_start_day": row.week_start_day,
            "fiscal_year_start_month": row.fiscal_year_start_month,
            "is_default": row.is_default,
        }
        for row in rows
    ]


def _members(session: Session, company_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(CompanyUser, User, Role)
        .join(User, User.id == CompanyUser.user_id)
        .join(Role, Role.id == CompanyUser.role_id)
        .where(CompanyUser.company_id == company_id)
        .order_by(Role.rank, User.full_name)
    ).all()
    return [
        {
            "user_id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "role": role.role_key,
            "status": membership.status,
            "row_scope": membership.row_scope,
            "denied_columns": membership.denied_columns,
        }
        for membership, user, role in rows
    ]


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------
def _selected_tables(session: Session, company_id: str) -> list[SourceTable]:
    return list(
        session.scalars(
            select(SourceTable)
            .join(SelectedTable, SelectedTable.source_table_id == SourceTable.id)
            .where(SourceTable.company_id == company_id, SelectedTable.enabled.is_(True))
            .order_by(SourceTable.table_name)
        )
    )


def _by_table(session: Session, model: type, table_ids: list[str]) -> dict[str, Any]:
    if not table_ids:
        return {}
    rows = session.scalars(select(model).where(model.source_table_id.in_(table_ids)))
    return {row.source_table_id: row for row in rows}


def _column_profiles(session: Session, table_ids: list[str]) -> dict[str, ColumnProfile]:
    if not table_ids:
        return {}
    rows = session.scalars(
        select(ColumnProfile).where(ColumnProfile.source_table_id.in_(table_ids))
    )
    return {row.source_column_id: row for row in rows}


def _table_names(session: Session, table_ids: list[str]) -> dict[str, str]:
    rows = session.execute(
        select(SourceTable.id, SourceTable.table_name).where(SourceTable.id.in_(table_ids))
    ).all()
    return {row[0]: row[1] for row in rows}


def _discovered_count(session: Session, data_source_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(SourceTable.id)).where(
                SourceTable.data_source_id == data_source_id
            )
        )
        or 0
    )
