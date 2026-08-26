"""Domain exceptions mapped to HTTP responses in ``app.main``."""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    """Base class for expected, user-facing failures."""

    status_code = 400
    code = "platform_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class AuthenticationError(PlatformError):
    status_code = 401
    code = "authentication_failed"


class PermissionDenied(PlatformError):
    status_code = 403
    code = "permission_denied"


class TenantIsolationError(PermissionDenied):
    """Raised when a user reaches for a company they are not a member of.

    Deliberately a 403 with a generic message: confirming that company X
    exists is itself an information leak across tenants.
    """

    code = "tenant_isolation"

    def __init__(self, message: str = "You do not have access to this company.") -> None:
        super().__init__(message)


class NotFound(PlatformError):
    status_code = 404
    code = "not_found"


class Conflict(PlatformError):
    status_code = 409
    code = "conflict"


class ValidationFailure(PlatformError):
    status_code = 422
    code = "validation_failed"


class ConnectorError(PlatformError):
    status_code = 502
    code = "connector_error"


class UnsafeQueryError(PlatformError):
    """A query or identifier failed the read-only / allow-list guard rails."""

    status_code = 400
    code = "unsafe_query"
