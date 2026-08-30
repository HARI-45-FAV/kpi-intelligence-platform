"""The two key-management invariants, pinned so they cannot regress quietly.

Both of these were real defects rather than hypotheticals.

**One key doing two jobs.** ``secret_key`` signed access tokens *and* derived the
Fernet key sealing every tenant data-source credential. Rotating the signing key --
routine, and mandatory after a leak -- silently made every stored DSN in every
company unreadable, and it surfaced at connector time rather than at boot. The keys
are now separable, and the fallback is preserved so an existing install keeps
decrypting what it always could.

**The published default booting a deployment.** The development secret is in this
repository and in ``.env.example``. Anything it protects is unprotected: with it, a
copy of the repo is enough to forge an administrator token for any company and to
decrypt every stored credential. Outside development the app now refuses to start.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import DEV_DEFAULT_SECRET_KEY, MIN_SECRET_LENGTH, Settings
from app.core.security import (
    _credential_key,
    decrypt_secret,
    encrypt_secret,
    migrate_legacy_secret,
)

STRONG_KEY = "b6Qw" + "x" * 60
OTHER_STRONG_KEY = "z9Kp" + "y" * 60


# ---------------------------------------------------------------------------
# The boot guard
# ---------------------------------------------------------------------------
def test_the_published_development_secret_is_refused_outside_development():
    """The failure has to be at boot, because there is no safe degraded mode."""
    with pytest.raises(ValidationError) as raised:
        Settings(environment="production", secret_key=DEV_DEFAULT_SECRET_KEY)

    message = str(raised.value)
    assert "development default" in message
    # And it says what to do about it rather than only what is wrong.
    assert "secrets.token_urlsafe" in message


def test_a_short_signing_key_is_refused_outside_development():
    with pytest.raises(ValidationError) as raised:
        Settings(environment="production", secret_key="x" * (MIN_SECRET_LENGTH - 1))
    assert str(MIN_SECRET_LENGTH) in str(raised.value)


def test_a_weak_credential_key_is_refused_even_when_the_signing_key_is_strong():
    """The credential key is checked on its own; a strong neighbour does not vouch."""
    with pytest.raises(ValidationError) as raised:
        Settings(
            environment="production",
            secret_key=STRONG_KEY,
            credential_encryption_key=DEV_DEFAULT_SECRET_KEY,
        )
    assert "CREDENTIAL_ENCRYPTION_KEY" in str(raised.value)


def test_a_properly_configured_deployment_starts():
    settings = Settings(
        environment="production",
        secret_key=STRONG_KEY,
        credential_encryption_key=OTHER_STRONG_KEY,
    )
    assert settings.is_development is False
    assert settings.debug is False, "a production deployment must not carry debug on"


def test_interactive_docs_are_served_in_development_and_withheld_outside_it(monkeypatch):
    """``/docs`` enumerates every route and body shape to an anonymous caller.

    That is the whole point of it locally, and a free reconnaissance pass anywhere
    else. ``create_app`` reads the environment rather than a separate switch, so
    there is no way to deploy with the guard on and the docs still open.
    """
    from app.core.config import settings as live
    from app.main import create_app

    monkeypatch.setattr(live, "environment", "development")
    local = create_app()
    assert local.docs_url == "/docs"
    assert local.redoc_url == "/redoc"

    monkeypatch.setattr(live, "environment", "production")
    deployed = create_app()
    assert deployed.docs_url is None
    assert deployed.redoc_url is None
    # The schema those pages render is withheld with them.
    assert deployed.openapi_url is None


@pytest.mark.parametrize("environment", ["development", "test", "testing", "local", "DEVELOPMENT"])
def test_development_keeps_working_on_the_shipped_defaults(environment):
    """The guard must not make local setup harder; that is how guards get removed."""
    settings = Settings(environment=environment, secret_key=DEV_DEFAULT_SECRET_KEY)
    assert settings.is_development is True
    assert settings.secret_key == DEV_DEFAULT_SECRET_KEY


# ---------------------------------------------------------------------------
# Splitting the credential key out of the signing key
# ---------------------------------------------------------------------------
def test_credentials_fall_back_to_the_signing_key_when_no_dedicated_key_is_set(monkeypatch):
    """An install that predates the split must keep reading its own credentials."""
    from app.core.config import settings as live

    monkeypatch.setattr(live, "credential_encryption_key", None)
    assert _credential_key() == live.secret_key
    assert decrypt_secret(encrypt_secret("postgresql://u:p@host/db")) == "postgresql://u:p@host/db"


def test_configuring_a_dedicated_key_re_seals_existing_credentials(monkeypatch):
    """The migration path: sealed under the old key, readable, re-sealed under the new.

    This is the operation ``services.credential_migration`` performs at boot for
    every registered source, and the reason rotating the signing key no longer
    destroys them.
    """
    from app.core.config import settings as live

    secret = "postgresql://user:pa55@db.internal:5432/tenant"

    # Sealed the old way, with the signing key doing both jobs.
    monkeypatch.setattr(live, "credential_encryption_key", None)
    sealed_with_signing_key = encrypt_secret(secret)

    # Now a dedicated credential key is configured.
    monkeypatch.setattr(live, "credential_encryption_key", OTHER_STRONG_KEY)
    assert _credential_key() == OTHER_STRONG_KEY

    # The old ciphertext is no longer readable directly -- which is exactly the
    # breakage that used to be silent and permanent.
    with pytest.raises(ValueError):
        decrypt_secret(sealed_with_signing_key)

    # But it is recoverable, because the signing key is still configured.
    rotated = migrate_legacy_secret(sealed_with_signing_key)
    assert rotated is not None, "a credential sealed with a key we still hold must be recoverable"
    assert rotated != sealed_with_signing_key
    assert decrypt_secret(rotated) == secret


def test_a_credential_already_current_is_left_alone(monkeypatch):
    """``migrate_legacy_secret`` reports 'nothing to do' rather than rewriting."""
    from app.core.config import settings as live

    monkeypatch.setattr(live, "credential_encryption_key", OTHER_STRONG_KEY)
    assert migrate_legacy_secret(encrypt_secret("already-current")) is None


def test_an_unreadable_credential_is_never_guessed(monkeypatch):
    """No key we hold decrypts it, so the answer is None -- not a partial write."""
    from app.core.config import settings as live

    monkeypatch.setattr(live, "credential_encryption_key", OTHER_STRONG_KEY)
    assert migrate_legacy_secret("gAAAAABsomethingThatWasNeverOurCiphertext") is None


def test_the_signing_key_and_the_credential_key_derive_different_fernet_keys(monkeypatch):
    """Otherwise the split would be cosmetic and rotation would still be fatal."""
    from app.core.config import settings as live

    monkeypatch.setattr(live, "credential_encryption_key", None)
    under_signing_key = encrypt_secret("same-plaintext")

    monkeypatch.setattr(live, "credential_encryption_key", OTHER_STRONG_KEY)
    under_dedicated_key = encrypt_secret("same-plaintext")

    # Fernet is randomised per call, so compare readability rather than bytes.
    assert decrypt_secret(under_dedicated_key) == "same-plaintext"
    with pytest.raises(ValueError):
        decrypt_secret(under_signing_key)
