"""Request-scoped identity, tenant isolation and the entitlement model.

The chain every protected request walks:

    JWT -> User -> Company membership -> Role -> Permission -> Company scope

The company in the URL is treated as an *assertion by the caller*, never as
authorisation. ``AccessContext`` is resolved from the database on every request,
so editing a company id in a path or a token buys nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.errors import AuthenticationError, NotFound, PermissionDenied, TenantIsolationError
from app.core.security import decode_access_token
from app.models.base import Classification, MembershipStatus
from app.models.source import SourceColumn, SourceTable
from app.models.tenant import Company, CompanyUser, Permission, Role, RolePermission, User

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[Session, Depends(get_session)]


# ---------------------------------------------------------------------------
# Entitlement context
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AccessContext:
    """Everything the request is allowed to do, resolved once per request."""

    user: User
    company: Company
    membership: CompanyUser
    role: Role
    permissions: frozenset[str]
    row_scope: dict = field(default_factory=dict)
    denied_columns: frozenset[str] = frozenset()

    # -- permissions -----------------------------------------------------
    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, *permissions: str) -> None:
        missing = [p for p in permissions if p not in self.permissions]
        if missing:
            raise PermissionDenied(
                f"Your role ({self.role.role_key}) lacks: {', '.join(missing)}.",
                details={"missing_permissions": missing, "role": self.role.role_key},
            )

    @property
    def is_admin(self) -> bool:
        return bool(self.role.is_admin_role)

    # -- column-level entitlement ---------------------------------------
    def can_read_column(self, column: SourceColumn, *, table_name: str | None = None) -> bool:
        """Decide *before* reading, not after.

        Profiling asks this for every column and skips the ones it may not see,
        recording them as withheld. Nothing sensitive is read and then filtered
        out of a response.
        """
        qualified = f"{table_name or ''}.{column.column_name}".lstrip(".")
        if qualified in self.denied_columns or column.column_name in self.denied_columns:
            return False
        if column.is_restricted or column.classification == Classification.RESTRICTED:
            return self.has("data.read_restricted")
        if column.is_pii:
            return self.has("data.read_pii")
        if column.is_sensitive or column.classification == Classification.CONFIDENTIAL:
            return self.has("data.read_confidential")
        return True

    def withheld_reason(self, column: SourceColumn) -> str:
        if column.is_restricted or column.classification == Classification.RESTRICTED:
            return "RESTRICTED classification"
        if column.is_pii:
            return "personal data"
        if column.is_sensitive or column.classification == Classification.CONFIDENTIAL:
            return "CONFIDENTIAL classification"
        return "explicitly denied for this membership"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _load_permissions(session: Session, role_id: str) -> frozenset[str]:
    keys = session.execute(
        select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    ).scalars()
    return frozenset(keys)


def get_current_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication required.")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Session expired. Please sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid access token.") from exc

    user = session.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise AuthenticationError("Account is inactive or no longer exists.")

    # Stash for telemetry without another lookup.
    request.state.user_id = user.id
    request.state.token_company_id = payload.get("company_id")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def resolve_access(
    request: Request,
    session: Session,
    user: User,
    company_id: str,
) -> AccessContext:
    """Resolve entitlement for ``user`` acting inside ``company_id``."""
    company = session.get(Company, company_id)
    membership = session.scalar(
        select(CompanyUser).where(
            CompanyUser.company_id == company_id, CompanyUser.user_id == user.id
        )
    )

    # A non-member gets the same 403 whether or not the company exists:
    # confirming existence would itself leak across tenants.
    if company is None or membership is None:
        if user.is_platform_admin and company is not None:
            raise PermissionDenied(
                "Platform administrators must hold an explicit company membership "
                "to act inside a company workspace."
            )
        raise TenantIsolationError()

    if membership.status != MembershipStatus.ACTIVE:
        raise PermissionDenied(f"Your membership in this company is {membership.status}.")

    role = session.get(Role, membership.role_id)
    if role is None:  # pragma: no cover - FK protected
        raise PermissionDenied("Your membership has no valid role assigned.")

    request.state.company_id = company.id
    return AccessContext(
        user=user,
        company=company,
        membership=membership,
        role=role,
        permissions=_load_permissions(session, role.id),
        row_scope=dict(membership.row_scope or {}),
        denied_columns=frozenset(membership.denied_columns or []),
    )


def get_access(
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    company_id: str,
) -> AccessContext:
    """Path dependency for every ``/companies/{company_id}/...`` route."""
    return resolve_access(request, session, user, company_id)


AccessDep = Annotated[AccessContext, Depends(get_access)]


def require_permissions(*permissions: str):
    """Build a dependency asserting the caller holds every listed permission."""

    def dependency(access: AccessDep) -> AccessContext:
        access.require(*permissions)
        return access

    return dependency


# ---------------------------------------------------------------------------
# Scoped lookups
# ---------------------------------------------------------------------------
def load_scoped(session: Session, model: type, resource_id: str, access: AccessContext):
    """Fetch a tenant-owned row, refusing anything outside the caller's company.

    Using this everywhere is what stops an id from one company being passed to
    an endpoint authorised for another.
    """
    instance = session.get(model, resource_id)
    if instance is None:
        raise NotFound(f"{model.__name__} {resource_id} was not found.")
    owner = getattr(instance, "company_id", None)
    if owner != access.company.id:
        # Same response shape as a genuine miss.
        raise NotFound(f"{model.__name__} {resource_id} was not found.")
    return instance


def load_selected_table(session: Session, source_table_id: str, access: AccessContext) -> SourceTable:
    """Load a table and assert it is inside the company's approved data scope.

    Selection is the analytical boundary: profiling, catalog and KPI
    registration all refuse tables the administrator has not explicitly enabled.
    """
    table: SourceTable = load_scoped(session, SourceTable, source_table_id, access)
    selection = table.selection
    if selection is None or not selection.enabled:
        raise PermissionDenied(
            f"Table {table.qualified_name} is not in this company's approved data scope. "
            "Select it under Data Scope first.",
            details={"source_table_id": table.id, "table": table.qualified_name},
        )
    return table
