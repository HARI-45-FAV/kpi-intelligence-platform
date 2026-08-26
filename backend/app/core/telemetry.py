"""Runtime telemetry.

Sprint 1 has no LLM calls, but latency and connector-query accounting are built
in now so the reasoning sprints inherit instrumentation instead of retrofitting
it. Query *text* is never stored — only a hash — because a WHERE clause can
carry business data.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.clock import utcnow
from app.core.database import SessionLocal
from app.models.observability import ExecutionLog

# Paths that would only add noise to the telemetry table.
_SKIP_PREFIXES = ("/docs", "/redoc", "/openapi.json", "/favicon.ico", "/health")


class ConnectorUsage:
    """Per-request accumulator that services attach connector work to."""

    __slots__ = ("connector", "query_count", "query_duration_ms", "rows_returned", "query_hash")

    def __init__(self) -> None:
        self.connector: str | None = None
        self.query_count = 0
        self.query_duration_ms = 0
        self.rows_returned = 0
        self.query_hash: str | None = None

    def absorb(self, connector: object) -> None:
        """Fold a connector's counters into the request total."""
        self.connector = str(getattr(connector, "source_type", None) or self.connector or "")
        self.query_count += int(getattr(connector, "query_count", 0) or 0)
        self.query_duration_ms += int(getattr(connector, "query_duration_ms", 0) or 0)
        self.rows_returned += int(getattr(connector, "rows_returned", 0) or 0)
        self.query_hash = getattr(connector, "last_query_hash", None) or self.query_hash


def usage_of(request: Request) -> ConnectorUsage:
    usage = getattr(request.state, "connector_usage", None)
    if usage is None:
        usage = ConnectorUsage()
        request.state.connector_usage = usage
    return usage


def _service_for(path: str) -> str:
    parts = [segment for segment in path.split("/") if segment]
    # /api/v1/companies/{id}/kpis/... -> "kpis"
    if len(parts) >= 3 and parts[0] == "api":
        tail = parts[2:]
        if len(tail) >= 3 and tail[0] == "companies":
            return tail[2] if len(tail) > 2 else "companies"
        return tail[0] if tail else "api"
    return parts[0] if parts else "root"


class TelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        request.state.connector_usage = ConnectorUsage()
        started_at = utcnow()
        started = time.perf_counter()

        status_code = 500
        error: str | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        except Exception as exc:  # pragma: no cover - re-raised for the handlers
            error = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if not request.url.path.startswith(_SKIP_PREFIXES):
                self._persist(
                    request=request,
                    request_id=request_id,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    status_code=status_code,
                    error=error,
                )

    def _persist(
        self,
        *,
        request: Request,
        request_id: str,
        started_at,
        duration_ms: int,
        status_code: int,
        error: str | None,
    ) -> None:
        usage: ConnectorUsage = getattr(request.state, "connector_usage", ConnectorUsage())
        # A dedicated session: the request's own session may have been rolled
        # back, and losing the trace of a failed request is the worst time to
        # lose it.
        session = SessionLocal()
        try:
            session.add(
                ExecutionLog(
                    request_id=request_id,
                    company_id=getattr(request.state, "company_id", None),
                    user_id=getattr(request.state, "user_id", None),
                    service=_service_for(request.url.path),
                    operation=f"{request.method} {request.scope.get('root_path', '')}{request.url.path}",
                    http_method=request.method,
                    http_path=request.url.path,
                    http_status=status_code,
                    started_at=started_at,
                    duration_ms=duration_ms,
                    status="OK" if status_code < 400 and not error else "ERROR",
                    error=error,
                    connector=usage.connector or None,
                    query_hash=usage.query_hash,
                    query_count=usage.query_count or None,
                    query_duration_ms=usage.query_duration_ms or None,
                    rows_returned=usage.rows_returned or None,
                )
            )
            session.commit()
        except Exception:  # pragma: no cover - telemetry must never break a request
            session.rollback()
        finally:
            session.close()
