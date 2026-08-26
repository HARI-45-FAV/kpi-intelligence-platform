"""Company document store API.

Sprint 1 scope: upload, store, version, access-control, retrieve metadata.
No chunking, no embeddings, no retrieval — those arrive with the RAG sprint and
will read the access rules proven here.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from sqlalchemy import select

from app.core.deps import AccessContext, SessionDep, load_scoped, require_permissions
from app.core.errors import ValidationFailure
from app.models.base import DocumentClass, DocumentType
from app.models.document import CompanyDocument
from app.schemas import DocumentCreate, DocumentOut, DocumentVersionOut
from app.services import audit, documents as document_service
from app.services.documents import DocumentWritePayload

router = APIRouter(tags=["documents"])


def _document_out(document: CompanyDocument) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        document_key=document.document_key,
        title=document.title,
        description=document.description,
        document_type=document.document_type,
        document_class=document.document_class,
        status=document.status,
        current_version=document.current_version,
        access_scope=document.access_scope or [],
        tags=document.tags or {},
        owner_user_id=document.owner_user_id,
        created_at=document.created_at,
        updated_at=document.updated_at,
        versions=[
            DocumentVersionOut(
                id=v.id,
                version=v.version,
                original_filename=v.original_filename,
                content_type=v.content_type,
                size_bytes=v.size_bytes,
                checksum_sha256=v.checksum_sha256,
                effective_from=v.effective_from,
                effective_to=v.effective_to,
                is_current=v.is_current,
                change_note=v.change_note,
                uploaded_by=v.uploaded_by,
                uploaded_at=v.uploaded_at,
                has_inline_content=v.inline_content is not None,
            )
            for v in sorted(document.versions, key=lambda x: x.version)
        ],
        retrieval_ready=False,
    )


def _payload(data: DocumentCreate) -> DocumentWritePayload:
    return DocumentWritePayload(
        title=data.title,
        document_type=data.document_type,
        document_key=data.document_key,
        description=data.description,
        access_scope=data.access_scope,
        tags=data.tags,
        effective_from=data.effective_from,
        effective_to=data.effective_to,
        change_note=data.change_note,
        inline_content=data.inline_content,
    )


@router.get("/companies/{company_id}/document-types")
def list_document_types() -> dict:
    """The two document classes matter downstream.

    Reference documents describe *how the company operates*; event documents
    describe *what happened*. RAG will answer different questions from each.
    """
    return {
        "types": [
            {
                "value": value,
                "label": value.replace("_", " ").title(),
                "document_class": document_service.classify_document(value),
            }
            for value in DocumentType
        ],
        "classes": {
            DocumentClass.REFERENCE: "Defines how the company operates. Relatively stable.",
            DocumentClass.EVENT: "Describes what happened. Time-dependent evidence.",
        },
    }


@router.get("/companies/{company_id}/documents", response_model=list[DocumentOut])
def list_documents(
    session: SessionDep,
    document_class: str | None = None,
    access: AccessContext = Depends(require_permissions("document.read")),
) -> list[DocumentOut]:
    query = select(CompanyDocument).where(CompanyDocument.company_id == access.company.id)
    if document_class:
        query = query.where(CompanyDocument.document_class == document_class.upper())
    rows = session.scalars(query.order_by(CompanyDocument.title))
    # Documents outside the caller's scope are omitted from the listing, not
    # returned with their metadata redacted.
    visible = []
    for document in rows:
        try:
            document_service.assert_readable(document, access)
        except Exception:
            continue
        visible.append(_document_out(document))
    return visible


@router.post(
    "/companies/{company_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_document(
    session: SessionDep,
    request: Request,
    metadata: str = Form(..., description="JSON body matching DocumentCreate"),
    file: UploadFile | None = File(default=None),
    access: AccessContext = Depends(require_permissions("document.manage")),
) -> DocumentOut:
    """Create a document and store its first version.

    Multipart so a file and its governance metadata arrive together — a stored
    document with no effective date or access scope is not governed material.
    """
    try:
        parsed = DocumentCreate.model_validate(json.loads(metadata))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(f"metadata is not valid JSON: {exc}") from exc

    payload = _payload(parsed)
    document = document_service.create_document(session, access, payload)
    version = document_service.add_version(
        session,
        access,
        document,
        payload,
        upload=file.file if file is not None else None,
        original_filename=file.filename if file is not None else None,
        content_type=file.content_type if file is not None else "text/plain",
    )
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.DOCUMENT_CREATED,
        resource_type="document",
        resource_id=document.id,
        resource_label=document.title,
        summary=f"Uploaded {document.title} v{version.version}.",
        new_version=str(version.version),
        details={
            "document_type": document.document_type,
            "document_class": document.document_class,
            "access_scope": document.access_scope,
            "size_bytes": version.size_bytes,
            "checksum": version.checksum_sha256,
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="DOCUMENT",
        title="Document uploaded",
        message=f"{document.title} v{version.version}",
    )
    return _document_out(document)


@router.get("/companies/{company_id}/documents/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("document.read")),
) -> DocumentOut:
    document: CompanyDocument = load_scoped(session, CompanyDocument, document_id, access)
    document_service.assert_readable(document, access)
    return _document_out(document)


@router.post(
    "/companies/{company_id}/documents/{document_id}/versions", response_model=DocumentOut
)
async def add_document_version(
    document_id: str,
    session: SessionDep,
    request: Request,
    metadata: str = Form(...),
    file: UploadFile | None = File(default=None),
    access: AccessContext = Depends(require_permissions("document.manage")),
) -> DocumentOut:
    """Add v(n+1). The previous version is retained, never overwritten —
    a KPI contract citing "Handbook v3" must stay resolvable."""
    document: CompanyDocument = load_scoped(session, CompanyDocument, document_id, access)
    try:
        parsed = DocumentCreate.model_validate(json.loads(metadata))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(f"metadata is not valid JSON: {exc}") from exc

    payload = _payload(parsed)
    previous = document.current_version
    version = document_service.add_version(
        session,
        access,
        document,
        payload,
        upload=file.file if file is not None else None,
        original_filename=file.filename if file is not None else None,
        content_type=file.content_type if file is not None else "text/plain",
    )
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.DOCUMENT_VERSION_ADDED,
        resource_type="document",
        resource_id=document.id,
        resource_label=document.title,
        summary=f"Added {document.title} v{version.version}.",
        old_version=str(previous),
        new_version=str(version.version),
        details={"change_note": payload.change_note},
        request=request,
    )
    return _document_out(document)


@router.patch("/companies/{company_id}/documents/{document_id}", response_model=DocumentOut)
def update_document(
    document_id: str,
    payload: DocumentCreate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("document.manage")),
) -> DocumentOut:
    document: CompanyDocument = load_scoped(session, CompanyDocument, document_id, access)
    document_service.update_metadata(session, access, document, _payload(payload))
    session.flush()
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.DOCUMENT_UPDATED,
        resource_type="document",
        resource_id=document.id,
        resource_label=document.title,
        summary="Updated document metadata.",
        details={"access_scope": document.access_scope, "type": document.document_type},
        request=request,
    )
    return _document_out(document)


@router.get("/companies/{company_id}/documents/{document_id}/content")
def download_document(
    document_id: str,
    session: SessionDep,
    version: int | None = None,
    access: AccessContext = Depends(require_permissions("document.read")),
) -> Response:
    document: CompanyDocument = load_scoped(session, CompanyDocument, document_id, access)
    document_service.assert_readable(document, access)
    target = document_service.resolve_version(document, version)
    content, content_type = document_service.read_content(target)
    filename = target.original_filename or f"{document.document_key}_v{target.version}.txt"
    return Response(
        content=content,
        media_type=content_type,
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/companies/{company_id}/documents/{document_id}/archive", response_model=DocumentOut)
def archive_document(
    document_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("document.manage")),
) -> DocumentOut:
    """Archive, never delete: a cited document must remain resolvable."""
    document: CompanyDocument = load_scoped(session, CompanyDocument, document_id, access)
    document_service.archive(session, document)
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.DOCUMENT_ARCHIVED,
        resource_type="document",
        resource_id=document.id,
        resource_label=document.title,
        summary=f"Archived {document.title}.",
        request=request,
    )
    return _document_out(document)
