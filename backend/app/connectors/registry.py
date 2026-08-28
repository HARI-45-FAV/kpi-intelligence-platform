"""Connector registry.

Nothing outside this module imports a concrete connector. Callers ask the
registry for one given a ``DataSource`` row, which is what keeps the platform
source-agnostic: implementing Snowflake later touches this file and
``warehouse.py``, not the profiling, catalog or KPI services.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.connectors.base import DataSourceConnector
from app.connectors.postgres import PostgreSQLConnector
from app.connectors.sqlite import SQLiteConnector
from app.connectors.supabase_rest import SupabaseRestConnector
from app.connectors.warehouse import BigQueryConnector, SnowflakeConnector
from app.core.errors import ConnectorError, ValidationFailure
from app.core.security import decrypt_secret
from app.models.base import DataSourceType
from app.models.source import DataSource


@dataclass(frozen=True, slots=True)
class ConnectorField:
    """One input the registration form must collect."""

    name: str
    label: str
    required: bool = True
    # text | number | password | select
    kind: str = "text"
    placeholder: str = ""
    help_text: str = ""
    secret: bool = False


@dataclass(frozen=True, slots=True)
class ConnectorDescriptor:
    source_type: str
    label: str
    implemented: bool
    supports_profiling: bool
    fields: tuple[ConnectorField, ...]
    notes: str
    # When set, the form offers "paste a connection string" as an alternative
    # to filling every field by hand.
    accepts_connection_uri: bool = False


_SUPABASE_FIELDS = (
    ConnectorField(
        name="supabase_url",
        label="Supabase URL",
        placeholder="https://your-project-ref.supabase.co",
        help_text="Supabase → Project Settings → API → Project URL.",
    ),
    ConnectorField(
        name="secret_key",
        label="Secret key",
        kind="password",
        secret=True,
        placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        help_text=(
            "Supabase → Project Settings → API → service_role secret. "
            "Encrypted at rest and never returned by the API."
        ),
    ),
)

_POSTGRES_FIELDS = (
    ConnectorField(name="host", label="Host", placeholder="db.example.com"),
    ConnectorField(name="port", label="Port", kind="number", placeholder="5432", required=False),
    ConnectorField(name="database_name", label="Database", placeholder="postgres"),
    ConnectorField(name="schema_name", label="Schema", required=False, placeholder="public"),
    ConnectorField(name="username", label="Username", placeholder="postgres"),
    ConnectorField(name="password", label="Password", kind="password", secret=True),
    ConnectorField(
        name="sslmode",
        label="SSL mode",
        required=False,
        placeholder="require",
        help_text="require, verify-full, prefer, disable.",
    ),
)


CONNECTOR_CATALOG: tuple[ConnectorDescriptor, ...] = (
    ConnectorDescriptor(
        source_type=DataSourceType.SUPABASE,
        label="Supabase",
        implemented=True,
        supports_profiling=True,
        fields=_SUPABASE_FIELDS,
        # A secret key is a REST credential, not a DSN, so there is no
        # connection string to paste for this type.
        accepts_connection_uri=False,
        notes=(
            "Connects through the project REST API. Discovers tables, columns, keys and "
            "exact row counts; profiles from a bounded sample, labelled as sampled. "
            "For full-scan SQL pushdown, register the project as PostgreSQL with its "
            "database password instead."
        ),
    ),
    ConnectorDescriptor(
        source_type=DataSourceType.POSTGRESQL,
        label="PostgreSQL",
        implemented=True,
        supports_profiling=True,
        fields=_POSTGRES_FIELDS,
        accepts_connection_uri=True,
        notes="Full metadata reflection and pushdown profiling.",
    ),
    ConnectorDescriptor(
        source_type=DataSourceType.SQLITE,
        label="SQLite file",
        implemented=True,
        supports_profiling=True,
        fields=(
            ConnectorField(
                name="path",
                label="Database file path",
                placeholder="/path/to/database.db",
            ),
        ),
        notes="Local file source. Used by the automated test suite.",
    ),
    ConnectorDescriptor(
        source_type=DataSourceType.SNOWFLAKE,
        label="Snowflake",
        implemented=False,
        supports_profiling=False,
        fields=(
            ConnectorField(name="account", label="Account identifier"),
            ConnectorField(name="warehouse", label="Warehouse"),
            ConnectorField(name="role", label="Role"),
            ConnectorField(name="database_name", label="Database"),
            ConnectorField(name="schema_name", label="Schema"),
        ),
        notes="Interface defined; driver implementation deferred beyond Sprint 1.",
    ),
    ConnectorDescriptor(
        source_type=DataSourceType.BIGQUERY,
        label="Google BigQuery",
        implemented=False,
        supports_profiling=False,
        fields=(
            ConnectorField(name="project_id", label="Project ID"),
            ConnectorField(name="dataset", label="Dataset"),
            ConnectorField(
                name="credentials_json", label="Service account JSON", kind="password", secret=True
            ),
        ),
        notes="Interface defined; driver implementation deferred beyond Sprint 1.",
    ),
)

DESCRIPTORS_BY_TYPE = {d.source_type: d for d in CONNECTOR_CATALOG}


def _password_of(source: DataSource) -> str | None:
    if not source.encrypted_credentials:
        return None
    try:
        return decrypt_secret(source.encrypted_credentials)
    except ValueError as exc:
        raise ValidationFailure(
            "The saved credentials for this data source can no longer be read. "
            "Reconnect the source to continue."
        ) from exc


def _build_supabase(source: DataSource) -> DataSourceConnector:
    """Supabase is reached over REST: the stored credential is the secret key."""
    options = source.options or {}
    url = options.get("supabase_url") or (
        f"https://{source.host}" if source.host else None
    )
    if not url:
        raise ValidationFailure("This Supabase source has no project URL stored.")
    if not source.encrypted_credentials:
        raise ValidationFailure("This Supabase source has no secret key stored.")
    try:
        secret_key = decrypt_secret(source.encrypted_credentials)
    except ValueError as exc:
        raise ValidationFailure(
            "The saved credentials for this data source can no longer be read. "
            "Reconnect the source to continue."
        ) from exc
    return SupabaseRestConnector(
        url=url,
        secret_key=secret_key,
        schema=source.schema_name or "public",
    )


def _build_postgres(source: DataSource) -> DataSourceConnector:
    if not source.host or not source.database_name:
        raise ValidationFailure("PostgreSQL sources require a host and database name.")
    return PostgreSQLConnector(
        host=source.host,
        port=source.port,
        database=source.database_name,
        username=source.username,
        password=_password_of(source),
        schema=source.schema_name or "public",
        sslmode=(source.options or {}).get("sslmode"),
    )


def _build_sqlite(source: DataSource) -> DataSourceConnector:
    path = (source.options or {}).get("path") or source.database_name
    if not path:
        raise ValidationFailure("SQLite sources require a file path.")
    return SQLiteConnector(path, schema=source.schema_name)


_FACTORIES: dict[str, Callable[[DataSource], DataSourceConnector]] = {
    DataSourceType.SUPABASE: _build_supabase,
    DataSourceType.POSTGRESQL: _build_postgres,
    DataSourceType.SQLITE: _build_sqlite,
    DataSourceType.SNOWFLAKE: lambda _source: SnowflakeConnector(),
    DataSourceType.BIGQUERY: lambda _source: BigQueryConnector(),
}


def build_connector(source: DataSource) -> DataSourceConnector:
    factory = _FACTORIES.get(source.source_type)
    if factory is None:
        raise ConnectorError(f"Unsupported data source type: {source.source_type}")
    return factory(source)


def descriptor_for(source_type: str) -> ConnectorDescriptor | None:
    return DESCRIPTORS_BY_TYPE.get(source_type)
