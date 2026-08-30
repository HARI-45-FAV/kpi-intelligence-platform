"""Company workspace: profile, calendars, members and roles."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, or_, select

from app.core.deps import (
    AccessContext,
    CurrentUser,
    SessionDep,
    load_scoped,
    require_permissions,
)
from app.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailure
from app.core.permissions import ADMIN_ROLE_KEY, ROLES_BY_KEY
from app.core.security import hash_password
from app.models.base import CompanyStatus, MembershipStatus
from app.models.tenant import (
    Company,
    CompanyCalendar,
    CompanyUser,
    Permission,
    Role,
    RolePermission,
    User,
)
from app.schemas import (
    AccessScopeOut,
    CalendarOut,
    CalendarUpsert,
    CompanyCreate,
    CompanyOut,
    CompanyUpdate,
    MemberInvite,
    MemberOut,
    MemberUpdate,
    RoleOut,
)
from app.services import audit

router = APIRouter(tags=["companies"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")[:80]
    if not slug:
        raise ValidationFailure("Company name must contain letters or digits.")
    return slug


def _unique_slug(session, base: str) -> str:
    slug = base
    suffix = 2
    while session.scalar(select(Company).where(Company.slug == slug)) is not None:
        slug = f"{base[:74]}-{suffix}"
        suffix += 1
    return slug


def _role_for(session, company_id: str, role_key: str) -> Role:
    """Resolve a role, preferring a company-specific override of a shared role.

    Note the explicit OR rather than ``IN (company_id, None)``: in SQL, ``x IN
    (..., NULL)`` never matches a NULL row, so an IN-list would silently fail to
    find the platform-wide roles, which are exactly the ones stored with
    ``company_id NULL``.
    """
    role = session.scalar(
        select(Role)
        .where(
            Role.role_key == role_key,
            or_(Role.company_id == company_id, Role.company_id.is_(None)),
        )
        # Company-specific first (is_(None) is False -> 0), platform role second.
        .order_by(Role.company_id.is_(None))
    )
    if role is None:
        raise NotFound(
            f"Role '{role_key}' does not exist. Available: {', '.join(sorted(ROLES_BY_KEY))}."
        )
    return role


def _member_out(membership: CompanyUser, user: User, role: Role) -> MemberOut:
    return MemberOut(
        membership_id=membership.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role_key=role.role_key,
        role_name=role.name,
        is_admin_role=role.is_admin_role,
        status=membership.status,
        row_scope=membership.row_scope or {},
        denied_columns=membership.denied_columns or [],
        allowed_domains=membership.allowed_domains or [],
        allowed_document_scopes=membership.allowed_document_scopes or [],
        created_at=membership.created_at,
    )


# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------
@router.post("/companies", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def create_company(
    payload: CompanyCreate,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
) -> CompanyOut:
    """Create a company and make the caller its first administrator.

    Company creation is the one tenant-scoped action with no prior membership to
    check, so the creator is granted ADMIN atomically — leaving a company with no
    administrator would make it permanently ungovernable.
    """
    slug = _unique_slug(session, _slugify(payload.slug or payload.company_name))
    company = Company(
        company_name=payload.company_name.strip(),
        slug=slug,
        industry=payload.industry,
        description=payload.description,
        country=payload.country,
        timezone=payload.timezone,
        currency=payload.currency.upper(),
        fiscal_year_start_month=payload.fiscal_year_start_month,
        week_start_day=payload.week_start_day,
        status=CompanyStatus.DRAFT,
    )
    session.add(company)
    session.flush()

    admin_role = _role_for(session, company.id, ADMIN_ROLE_KEY)
    session.add(
        CompanyUser(
            company_id=company.id,
            user_id=user.id,
            role_id=admin_role.id,
            status=MembershipStatus.ACTIVE,
        )
    )

    # A default calendar so "month" and "week" have a governed meaning from the
    # moment the first KPI is defined.
    session.add(
        CompanyCalendar(
            company_id=company.id,
            calendar_key="default",
            name=f"{company.company_name} business calendar",
            timezone=company.timezone,
            week_start_day=company.week_start_day,
            fiscal_year_start_month=company.fiscal_year_start_month,
            is_default=True,
            notes="Created automatically. Adjust under Company settings.",
        )
    )
    session.flush()

    audit.record(
        session,
        access=None,
        action=audit.AuditAction.COMPANY_CREATED,
        resource_type="company",
        resource_id=company.id,
        resource_label=company.company_name,
        summary=f"{user.email} created {company.company_name}.",
        details={"slug": slug, "currency": company.currency, "timezone": company.timezone},
        request=request,
        company_id=company.id,
        user_id=user.id,
        actor_email=user.email,
    )
    audit.event(
        session,
        company_id=company.id,
        category="COMPANY",
        title="Company created",
        message=f"{company.company_name} workspace created.",
    )
    return CompanyOut.model_validate(company)


@router.get("/companies/{company_id}", response_model=CompanyOut)
def get_company(access: AccessContext = Depends(require_permissions("company.read"))) -> CompanyOut:
    return CompanyOut.model_validate(access.company)


@router.patch("/companies/{company_id}", response_model=CompanyOut)
def update_company(
    payload: CompanyUpdate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("company.manage")),
) -> CompanyOut:
    company = access.company
    changes: dict[str, object] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        if field == "currency":
            value = str(value).upper()
        if getattr(company, field) != value:
            changes[field] = value
            setattr(company, field, value)

    if changes:
        audit.record(
            session,
            access=access,
            action=audit.AuditAction.COMPANY_UPDATED,
            resource_type="company",
            resource_id=company.id,
            resource_label=company.company_name,
            summary=f"Updated {', '.join(sorted(changes))}.",
            details={"changes": changes},
            request=request,
        )
    return CompanyOut.model_validate(company)


@router.post("/companies/{company_id}/activate", response_model=CompanyOut)
def activate_company(
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("company.manage")),
) -> CompanyOut:
    """Activate the workspace once its foundation is in place.

    Refusing to activate an empty company is not bureaucracy: an ACTIVE company
    with no data scope and no KPIs would report a healthy system that cannot
    answer a single question.
    """
    from app.models.kpi import KpiDefinition
    from app.models.source import SelectedTable

    company = access.company
    selected = session.scalar(
        select(func.count(SelectedTable.id)).where(
            SelectedTable.company_id == company.id, SelectedTable.enabled.is_(True)
        )
    )
    kpis = session.scalar(
        select(func.count(KpiDefinition.id)).where(KpiDefinition.company_id == company.id)
    )
    blockers = []
    if not selected:
        blockers.append("no tables have been selected into the analytical scope")
    if not kpis:
        blockers.append("no KPIs have been registered")
    if blockers:
        raise ValidationFailure(
            "The company is not ready to activate: " + "; ".join(blockers) + ".",
            details={"selected_tables": selected or 0, "kpis": kpis or 0},
        )

    company.status = CompanyStatus.ACTIVE
    audit.record(
        session,
        access=access,
        action=audit.AuditAction.COMPANY_ACTIVATED,
        resource_type="company",
        resource_id=company.id,
        resource_label=company.company_name,
        summary="Company activated.",
        old_version=CompanyStatus.DRAFT,
        new_version=CompanyStatus.ACTIVE,
        request=request,
    )
    audit.event(
        session,
        company_id=company.id,
        category="COMPANY",
        title="Company activated",
        message=f"{company.company_name} is now active.",
    )
    return CompanyOut.model_validate(company)


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
@router.get("/companies/{company_id}/calendars", response_model=list[CalendarOut])
def list_calendars(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("company.read")),
) -> list[CalendarOut]:
    rows = session.scalars(
        select(CompanyCalendar)
        .where(CompanyCalendar.company_id == access.company.id)
        .order_by(CompanyCalendar.calendar_key)
    )
    return [CalendarOut.model_validate(row) for row in rows]


@router.put("/companies/{company_id}/calendars", response_model=CalendarOut)
def upsert_calendar(
    payload: CalendarUpsert,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("company.manage")),
) -> CalendarOut:
    calendar = session.scalar(
        select(CompanyCalendar).where(
            CompanyCalendar.company_id == access.company.id,
            CompanyCalendar.calendar_key == payload.calendar_key,
        )
    )
    created = calendar is None
    if calendar is None:
        calendar = CompanyCalendar(
            company_id=access.company.id, calendar_key=payload.calendar_key
        )
        session.add(calendar)

    calendar.name = payload.name
    calendar.timezone = payload.timezone
    calendar.week_start_day = payload.week_start_day
    calendar.fiscal_year_start_month = payload.fiscal_year_start_month
    calendar.notes = payload.notes
    session.flush()

    if payload.is_default:
        # Exactly one default, or "month" becomes ambiguous again.
        for other in session.scalars(
            select(CompanyCalendar).where(CompanyCalendar.company_id == access.company.id)
        ):
            other.is_default = other.id == calendar.id
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.CALENDAR_UPDATED,
        resource_type="calendar",
        resource_id=calendar.id,
        resource_label=calendar.calendar_key,
        summary=("Created" if created else "Updated") + f" calendar {calendar.calendar_key}.",
        details={
            "timezone": calendar.timezone,
            "week_start_day": calendar.week_start_day,
            "fiscal_year_start_month": calendar.fiscal_year_start_month,
            "is_default": calendar.is_default,
        },
        request=request,
    )
    return CalendarOut.model_validate(calendar)


# ---------------------------------------------------------------------------
# Roles and permissions
# ---------------------------------------------------------------------------
@router.get("/companies/{company_id}/roles", response_model=list[RoleOut])
def list_roles(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("user.read")),
) -> list[RoleOut]:
    roles = list(
        session.scalars(
            select(Role)
            .where(or_(Role.company_id == access.company.id, Role.company_id.is_(None)))
            .order_by(Role.rank)
        )
    )
    permission_map: dict[str, list[str]] = {}
    if roles:
        rows = session.execute(
            select(RolePermission.role_id, Permission.key)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id.in_([r.id for r in roles]))
        ).all()
        for role_id, key in rows:
            permission_map.setdefault(role_id, []).append(key)

    out: list[RoleOut] = []
    for role in roles:
        granted = sorted(permission_map.get(role.id, []))
        spec = ROLES_BY_KEY.get(role.role_key)
        out.append(
            RoleOut(
                role_key=role.role_key,
                name=role.name,
                description=role.description,
                is_admin_role=role.is_admin_role,
                rank=role.rank,
                permissions=granted,
                is_core=bool(spec and spec.is_core),
                access_summary=(spec.access_summary or None) if spec else None,
                # The four boundaries that actually matter to a business reader,
                # each derived from the permissions this role really holds.
                access_areas=_access_areas(granted),
            )
        )
    return out


# The permission that decides each headline access boundary. Derived, never
# duplicated: if a role's grants change, this view changes with them.
_ACCESS_AREAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workspace_configuration", ("company.manage",)),
    ("kpi_definitions", ("kpi.approve", "kpi.create", "kpi.edit")),
    ("sensitive_data", ("data.read_pii", "data.read_confidential", "data.read_restricted")),
    ("documents", ("document.read",)),
)


def _access_areas(granted: list[str]) -> dict[str, bool]:
    held = set(granted)
    return {area: any(key in held for key in keys) for area, keys in _ACCESS_AREAS}


@router.get("/companies/{company_id}/permissions", response_model=list[str])
def list_my_permissions(
    access: AccessContext = Depends(require_permissions("company.read")),
) -> list[str]:
    return sorted(access.permissions)


@router.get("/companies/{company_id}/access-scope", response_model=AccessScopeOut)
def read_my_access_scope(
    access: AccessContext = Depends(require_permissions("company.read")),
) -> AccessScopeOut:
    """What the caller may reach inside this company, as the backend resolved it.

    The company id in the path is an assertion; the dependency chain has already
    turned it into a verified membership by the time this runs, so the response
    describes real entitlement rather than repeating what was asked for.
    """
    return AccessScopeOut(**access.as_scope())


# The three membership fields that widen or narrow reach rather than describing
# the person. Used only to label the audit entry.
_SCOPE_FIELDS = frozenset(
    {"row_scope", "denied_columns", "allowed_domains", "allowed_document_scopes"}
)


def _membership_action(changes: dict[str, object]) -> str:
    if "role" in changes:
        return audit.AuditAction.MEMBER_ROLE_CHANGED
    if changes and set(changes) <= _SCOPE_FIELDS:
        return audit.AuditAction.MEMBER_SCOPE_UPDATED
    return audit.AuditAction.MEMBER_UPDATED


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
@router.get("/companies/{company_id}/members", response_model=list[MemberOut])
def list_members(
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("user.read")),
) -> list[MemberOut]:
    rows = session.execute(
        select(CompanyUser, User, Role)
        .join(User, User.id == CompanyUser.user_id)
        .join(Role, Role.id == CompanyUser.role_id)
        .where(CompanyUser.company_id == access.company.id)
        .order_by(Role.rank, User.full_name)
    ).all()
    return [_member_out(m, u, r) for m, u, r in rows]


@router.post(
    "/companies/{company_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    payload: MemberInvite,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("user.manage")),
) -> MemberOut:
    role = _role_for(session, access.company.id, payload.role_key.upper())
    user = session.scalar(select(User).where(User.email == payload.email.lower()))

    if user is None:
        if not payload.password or not payload.full_name:
            raise ValidationFailure(
                "This email has no account yet, so a full name and initial password "
                "are required to create one."
            )
        user = User(
            email=payload.email.lower(),
            password_hash=hash_password(payload.password),
            full_name=payload.full_name.strip(),
            is_active=True,
        )
        session.add(user)
        session.flush()

    existing = session.scalar(
        select(CompanyUser).where(
            CompanyUser.company_id == access.company.id, CompanyUser.user_id == user.id
        )
    )
    if existing is not None:
        raise Conflict(f"{user.email} is already a member of this company.")

    membership = CompanyUser(
        company_id=access.company.id,
        user_id=user.id,
        role_id=role.id,
        status=MembershipStatus.ACTIVE,
        row_scope=payload.row_scope,
        denied_columns=payload.denied_columns,
        allowed_domains=payload.allowed_domains,
        allowed_document_scopes=payload.allowed_document_scopes,
    )
    session.add(membership)
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.MEMBER_ADDED,
        resource_type="membership",
        resource_id=membership.id,
        resource_label=user.email,
        summary=f"Added {user.email} as {role.role_key}.",
        details={
            "role": role.role_key,
            "row_scope": payload.row_scope,
            "denied_columns": payload.denied_columns,
            "allowed_domains": payload.allowed_domains,
            "allowed_document_scopes": payload.allowed_document_scopes,
        },
        request=request,
    )
    return _member_out(membership, user, role)


@router.patch("/companies/{company_id}/members/{membership_id}", response_model=MemberOut)
def update_member(
    membership_id: str,
    payload: MemberUpdate,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("user.manage")),
) -> MemberOut:
    membership: CompanyUser = load_scoped(session, CompanyUser, membership_id, access)
    changes: dict[str, object] = {}

    if payload.role_key:
        role = _role_for(session, access.company.id, payload.role_key.upper())
        if role.id != membership.role_id:
            _guard_last_admin(session, access, membership, new_role=role)
            changes["role"] = role.role_key
            membership.role_id = role.id
    if payload.status is not None and payload.status != membership.status:
        if payload.status != MembershipStatus.ACTIVE:
            _guard_last_admin(session, access, membership, new_role=None)
        changes["status"] = payload.status
        membership.status = payload.status
    if payload.row_scope is not None:
        changes["row_scope"] = payload.row_scope
        membership.row_scope = payload.row_scope
    if payload.denied_columns is not None:
        changes["denied_columns"] = payload.denied_columns
        membership.denied_columns = payload.denied_columns
    if payload.allowed_domains is not None:
        changes["allowed_domains"] = payload.allowed_domains
        membership.allowed_domains = payload.allowed_domains
    if payload.allowed_document_scopes is not None:
        changes["allowed_document_scopes"] = payload.allowed_document_scopes
        membership.allowed_document_scopes = payload.allowed_document_scopes

    session.flush()
    user = session.get(User, membership.user_id)
    role = session.get(Role, membership.role_id)

    if changes:
        audit.record(
            session,
            access=access,
            action=_membership_action(changes),
            resource_type="membership",
            resource_id=membership.id,
            resource_label=user.email if user else membership.user_id,
            summary=f"Updated {', '.join(sorted(changes))}.",
            details={"changes": changes},
            request=request,
        )
    return _member_out(membership, user, role)


@router.delete(
    "/companies/{company_id}/members/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Explicit: with `from __future__ import annotations`, a `-> None` return
    # annotation resolves to NoneType, which FastAPI would treat as a response
    # model and reject against a bodiless 204.
    response_model=None,
)
def remove_member(
    membership_id: str,
    session: SessionDep,
    request: Request,
    access: AccessContext = Depends(require_permissions("user.manage")),
) -> None:
    membership: CompanyUser = load_scoped(session, CompanyUser, membership_id, access)
    _guard_last_admin(session, access, membership, new_role=None)
    user = session.get(User, membership.user_id)

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.MEMBER_REMOVED,
        resource_type="membership",
        resource_id=membership.id,
        resource_label=user.email if user else membership.user_id,
        summary=f"Removed {user.email if user else membership.user_id} from the company.",
        request=request,
    )
    session.delete(membership)


def _guard_last_admin(
    session, access, membership: CompanyUser, *, new_role: Role | None
) -> None:
    """Refuse any change that would leave the company without an administrator."""
    current_role = session.get(Role, membership.role_id)
    if current_role is None or not current_role.is_admin_role:
        return
    if new_role is not None and new_role.is_admin_role:
        return

    admin_count = session.scalar(
        select(func.count(CompanyUser.id))
        .join(Role, Role.id == CompanyUser.role_id)
        .where(
            CompanyUser.company_id == access.company.id,
            CompanyUser.status == MembershipStatus.ACTIVE,
            Role.is_admin_role.is_(True),
        )
    )
    if (admin_count or 0) <= 1:
        raise PermissionDenied(
            "This is the company's only active administrator. Promote another member "
            "first, otherwise the workspace would become ungovernable."
        )
