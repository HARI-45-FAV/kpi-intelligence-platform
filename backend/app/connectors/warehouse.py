"""Warehouse connector interfaces.

Sprint 1 defines these so the registry, UI and KPI contracts are already
warehouse-shaped, and deliberately does not implement their infrastructure —
that work buys nothing for the Sprint 1 goal. Each class documents exactly what
a full implementation must supply, and every method fails loudly rather than
silently returning empty metadata, which would look like "a warehouse with no
tables" in the catalog.
"""

from __future__ import annotations

from typing import Any, NoReturn

from app.connectors.base import (
    ColumnMeta,
    ColumnStats,
    ConnectionTestResult,
    DataSourceConnector,
    ForeignKeyMeta,
    RefreshMetadata,
    TableMeta,
)
from app.core.errors import ConnectorError
from app.models.base import DataSourceType


class _PlannedConnector(DataSourceConnector):
    """Interface-only connector. Registered, discoverable, not yet implemented."""

    supports_profiling = False
    driver_package: str = ""
    required_options: tuple[str, ...] = ()

    def _unavailable(self, operation: str) -> NoReturn:
        raise ConnectorError(
            f"{self.source_type} support is defined but not implemented in Sprint 1 "
            f"(requested: {operation}). Install {self.driver_package} and provide "
            f"{', '.join(self.required_options)} to enable it.",
            details={
                "source_type": str(self.source_type),
                "driver_package": self.driver_package,
                "required_options": list(self.required_options),
                "status": "INTERFACE_ONLY",
            },
        )

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(
            ok=False,
            message=f"{self.source_type} connector is interface-only in Sprint 1.",
            checks=[
                {"check": "Connector registered", "ok": True},
                {"check": "Driver implemented", "ok": False, "detail": self.driver_package},
            ],
            error="INTERFACE_ONLY",
        )

    def list_databases(self) -> list[str]:
        self._unavailable("list_databases")

    def list_schemas(self) -> list[str]:
        self._unavailable("list_schemas")

    def list_tables(self, schema: str | None = None) -> list[TableMeta]:
        self._unavailable("list_tables")

    def get_table_metadata(self, schema: str, table: str) -> TableMeta:
        self._unavailable("get_table_metadata")

    def get_column_metadata(self, schema: str, table: str) -> list[ColumnMeta]:
        self._unavailable("get_column_metadata")

    def get_primary_keys(self, schema: str, table: str) -> list[str]:
        self._unavailable("get_primary_keys")

    def get_foreign_keys(self, schema: str, table: str) -> list[ForeignKeyMeta]:
        self._unavailable("get_foreign_keys")

    def estimate_row_count(self, schema: str, table: str) -> int | None:
        self._unavailable("estimate_row_count")

    def execute_query(
        self, sql: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._unavailable("execute_query")

    def get_refresh_metadata(
        self, schema: str, table: str, time_column: str | None = None
    ) -> RefreshMetadata:
        self._unavailable("get_refresh_metadata")

    def count_rows(self, schema: str, table: str) -> int | None:
        self._unavailable("count_rows")

    def profile_column(
        self, schema: str, table: str, column: str, *, type_family: str = "OTHER"
    ) -> ColumnStats:
        self._unavailable("profile_column")

    def count_distinct_combination(
        self, schema: str, table: str, columns: list[str]
    ) -> int | None:
        self._unavailable("count_distinct_combination")

    def count_orphans(
        self,
        child_schema: str,
        child_table: str,
        child_column: str,
        parent_schema: str,
        parent_table: str,
        parent_column: str,
    ) -> int | None:
        self._unavailable("count_orphans")

    def max_group_size(self, schema: str, table: str, column: str) -> int | None:
        self._unavailable("max_group_size")


class SnowflakeConnector(_PlannedConnector):
    """Snowflake.

    A full implementation needs: key-pair or OAuth auth, warehouse/role
    selection per query, ``INFORMATION_SCHEMA`` reflection, and
    ``SYSTEM$CLUSTERING_INFORMATION`` for cheap row estimates.
    """

    source_type = DataSourceType.SNOWFLAKE
    driver_package = "snowflake-sqlalchemy"
    required_options = ("account", "warehouse", "role", "database", "schema")


class BigQueryConnector(_PlannedConnector):
    """BigQuery.

    A full implementation needs: service-account credentials, dataset-scoped
    discovery via ``INFORMATION_SCHEMA.TABLES``, ``__TABLES__`` for row counts,
    and byte-scanned accounting so profiling cost is visible in telemetry.
    """

    source_type = DataSourceType.BIGQUERY
    driver_package = "sqlalchemy-bigquery"
    required_options = ("project_id", "dataset", "credentials_json")
