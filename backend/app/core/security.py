"""Password hashing, JWT issuance/verification, and credential encryption."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_BCRYPT_ROUNDS = 12


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def _prehash(password: str) -> bytes:
    """bcrypt silently truncates at 72 bytes; SHA-256 first so long passwords
    stay fully significant."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------
def create_access_token(
    user_id: str,
    email: str,
    *,
    company_id: str | None = None,
    ttl_minutes: int | None = None,
) -> tuple[str, datetime]:
    """Issue a signed access token.

    ``company_id`` records the tenant the session is *scoped to*, but it is
    never trusted on its own: every request re-verifies membership against the
    database (see ``app.core.deps``). A stolen or hand-edited token therefore
    cannot reach another company's data.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=ttl_minutes or settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "company_id": company_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify a token. Raises ``jwt.PyJWTError`` on any problem."""
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub"]},
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("unexpected token type")
    return payload


# ---------------------------------------------------------------------------
# Data-source credential encryption
# ---------------------------------------------------------------------------
def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - only on key rotation
        raise ValueError("stored credential could not be decrypted") from exc


def migrate_legacy_secret(ciphertext: str) -> str | None:
    """Re-encrypt a credential created with the original development key.

    Early local workspaces used the documented default before a project-specific
    ``SECRET_KEY`` was configured.  This helper permits a one-time, explicit
    migration of those recoverable records; it never guesses arbitrary keys.
    """
    legacy_key = "dev-only-secret-change-me-in-production-0123456789abcdef"
    if settings.secret_key == legacy_key:
        return None
    legacy_fernet = Fernet(
        base64.urlsafe_b64encode(hashlib.sha256(legacy_key.encode("utf-8")).digest())
    )
    try:
        plaintext = legacy_fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
    return encrypt_secret(plaintext)


def redact(value: str | None, keep: int = 2) -> str:
    """Render a secret safe for logs and API responses."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)
