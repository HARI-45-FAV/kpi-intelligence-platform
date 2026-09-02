"""The connector for an uploaded spreadsheet.

Deliberately almost empty, and that is the point. An uploaded CSV or Excel sheet is
loaded into a SQLite database owned by the company (see ``services.tabular``), so by
the time anything wants to *read* it, it is a SQL source — and ``SqlConnector``
already knows how to reflect, profile and query one of those under the platform's
read-only rules.

It exists as its own type rather than registering uploads as ``SQLITE`` because the
audit trail should not have to lie about where data came from. A source that says
``UPLOAD`` and carries the original filename, its checksum and the moment it was
loaded is answerable; one that says ``SQLITE`` and points at a server path is not.
The distinction also drives what the UI offers: an upload can be *re-uploaded*,
which is a different gesture from editing a connection.
"""

from __future__ import annotations

from app.connectors.sql import SqlConnector
from app.models.base import DataSourceType


class UploadedFileConnector(SqlConnector):
    """Reads the SQLite database an uploaded file was loaded into.

    The schema is always ``main``: a SQLite file has exactly one, and pretending
    otherwise would put a name in the catalog that no query could resolve.
    """

    source_type = DataSourceType.UPLOAD
    default_schema = "main"

    def __init__(self, path: str) -> None:
        super().__init__(
            f"sqlite:///{path}", source_type=DataSourceType.UPLOAD, schema="main"
        )
