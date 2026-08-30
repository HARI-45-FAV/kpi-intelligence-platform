"""Audit trail and activity feed writers.

Every governance action goes through ``record``. The audit log is append-only by
convention — no endpoint updates or deletes it — which is what makes "who
approved Revenue v2, and when" answerable months later.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.deps import AccessContext
from app.models.observability import AuditLog, SystemEvent


class AuditAction:
    COMPANY_CREATED = "company.created"
    COMPANY_UPDATED = "company.updated"
    COMPANY_ACTIVATED = "company.activated"
    CALENDAR_UPDATED = "calendar.updated"

    USER_REGISTERED = "user.registered"
    USER_LOGGED_IN = "user.logged_in"
    MEMBER_ADDED = "member.added"
    MEMBER_UPDATED = "member.updated"
    # Split out of MEMBER_UPDATED so the history screen can answer "who changed
    # what someone may reach" without reading every membership edit. Same writer,
    # same table — only the action key differs.
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_SCOPE_UPDATED = "member.scope_updated"
    MEMBER_REMOVED = "member.removed"

    SOURCE_CREATED = "source.created"
    SOURCE_UPDATED = "source.updated"
    SOURCE_TESTED = "source.tested"
    SOURCE_DELETED = "source.deleted"
    TABLES_DISCOVERED = "source.tables_discovered"
    SCOPE_UPDATED = "source.scope_updated"

    PROFILE_RUN = "profiling.executed"
    GRAIN_DETECTED = "profiling.grain_detected"
    RELATIONSHIPS_DETECTED = "profiling.relationships_detected"
    JOIN_SAFETY_ANALYSED = "profiling.join_safety_analysed"
    FRESHNESS_CHECKED = "profiling.freshness_checked"
    RECONCILIATION_ANALYSED = "profiling.reconciliation_analysed"
    COLUMN_CLASSIFIED = "profiling.column_classified"

    DOCUMENT_CREATED = "document.created"
    DOCUMENT_VERSION_ADDED = "document.version_added"
    DOCUMENT_UPDATED = "document.updated"
    DOCUMENT_ARCHIVED = "document.archived"

    CATALOG_PUBLISHED = "catalog.published"

    KPI_CREATED = "kpi.created"
    KPI_PROPOSED = "kpi.proposed"
    KPI_IMPORTED = "kpi.imported_from_source"
    KPI_UPDATED = "kpi.updated"
    KPI_SUBMITTED = "kpi.submitted_for_review"
    KPI_VALIDATED = "kpi.validated"
    KPI_APPROVED = "kpi.approved"
    KPI_ACTIVATED = "kpi.activated"
    KPI_REJECTED = "kpi.rejected"
    KPI_DEPRECATED = "kpi.deprecated"
    KPI_VERSION_CREATED = "kpi.version_created"

    BUCKET_CONFIG_CREATED = "detection.bucket_config_created"
    BUCKET_CONFIG_UPDATED = "detection.bucket_config_updated"
    BUCKET_CONFIG_EXTRACTED = "detection.bucket_config_extracted"
    BUCKET_CONFIG_APPROVED = "detection.bucket_config_approved"
    BUCKET_CONFIG_ARCHIVED = "detection.bucket_config_archived"
    DETECTION_RUN = "detection.executed"
    AGENT_RUN = "AGENT_RUN"

    # Investigation is a read, but a read of the company's own business data
    # broken down by an entity someone chose -- so who looked at which part of the
    # business, and when, is exactly what an audit trail is for.
    CONTRIBUTION_ANALYSED = "investigation.contribution_analysed"
    ENTITY_ANALYSED = "investigation.entity_analysed"


def record(
    session: Session,
    *,
    access: AccessContext | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    resource_label: str | None = None,
    summary: str | None = None,
    old_version: str | None = None,
    new_version: str | None = None,
    outcome: str = "SUCCESS",
    details: dict[str, Any] | None = None,
    request: Request | None = None,
    company_id: str | None = None,
    user_id: str | None = None,
    actor_email: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        company_id=company_id or (access.company.id if access else None),
        user_id=user_id or (access.user.id if access else None),
        actor_email=actor_email or (access.user.email if access else None),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_label=resource_label,
        old_version=old_version,
        new_version=new_version,
        outcome=outcome,
        summary=summary,
        details=_scrub(details or {}),
        request_id=getattr(request.state, "request_id", None) if request else None,
        ip_address=_client_ip(request),
        occurred_at=utcnow(),
    )
    session.add(entry)
    return entry


def event(
    session: Session,
    *,
    company_id: str | None,
    category: str,
    title: str,
    message: str | None = None,
    severity: str = "INFO",
    details: dict[str, Any] | None = None,
) -> SystemEvent:
    """Coarse activity item for the dashboard feed."""
    item = SystemEvent(
        company_id=company_id,
        category=category,
        severity=severity,
        title=title,
        message=message,
        occurred_at=utcnow(),
        details=_scrub(details or {}),
    )
    session.add(item)
    return item


_SECRET_HINTS = ("password", "secret", "token", "key", "credential", "dsn", "connection_uri")


def _scrub(payload: dict[str, Any]) -> dict[str, Any]:
    """Never let a credential reach the audit trail."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            clean[key] = "[redacted]"
        elif isinstance(value, dict):
            clean[key] = _scrub(value)
        else:
            clean[key] = value
    return clean


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None
