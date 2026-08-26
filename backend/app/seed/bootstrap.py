"""Reference data bootstrap.

Roles and permissions are reference data the entire authorization model reads,
so they are seeded rather than created ad hoc. Running this is idempotent: it
adds what is missing and re-syncs role grants without disturbing memberships.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PERMISSIONS, ROLES
from app.models.tenant import Permission, Role, RolePermission


def sync_reference_data(session: Session) -> dict[str, int]:
    """Ensure every permission and platform role exists with correct grants."""
    created_permissions = 0
    permission_ids: dict[str, str] = {}

    for spec in PERMISSIONS:
        permission = session.scalar(select(Permission).where(Permission.key == spec.key))
        if permission is None:
            permission = Permission(
                key=spec.key, description=spec.description, category=spec.category
            )
            session.add(permission)
            session.flush()
            created_permissions += 1
        else:
            permission.description = spec.description
            permission.category = spec.category
        permission_ids[spec.key] = permission.id

    created_roles = 0
    synced_grants = 0
    for spec in ROLES:
        # Platform-wide roles carry company_id NULL so every tenant shares the
        # same vocabulary.
        role = session.scalar(
            select(Role).where(Role.role_key == spec.key, Role.company_id.is_(None))
        )
        if role is None:
            # Every NOT NULL column must be populated before the flush.
            role = Role(
                company_id=None,
                role_key=spec.key,
                name=spec.name,
                description=spec.description,
                is_admin_role=spec.is_admin_role,
                rank=spec.rank,
            )
            session.add(role)
            session.flush()
            created_roles += 1
        else:
            role.name = spec.name
            role.description = spec.description
            role.is_admin_role = spec.is_admin_role
            role.rank = spec.rank

        desired = {permission_ids[key] for key in spec.permissions if key in permission_ids}
        current = {
            grant.permission_id: grant
            for grant in session.scalars(
                select(RolePermission).where(RolePermission.role_id == role.id)
            )
        }
        for permission_id in desired - set(current):
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
            synced_grants += 1
        # Revoking here matters: a permission removed from the catalogue must
        # stop being granted, or the role silently keeps stale authority.
        for permission_id in set(current) - desired:
            session.delete(current[permission_id])
            synced_grants += 1

    session.flush()
    return {
        "permissions_created": created_permissions,
        "roles_created": created_roles,
        "grants_changed": synced_grants,
        "permissions_total": len(PERMISSIONS),
        "roles_total": len(ROLES),
    }


def main() -> None:  # pragma: no cover - operational entry point
    from app.core.database import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        result = sync_reference_data(session)
        session.commit()
    finally:
        session.close()

    print("Reference data synchronised:")
    for key, value in result.items():
        print(f"  {key:<22} {value}")


if __name__ == "__main__":  # pragma: no cover
    main()
