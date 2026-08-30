"""Supabase connector over the project REST API (PostgREST).

Onboarding asks for exactly two things — the project URL and the secret key —
because those are the two values the Supabase dashboard actually gives you.

That choice has a real consequence worth being explicit about: a Supabase secret
key is a JWT for the REST API, **not** the database password, so this connector
cannot open a Postgres session and cannot run arbitrary SQL. What it can do:

* read the project's PostgREST OpenAPI document to discover tables, columns,
  types and primary keys — no guessing at schema;
* get **exact** row counts cheaply via ``Prefer: count=exact``;
* profile columns and evaluate grain, relationships and fan-out from a bounded
  sample of rows.

Sampled statistics are labelled as sampled (``stats_are_sampled``) rather than
presented as full-table facts. A percentage computed over 5,000 of 2,000,000 rows
is an estimate, and the catalog says so instead of quietly implying certainty.
For full-scan pushdown, register the same project as a ``POSTGRESQL`` source with
its database password.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors.base import (
    ColumnMeta,
    ColumnStats,
    ConnectionTestResult,
    DataSourceConnector,
    ForeignKeyMeta,
    RefreshMetadata,
    TableMeta,
    jsonable,
    validate_identifier,
)
from app.core.config import settings
from app.core.errors import ConnectorError, ValidationFailure
from app.models.base import DataSourceType

_PROJECT_REF_RE = re.compile(r"^[a-z0-9]{16,32}$")
# PostgREST marks keys inside the column description.
_PK_MARKER = "<pk/>"
_FK_RE = re.compile(r"<fk table='([^']+)' column='([^']+)'/>")

SAMPLE_LIMIT = 5_000


@dataclass(slots=True)
class ProjectionPage:
    """One bounded, filtered read: the rows, the true count, and how it was asked.

    ``total`` is the number of rows matching the filter at the source, which is
    exact even when ``rows`` was capped -- so a row count never has to be
    approximated. ``truncated`` says the two disagree, which is the caller's
    signal that any aggregate needing every value cannot be computed honestly.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    truncated: bool = False
    #: The request path and query, free of credentials -- safe for logs and evidence.
    descriptor: str = ""


