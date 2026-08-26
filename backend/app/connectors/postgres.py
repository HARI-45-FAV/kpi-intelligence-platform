"""PostgreSQL and Supabase connectors.

Supabase *is* PostgreSQL, so the analytical path is identical. What differs is
onboarding: an administrator has a project URL and a database password, not a
host/port/database triple. ``SupabaseConnector`` derives the connection details
from what the Supabase dashboard actually shows, so the UI can ask for the two
things the user has rather than making them translate.

Aggregate pushdown (profiling, grain scans, orphan counts) needs SQL, so the
connection is a real Postgres session — not the Supabase REST endpoint, which
cannot express ``COUNT(DISTINCT a, b)`` or an anti-join. A service-role API key
can still be stored alongside for later non-SQL use (storage, auth admin).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

from sqlalchemy.exc import SQLAlchemyError

from app.connectors.sql import SqlConnector
from app.core.errors import ValidationFailure
from app.models.base import DataSourceType

# Supabase project refs are 20 lowercase letters, e.g. abcdefghijklmnopqrst
_PROJECT_REF_RE = re.compile(r"^[a-z0-9]{16,32}$")

# Pooled connection ports Supabase exposes. 5432 is the direct connection,
# 6543 the transaction pooler, 5432-on-pooler-host the session pooler.
SUPABASE_DIRECT_PORT = 5432
SUPABASE_POOLER_PORT = 6543


class PostgreSQLConnector(SqlConnector):
    source_type = DataSourceType.POSTGRESQL
    default_schema = "public"

    def __init__(
        self,
        *,
        host: str,
        port: int | None,
        database: str,
        username: str | None,
        password: str | None,
        schema: str | None = "public",
        sslmode: str | None = None,
        source_type: str = DataSourceType.POSTGRESQL,
    ) -> None:
        user_part = ""
        if username:
            user_part = quote_plus(username)
            if password:
                user_part += f":{quote_plus(password)}"
            user_part += "@"
        query = f"?sslmode={sslmode}" if sslmode else ""
        url = f"postgresql+psycopg://{user_part}{host}:{port or 5432}/{database}{query}"
        super().__init__(url, source_type=source_type, schema=schema)

    def estimate_row_count(self, schema: str, table: str) -> int | None:
        """Prefer the planner's estimate.

        ``COUNT(*)`` on a large Postgres table is a full sequential scan, and
        discovery lists every table in a schema. ``reltuples`` is free; fall
        back to an exact count only when statistics are missing (a table that
        has never been analysed reports -1).
        """
        target = self.resolve_schema(schema)
        try:
            rows = self._run(
                "SELECT c.reltuples::bigint AS estimate "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table",
                {"schema": target, "table": table},
            )
        except SQLAlchemyError:  # pragma: no cover
            return super().estimate_row_count(target, table)
        if rows and rows[0]["estimate"] is not None and int(rows[0]["estimate"]) >= 0:
            return int(rows[0]["estimate"])
        return super().estimate_row_count(target, table)


class SupabaseConnector(PostgreSQLConnector):
    """Postgres connection derived from Supabase project coordinates."""

    source_type = DataSourceType.SUPABASE

    def __init__(
        self,
        *,
        host: str,
        port: int | None,
        database: str,
        username: str | None,
        password: str | None,
        schema: str | None = "public",
        sslmode: str | None = "require",
    ) -> None:
        super().__init__(
            host=host,
            port=port or SUPABASE_DIRECT_PORT,
            database=database or "postgres",
            username=username or "postgres",
            password=password,
            schema=schema or "public",
            # Supabase terminates TLS at the database; require it by default so
            # credentials never cross the network in the clear.
            sslmode=sslmode or "require",
            source_type=DataSourceType.SUPABASE,
        )


# ---------------------------------------------------------------------------
# Onboarding helpers: turn what the user pastes into connection coordinates
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ParsedConnection:
    host: str
    port: int
    database: str
    username: str
    password: str | None
    schema: str
    sslmode: str | None = None
    project_ref: str | None = None


def parse_connection_uri(uri: str, *, default_schema: str = "public") -> ParsedConnection:
    """Parse a ``postgres(ql)://`` URI, e.g. Supabase's "Connection string → URI".

    Accepting the string the dashboard hands out removes the most common
    onboarding mistake: transcribing five fields by hand and mistyping one.
    """
    candidate = (uri or "").strip()
    if not candidate:
        raise ValidationFailure("Connection string is empty.")
    if candidate.startswith("postgres://"):
        candidate = "postgresql://" + candidate[len("postgres://") :]
    if not candidate.startswith("postgresql"):
        raise ValidationFailure(
            "Connection string must start with postgresql:// or postgres://."
        )

    parsed = urlparse(candidate)
    if not parsed.hostname:
        raise ValidationFailure("Connection string is missing a host.")

    database = (parsed.path or "/postgres").lstrip("/") or "postgres"
    query_params = dict(
        pair.split("=", 1) for pair in (parsed.query or "").split("&") if "=" in pair
    )
    host = parsed.hostname
    project_ref = None
    # Direct connections look like db.<ref>.supabase.co; pooler usernames look
    # like postgres.<ref>.
    if host.endswith(".supabase.co") or host.endswith(".supabase.com"):
        parts = host.split(".")
        if len(parts) >= 3 and _PROJECT_REF_RE.match(parts[-3]):
            project_ref = parts[-3]
    username = parsed.username or "postgres"
    if project_ref is None and "." in username:
        tail = username.split(".", 1)[1]
        if _PROJECT_REF_RE.match(tail):
            project_ref = tail

    return ParsedConnection(
        host=host,
        port=parsed.port or SUPABASE_DIRECT_PORT,
        database=database,
        username=username,
        password=parsed.password,
        schema=query_params.get("search_path") or default_schema,
        sslmode=query_params.get("sslmode"),
        project_ref=project_ref,
    )


def supabase_coordinates(
    project_url: str,
    *,
    database: str = "postgres",
    username: str = "postgres",
    schema: str = "public",
) -> ParsedConnection:
    """Derive Postgres coordinates from a Supabase project URL or bare ref.

    Accepts ``https://abcdefghijklmnopqrst.supabase.co``, the bare project ref,
    or an already-qualified ``db.<ref>.supabase.co`` host.
    """
    value = (project_url or "").strip()
    if not value:
        raise ValidationFailure("Supabase project URL or project reference is required.")

    host = value
    if "://" in value:
        host = urlparse(value).hostname or ""
    host = host.strip("/")

    if _PROJECT_REF_RE.match(host):
        project_ref = host
    elif host.startswith("db.") and host.endswith((".supabase.co", ".supabase.com")):
        project_ref = host.split(".")[1]
    elif host.endswith((".supabase.co", ".supabase.com")):
        project_ref = host.split(".")[0]
    else:
        raise ValidationFailure(
            "Could not read a Supabase project reference from "
            f"{project_url!r}. Expected https://<ref>.supabase.co or the project ref itself."
        )

    if not _PROJECT_REF_RE.match(project_ref):
        raise ValidationFailure(f"{project_ref!r} is not a valid Supabase project reference.")

    return ParsedConnection(
        host=f"db.{project_ref}.supabase.co",
        port=SUPABASE_DIRECT_PORT,
        database=database or "postgres",
        username=username or "postgres",
        password=None,
        schema=schema or "public",
        sslmode="require",
        project_ref=project_ref,
    )
