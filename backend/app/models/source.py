"""Data source registry: sources, discovered tables/columns, explicit scope, health."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import (
    Classification,
    ConnectionStatus,
    DataSourceType,
    FreshnessStatus,
    RefreshFrequency,
    SemanticType,
    Timestamped,
    UUIDPrimaryKey,
)


class DataSource(Base, UUIDPrimaryKey, Timestamped):
    """A registered tenant database.

    Credentials live in ``encrypted_credentials`` and are decrypted only inside
    a connector. No API schema ever exposes that column.
    """

    __tablename__ = "data_sources"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_source_company_name"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    database_name: Mapped[str | None] = mapped_column(String(160))
    schema_name: Mapped[str | None] = mapped_column(String(160), default="public")
    username: Mapped[str | None] = mapped_column(String(160))
    encrypted_credentials: Mapped[str | None] = mapped_column(Text)
    # Non-secret extras: sslmode, warehouse, project id, ...
    options: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    connection_status: Mapped[str] = mapped_column(
        String(20), default=ConnectionStatus.UNTESTED, nullable=False
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_error: Mapped[str | None] = mapped_column(Text)

    refresh_frequency: Mapped[str] = mapped_column(
        String(20), default=RefreshFrequency.UNKNOWN, nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    known_limitations: Mapped[str | None] = mapped_column(Text)

    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tables: Mapped[list["SourceTable"]] = relationship(
        back_populates="data_source", cascade="all, delete-orphan"
    )


class SourceTable(Base, UUIDPrimaryKey, Timestamped):
    """A table discovered in a source. Discovery alone grants no analytical access."""

    __tablename__ = "source_tables"
    __table_args__ = (
        UniqueConstraint(
            "data_source_id", "schema_name", "table_name", name="uq_source_table_identity"
        ),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    database_name: Mapped[str | None] = mapped_column(String(160))
    schema_name: Mapped[str] = mapped_column(String(160), nullable=False)
    table_name: Mapped[str] = mapped_column(String(200), nullable=False)
    table_type: Mapped[str] = mapped_column(String(20), default="TABLE", nullable=False)
    approx_row_count: Mapped[int | None] = mapped_column(Integer)
    column_count: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    data_source: Mapped[DataSource] = relationship(back_populates="tables")
    columns: Mapped[list["SourceColumn"]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    selection: Mapped["SelectedTable | None"] = relationship(
        back_populates="table", cascade="all, delete-orphan", uselist=False
    )

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


class SourceColumn(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "source_columns"
    __table_args__ = (
        UniqueConstraint("source_table_id", "column_name", name="uq_source_column_identity"),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_name: Mapped[str] = mapped_column(String(200), nullable=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    data_type: Mapped[str] = mapped_column(String(80), nullable=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_value: Mapped[str | None] = mapped_column(Text)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_foreign_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    references_table: Mapped[str | None] = mapped_column(String(200))
    references_column: Mapped[str | None] = mapped_column(String(200))

    semantic_type: Mapped[str] = mapped_column(
        String(30), default=SemanticType.UNKNOWN, nullable=False
    )
    # Sprint 1 builds the data model for sensitivity; it does not attempt
    # AI-based PII discovery.
    classification: Mapped[str] = mapped_column(
        String(20), default=Classification.INTERNAL, nullable=False
    )
    is_pii: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_restricted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)

    table: Mapped[SourceTable] = relationship(back_populates="columns")


class SelectedTable(Base, UUIDPrimaryKey, Timestamped):
    """Explicit administrator consent for a table to enter semantic processing.

    This is the maximum analytical scope for the company: profiling, catalog
    and KPI registration all refuse to touch an unselected table.
    """

    __tablename__ = "selected_tables"
    __table_args__ = (UniqueConstraint("source_table_id", name="uq_selected_table"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    business_alias: Mapped[str | None] = mapped_column(String(200))
    declared_grain: Mapped[str | None] = mapped_column(String(300))
    # Column used for freshness/time series. Set by admin or inferred.
    primary_time_column: Mapped[str | None] = mapped_column(String(200))
    selected_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    table: Mapped[SourceTable] = relationship(back_populates="selection")


class SourceHealth(Base, UUIDPrimaryKey, Timestamped):
    """Freshness / coverage observation.

    Rows are never deleted or corrected — a stale source stays recorded as
    stale so the Sprint 4 confidence engine can discount evidence built on it.
    """

    __tablename__ = "source_health"

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_tables.id", ondelete="CASCADE"), index=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_column: Mapped[str | None] = mapped_column(String(200))
    coverage_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    freshness_lag_seconds: Mapped[int | None] = mapped_column(Integer)
    expected_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    freshness_status: Mapped[str] = mapped_column(
        String(20), default=FreshnessStatus.UNKNOWN, nullable=False
    )
    row_count: Mapped[int | None] = mapped_column(Integer)
    completeness_pct: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
