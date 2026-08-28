"""One-time repair for local credentials encrypted before key configuration."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import migrate_legacy_secret
from app.models.source import DataSource


def migrate_legacy_source_credentials(session: Session) -> int:
    """Rotate only credentials demonstrably encrypted with the old dev key."""
    migrated = 0
    for source in session.scalars(select(DataSource).where(DataSource.encrypted_credentials.is_not(None))):
        rotated = migrate_legacy_secret(source.encrypted_credentials or "")
        if rotated is not None:
            source.encrypted_credentials = rotated
            migrated += 1
    return migrated
