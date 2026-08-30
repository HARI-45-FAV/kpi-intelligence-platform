"""Company document store.

Sprint 1's scope is exactly: upload, store, version, access-control, retrieve
metadata. Chunking, embedding and retrieval belong to the RAG sprint, and doing
them early would mean embedding documents before the access model that must gate
retrieval is proven.

Versioning is the substantive part. Replacing a policy creates v(n+1); v(n) stays
readable, because a KPI contract can cite "KPI Handbook v3" and an investigation
recorded against it has to remain reproducible after the handbook changes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import settings
from app.core.deps import AccessContext
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailure
from app.models.base import (
    REFERENCE_DOCUMENT_TYPES,
    DocumentClass,
    DocumentStatus,
    DocumentType,
)
from app.models.document import CompanyDocument, CompanyDocumentVersion

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_CHUNK = 1024 * 1024

# Extensions accepted for reference material. Anything executable is refused.
ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".csv", ".json", ".docx", ".xlsx", ".pptx", ".rtf", ".html",
}


@dataclass(slots=True)
class DocumentWritePayload:
    title: str
    document_type: str = DocumentType.OTHER
    document_key: str | None = None
    description: str | None = None
    access_scope: list[str] | None = None
    tags: dict | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    change_note: str | None = None
    inline_content: str | None = None


def classify_document(document_type: str) -> str:
    """Reference material describes how the company operates; event material
    describes what happened. The split matters for later retrieval."""
    return (
        DocumentClass.REFERENCE
        if document_type in REFERENCE_DOCUMENT_TYPES
        else DocumentClass.EVENT
    )


#: What a document is retrieved *for*. A handbook or a policy explains what a
#: measure means and how the business defines it; an incident, campaign or
#: management note records what happened, on dates. Those answer different
#: questions, and quoting a definition as though it were evidence that something
#: occurred is wrong in a way that reads convincingly -- so the purpose is stated
#: on every retrieved passage rather than inferred downstream from its prose.
DEFINITION_PURPOSE = "business_definition"
EVIDENCE_PURPOSE = "business_evidence"


def retrieval_purpose(document_class: str) -> str:
    """The purpose a document of this class serves when retrieved."""
    return (
        DEFINITION_PURPOSE
        if document_class == DocumentClass.REFERENCE
        else EVIDENCE_PURPOSE
    )


def create_document(
    session: Session, access: AccessContext, payload: DocumentWritePayload
) -> CompanyDocument:
    key = _slug(payload.document_key or payload.title)
    existing = session.scalar(
        select(CompanyDocument).where(
            CompanyDocument.company_id == access.company.id,
            CompanyDocument.document_key == key,
        )
    )
    if existing is not None:
        raise Conflict(
            f"A document with key '{key}' already exists. Upload a new version of it "
            "instead of creating a duplicate.",
            details={"document_id": existing.id, "document_key": key},
        )

    document = CompanyDocument(
        company_id=access.company.id,
        document_key=key,
        title=payload.title,
        description=payload.description,
        document_type=payload.document_type,
        document_class=classify_document(payload.document_type),
        status=DocumentStatus.ACTIVE,
        current_version=0,
        access_scope=payload.access_scope or [],
        tags=payload.tags or {},
        owner_user_id=access.user.id,
    )
    session.add(document)
    session.flush()
    return document


def add_version(
    session: Session,
    access: AccessContext,
    document: CompanyDocument,
    payload: DocumentWritePayload,
    *,
    upload: BinaryIO | None = None,
    original_filename: str | None = None,
    content_type: str | None = None,
) -> CompanyDocumentVersion:
    """Store a new version. The previous version is retained, not replaced."""
    if upload is None and not payload.inline_content:
        raise ValidationFailure("Provide either a file upload or inline content.")

    next_version = (document.current_version or 0) + 1
    storage_path: str | None = None
    checksum: str | None = None
    size_bytes: int | None = None

    if upload is not None:
        storage_path, checksum, size_bytes = _persist_file(
            company_id=document.company_id,
            document_key=document.document_key,
            version=next_version,
            upload=upload,
            original_filename=original_filename,
        )
    elif payload.inline_content:
        encoded = payload.inline_content.encode("utf-8")
        size_bytes = len(encoded)
        checksum = hashlib.sha256(encoded).hexdigest()

    # Only one version is current at a time.
    for existing in document.versions:
        existing.is_current = False

    version = CompanyDocumentVersion(
        company_id=document.company_id,
        version=next_version,
        storage_path=storage_path,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum,
        inline_content=payload.inline_content,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        is_current=True,
        change_note=payload.change_note,
        uploaded_by=access.user.id,
        uploaded_at=utcnow(),
    )
    # Appended through the relationship rather than by setting document_id: that
    # sets the foreign key *and* keeps the already-loaded collection correct, so
    # the response reflects the new version instead of the stale list.
    document.versions.append(version)
    session.flush()

    document.current_version = next_version
    if document.status == DocumentStatus.SUPERSEDED:
        document.status = DocumentStatus.ACTIVE
    return version


def update_metadata(
    session: Session,
    access: AccessContext,
    document: CompanyDocument,
    payload: DocumentWritePayload,
) -> CompanyDocument:
    document.title = payload.title or document.title
    if payload.description is not None:
        document.description = payload.description
    if payload.document_type:
        document.document_type = payload.document_type
        document.document_class = classify_document(payload.document_type)
    if payload.access_scope is not None:
        document.access_scope = payload.access_scope
    if payload.tags is not None:
        document.tags = payload.tags
    return document


def archive(session: Session, document: CompanyDocument) -> CompanyDocument:
    """Archive rather than delete: a cited document must remain resolvable."""
    document.status = DocumentStatus.ARCHIVED
    return document


def assert_readable(document: CompanyDocument, access: AccessContext) -> None:
    """Document-level entitlement, enforced before content is returned.

    An empty ``access_scope`` means every member of the company may read it.
    """
    if access.is_admin or not document.access_scope:
        return
    if access.role.role_key not in document.access_scope:
        raise PermissionDenied(
            f"'{document.title}' is restricted to: {', '.join(document.access_scope)}.",
            details={"required_roles": document.access_scope, "your_role": access.role.role_key},
        )


def permits_document_scope(document: CompanyDocument, access: AccessContext) -> bool:
    """Does the membership's own document-scope list admit this document?

    An empty list is unrestricted. A non-empty one names the kinds of document
    this membership may read, matched against either label the document carries,
    so an operator who wrote the class and one who wrote the type both get what
    they meant.
    """
    if not access.allowed_document_scopes:
        return True
    return access.allows_document_scope(document.document_type) or access.allows_document_scope(
        document.document_class
    )


def permits_row_scope(document: CompanyDocument, access: AccessContext) -> bool:
    """Does the membership's row scope permit this document's business coordinates?

    ``tags`` is where a document records which part of the business it belongs
    to: region, sector, channel, event id. A membership restricted to one region
    must not reach another region's incident note through retrieval any more than
    through a query -- an answer assembled from material the caller could not
    have opened is the same disclosure by a longer route.

    The per-coordinate decision is ``AccessContext.permits_scope_value``, the same
    predicate a grouped read uses, so a document and a query row are judged by one
    rule. A coordinate the document does not state cannot conflict with a scope,
    so an untagged document stays visible -- the rule ``allows_domain`` already
    applies to an unlabelled item.
    """
    return all(
        access.permits_scope_value(coordinate, value)
        for coordinate, value in (document.tags or {}).items()
    )


def is_retrievable(document: CompanyDocument, access: AccessContext) -> bool:
    """The full entitlement gate every retrieval path shares.

    Retrieval assembles material the caller never asked for by name, so it has to
    be at least as strict as opening a document deliberately: the document's own
    role scope, then the two membership axes. Returned as a boolean rather than
    raised, because a retrieval path skips what it may not read -- naming a
    restricted document is itself a disclosure.
    """
    try:
        assert_readable(document, access)
    except PermissionDenied:
        return False
    return permits_document_scope(document, access) and permits_row_scope(document, access)


def resolve_version(
    document: CompanyDocument, version: int | None
) -> CompanyDocumentVersion:
    if version is None:
        current = next((v for v in document.versions if v.is_current), None)
        if current is None:
            raise NotFound(f"'{document.title}' has no stored version yet.")
        return current
    match = next((v for v in document.versions if v.version == version), None)
    if match is None:
        raise NotFound(f"'{document.title}' has no version {version}.")
    return match


def read_content(version: CompanyDocumentVersion) -> tuple[bytes, str]:
    if version.inline_content is not None:
        return (version.inline_content.encode("utf-8"), version.content_type or "text/plain")
    if not version.storage_path:
        raise NotFound("This document version has no stored content.")
    path = Path(version.storage_path)
    if not path.exists():
        raise NotFound("Stored file is missing from document storage.")
    return (path.read_bytes(), version.content_type or "application/octet-stream")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _persist_file(
    *,
    company_id: str,
    document_key: str,
    version: int,
    upload: BinaryIO,
    original_filename: str | None,
) -> tuple[str, str, int]:
    safe_name = _SAFE_FILENAME.sub("_", original_filename or "document")
    suffix = Path(safe_name).suffix.lower()
    if suffix and suffix not in ALLOWED_EXTENSIONS:
        raise ValidationFailure(
            f"File type '{suffix}' is not accepted for reference documents.",
            details={"allowed": sorted(ALLOWED_EXTENSIONS)},
        )

    # Files are stored under a per-company directory so one tenant's documents
    # are never reachable by guessing another's path.
    directory = Path(settings.document_storage_dir) / company_id / document_key
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"v{version}_{safe_name}"

    digest = hashlib.sha256()
    written = 0
    with target.open("wb") as handle:
        while chunk := upload.read(_CHUNK):
            written += len(chunk)
            if written > settings.max_document_bytes:
                handle.close()
                target.unlink(missing_ok=True)
                raise ValidationFailure(
                    f"Document exceeds the {settings.max_document_bytes // (1024 * 1024)} MB limit."
                )
            digest.update(chunk)
            handle.write(chunk)

    if written == 0:
        target.unlink(missing_ok=True)
        raise ValidationFailure("Uploaded file is empty.")

    return (str(target), digest.hexdigest(), written)


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in (value or "").lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    slug = cleaned.strip("_")[:110]
    if not slug:
        raise ValidationFailure("A document needs a title containing letters or digits.")
    return slug
