"""Application settings.

Everything environment-specific lives here. Note the deliberate separation of
two database concepts:

* ``database_url``      -- the *platform* metadata database (companies, users,
                           KPI contracts, catalog, audit). Owned by us.
* data source DSNs      -- the *tenant's* business databases. Never configured
                           here; they are registered at runtime through the
                           Data Source Registry and reached only via connectors.

Confusing the two is the single most common way a multi-tenant BI platform
leaks data across companies, so the codebase keeps them physically apart.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = BACKEND_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Identity -----------------------------------------------------
    app_name: str = "BusinessIntelligence.ai"
    api_v1_prefix: str = "/api/v1"
    environment: str = "development"
    debug: bool = True

    # ---- Platform metadata database -----------------------------------
    # SQLite by default so the project runs with zero external setup.
    # Point at Supabase/Postgres with:
    #   DATABASE_URL=postgresql+psycopg://user:pass@host:5432/postgres
    database_url: str = Field(default=f"sqlite:///{(DATA_DIR / 'platform.db').as_posix()}")

    # ---- Security ------------------------------------------------------
    secret_key: str = "dev-only-secret-change-me-in-production-0123456789abcdef"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 12 * 60

    # ---- Document storage ---------------------------------------------
    document_storage_dir: str = Field(default=str(DATA_DIR / "documents"))
    max_document_bytes: int = 20 * 1024 * 1024

    # ---- Guard rails on connector work -------------------------------
    # Profiling pushes aggregates into the source database, but a runaway
    # query still has to be bounded.
    connector_query_timeout_seconds: int = 30
    connector_max_rows_returned: int = 5_000
    profiling_sample_value_limit: int = 5
    grain_max_candidate_columns: int = 4

    # ---- CORS ----------------------------------------------------------
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(settings.document_storage_dir).mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
