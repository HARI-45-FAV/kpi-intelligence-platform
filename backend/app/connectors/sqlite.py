"""SQLite file connector.

Present so the platform has a zero-credential SQL source for the automated test
suite: the golden end-to-end test registers a real data source and drives the
real API rather than stubbing the connector layer. It is not a demo dataset and
carries no bundled business data.
"""

from __future__ import annotations

from app.connectors.sql import SqlConnector
from app.models.base import DataSourceType


class SQLiteConnector(SqlConnector):
    source_type = DataSourceType.SQLITE
    default_schema = "main"

    def __init__(self, path: str, schema: str | None = None) -> None:
        url = path if path.startswith("sqlite") else f"sqlite:///{path}"
        super().__init__(url, source_type=DataSourceType.SQLITE, schema=schema or "main")
