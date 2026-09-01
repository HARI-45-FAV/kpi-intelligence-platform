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
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = BACKEND_ROOT / "data"

# The documented development secret. Published in .env.example and in this file,
# so it is public by construction: anything it protects is unprotected. Named
# here rather than repeated as a literal because two places need to recognise it
# -- the boot guard below, which refuses it outside development, and
# ``security.migrate_legacy_secret``, which re-encrypts credentials that were
# sealed with it before a real key was configured.
DEV_DEFAULT_SECRET_KEY = "dev-only-secret-change-me-in-production-0123456789abcdef"

# Environments where the convenience defaults above are acceptable. Anything
# else -- production, staging, whatever a deployment calls itself -- has to
# supply real keys.
RELAXED_ENVIRONMENTS = frozenset({"development", "test", "testing", "local"})

# Below this, an HS256 signing key is guessable in a way that defeats the point.
MIN_SECRET_LENGTH = 32


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
    # ``secret_key`` signs access tokens and nothing else.
    #
    # ``credential_encryption_key`` seals tenant data-source credentials. It is a
    # separate setting because the two keys have opposite lifecycles: a signing
    # key should be rotated freely -- after a leak, on a schedule, whenever a
    # deployment wants every session invalidated -- and the cost of rotating it
    # is that everyone signs in again. A credential key cannot be rotated
    # casually, because every stored DSN in every company is only readable with
    # the key that sealed it. While one value did both jobs, rotating the signing
    # key silently bricked every data source in the platform, and it surfaced at
    # connector time rather than at boot.
    #
    # Left unset it falls back to ``secret_key``, so an existing install keeps
    # decrypting exactly what it could decrypt before. Set it and the boot-time
    # walker in ``services.credential_migration`` re-seals stored credentials
    # under the dedicated key, once, and logs how many it moved.
    secret_key: str = DEV_DEFAULT_SECRET_KEY
    credential_encryption_key: str | None = None
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
    # ``NoDecode`` hands the raw string to ``_split_origins`` below. Without it
    # pydantic-settings tries to JSON-decode any complex-typed field first, so
    # the comma-separated form documented in .env.example
    # (CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173) would fail to
    # parse before the validator ever ran.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # ---- Optional language model layer --------------------------------
    # Off by default, and the platform is fully functional with it off: no
    # AI package is imported, no model is contacted, and every deterministic
    # endpoint behaves identically. Turning it on adds an explanation and
    # retrieval layer over data the platform already governs -- it never
    # becomes a calculation path.
    #
    # ``llm_provider`` selects a transport implemented in ``app.llm``. Any
    # provider speaking the OpenAI chat-completions shape (vLLM, llama.cpp,
    # Ollama, TGI, OpenAI itself) works through ``openai_compatible``, so the
    # rest of the codebase never learns which model is behind it.
    llm_enabled: bool = False
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "EMPTY"
    llm_model: str = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    # Read only when ``llm_provider`` selects the hosted Google transport, which
    # takes its endpoint, key and model from these three rather than from the
    # ``llm_*`` values above -- so switching provider is one line in the
    # environment and the local-endpoint settings stay intact for switching
    # back. The key is server-side configuration: it is never returned by an
    # endpoint, logged, audited or sent to the browser, and the model layer's
    # ``describe()`` deliberately has no field for it.
    gemini_api_key: str = ""
    gemini_model: str = ""
    # Overridable so a deployment can pin an API version, but the default in
    # ``app.llm.config`` is the one Google documents.
    gemini_base_url: str = ""
    llm_temperature: float = 0.1
    llm_max_output_tokens: int = 1_200
    llm_request_timeout_seconds: int = 90
    # ``none`` prevents reasoning-capable local models such as Qwen3 from
    # spending the entire response budget on hidden deliberation.  Keep this
    # optional because generic OpenAI-compatible servers need not implement it.
    llm_reasoning_effort: str | None = None
    # Some small local models stall while choosing OpenAI-style function calls.
    # Retrieval remains governed and fully available when this is disabled.
    llm_tool_calling_enabled: bool = True
    # How many tool-calling rounds one Copilot turn may take before the
    # orchestrator stops and answers with the evidence it has.
    llm_max_tool_iterations: int = 4
    # Cost accounting for ``execution_logs.estimated_cost_usd``. Zero for a
    # locally hosted model, which is the default deployment.
    llm_input_cost_per_1k_usd: float = 0.0
    llm_output_cost_per_1k_usd: float = 0.0

    # ---- Post-run email --------------------------------------------------
    # A completed Agent Run can be sent out as a summary of what was already
    # stored. Off unless a host is configured, and the transport is selected in
    # ``app.notifications`` -- nothing outside that package speaks SMTP.
    #
    # ``email_recipients`` is a comma-separated list. It is deliberately a
    # deployment setting rather than a per-user preference: this build has no
    # subscription model, and inventing one silently would mean deciding on a
    # company's behalf who receives its KPI results.
    email_enabled: bool = False
    email_provider: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_timeout_seconds: int = 20
    email_from: str = ""
    email_recipients: str = ""
    email_subject_prefix: str = "[KPI Intelligence]"

    # ---- Copilot retrieval ---------------------------------------------
    # Retrieval reads governed platform metadata and authorised documents.
    # Tenant business rows are never indexed, so these bound text volume,
    # not data access.
    copilot_retrieval_top_k: int = 8
    copilot_chunk_chars: int = 1_200
    copilot_max_document_bytes_scanned: int = 2 * 1024 * 1024
    # How far from the date under discussion a business event document stays
    # relevant. An incident logged three weeks before a movement can still
    # explain it; one logged a year earlier is not context, it is noise -- and
    # retrieving it invites the model to associate the two. Only applied when a
    # date is actually in context and the document states its own window.
    copilot_event_relevance_days: int = 45

    # ---- Contribution analysis -----------------------------------------
    # How a movement is broken down when someone investigates. Detection stays
    # at the KPI level and runs continuously; nothing here runs on a schedule.
    #
    # Top-K bounds how many contributors are ranked and returned. It is a display
    # and cost bound, not an analytical one: the shares are always computed
    # against the whole movement, so a truncated list still reports honestly how
    # much of the movement it accounts for.
    contribution_top_k: int = 10
    contribution_max_top_k: int = 50
    # When the leading contributor accounts for at least this much of the
    # movement, the platform says the explanation is sufficient and stops
    # suggesting the rest be drilled through. It is a stopping hint for a person,
    # never a verdict about the contributor.
    contribution_sufficiency_pct: float = 60.0
    # A breakdown re-reads the KPI once per comparable date, so the reference
    # window is capped. The most recent comparable dates are kept, and dropping
    # any is reported rather than hidden.
    contribution_max_reference_dates: int = 12

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _refuse_development_defaults_outside_development(self) -> "Settings":
        """Fail at boot rather than serve a deployment with a published key.

        The development secret is in this repository and in .env.example, so a
        deployment running on it is one ``git clone`` away from forged
        administrator tokens for any company and readable data-source
        credentials for every tenant. There is no partial version of that
        failure worth degrading into, so this raises instead of warning, and it
        raises at import -- before a port is bound, and equally for the API,
        Alembic and any management script.

        Development is untouched: with ``ENVIRONMENT`` at its default, every
        check below is skipped.
        """
        if self.environment.strip().lower() in RELAXED_ENVIRONMENTS:
            return self

        problems: list[str] = []
        if self.secret_key == DEV_DEFAULT_SECRET_KEY:
            problems.append(
                "SECRET_KEY is still the published development default. Generate one "
                "with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        elif len(self.secret_key) < MIN_SECRET_LENGTH:
            problems.append(
                f"SECRET_KEY is {len(self.secret_key)} characters; "
                f"at least {MIN_SECRET_LENGTH} are required."
            )

        key = self.credential_encryption_key
        if key is not None:
            if key == DEV_DEFAULT_SECRET_KEY:
                problems.append(
                    "CREDENTIAL_ENCRYPTION_KEY is the published development default."
                )
            elif len(key) < MIN_SECRET_LENGTH:
                problems.append(
                    f"CREDENTIAL_ENCRYPTION_KEY is {len(key)} characters; "
                    f"at least {MIN_SECRET_LENGTH} are required."
                )

        if problems:
            detail = "\n  - ".join(problems)
            raise ValueError(
                f"Refusing to start with ENVIRONMENT={self.environment!r}:\n  - {detail}"
            )

        # Nothing reads ``debug`` today; pinning it here means nothing ever
        # inherits a debug-on production by adding the first reader.
        if self.debug:
            self.debug = False
        return self

    @property
    def is_development(self) -> bool:
        """Whether convenience surfaces -- interactive API docs -- may be served."""
        return self.environment.strip().lower() in RELAXED_ENVIRONMENTS

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
