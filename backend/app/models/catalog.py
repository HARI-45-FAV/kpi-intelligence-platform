"""Versioned semantic catalog snapshots.

Two version concepts must never be conflated (sprint 1 §39):

* **Catalog version** -- "what did we know about this company's data then?"
* **KPI version**     -- "what did this company mean by Revenue then?"

A catalog version is immutable. Re-publishing produces v2; v1 is retained so an
investigation recorded months ago can be reproduced exactly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import Timestamped, UUIDPrimaryKey


class CatalogVersion(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "catalog_versions"
    __table_args__ = (UniqueConstraint("company_id", "version", name="uq_catalog_version"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    selected_table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profiled_table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    relationship_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_kpi_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
