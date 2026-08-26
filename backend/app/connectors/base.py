"""The universal data-source connector interface.

Every analytical read in the platform goes through this interface. Two rules are
enforced here rather than trusted to callers:

* **Identifiers are allow-listed, never interpolated blindly.** Table and column
  names cannot be bound as SQL parameters, so each one is validated against a
  strict pattern and then quoted for the dialect.
* **Reads only.** ``execute_query`` rejects anything that is not a single
  SELECT/WITH statement. Profiling never needs more, and a BI platform that can
  write to a tenant's production database is a liability.

Profiling is expressed as *aggregate queries pushed down to the source*. The
platform never streams a table into Python to compute a null percentage.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.errors import UnsafeQueryError

# Deliberately conservative: letters, digits, underscore, dollar. Anything with
# whitespace, quotes, semicolons or comment markers is rejected outright.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|"
    r"copy|vacuum|attach|detach|pragma|call|do|execute|commit|rollback)\b",
    re.IGNORECASE,
)


def validate_identifier(name: str, *, kind: str = "identifier") -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise UnsafeQueryError(f"Rejected unsafe {kind}: {name!r}")
    return name


def assert_read_only(sql: str) -> None:
    """Allow exactly one SELECT/WITH statement."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise UnsafeQueryError("Empty query.")
    if ";" in stripped:
        raise UnsafeQueryError("Multiple statements are not permitted.")
    if "--" in stripped or "/*" in stripped:
        raise UnsafeQueryError("SQL comments are not permitted.")
    lowered = stripped.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise UnsafeQueryError("Only SELECT statements are permitted.")
    if _FORBIDDEN_SQL.search(stripped):
        raise UnsafeQueryError("Query contains a non-read operation.")


# ---------------------------------------------------------------------------
# Metadata value objects
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TableMeta:
    schema_name: str
    table_name: str
    table_type: str = "TABLE"
    approx_row_count: int | None = None
    column_count: int | None = None
    comment: str | None = None
    database_name: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


@dataclass(slots=True)
class ColumnMeta:
    column_name: str
    ordinal_position: int
    data_type: str
    is_nullable: bool = True
    default_value: str | None = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    references_table: str | None = None
    references_column: str | None = None
    comment: str | None = None
    # NUMERIC | TEMPORAL | TEXT | BOOLEAN | OTHER
    type_family: str = "OTHER"


@dataclass(slots=True)
class ForeignKeyMeta:
    constrained_columns: list[str]
    referred_schema: str | None
    referred_table: str
    referred_columns: list[str]
    name: str | None = None


@dataclass(slots=True)
class ColumnStats:
    """Result of one pushed-down profiling pass over a single column."""

    row_count: int | None = None
    null_count: int | None = None
    distinct_count: int | None = None
    min_value: Any = None
    max_value: Any = None
    mean_value: float | None = None
    zero_count: int | None = None
    negative_count: int | None = None
    blank_count: int | None = None
    sample_values: list[Any] = field(default_factory=list)

    @property
    def null_pct(self) -> float | None:
        if not self.row_count:
            return 0.0 if self.row_count == 0 else None
        if self.null_count is None:
            return None
        return round(self.null_count / self.row_count * 100, 4)

    @property
    def distinct_pct(self) -> float | None:
        if not self.row_count or self.distinct_count is None:
            return None
        return round(self.distinct_count / self.row_count * 100, 4)

    @property
    def is_unique(self) -> bool | None:
        if not self.row_count or self.distinct_count is None or self.null_count is None:
            return None
        non_null = self.row_count - self.null_count
        return non_null > 0 and self.distinct_count == non_null


@dataclass(slots=True)
class ConnectionTestResult:
    ok: bool
    message: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    server_version: str | None = None
    table_count: int | None = None
    duration_ms: int | None = None
    error: str | None = None


@dataclass(slots=True)
class RefreshMetadata:
    time_column: str | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    row_count: int | None = None
    observed_at: datetime | None = None
    note: str | None = None


def jsonable(value: Any) -> Any:
    """Coerce a database value into something JSON-serialisable."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------
class DataSourceConnector(ABC):
    """One interface, many source systems.

    Subclasses that speak SQL should extend ``SqlConnector`` rather than
    implementing this directly — it derives every profiling query from the four
    primitives below.
    """

    source_type: str = "ABSTRACT"
    supports_profiling: bool = True

    # -- connection ------------------------------------------------------
    @abstractmethod
    def test_connection(self) -> ConnectionTestResult: ...

    @abstractmethod
    def list_databases(self) -> list[str]: ...

    @abstractmethod
    def list_schemas(self) -> list[str]: ...

    @abstractmethod
    def list_tables(self, schema: str | None = None) -> list[TableMeta]: ...

    # -- structure -------------------------------------------------------
    @abstractmethod
    def get_table_metadata(self, schema: str, table: str) -> TableMeta: ...

    @abstractmethod
    def get_column_metadata(self, schema: str, table: str) -> list[ColumnMeta]: ...

    @abstractmethod
    def get_primary_keys(self, schema: str, table: str) -> list[str]: ...

    @abstractmethod
    def get_foreign_keys(self, schema: str, table: str) -> list[ForeignKeyMeta]: ...

    @abstractmethod
    def estimate_row_count(self, schema: str, table: str) -> int | None: ...

    # -- execution -------------------------------------------------------
    @abstractmethod
    def execute_query(
        self, sql: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_refresh_metadata(
        self, schema: str, table: str, time_column: str | None = None
    ) -> RefreshMetadata: ...

    def fetch_rows(
        self, schema: str, table: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Read up to ``limit`` whole rows from one table.

        Kept separate from ``execute_query`` because not every connector can run
        SQL — PostgREST cannot — yet every connector can read a bounded page of
        rows. Used for small governance tables the company maintains itself, such
        as a KPI-definition registry. Never used for analytical volume: profiling
        still pushes aggregates down to the source.
        """
        raise NotImplementedError(
            f"{self.source_type} cannot read rows from {schema}.{table}."
        )

    # -- profiling primitives (pushed down) -----------------------------
    @abstractmethod
    def count_rows(self, schema: str, table: str) -> int | None: ...

    @abstractmethod
    def profile_column(
        self, schema: str, table: str, column: str, *, type_family: str = "OTHER"
    ) -> ColumnStats: ...

    @abstractmethod
    def count_distinct_combination(
        self, schema: str, table: str, columns: list[str]
    ) -> int | None: ...

    @abstractmethod
    def count_orphans(
        self,
        child_schema: str,
        child_table: str,
        child_column: str,
        parent_schema: str,
        parent_table: str,
        parent_column: str,
    ) -> int | None: ...

    @abstractmethod
    def max_group_size(self, schema: str, table: str, column: str) -> int | None: ...

    # -- convenience -----------------------------------------------------
    def close(self) -> None:  # pragma: no cover - most connectors are stateless
        return None

    def __enter__(self) -> DataSourceConnector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
