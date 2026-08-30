"""Shared SQL implementation of ``DataSourceConnector``.

All profiling is a single aggregate query per column, executed *inside* the
source database. Postgres and the sample SQLite source share every line of this
file; a warehouse connector only needs to override dialect specifics.
"""

from __future__ import annotations

import hashlib
import time
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.connectors.base import (
    ColumnMeta,
    ColumnStats,
    ConnectionTestResult,
    DataSourceConnector,
    ForeignKeyMeta,
    RefreshMetadata,
    TableMeta,
    assert_read_only,
    jsonable,
    validate_identifier,
)
from app.core.config import settings
from app.core.errors import ConnectorError

_NUMERIC_HINTS = ("int", "numeric", "decimal", "float", "double", "real", "money", "serial")
_TEMPORAL_HINTS = ("date", "time", "timestamp")
_BOOLEAN_HINTS = ("bool",)
_TEXT_HINTS = ("char", "text", "string", "uuid", "json", "enum")


def classify_type_family(data_type: str) -> str:
    lowered = (data_type or "").lower()
    # Order matters: "timestamp" contains no numeric hint, but "smallint" does.
    if any(hint in lowered for hint in _BOOLEAN_HINTS):
        return "BOOLEAN"
    if any(hint in lowered for hint in _TEMPORAL_HINTS):
        return "TEMPORAL"
    if any(hint in lowered for hint in _NUMERIC_HINTS):
        return "NUMERIC"
    if any(hint in lowered for hint in _TEXT_HINTS):
        return "TEXT"
    return "OTHER"