def normalise_supabase_url(value: str) -> str:
    """Accept a full URL, a bare host, or just the project ref."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise ValidationFailure("Supabase URL is required.")
    host = urlparse(raw).hostname if "://" in raw else raw
    host = (host or "").strip("/")
    if _PROJECT_REF_RE.match(host):
        host = f"{host}.supabase.co"
    if not host.endswith((".supabase.co", ".supabase.com")) and "." not in host:
        raise ValidationFailure(
            f"Could not read a Supabase project from {value!r}. "
            "Expected https://<project-ref>.supabase.co"
        )
    return f"https://{host}"


def project_ref_of(url: str) -> str | None:
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    return parts[0] if len(parts) >= 3 and _PROJECT_REF_RE.match(parts[0]) else None


class SupabaseRestConnector(DataSourceConnector):
    source_type = DataSourceType.SUPABASE
    supports_profiling = True

    def __init__(self, url: str, secret_key: str, schema: str | None = None) -> None:
        if not secret_key:
            raise ValidationFailure("Supabase secret key is required.")
        self.base_url = normalise_supabase_url(url)
        self._key = secret_key
        self.schema = schema or "public"
        self._client: httpx.Client | None = None
        self._spec: dict[str, Any] | None = None

        # Telemetry, absorbed into the request record like any other connector.
        self.query_count = 0
        self.query_duration_ms = 0
        self.rows_returned = 0
        self.last_query_hash: str | None = None
        # True once any statistic on this connector came from a partial sample.
        self.stats_are_sampled = False

    # -- transport -------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=f"{self.base_url}/rest/v1",
                headers={
                    "apikey": self._key,
                    "Authorization": f"Bearer {self._key}",
                    "Accept-Profile": self.schema,
                },
                timeout=settings.connector_query_timeout_seconds,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _request(self, path: str, **kwargs: Any) -> httpx.Response:
        started = datetime.now()
        try:
            response = self.client.get(path, **kwargs)
        except httpx.HTTPError as exc:
            self.query_count += 1
            raise ConnectorError(f"Supabase request failed: {type(exc).__name__}") from exc
        finally:
            self.query_duration_ms += int((datetime.now() - started).total_seconds() * 1000)
        self.query_count += 1

        if response.status_code == 401 or response.status_code == 403:
            raise ConnectorError(
                "Supabase rejected the secret key. Check Project Settings → API → "
                "service_role key."
            )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("message", ""))[:200]
            except Exception:
                detail = response.text[:200]
            raise ConnectorError(f"Supabase returned {response.status_code}: {detail}")
        return response

    def _openapi(self) -> dict[str, Any]:
        """The project's PostgREST schema document — our source of metadata truth."""
        if self._spec is None:
            self._spec = self._request("/").json()
        return self._spec

    def _definitions(self) -> dict[str, Any]:
        spec = self._openapi()
        definitions = spec.get("definitions")
        if isinstance(definitions, dict) and definitions:
            return definitions
        return (spec.get("components") or {}).get("schemas") or {}

    def _rows(self, table: str, select: str = "*", limit: int = SAMPLE_LIMIT) -> list[dict]:
        validate_identifier(table, kind="table name")
        capped = min(limit, settings.connector_max_rows_returned)
        response = self._request(
            f"/{table}", params={"select": select, "limit": capped}
        )
        rows = response.json()
        if not isinstance(rows, list):
            return []
        self.rows_returned += len(rows)
        return rows

    # -- connection ------------------------------------------------------
    def test_connection(self) -> ConnectionTestResult:
        checks: list[dict[str, Any]] = []
        started = datetime.now()
        try:
            self._openapi()
            checks.append({"check": "Project reachable", "ok": True, "detail": self.base_url})
            checks.append({"check": "Secret key accepted", "ok": True})
            tables = self.list_tables()
            checks.append(
                {"check": f"Schema readable: {self.schema}", "ok": True}
            )
            checks.append({"check": f"{len(tables)} tables detected", "ok": bool(tables)})
            ok = all(c["ok"] for c in checks)
            return ConnectionTestResult(
                ok=ok,
                message="Connected." if ok else "Connected, but no readable tables found.",
                checks=checks,
                server_version="Supabase REST (PostgREST)",
                table_count=len(tables),
                duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            )
        except ConnectorError as exc:
            checks.append({"check": "Connection", "ok": False, "detail": exc.message})
            return ConnectionTestResult(
                ok=False,
                message="Connection failed.",
                checks=checks,
                duration_ms=int((datetime.now() - started).total_seconds() * 1000),
                error=exc.message,
            )

    def list_databases(self) -> list[str]:
        return [project_ref_of(self.base_url) or "supabase"]

    def list_schemas(self) -> list[str]:
        return [self.schema]

    def list_tables(self, schema: str | None = None) -> list[TableMeta]:
        results: list[TableMeta] = []
        for name, definition in self._definitions().items():
            if not isinstance(definition, dict) or "properties" not in definition:
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                continue
            results.append(
                TableMeta(
                    schema_name=self.schema,
                    table_name=name,
                    approx_row_count=self.count_rows(self.schema, name),
                    column_count=len(definition.get("properties") or {}),
                    database_name=project_ref_of(self.base_url),
                )
            )
        return sorted(results, key=lambda t: t.table_name)

    # -- structure -------------------------------------------------------
    def get_table_metadata(self, schema: str, table: str) -> TableMeta:
        columns = self.get_column_metadata(schema, table)
        return TableMeta(
            schema_name=self.schema,
            table_name=table,
            approx_row_count=self.count_rows(schema, table),
            column_count=len(columns),
            database_name=project_ref_of(self.base_url),
        )

    def get_column_metadata(self, schema: str, table: str) -> list[ColumnMeta]:
        definition = self._definitions().get(table)
        if not isinstance(definition, dict):
            raise ConnectorError(f"Table '{table}' is not exposed by the Supabase REST schema.")

        required = set(definition.get("required") or [])
        columns: list[ColumnMeta] = []
        for position, (name, spec) in enumerate((definition.get("properties") or {}).items(), 1):
            spec = spec if isinstance(spec, dict) else {}
            description = str(spec.get("description") or "")
            data_type = str(spec.get("format") or spec.get("type") or "unknown")
            fk = _FK_RE.search(description)
            columns.append(
                ColumnMeta(
                    column_name=name,
                    ordinal_position=position,
                    data_type=data_type,
                    is_nullable=name not in required,
                    default_value=(
                        str(spec.get("default")) if spec.get("default") is not None else None
                    ),
                    is_primary_key=_PK_MARKER in description,
                    is_foreign_key=fk is not None,
                    references_table=fk.group(1) if fk else None,
                    references_column=fk.group(2) if fk else None,
                    comment=description.split("<")[0].strip() or None,
                    type_family=_family_of(data_type),
                )
            )
        return columns

    def get_primary_keys(self, schema: str, table: str) -> list[str]:
        return [c.column_name for c in self.get_column_metadata(schema, table) if c.is_primary_key]

    def get_foreign_keys(self, schema: str, table: str) -> list[ForeignKeyMeta]:
        return [
            ForeignKeyMeta(
                constrained_columns=[c.column_name],
                referred_schema=self.schema,
                referred_table=c.references_table or "",
                referred_columns=[c.references_column or ""],
                name=None,
            )
            for c in self.get_column_metadata(schema, table)
            if c.is_foreign_key and c.references_table
        ]

    def estimate_row_count(self, schema: str, table: str) -> int | None:
        return self.count_rows(schema, table)

    def count_rows(self, schema: str, table: str) -> int | None:
        """Exact count from the Content-Range header — no row transfer."""
        validate_identifier(table, kind="table name")
        try:
            response = self._request(
                f"/{table}",
                params={"select": "*", "limit": 1},
                headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
            )
        except ConnectorError:
            return None
        total = (response.headers.get("content-range") or "").split("/")[-1]
        return int(total) if total.isdigit() else None

    def time_extent(self, schema: str, table: str, column: str) -> tuple[Any, Any] | None:
        """Earliest and latest value of ``column``, as two one-row reads.

        PostgREST may have aggregate functions disabled, so MIN/MAX are obtained
        the way the transport always can: order by the column and take one row
        from each end. Two rows cross the wire, not a table.
        """
        validate_identifier(table, kind="table name")
        validate_identifier(column, kind="time column")

        def edge(direction: str) -> Any:
            response = self._request(
                f"/{table}",
                params=[
                    ("select", column),
                    ("order", f"{column}.{direction}.nullslast"),
                    ("limit", "1"),
                ],
            )
            body = response.json()
            if not isinstance(body, list) or not body:
                return None
            self.rows_returned += len(body)
            return body[0].get(column)

        try:
            return (edge("asc"), edge("desc"))
        except ConnectorError:
            return None

    # -- execution -------------------------------------------------------
    def execute_query(
        self, sql: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        raise ConnectorError(
            "This Supabase source is connected through the REST API, which cannot run SQL. "
            "Register the project as a PostgreSQL source with its database password to "
            "enable SQL execution.",
            details={"source_type": str(self.source_type), "capability": "sql"},
        )

    def fetch_rows(
        self, schema: str, table: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """A bounded page of rows over PostgREST — no SQL required."""
        return self._rows(table, limit=limit)

    def fetch_projection(
        self,
        schema: str,
        table: str,
        *,
        columns: Sequence[str] = (),
        predicates: Sequence[tuple[str, str]] = (),
        max_rows: int | None = None,
        page_size: int = 1000,
    ) -> ProjectionPage:
        """Rows for one filtered window, projecting only the columns asked for.

        This is what stands in for aggregate pushdown on a project whose REST API
        has aggregate functions disabled (PostgREST answers ``PGRST123``). The
        substitution is only sound because of what it does *not* relax:

        * **Filtering still happens at the source.** Every predicate is a
          PostgREST operator on the URL, so the window is selected by Postgres
          and only matching rows cross the wire. This is not a table scan in
          Python.
        * **The projection is minimal.** Only the columns the caller names are
          selected, so a formula over one numeric column transfers one numeric
          column.
        * **The count is exact regardless of transfer.** ``Prefer: count=exact``
          gives the true number of matching rows in the header, so a row count is
          right even when the rows themselves were capped.
        * **Truncation is reported, never hidden.** ``truncated`` is set when the
          window held more rows than the cap allowed, and the caller is expected
          to decline to compute rather than return a number derived from part of
          the window.

        ``predicates`` are ``(column, "op.value")`` pairs already in PostgREST
        form; translating governed filter operators is the caller's job, so no
        formula semantics live in this transport.
        """

        validate_identifier(table, kind="table name")
        for column in columns:
            validate_identifier(column, kind="column name")
        for column, _expression in predicates:
            validate_identifier(column, kind="filter column")

        cap = min(max_rows or settings.connector_max_rows_returned,
                  settings.connector_max_rows_returned)
        page = max(1, min(page_size, cap))
        # PostgREST requires a select list. When no column is needed -- a pure row
        # count -- the primary key is not necessarily known here, so the narrowest
        # honest request is one row, taken only for its Content-Range header.
        select = ",".join(columns) if columns else "*"

        base: list[tuple[str, str]] = [(column, expression) for column, expression in predicates]
        rows: list[dict[str, Any]] = []
        total: int | None = None
        offset = 0

        while True:
            params = [*base, ("select", select), ("limit", str(page)), ("offset", str(offset))]
            headers = {"Prefer": "count=exact"} if total is None else None
            response = self._request(f"/{table}", params=params, headers=headers)
            if total is None:
                reported = (response.headers.get("content-range") or "").split("/")[-1]
                total = int(reported) if reported.isdigit() else None

            body = response.json()
            if not isinstance(body, list):
                break
            rows.extend(body)
            self.rows_returned += len(body)

            if not columns:
                # Only the count was wanted; one page was enough to obtain it.
                break
            if len(body) < page or len(rows) >= cap:
                break
            offset += page

        truncated = bool(columns) and total is not None and total > len(rows)
        descriptor = self._describe(table, select, base, cap)
        return ProjectionPage(
            rows=rows, total=total, truncated=truncated, descriptor=descriptor
        )

    @staticmethod
    def _describe(
        table: str, select: str, predicates: Sequence[tuple[str, str]], cap: int
    ) -> str:
        """The request, in a form safe to log and show.

        Path and query only: the project URL, the API key and the bearer token all
        live in client configuration and headers, none of which appear here.
        """

        query = "&".join(f"{column}={expression}" for column, expression in predicates)
        parts = [f"select={select}"]
        if query:
            parts.append(query)
        parts.append(f"limit={cap}")
        return f"GET /{table}?" + "&".join(parts)

    def get_refresh_metadata(
        self, schema: str, table: str, time_column: str | None = None
    ) -> RefreshMetadata:
        total = self.count_rows(schema, table)
        if not time_column:
            return RefreshMetadata(
                row_count=total,
                observed_at=datetime.now(),
                note="No time column configured; freshness cannot be determined.",
            )
        validate_identifier(time_column, kind="column name")
        # Ordered single-row reads give exact coverage bounds without a scan.
        newest = self._request(
            f"/{table}",
            params={"select": time_column, "order": f"{time_column}.desc", "limit": 1},
        ).json()
        oldest = self._request(
            f"/{table}",
            params={"select": time_column, "order": f"{time_column}.asc", "limit": 1},
        ).json()
        return RefreshMetadata(
            time_column=time_column,
            coverage_start=_parse_dt(_first(oldest, time_column)),
            coverage_end=_parse_dt(_first(newest, time_column)),
            row_count=total,
            observed_at=datetime.now(),
        )

    # -- profiling (sampled) ---------------------------------------------
    def profile_column(
        self, schema: str, table: str, column: str, *, type_family: str = "OTHER"
    ) -> ColumnStats:
        validate_identifier(column, kind="column name")
        total = self.count_rows(schema, table) or 0
        rows = self._rows(table, select=column, limit=SAMPLE_LIMIT)
        if total > len(rows):
            self.stats_are_sampled = True

        values = [row.get(column) for row in rows]
        present = [v for v in values if v is not None]
        numeric = [float(v) for v in present if isinstance(v, int | float) and not isinstance(v, bool)]

        return ColumnStats(
            # Rows actually examined, so every percentage below is honest about
            # its own denominator.
            row_count=len(values),
            null_count=len(values) - len(present),
            distinct_count=len({_hashable(v) for v in present}),
            min_value=jsonable(min(numeric)) if numeric else _extreme(present, minimum=True),
            max_value=jsonable(max(numeric)) if numeric else _extreme(present, minimum=False),
            mean_value=round(sum(numeric) / len(numeric), 6) if numeric else None,
            zero_count=sum(1 for v in numeric if v == 0) if numeric else None,
            negative_count=sum(1 for v in numeric if v < 0) if numeric else None,
            blank_count=sum(1 for v in present if isinstance(v, str) and not v.strip()),
            sample_values=[
                jsonable(v) for v in list(dict.fromkeys(_hashable(v) for v in present))[
                    : settings.profiling_sample_value_limit
                ]
            ],
        )

    def count_distinct_combination(
        self, schema: str, table: str, columns: list[str]
    ) -> int | None:
        if not columns:
            return None
        for column in columns:
            validate_identifier(column, kind="column name")
        rows = self._rows(table, select=",".join(columns), limit=SAMPLE_LIMIT)
        total = self.count_rows(schema, table) or 0
        if total > len(rows):
            self.stats_are_sampled = True
        return len({tuple(_hashable(row.get(c)) for c in columns) for row in rows})

    def count_orphans(
        self,
        child_schema: str,
        child_table: str,
        child_column: str,
        parent_schema: str,
        parent_table: str,
        parent_column: str,
    ) -> int | None:
        validate_identifier(child_column, kind="column name")
        validate_identifier(parent_column, kind="column name")
        parent_keys = {
            _hashable(row.get(parent_column))
            for row in self._rows(parent_table, select=parent_column)
        }
        child_rows = self._rows(child_table, select=child_column)
        self.stats_are_sampled = True
        return sum(
            1
            for row in child_rows
            if row.get(child_column) is not None
            and _hashable(row.get(child_column)) not in parent_keys
        )

    def max_group_size(self, schema: str, table: str, column: str) -> int | None:
        validate_identifier(column, kind="column name")
        rows = self._rows(table, select=column)
        total = self.count_rows(schema, table) or 0
        if total > len(rows):
            self.stats_are_sampled = True
        counts = Counter(
            _hashable(row.get(column)) for row in rows if row.get(column) is not None
        )
        return max(counts.values()) if counts else None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _family_of(data_type: str) -> str:
    lowered = (data_type or "").lower()
    if "bool" in lowered:
        return "BOOLEAN"
    if any(hint in lowered for hint in ("timestamp", "date", "time")):
        return "TEMPORAL"
    if any(
        hint in lowered
        for hint in ("int", "numeric", "double", "real", "float", "decimal", "money", "number")
    ):
        return "NUMERIC"
    if any(hint in lowered for hint in ("char", "text", "uuid", "json", "string")):
        return "TEXT"
    return "OTHER"


def _hashable(value: Any) -> Any:
    if isinstance(value, dict | list):
        return repr(value)
    return value


def _extreme(values: list[Any], *, minimum: bool) -> Any:
    comparable = [v for v in values if isinstance(v, str | int | float)]
    if not comparable:
        return None
    try:
        return jsonable(min(comparable) if minimum else max(comparable))
    except TypeError:
        return None


def _first(payload: Any, key: str) -> Any:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0].get(key)
    return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
