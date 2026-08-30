"""Tenant layer: users, companies, roles, permissions, memberships, calendar.

Every tenant-owned table below carries ``company_id``. That column, plus the
membership check in ``app.core.deps``, is the whole tenant isolation story.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import (
    CompanyStatus,
    MembershipStatus,
    Timestamped,
    UUIDPrimaryKey,
    UtcDateTime,
)


class User(Base, UUIDPrimaryKey, Timestamped):
    """A person. Users exist above companies and may belong to several."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    memberships: Mapped[list["CompanyUser"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Company(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "companies"

    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(80))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    # 1 == January, 4 == April (common Indian fiscal year), etc.
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    week_start_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 1 == Monday
    status: Mapped[str] = mapped_column(String(20), default=CompanyStatus.DRAFT, nullable=False)

    memberships: Mapped[list["CompanyUser"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Role(Base, UUIDPrimaryKey, Timestamped):
    """A named bundle of permissions.

    Roles are platform-defined (``company_id`` NULL) so every tenant shares the
    same vocabulary, with room for company-specific roles later.
    """

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("company_id", "role_key", name="uq_role_company_key"),)

    company_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    role_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_admin_role: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class Permission(Base, UUIDPrimaryKey, Timestamped):
    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)


class RolePermission(Base, UUIDPrimaryKey):
    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )

    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    role: Mapped[Role] = relationship(back_populates="permissions")
    permission: Mapped[Permission] = relationship()


class CompanyUser(Base, UUIDPrimaryKey, Timestamped):
    """Membership: which user acts in which company, under which role and scope."""

    __tablename__ = "company_users"
    __table_args__ = (UniqueConstraint("company_id", "user_id", name="uq_membership"),)

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=MembershipStatus.ACTIVE, nullable=False
    )
    # Row-level scope, e.g. {"region": ["South"]}. Empty == unrestricted.
    row_scope: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Column-level denials, e.g. ["customers.email"]. Applied on top of the
    # column classification rules.
    denied_columns: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Business domains this membership may see, e.g. ["SALES", "MARKETING"].
    # Free-form strings rather than an enum: a company's domain vocabulary is
    # its own, and hardcoding one here would bake a customer into the platform.
    # Empty == unrestricted, which is what an administrator holds.
    allowed_domains: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Document scopes this membership may retrieve, e.g. ["FINANCE_POLICY"].
    # Empty == unrestricted. Read by later retrieval so that "which documents may
    # answer this question" is decided from the membership, never from the query.
    allowed_document_scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    user: Mapped[User] = relationship(back_populates="memberships")
    company: Mapped[Company] = relationship(back_populates="memberships")
    role: Mapped[Role] = relationship()


class CompanyCalendar(Base, UUIDPrimaryKey, Timestamped):
    """Governed meaning of "day", "week", "month" for a company.

    Without this, a KPI defined as "Monthly Revenue" has no reproducible
    meaning. Sprint 2's monitoring reads the calendar rather than assuming
    Gregorian months in UTC.
    """

    __tablename__ = "company_calendars"
    __table_args__ = (
        UniqueConstraint("company_id", "calendar_key", name="uq_calendar_company_key"),
    )

    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    calendar_key: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    week_start_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
