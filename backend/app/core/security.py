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

from app.core.config import DEV_DEFAULT_SECRET_KEY, settings

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
# Sealing tenant credentials is deliberately *not* done with ``secret_key``
# unless nothing else is configured. See the note on
# ``Settings.credential_encryption_key``: the signing key is meant to be
# rotatable and this one is not, so coupling them made every data source in the
# platform a hostage of token rotation.
def _derive_fernet(material: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest()))


def _credential_key() -> str:
    """The key credentials are sealed with now."""
    return settings.credential_encryption_key or settings.secret_key


def _superseded_keys() -> tuple[str, ...]:
    """Keys a stored credential may still be sealed under, newest intent first.

    Only keys this deployment has actually been configured with, plus the
    published development default. It never guesses.
    """
    current = _credential_key()
    candidates = (settings.secret_key, DEV_DEFAULT_SECRET_KEY)
    seen: set[str] = {current}
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)


def _fernet() -> Fernet:
    return _derive_fernet(_credential_key())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - only on key rotation
        raise ValueError("stored credential could not be decrypted") from exc


def migrate_legacy_secret(ciphertext: str) -> str | None:
    """Re-seal a credential encrypted under a key this deployment has replaced.

    Two cases reach here, and they are the same operation. A workspace that ran
    on the documented development default before a real ``SECRET_KEY`` was set,
    and a deployment that has just split ``CREDENTIAL_ENCRYPTION_KEY`` out of a
    ``SECRET_KEY`` that used to do both jobs. Either way the record is
    recoverable with a key we hold, so it is re-sealed with the current one.

    Returns ``None`` when the credential is already current or is not readable
    with any configured key -- never a guess, and never a partial write.
    """
    for material in _superseded_keys():
        try:
            plaintext = _derive_fernet(material).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            continue
        return encrypt_secret(plaintext)
    return None


def redact(value: str | None, keep: int = 2) -> str:
    """Render a secret safe for logs and API responses."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)
