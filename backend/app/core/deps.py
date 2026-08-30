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


def _scope_values(value: object) -> set[str]:
    """The comparable values behind one scope entry, whatever shape it was stored in.

    Row scope is administrator-authored JSON, so a coordinate may hold a single
    string or a list, in whatever case someone typed. Comparison is normalised
    here so no caller has to remember to do it.
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    text = str(value).strip().lower()
    return {text} if text else set()


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
    # Business domains and document scopes this membership may reach. Empty means
    # unrestricted in both cases, which is what an administrator holds. They are
    # carried here so later retrieval resolves *what may be searched* from the
    # membership, before a question is ever read.
    allowed_domains: frozenset[str] = frozenset()
    allowed_document_scopes: frozenset[str] = frozenset()

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

    # -- access scope ----------------------------------------------------
    @property
    def allowed_data_scopes(self) -> dict:
        """Row-level data scope, under the name the wider design uses for it."""
        return self.row_scope

    def allows_domain(self, domain: str | None) -> bool:
        """Empty scope means unrestricted; an unlabelled item is never hidden."""
        if not self.allowed_domains or domain is None:
            return True
        return domain in self.allowed_domains

    def allows_document_scope(self, scope: str | None) -> bool:
        if not self.allowed_document_scopes or scope is None:
            return True
        return scope in self.allowed_document_scopes

    def permits_scope_value(self, coordinate: str | None, value: object) -> bool:
        """Does the row scope permit this value of one business coordinate?

        ``row_scope`` is written by an administrator as ``{"region": ["North"]}``
        or ``{"region": "North"}``, so both shapes are accepted and compared
        case-insensitively -- an entitlement that depends on how someone typed a
        region is not an entitlement.

        A coordinate the scope does not mention is unrestricted, and a value that
        was not supplied cannot conflict with anything. Everything that *is*
        constrained is checked here, in one place, so a dimension a user typed by
        hand goes through the same gate as one the platform chose. Any caller
        about to read, group by or retrieve along a coordinate asks this first.
        """
        if not self.row_scope or coordinate is None or value is None:
            return True
        permitted = self.row_scope.get(coordinate)
        if permitted is None:
            return True
        allowed = _scope_values(permitted)
        if not allowed:
            return True
        stated = _scope_values(value)
        return stated.issubset(allowed) if stated else True

    def as_scope(self) -> dict:
        """The resolved access scope, in one serialisable shape.

        This is the contract later stages read instead of re-deriving
        entitlement: company, roles, permissions and the three scope axes. It
        carries no credential and no user record, so it is safe to log or return.
        """
        return {
            "company_id": self.company.id,
            "user_id": self.user.id,
            "roles": [self.role.role_key],
            "permissions": sorted(self.permissions),
            "allowed_domains": sorted(self.allowed_domains),
            "allowed_data_scopes": dict(self.row_scope),
            "allowed_document_scopes": sorted(self.allowed_document_scopes),
            "denied_columns": sorted(self.denied_columns),
            "is_admin": self.is_admin,
        }

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
        allowed_domains=frozenset(membership.allowed_domains or []),
        allowed_document_scopes=frozenset(membership.allowed_document_scopes or []),
    )


def resolve_access_context(
    session: Session, user: User, company: Company | str
) -> AccessContext:
    """Resolve entitlement outside a request.

    The same chain as ``resolve_access`` — membership, role, permissions, scope —
    for callers that have no ``Request``: background detection runs, the governed
    Copilot tool layer, and tests. Deliberately the *only* other door: anything
    that needs to know what a user may do inside a company comes through here
    rather than reading ``CompanyUser`` and interpreting the columns itself.
    """
    company_id = company if isinstance(company, str) else company.id
    return resolve_access(_NoRequest(), session, user, company_id)  # type: ignore[arg-type]


class _NoRequest:
    """Stand-in for the request-state stash when there is no request."""

    class _State:
        def __setattr__(self, name: str, value: object) -> None:
            return None

    state = _State()


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
