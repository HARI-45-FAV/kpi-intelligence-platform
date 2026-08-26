"""Authentication and the protected-workspace unlock."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.core.clock import utcnow
from app.core.deps import CurrentUser, SessionDep, resolve_access
from app.core.errors import AuthenticationError, Conflict, PermissionDenied
from app.core.security import create_access_token, hash_password, verify_password
from app.models.base import MembershipStatus
from app.models.tenant import Company, CompanyUser, Role, User
from app.schemas import (
    AdminUnlockRequest,
    AdminUnlockResponse,
    LoginRequest,
    MembershipSummary,
    RegisterRequest,
    SessionResponse,
    TokenResponse,
    UserOut,
)
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _memberships(session, user: User) -> list[MembershipSummary]:
    rows = session.execute(
        select(CompanyUser, Company, Role)
        .join(Company, Company.id == CompanyUser.company_id)
        .join(Role, Role.id == CompanyUser.role_id)
        .where(CompanyUser.user_id == user.id)
        .order_by(Company.company_name)
    ).all()
    return [
        MembershipSummary(
            company_id=company.id,
            company_name=company.company_name,
            company_slug=company.slug,
            role_key=role.role_key,
            role_name=role.name,
            status=membership.status,
            is_admin_role=role.is_admin_role,
        )
        for membership, company, role in rows
    ]


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, session: SessionDep, request: Request) -> TokenResponse:
    existing = session.scalar(select(User).where(User.email == payload.email.lower()))
    if existing is not None:
        raise Conflict("An account with that email already exists.")

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        is_active=True,
    )
    session.add(user)
    session.flush()

    audit.record(
        session,
        access=None,
        action=audit.AuditAction.USER_REGISTERED,
        resource_type="user",
        resource_id=user.id,
        resource_label=user.email,
        summary=f"{user.email} registered.",
        request=request,
        user_id=user.id,
        actor_email=user.email,
    )

    token, expires_at = create_access_token(user.id, user.email)
    return TokenResponse(
        access_token=token,
        expires_at=expires_at,
        user=UserOut.model_validate(user),
        memberships=[],
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, session: SessionDep, request: Request) -> TokenResponse:
    user = session.scalar(select(User).where(User.email == payload.email.lower()))
    # One message for both "no such user" and "wrong password": distinguishing
    # them turns the login form into an account-enumeration oracle.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthenticationError("Email or password is incorrect.")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.")

    memberships = _memberships(session, user)
    company_id = payload.company_id
    if company_id and not any(m.company_id == company_id for m in memberships):
        raise PermissionDenied("You are not a member of that company.")
    if company_id is None and len(memberships) == 1:
        company_id = memberships[0].company_id

    user.last_login_at = utcnow()
    audit.record(
        session,
        access=None,
        action=audit.AuditAction.USER_LOGGED_IN,
        resource_type="user",
        resource_id=user.id,
        resource_label=user.email,
        summary=f"{user.email} signed in.",
        request=request,
        company_id=company_id,
        user_id=user.id,
        actor_email=user.email,
    )

    token, expires_at = create_access_token(user.id, user.email, company_id=company_id)
    return TokenResponse(
        access_token=token,
        expires_at=expires_at,
        user=UserOut.model_validate(user),
        memberships=memberships,
    )


@router.get("/session", response_model=SessionResponse)
def current_session(user: CurrentUser, session: SessionDep) -> SessionResponse:
    return SessionResponse(
        user=UserOut.model_validate(user),
        memberships=_memberships(session, user),
    )


@router.post("/admin-unlock", response_model=AdminUnlockResponse)
def admin_unlock(
    payload: AdminUnlockRequest,
    session: SessionDep,
    request: Request,
) -> AdminUnlockResponse:
    """Re-authenticate to enter the KPI Setup / governance workspace.

    Deliberately a fresh credential check rather than a check of the existing
    session: changing what a KPI means is a governance act, and an unattended
    tab should not be enough to perform one. The returned token is short-lived
    and scoped to the company being administered.
    """
    user = session.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthenticationError("Email or password is incorrect.")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.")

    access = resolve_access(request, session, user, payload.company_id)
    if access.membership.status != MembershipStatus.ACTIVE:
        raise PermissionDenied("Your membership is not active.")
    # Entry is gated on the governance permissions themselves, not on a role name.
    access.require("company.manage", "kpi.approve")

    token, expires_at = create_access_token(
        user.id, user.email, company_id=access.company.id, ttl_minutes=60
    )
    return AdminUnlockResponse(
        access_token=token,
        expires_at=expires_at,
        company_id=access.company.id,
        company_name=access.company.company_name,
        role_key=access.role.role_key,
        permissions=sorted(access.permissions),
        user=UserOut.model_validate(user),
        memberships=_memberships(session, user),
    )