class SqlConnector(DataSourceConnector):
    """Concrete connector for anything SQLAlchemy can reach."""

    default_schema = "public"

    def __init__(self, url: str, *, source_type: str = "SQL", schema: str | None = None) -> None:
        self._url = url
        self.source_type = source_type
        self._schema = schema or self.default_schema
        self._engine: Engine | None = None
        # Telemetry: how much work this connector did for the current request.
        self.query_count = 0
        self.query_duration_ms = 0
        self.rows_returned = 0
        self.last_query_hash: str | None = None

    # -- engine ----------------------------------------------------------
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            try:
                self._engine = create_engine(
                    self._url,
                    pool_pre_ping=True,
                    connect_args=self._connect_args(),
                    future=True,
                )
            except SQLAlchemyError as exc:
                raise ConnectorError(f"Could not create engine: {exc}") from exc
        return self._engine

    def _connect_args(self) -> dict[str, Any]:
        if self._url.startswith("sqlite"):
            return {"check_same_thread": False}
        return {"connect_timeout": settings.connector_query_timeout_seconds}

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # -- identifier handling --------------------------------------------
    def quote(self, name: str, *, kind: str = "identifier") -> str:
        validate_identifier(name, kind=kind)
        return self.engine.dialect.identifier_preparer.quote(name)

    def qualify(self, schema: str | None, table: str) -> str:
        quoted_table = self.quote(table, kind="table name")
        if not schema or not self.supports_schemas:
            return quoted_table
        return f"{self.quote(schema, kind='schema name')}.{quoted_table}"

    @property
    def supports_schemas(self) -> bool:
        return self.engine.dialect.name != "sqlite"

    def resolve_schema(self, schema: str | None) -> str:
        if not self.supports_schemas:
            return "main"
        return schema or self._schema

    def temporal_param(self, value: object) -> object:
        """Adapt a date/datetime bind parameter for this dialect.

        SQLite stores temporal values as ISO text and compares them as strings,
        so a ``date`` object must be rendered. PostgreSQL types its parameters,
        and sending text where a DATE is expected raises "operator does not
        exist: date >= text" — so the native object must be preserved there.
        """
        if value is None:
            return None
        if self.engine.dialect.name == "sqlite" and isinstance(value, date | datetime):
            return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
        return value

    # -- raw execution ---------------------------------------------------
    def _run(
        self, sql: str, params: dict[str, Any] | None = None, *, guard: bool = True
    ) -> list[dict[str, Any]]:
        if guard:
            assert_read_only(sql)
        self.last_query_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        started = time.perf_counter()
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql), params or {})
                rows = [dict(row) for row in result.mappings()]
        except SQLAlchemyError as exc:
            self.query_count += 1
            raise ConnectorError(self._clean_error(exc)) from exc
        finally:
            self.query_duration_ms += int((time.perf_counter() - started) * 1000)
        self.query_count += 1
        self.rows_returned += len(rows)
        return rows

    @staticmethod
    def _clean_error(exc: Exception) -> str:
        """Strip credentials and stack noise out of driver errors."""
        message = str(getattr(exc, "orig", exc)).strip().splitlines()
        first = message[0] if message else "unknown database error"
        return first[:400]

    def execute_query(
        self, sql: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        effective_limit = min(limit or settings.connector_max_rows_returned,
                              settings.connector_max_rows_returned)
        assert_read_only(sql)
        wrapped = f"SELECT * FROM ({sql.strip().rstrip(';')}) AS _bounded LIMIT {effective_limit:d}"
        rows = self._run(wrapped, params, guard=False)
        return [{k: jsonable(v) for k, v in row.items()} for row in rows]

    def fetch_rows(
        self, schema: str, table: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        validate_identifier(table, kind="table name")
        capped = min(int(limit), settings.connector_max_rows_returned)
        rows = self._run(f"SELECT * FROM {self.qualify(schema, table)} LIMIT {capped:d}")
        return [{k: jsonable(v) for k, v in row.items()} for row in rows]

    # -- connection ------------------------------------------------------
    def test_connection(self) -> ConnectionTestResult:
        started = time.perf_counter()
        checks: list[dict[str, Any]] = []
        try:
            rows = self._run("SELECT 1 AS ok")
            checks.append({"check": "Connection established", "ok": bool(rows)})
            checks.append({"check": "Authentication successful", "ok": True})
            version = self._server_version()
            schemas = self.list_schemas()
            target = self.resolve_schema(self._schema)
            schema_ok = (not self.supports_schemas) or target in schemas
            checks.append(
                {
                    "check": f"Schema accessible: {target}",
                    "ok": schema_ok,
                    "detail": None if schema_ok else f"available: {', '.join(schemas[:8])}",
                }
            )
            tables = self.list_tables(target) if schema_ok else []
            checks.append({"check": f"{len(tables)} tables detected", "ok": bool(tables)})
            duration = int((time.perf_counter() - started) * 1000)
            ok = all(c["ok"] for c in checks)
            return ConnectionTestResult(
                ok=ok,
                message="Connected." if ok else "Connected with warnings.",
                checks=checks,
                server_version=version,
                table_count=len(tables),
                duration_ms=duration,
            )
        except ConnectorError as exc:
            checks.append({"check": "Connection established", "ok": False, "detail": exc.message})
            return ConnectionTestResult(
                ok=False,
                message="Connection failed.",
                checks=checks,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=exc.message,
            )

    def _server_version(self) -> str | None:
        try:
            with self.engine.connect() as conn:
                return str(conn.dialect.server_version_info or conn.dialect.name)
        except SQLAlchemyError:  # pragma: no cover
            return None

    def list_databases(self) -> list[str]:
        name = self.engine.url.database
        return [name] if name else []

    def list_schemas(self) -> list[str]:
        if not self.supports_schemas:
            return ["main"]
        try:
            return sorted(inspect(self.engine).get_schema_names())
        except SQLAlchemyError as exc:
            raise ConnectorError(self._clean_error(exc)) from exc

    def list_tables(self, schema: str | None = None) -> list[TableMeta]:
        target = self.resolve_schema(schema)
        inspector = inspect(self.engine)
        insp_schema = None if not self.supports_schemas else target
        try:
            names = sorted(inspector.get_table_names(schema=insp_schema))
            views = set(inspector.get_view_names(schema=insp_schema))
        except SQLAlchemyError as exc:
            raise ConnectorError(self._clean_error(exc)) from exc

        results: list[TableMeta] = []
        for name in names + sorted(views):
            try:
                columns = inspector.get_columns(name, schema=insp_schema)
            except SQLAlchemyError:  # pragma: no cover - permission-denied tables
                columns = []
            results.append(
                TableMeta(
                    schema_name=target,
                    table_name=name,
                    table_type="VIEW" if name in views else "TABLE",
                    approx_row_count=self.estimate_row_count(target, name),
                    column_count=len(columns),
                    database_name=self.engine.url.database,
                )
            )
        return results

    # -- structure -------------------------------------------------------
    def get_table_metadata(self, schema: str, table: str) -> TableMeta:
        target = self.resolve_schema(schema)
        columns = self.get_column_metadata(target, table)
        return TableMeta(
            schema_name=target,
            table_name=table,
            approx_row_count=self.estimate_row_count(target, table),
            column_count=len(columns),
            database_name=self.engine.url.database,
        )

    def get_column_metadata(self, schema: str, table: str) -> list[ColumnMeta]:
        target = self.resolve_schema(schema)
        validate_identifier(table, kind="table name")
        inspector = inspect(self.engine)
        insp_schema = None if not self.supports_schemas else target
        try:
            raw_columns = inspector.get_columns(table, schema=insp_schema)
        except SQLAlchemyError as exc:
            raise ConnectorError(self._clean_error(exc)) from exc

        primary_keys = set(self.get_primary_keys(target, table))
        fk_map: dict[str, tuple[str, str]] = {}
        for fk in self.get_foreign_keys(target, table):
            for local, remote in zip(fk.constrained_columns, fk.referred_columns, strict=False):
                fk_map[local] = (fk.referred_table, remote)

        columns: list[ColumnMeta] = []
        for position, raw in enumerate(raw_columns, start=1):
            name = raw["name"]
            data_type = str(raw.get("type", "UNKNOWN"))
            reference = fk_map.get(name)
            columns.append(
                ColumnMeta(
                    column_name=name,
                    ordinal_position=position,
                    data_type=data_type,
                    is_nullable=bool(raw.get("nullable", True)),
                    default_value=(
                        str(raw["default"]) if raw.get("default") is not None else None
                    ),
                    is_primary_key=name in primary_keys,
                    is_foreign_key=reference is not None,
                    references_table=reference[0] if reference else None,
                    references_column=reference[1] if reference else None,
                    comment=raw.get("comment"),
                    type_family=classify_type_family(data_type),
                )
            )
        return columns

    def get_primary_keys(self, schema: str, table: str) -> list[str]:
        target = self.resolve_schema(schema)
        validate_identifier(table, kind="table name")
        insp_schema = None if not self.supports_schemas else target
        try:
            constraint = inspect(self.engine).get_pk_constraint(table, schema=insp_schema)
        except SQLAlchemyError:  # pragma: no cover
            return []
        return list(constraint.get("constrained_columns") or [])

    def get_foreign_keys(self, schema: str, table: str) -> list[ForeignKeyMeta]:
        target = self.resolve_schema(schema)
        validate_identifier(table, kind="table name")
        insp_schema = None if not self.supports_schemas else target
        try:
            raw = inspect(self.engine).get_foreign_keys(table, schema=insp_schema)
        except SQLAlchemyError:  # pragma: no cover
            return []
        return [
            ForeignKeyMeta(
                constrained_columns=list(item.get("constrained_columns") or []),
                referred_schema=item.get("referred_schema"),
                referred_table=item.get("referred_table", ""),
                referred_columns=list(item.get("referred_columns") or []),
                name=item.get("name"),
            )
            for item in raw
            if item.get("referred_table")
        ]

    def estimate_row_count(self, schema: str, table: str) -> int | None:
        return self.count_rows(schema, table)

    # -- profiling primitives -------------------------------------------
    def count_rows(self, schema: str, table: str) -> int | None:
        target = self.resolve_schema(schema)
        rows = self._run(f"SELECT COUNT(*) AS n FROM {self.qualify(target, table)}")
        return int(rows[0]["n"]) if rows else None

    def time_extent(self, schema: str, table: str, column: str) -> tuple[Any, Any] | None:
        """MIN and MAX of one column, in a single pushed-down aggregate."""
        target = self.resolve_schema(schema)
        col = self.quote(column, kind="time column")
        rows = self._run(
            f"SELECT MIN({col}) AS lo, MAX({col}) AS hi "  # noqa: S608 - identifiers quoted
            f"FROM {self.qualify(target, table)}"
        )
        if not rows:
            return None
        return (rows[0].get("lo"), rows[0].get("hi"))

    def profile_column(
        self, schema: str, table: str, column: str, *, type_family: str = "OTHER"
    ) -> ColumnStats:
        """One aggregate query, computed in the database.

        The projection varies by type family so we never ask a text column for
        an average or a date column for a negative count.
        """
        target = self.resolve_schema(schema)
        relation = self.qualify(target, table)
        col = self.quote(column, kind="column name")

        projections = [
            "COUNT(*) AS row_count",
            f"COUNT({col}) AS non_null_count",
            f"COUNT(DISTINCT {col}) AS distinct_count",
        ]
        if type_family == "NUMERIC":
            projections += [
                f"MIN({col}) AS min_value",
                f"MAX({col}) AS max_value",
                f"AVG({col} * 1.0) AS mean_value",
                f"SUM(CASE WHEN {col} = 0 THEN 1 ELSE 0 END) AS zero_count",
                f"SUM(CASE WHEN {col} < 0 THEN 1 ELSE 0 END) AS negative_count",
            ]
        elif type_family in {"TEMPORAL", "BOOLEAN"}:
            projections += [f"MIN({col}) AS min_value", f"MAX({col}) AS max_value"]
        else:
            projections += [
                f"MIN({col}) AS min_value",
                f"MAX({col}) AS max_value",
                f"SUM(CASE WHEN TRIM({col}) = '' THEN 1 ELSE 0 END) AS blank_count",
            ]

        sql = f"SELECT {', '.join(projections)} FROM {relation}"  # noqa: S608 - identifiers validated
        row = self._run(sql)[0]
        row_count = int(row["row_count"] or 0)
        non_null = int(row["non_null_count"] or 0)

        return ColumnStats(
            row_count=row_count,
            null_count=row_count - non_null,
            distinct_count=int(row["distinct_count"]) if row.get("distinct_count") is not None else None,
            min_value=jsonable(row.get("min_value")),
            max_value=jsonable(row.get("max_value")),
            mean_value=float(row["mean_value"]) if row.get("mean_value") is not None else None,
            zero_count=int(row["zero_count"]) if row.get("zero_count") is not None else None,
            negative_count=(
                int(row["negative_count"]) if row.get("negative_count") is not None else None
            ),
            blank_count=int(row["blank_count"]) if row.get("blank_count") is not None else None,
            sample_values=self.sample_values(target, table, column),
        )

    def sample_values(
        self, schema: str, table: str, column: str, limit: int | None = None
    ) -> list[Any]:
        target = self.resolve_schema(schema)
        n = limit or settings.profiling_sample_value_limit
        col = self.quote(column, kind="column name")
        sql = (
            f"SELECT DISTINCT {col} AS v FROM {self.qualify(target, table)} "  # noqa: S608
            f"WHERE {col} IS NOT NULL ORDER BY {col} LIMIT {int(n)}"
        )
        return [jsonable(row["v"]) for row in self._run(sql)]

    def count_distinct_combination(
        self, schema: str, table: str, columns: list[str]
    ) -> int | None:
        if not columns:
            return None
        target = self.resolve_schema(schema)
        quoted = ", ".join(self.quote(c, kind="column name") for c in columns)
        sql = (
            f"SELECT COUNT(*) AS n FROM "  # noqa: S608
            f"(SELECT DISTINCT {quoted} FROM {self.qualify(target, table)}) AS _combo"
        )
        rows = self._run(sql)
        return int(rows[0]["n"]) if rows else None

    def count_orphans(
        self,
        child_schema: str,
        child_table: str,
        child_column: str,
        parent_schema: str,
        parent_table: str,
        parent_column: str,
    ) -> int | None:
        """Rows in the child whose key has no match in the parent."""
        child_rel = self.qualify(self.resolve_schema(child_schema), child_table)
        parent_rel = self.qualify(self.resolve_schema(parent_schema), parent_table)
        child_col = self.quote(child_column, kind="column name")
        parent_col = self.quote(parent_column, kind="column name")
        sql = (
            f"SELECT COUNT(*) AS n FROM {child_rel} AS c "  # noqa: S608
            f"LEFT JOIN {parent_rel} AS p ON c.{child_col} = p.{parent_col} "
            f"WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL"
        )
        rows = self._run(sql)
        return int(rows[0]["n"]) if rows else None

    def max_group_size(self, schema: str, table: str, column: str) -> int | None:
        """Largest number of rows sharing one key value — the fan-out ceiling."""
        target = self.resolve_schema(schema)
        col = self.quote(column, kind="column name")
        sql = (
            f"SELECT MAX(c) AS n FROM (SELECT {col}, COUNT(*) AS c "  # noqa: S608
            f"FROM {self.qualify(target, table)} WHERE {col} IS NOT NULL "
            f"GROUP BY {col}) AS _g"
        )
        rows = self._run(sql)
        value = rows[0]["n"] if rows else None
        return int(value) if value is not None else None

    # -- freshness -------------------------------------------------------
    def get_refresh_metadata(
        self, schema: str, table: str, time_column: str | None = None
    ) -> RefreshMetadata:
        target = self.resolve_schema(schema)
        if not time_column:
            return RefreshMetadata(
                row_count=self.count_rows(target, table),
                observed_at=datetime.now(),
                note="No time column configured; freshness cannot be determined.",
            )
        col = self.quote(time_column, kind="column name")
        sql = (
            f"SELECT MIN({col}) AS coverage_start, MAX({col}) AS coverage_end, "  # noqa: S608
            f"COUNT(*) AS row_count FROM {self.qualify(target, table)}"
        )
        row = self._run(sql)[0]
        return RefreshMetadata(
            time_column=time_column,
            coverage_start=_coerce_datetime(row.get("coverage_start")),
            coverage_end=_coerce_datetime(row.get("coverage_end")),
            row_count=int(row["row_count"] or 0),
            observed_at=datetime.now(),
        )


def _coerce_datetime(value: Any) -> datetime | None:
    """Source columns may come back as datetime, date or ISO text."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if hasattr(value, "year") and not isinstance(value, str):  # date
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
