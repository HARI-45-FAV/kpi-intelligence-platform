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
    # Whether one query against this source may read two related tables.
    #
    # False for every source reached over REST or registered as metadata only: a
    # row-per-record table and the row-per-line table beneath it can each be read,
    # but not matched to each other in a single pass. That distinction is what
    # decides whether a KPI measured at the coarser grain may be broken down along
    # the finer one, so it is recorded here -- beside the driver that has the
    # limitation -- rather than inferred from the source type somewhere downstream.
    supports_multi_table_reads: bool = False


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
        supports_multi_table_reads=True,
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
        supports_multi_table_reads=True,
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
    # --- Registry-and-metadata only -----------------------------------------
    # These have no driver and are not meant to get one at this stage. They exist
    # so a landscape can be described honestly: a CSV extract that feeds a KPI is
    # a governed source whether or not the platform can query it live. Registering
    # one records its grain, cadence, coverage and limitations; profiling it is
    # refused rather than faked.
    ConnectorDescriptor(
        source_type=DataSourceType.API,
        label="HTTP API (metadata only)",
        implemented=False,
        supports_profiling=False,
        fields=(
            ConnectorField(
                name="connection_reference",
                label="Endpoint reference",
                placeholder="https://api.example.com/v1/orders",
                help_text=(
                    "A reference, not a credential. Secrets must not be embedded "
                    "in this field; it is stored unencrypted and returned by the API."
                ),
            ),
            ConnectorField(name="description", label="What this feed contains", required=False),
        ),
        notes=(
            "Governed metadata only: no driver, so discovery, profiling and freshness "
            "measurement are unavailable. Grain, refresh cadence, coverage, "
            "completeness and quality must be declared by an administrator."
        ),
    ),
    ConnectorDescriptor(
        source_type=DataSourceType.CSV,
        label="CSV extract (metadata only)",
        implemented=False,
        supports_profiling=False,
        fields=(
            ConnectorField(
                name="connection_reference",
                label="File or location reference",
                placeholder="s3://exports/finance/gl_extract_daily.csv",
                help_text="Where the extract lands. A reference only — no credentials.",
            ),
            ConnectorField(name="description", label="What this extract contains", required=False),
        ),
        notes=(
            "Governed metadata only: no driver. Register it so KPIs built on the "
            "extract carry its declared grain, cadence and known limitations."
        ),
    ),
    ConnectorDescriptor(
        source_type=DataSourceType.FILE,
        label="File drop (metadata only)",
        implemented=False,
        supports_profiling=False,
        fields=(
            ConnectorField(
                name="connection_reference",
                label="Location reference",
                placeholder="//fileshare/finance/monthly/",
                help_text="Where the files arrive. A reference only — no credentials.",
            ),
            ConnectorField(name="description", label="What arrives here", required=False),
        ),
        notes=(
            "Governed metadata only: no driver. Freshness cannot be measured, so "
            "health stays UNKNOWN until an administrator records a refresh."
        ),
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
        descriptor = DESCRIPTORS_BY_TYPE.get(source.source_type)
        if descriptor is not None and not descriptor.implemented:
            # A known, deliberately driverless type. Saying so is more useful than
            # "unsupported", and it stops a caller from reading an empty profile as
            # evidence about the data.
            raise ConnectorError(
                f"{descriptor.label} sources are governed metadata only: there is no "
                "driver to connect with, so discovery, profiling and freshness "
                "measurement are unavailable. Record grain, cadence, coverage and "
                "known limitations on the source instead."
            )
        raise ConnectorError(f"Unsupported data source type: {source.source_type}")
    return factory(source)


def descriptor_for(source_type: str) -> ConnectorDescriptor | None:
    return DESCRIPTORS_BY_TYPE.get(source_type)


def supports_multi_table_reads(source_type: str | None) -> bool:
    """Whether one query against this source type may match two related tables.

    Asked before a breakdown along a finer-grained table is *offered*, not when it
    is run, so that a source which cannot make the match never produces a
    drill-down that refuses on click. An unknown type answers ``False``: declining
    to offer a breakdown is recoverable, and offering one that cannot be built is a
    dead end.
    """

    descriptor = DESCRIPTORS_BY_TYPE.get(source_type or "")
    return bool(descriptor and descriptor.supports_multi_table_reads)
