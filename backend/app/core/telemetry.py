"""Runtime telemetry.

Latency, connector-query accounting and model accounting all land on the same
``execution_logs`` row, so one request has one honest cost record. Query *text* is
never stored — only a hash — because a WHERE clause can carry business data. The
same rule governs the model columns: token counts and the model name are recorded,
never a prompt, a completion, an endpoint credential or an API key.

When no model is configured the model columns stay null, which is what makes
``llm.calls = 0`` a fact read from the database rather than a claim in a docstring.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


class LlmUsage:
    """Per-request accumulator for model calls.

    Deliberately holds counts and the model identifier only. There is no field
    for a prompt, a completion, a tool argument or a credential, so no code path
    can persist one through this object.
    """

    __slots__ = ("model", "calls", "prompt_tokens", "completion_tokens", "estimated_cost_usd")

    def __init__(self) -> None:
        self.model: str | None = None
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_cost_usd = 0.0

    def record(
        self,
        *,
        model: str | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Account for one completed model turn."""
        self.model = model or self.model
        self.calls += 1
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)
        self.estimated_cost_usd += float(cost_usd or 0.0)


def usage_of(request: Request) -> ConnectorUsage:
    usage = getattr(request.state, "connector_usage", None)
    if usage is None:
        usage = ConnectorUsage()
        request.state.connector_usage = usage
    return usage


def llm_usage_of(request: Request | None) -> LlmUsage:
    """The request's model accumulator, created on first use.

    Accepts ``None`` so a code path without a live request (a test, a script)
    still gets a working accumulator instead of having to special-case it.
    """
    if request is None:
        return LlmUsage()
    usage = getattr(request.state, "llm_usage", None)
    if usage is None:
        usage = LlmUsage()
        request.state.llm_usage = usage
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


class TelemetryMiddleware:
    """Records one ``execution_logs`` row per request, after the request is done.

    Deliberately a plain ASGI middleware rather than a ``BaseHTTPMiddleware``.
    That base class hands the response back to its caller while the route's
    dependency stack is still unwinding, so the request's own database
    transaction is still open at that moment. Writing the log from there means a
    second connection asking SQLite for a write lock the request itself still
    holds: five seconds of waiting, then ``database is locked``, then a swallowed
    exception and no record of the request at all. Wrapping the application
    directly puts the write after teardown, where the lock is free -- which is
    what makes the model-accounting columns readable at all for the requests that
    actually change something.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ``request.state`` is backed by ``scope["state"]``, so the accumulators
        # attached here are the same objects the endpoints and services later
        # reach through ``usage_of`` and ``llm_usage_of``.
        request = Request(scope)
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        request.state.connector_usage = ConnectorUsage()
        request.state.llm_usage = LlmUsage()
        started_at = utcnow()
        started = time.perf_counter()

        status_code = 500
        error: str | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
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
        started_at: Any,
        duration_ms: int,
        status_code: int,
        error: str | None,
    ) -> None:
        usage: ConnectorUsage = getattr(request.state, "connector_usage", ConnectorUsage())
        llm: LlmUsage = getattr(request.state, "llm_usage", None) or LlmUsage()
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
                    # Null rather than zero when no model ran, so a request that
                    # never touched a model is distinguishable from one that did.
                    llm_model=llm.model,
                    llm_calls=llm.calls or None,
                    prompt_tokens=llm.prompt_tokens or None,
                    completion_tokens=llm.completion_tokens or None,
                    estimated_cost_usd=llm.estimated_cost_usd or None,
                )
            )
            session.commit()
        except Exception:  # pragma: no cover - telemetry must never break a request
            session.rollback()
        finally:
            session.close()
