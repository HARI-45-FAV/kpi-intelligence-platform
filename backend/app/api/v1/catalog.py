"""Semantic catalog API: the current governed world, and its frozen versions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select

from app.core.deps import AccessContext, SessionDep, require_permissions
from app.core.errors import NotFound
from app.models.catalog import CatalogVersion
from app.schemas import CatalogPublishRequest, CatalogVersionOut
from app.services import audit
from app.services.catalog import build_catalog, publish_catalog

router = APIRouter(tags=["catalog"])


@router.get("/companies/{company_id}/catalog")
def get_current_catalog(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("catalog.read")),
) -> dict:
    """The live catalog, assembled under the caller's own entitlement.

    Two callers with different roles legitimately see different catalogs: a
    column one may not read is reported as present-but-withheld rather than
    silently dropped.
    """
    return build_catalog(session, access)


@router.post(
    "/companies/{company_id}/catalog/publish",
    response_model=CatalogVersionOut,
    status_code=status.HTTP_201_CREATED,
)
def publish(
    payload: CatalogPublishRequest,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("catalog.publish")),
) -> CatalogVersionOut:
    """Freeze the catalog as an immutable version.

    Reproducibility depends on this: an insight recorded against catalog v3 must
    still be explainable after the schema has moved on.
    """
    version = publish_catalog(session, access, note=payload.note)
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.CATALOG_PUBLISHED,
        resource_type="catalog",
        resource_id=version.id,
        resource_label=f"Catalog v{version.version}",
        summary=(
            f"Published catalog v{version.version}: "
            f"{version.selected_table_count} table(s), "
            f"{version.active_kpi_count} active KPI(s)."
        ),
        new_version=str(version.version),
        details={
            "checksum": version.checksum_sha256,
            "sources": version.source_count,
            "profiled_tables": version.profiled_table_count,
            "relationships": version.relationship_count,
            "documents": version.document_count,
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="CATALOG",
        title="Catalog published",
        message=f"Version {version.version} frozen.",
    )
    return CatalogVersionOut.model_validate(version)


@router.get("/companies/{company_id}/catalog/versions", response_model=list[CatalogVersionOut])
def list_versions(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("catalog.read")),
) -> list[CatalogVersionOut]:
    rows = session.scalars(
        select(CatalogVersion)
        .where(CatalogVersion.company_id == access.company.id)
        .order_by(CatalogVersion.version.desc())
    )
    return [CatalogVersionOut.model_validate(row) for row in rows]


@router.get("/companies/{company_id}/catalog/versions/{version}")
def get_version_snapshot(
    version: int,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("catalog.read")),
) -> dict:
    """Read a frozen snapshot exactly as it was published."""
    record = session.scalar(
        select(CatalogVersion).where(
            CatalogVersion.company_id == access.company.id, CatalogVersion.version == version
        )
    )
    if record is None:
        raise NotFound(f"Catalog version {version} does not exist for this company.")
    return {
        "version": record.version,
        "published_at": record.published_at,
        "published_by": record.published_by,
        "note": record.note,
        "checksum_sha256": record.checksum_sha256,
        "snapshot": record.snapshot,
    }
