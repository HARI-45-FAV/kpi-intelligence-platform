"""Observability: audit trail and execution telemetry.

Sprint 1 has no LLM calls, but the telemetry schema already carries the columns
Sprint 4 needs (model, tokens, cost) so instrumentation does not have to be
retrofitted through every service later.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import Timestamped, UUIDPrimaryKey, UtcDateTime


class AuditLog(Base, UUIDPrimaryKey):
    """Who changed what, when, and from which version to which.

    Append-only by convention: there is no update or delete path in the API.
    """

    __tablename__ = "audit_logs"

    company_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(80))
    resource_label: Mapped[str | None] = mapped_column(String(300))
    old_version: Mapped[str | None] = mapped_column(String(40))
    new_version: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str] = mapped_column(String(20), default="SUCCESS", nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(36), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, index=True
    )


class ExecutionLog(Base, UUIDPrimaryKey):
    """Per-request / per-operation runtime telemetry."""

    __tablename__ = "execution_logs"

    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    company_id: Mapped[str | None] = mapped_column(String(36), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    service: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(80))
    http_method: Mapped[str | None] = mapped_column(String(10))
    http_path: Mapped[str | None] = mapped_column(String(300))
    http_status: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False, index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="OK", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    # Connector work attributable to this request.
    connector: Mapped[str | None] = mapped_column(String(40))
    # Hash only -- never the query text, which can contain business data.
    query_hash: Mapped[str | None] = mapped_column(String(64))
    query_count: Mapped[int | None] = mapped_column(Integer)
    query_duration_ms: Mapped[int | None] = mapped_column(Integer)
    rows_returned: Mapped[int | None] = mapped_column(Integer)

    # Model accounting for the optional Copilot layer. NULL -- not zero -- on
    # every request that contacted no model, so "no model ran" and "a model ran
    # and reported nothing" stay distinguishable. Never holds a prompt, an
    # answer or a credential.
    llm_model: Mapped[str | None] = mapped_column(String(80))
    llm_calls: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)

    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class SystemEvent(Base, UUIDPrimaryKey, Timestamped):
    """Coarse activity feed powering the dashboard's "Recent Activity" panel."""

    __tablename__ = "system_events"

    company_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="INFO", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False, index=True
    )
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
