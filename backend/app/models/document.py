"""Company knowledge layer.

Sprint 1 deliberately stops at UPLOAD / STORE / VERSION / ACCESS CONTROL /
RETRIEVE METADATA. Chunking, embeddings and retrieval belong to the RAG sprint;
the tables below are shaped so that adding ``document_chunks`` later needs no
change here.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import (
    DocumentClass,
    DocumentStatus,
    DocumentType,
    Timestamped,
    UUIDPrimaryKey,
)


class CompanyDocument(Base, UUIDPrimaryKey, Timestamped):
    """The logical document. Content lives in ``CompanyDocumentVersion`` so a
    revision never overwrites the evidence an earlier investigation cited."""

    __tablename__ = "company_documents"
    __table_args__ = (
        UniqueConstraint("company_id", "document_key", name="uq_document_company_key"),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_key: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[str] = mapped_column(
        String(40), default=DocumentType.OTHER, nullable=False
    )
    document_class: Mapped[str] = mapped_column(
        String(20), default=DocumentClass.REFERENCE, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.ACTIVE, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Which roles may retrieve this document. Enforced at request time and,
    # later, before any semantic retrieval.
    access_scope: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Event documents carry business coordinates used later as evidence.
    tags: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )

    versions: Mapped[list["CompanyDocumentVersion"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="CompanyDocumentVersion.version",
    )


class CompanyDocumentVersion(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "company_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_version"),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("company_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(600))
    original_filename: Mapped[str | None] = mapped_column(String(300))
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    # Short policies can be stored inline instead of as a file.
    inline_content: Mapped[str | None] = mapped_column(Text)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[CompanyDocument] = relationship(back_populates="versions")
