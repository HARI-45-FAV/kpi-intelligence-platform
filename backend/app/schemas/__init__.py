"""Request and response schemas.

Requests are strictly validated — that is the platform's input boundary. Rich
analysis *responses* (profiles, grain evidence, validation reports, the catalog)
are returned as the service layer's own nested structures rather than being
re-declared as parallel Pydantic trees, which would double the surface area
without adding a guarantee.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.base import (
    Classification,
    DataSourceType,
    DocumentType,
    DriverType,
    MembershipStatus,
    RefreshFrequency,
    TimeGrain,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="forbid")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class RegisterRequest(ApiModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str) -> str:
        # Deliberately modest: a length floor plus a character-class floor. Long
        # passphrases should not be rejected for lacking a symbol.
        if value.strip() != value:
            raise ValueError("Password may not start or end with whitespace.")
        classes = sum(
            [
                any(c.islower() for c in value),
                any(c.isupper() for c in value),
                any(c.isdigit() for c in value),
                any(not c.isalnum() for c in value),
            ]
        )
        if classes < 2:
            raise ValueError("Use at least two of: lowercase, uppercase, digits, symbols.")
        return value


class LoginRequest(ApiModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    company_id: str | None = None


class UserOut(ApiModel):
    id: str
    email: str
    full_name: str
    is_active: bool
    is_platform_admin: bool
    last_login_at: datetime | None = None
    created_at: datetime


class MembershipSummary(ApiModel):
    company_id: str
    company_name: str
    company_slug: str
    role_key: str
    role_name: str
    status: str
    is_admin_role: bool


class TokenResponse(ApiModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user: UserOut
    memberships: list[MembershipSummary]


class SessionResponse(ApiModel):
    user: UserOut
    memberships: list[MembershipSummary]


class AdminUnlockRequest(ApiModel):
    """Re-authentication for the protected KPI Setup workspace.

    Holding a session token is not enough to enter governance: the password is
    re-entered and the admin permission re-checked, so an unattended browser tab
    cannot be used to change what a KPI means.
    """

    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    company_id: str


class AdminUnlockResponse(ApiModel):
    access_token: str
    expires_at: datetime
    company_id: str
    company_name: str
    role_key: str
    permissions: list[str]
    # Identity travels with the elevated token. Without it a client that adopts
    # this response as its session has to blank the signed-in user for a tick,
    # which tears down and remounts the whole authenticated tree -- losing
    # whatever the administrator was in the middle of configuring.
    user: UserOut
    memberships: list[MembershipSummary]


# ---------------------------------------------------------------------------
# Company / tenant
# ---------------------------------------------------------------------------
class CompanyCreate(ApiModel):
    company_name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=80)
    industry: str | None = Field(default=None, max_length=120)
    description: str | None = None
    country: str | None = Field(default=None, max_length=80)
    timezone: str = Field(default="UTC", max_length=64)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    week_start_day: int = Field(default=1, ge=1, le=7)


class CompanyUpdate(ApiModel):
    company_name: str | None = Field(default=None, min_length=1, max_length=200)
    industry: str | None = Field(default=None, max_length=120)
    description: str | None = None
    country: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, max_length=64)
    currency: str | None = Field(default=None, min_length=3, max_length=8)
    fiscal_year_start_month: int | None = Field(default=None, ge=1, le=12)
    week_start_day: int | None = Field(default=None, ge=1, le=7)
    status: Literal["DRAFT", "ACTIVE", "SUSPENDED"] | None = None


class CompanyOut(ApiModel):
    id: str
    company_name: str
    slug: str
    industry: str | None
    description: str | None
    country: str | None
    timezone: str
    currency: str
    fiscal_year_start_month: int
    week_start_day: int
    status: str
    created_at: datetime
    updated_at: datetime


class CalendarUpsert(ApiModel):
    calendar_key: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=160)
    timezone: str = Field(default="UTC", max_length=64)
    week_start_day: int = Field(default=1, ge=1, le=7)
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    is_default: bool = False
    notes: str | None = None


class CalendarOut(ApiModel):
    id: str
    calendar_key: str
    name: str
    timezone: str
    week_start_day: int
    fiscal_year_start_month: int
    is_default: bool
    notes: str | None


class MemberInvite(ApiModel):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, min_length=10, max_length=200)
    role_key: str = Field(min_length=1, max_length=40)
    row_scope: dict[str, Any] = Field(default_factory=dict)
    denied_columns: list[str] = Field(default_factory=list)


class MemberUpdate(ApiModel):
    role_key: str | None = Field(default=None, max_length=40)
    status: MembershipStatus | None = None
    row_scope: dict[str, Any] | None = None
    denied_columns: list[str] | None = None


class MemberOut(ApiModel):
    membership_id: str
    user_id: str
    email: str
    full_name: str
    role_key: str
    role_name: str
    is_admin_role: bool
    status: str
    row_scope: dict[str, Any]
    denied_columns: list[str]
    created_at: datetime


class RoleOut(ApiModel):
    role_key: str
    name: str
    description: str | None
    is_admin_role: bool
    rank: int
    permissions: list[str]
    # Presentation hints. Authorisation is decided by `permissions`; these only
    # let the security screen lead with a concise business view.
    is_core: bool = False
    access_summary: str | None = None
    access_areas: dict[str, bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
class DataSourceCreate(ApiModel):
    """Registration accepts either a pasted connection string or explicit fields.

    Supabase users normally have a project URL and a database password, so those
    are first-class inputs rather than something to translate by hand.
    """

    name: str = Field(min_length=1, max_length=160)
    source_type: DataSourceType
    description: str | None = None
    refresh_frequency: RefreshFrequency = RefreshFrequency.UNKNOWN
    timezone: str = Field(default="UTC", max_length=64)
    known_limitations: str | None = None

    # Supabase: exactly two inputs — the project URL and the secret key. The
    # secret key is a REST credential, not the database password, so this source
    # is reached over the project API rather than a Postgres session.
    supabase_url: str | None = Field(default=None, max_length=300)
    secret_key: str | None = Field(default=None, max_length=2000)
    # Option A: paste the connection URI (PostgreSQL → Connection string → URI)
    connection_uri: str | None = Field(default=None, max_length=1000)
    # Accepted as an alias for supabase_url so existing callers keep working.
    project_url: str | None = Field(default=None, max_length=300)
    # Option C: explicit Postgres fields
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, max_length=160)
    schema_name: str | None = Field(default=None, max_length=160)
    username: str | None = Field(default=None, max_length=160)
    password: str | None = Field(default=None, max_length=500)
    sslmode: str | None = Field(default=None, max_length=20)
    # Supabase service-role key, stored encrypted for later non-SQL use.
    service_role_key: str | None = Field(default=None, max_length=2000)
    # SQLite file path (local / test sources)
    path: str | None = Field(default=None, max_length=600)


class DataSourceUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    refresh_frequency: RefreshFrequency | None = None
    timezone: str | None = Field(default=None, max_length=64)
    known_limitations: str | None = None
    schema_name: str | None = Field(default=None, max_length=160)
    password: str | None = Field(default=None, max_length=500)
    service_role_key: str | None = Field(default=None, max_length=2000)


class DataSourceOut(ApiModel):
    """Never carries a credential. ``has_credentials`` is the only signal."""

    id: str
    name: str
    source_type: str
    description: str | None
    host: str | None
    port: int | None
    database_name: str | None
    schema_name: str | None
    username: str | None
    has_credentials: bool
    connection_status: str
    last_tested_at: datetime | None
    last_test_error: str | None
    refresh_frequency: str
    timezone: str
    known_limitations: str | None
    last_discovered_at: datetime | None
    discovered_table_count: int = 0
    selected_table_count: int = 0
    created_at: datetime
    updated_at: datetime


class SourceColumnOut(ApiModel):
    id: str
    column_name: str
    ordinal_position: int
    data_type: str
    is_nullable: bool
    is_primary_key: bool
    is_foreign_key: bool
    references_table: str | None
    references_column: str | None
    semantic_type: str
    classification: str
    is_pii: bool
    is_sensitive: bool
    is_restricted: bool
    readable: bool = True
    withheld_reason: str | None = None


class SourceTableOut(ApiModel):
    id: str
    data_source_id: str
    schema_name: str
    table_name: str
    qualified_name: str
    table_type: str
    approx_row_count: int | None
    column_count: int | None
    discovered_at: datetime | None
    selected: bool = False
    business_alias: str | None = None
    declared_grain: str | None = None
    primary_time_column: str | None = None
    inferred_grain: str | None = None
    quality_status: str | None = None
    freshness_status: str | None = None
    profiled_at: datetime | None = None


class ColumnClassificationUpdate(ApiModel):
    classification: Classification | None = None
    is_pii: bool | None = None
    is_sensitive: bool | None = None
    is_restricted: bool | None = None


class SelectedTableItem(ApiModel):
    source_table_id: str
    enabled: bool = True
    business_alias: str | None = Field(default=None, max_length=200)
    declared_grain: str | None = Field(default=None, max_length=300)
    primary_time_column: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class DataScopeUpdate(ApiModel):
    tables: list[SelectedTableItem] = Field(min_length=0)
    # When true, tables absent from ``tables`` are disabled rather than ignored.
    replace: bool = True


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
class DocumentCreate(ApiModel):
    title: str = Field(min_length=1, max_length=300)
    document_type: DocumentType = DocumentType.OTHER
    document_key: str | None = Field(default=None, max_length=120)
    description: str | None = None
    access_scope: list[str] = Field(default_factory=list)
    tags: dict[str, Any] = Field(default_factory=dict)
    effective_from: date | None = None
    effective_to: date | None = None
    change_note: str | None = None
    inline_content: str | None = Field(default=None, max_length=200_000)


class DocumentVersionOut(ApiModel):
    id: str
    version: int
    original_filename: str | None
    content_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    effective_from: date | None
    effective_to: date | None
    is_current: bool
    change_note: str | None
    uploaded_by: str | None
    uploaded_at: datetime | None
    has_inline_content: bool = False


class DocumentOut(ApiModel):
    id: str
    document_key: str
    title: str
    description: str | None
    document_type: str
    document_class: str
    status: str
    current_version: int
    access_scope: list[str]
    tags: dict[str, Any]
    owner_user_id: str | None
    created_at: datetime
    updated_at: datetime
    versions: list[DocumentVersionOut] = Field(default_factory=list)
    # Sprint 1 stores and versions documents; embeddings are a later sprint.
    retrieval_ready: bool = False


# ---------------------------------------------------------------------------
# KPI governance
# ---------------------------------------------------------------------------
class KpiDimensionInput(ApiModel):
    dimension_name: str = Field(min_length=1, max_length=120)
    source_column: str = Field(min_length=1, max_length=200)
    source_table_id: str | None = None
    hierarchy: list[str] = Field(default_factory=list)
    allowed: bool = True
    is_default_breakdown: bool = False
    approx_cardinality: int | None = Field(default=None, ge=0)
    notes: str | None = None


class KpiDriverInput(ApiModel):
    driver_name: str = Field(min_length=1, max_length=120)
    driver_type: DriverType = DriverType.OTHER
    source_table_id: str | None = None
    source_table: str | None = Field(default=None, max_length=200)
    source_column: str | None = Field(default=None, max_length=200)
    controllable: bool = False
    measurement_method: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class KpiMaterialityInput(ApiModel):
    relative_threshold_pct: float | None = Field(default=None, gt=0, le=1000)
    absolute_threshold: float | None = Field(default=None, ge=0)
    statistical_rule: str | None = Field(default=None, max_length=120)
    business_criticality: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    priority_policy: str | None = Field(default=None, max_length=120)
    persistence_periods: int = Field(default=1, ge=1, le=30)
    notes: str | None = None


class KpiAccessPolicyInput(ApiModel):
    role_key: str = Field(min_length=1, max_length=40)
    allowed: bool = True
    row_scope: dict[str, Any] = Field(default_factory=dict)
    column_scope: list[str] = Field(default_factory=list)
    domain_scope: list[str] = Field(default_factory=list)
    aggregate_only: bool = False
    notes: str | None = None


class KpiFilterInput(ApiModel):
    column: str = Field(min_length=1, max_length=200)
    operator: str = Field(min_length=1, max_length=12)
    value: Any = None
    table: str | None = Field(default=None, max_length=200)


class KpiVersionInput(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    business_definition: str = Field(min_length=1, max_length=4000)
    formula_expression: str = Field(min_length=1, max_length=400)
    source_table_id: str
    time_field: str | None = Field(default=None, max_length=200)
    time_grain: TimeGrain = TimeGrain.DAY
    kpi_key: str | None = Field(default=None, max_length=80)
    purpose: str | None = None
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=8)
    direction: Literal["HIGHER_IS_BETTER", "LOWER_IS_BETTER", "TARGET_BAND"] = "HIGHER_IS_BETTER"
    null_handling: Literal["TREAT_AS_ZERO", "TREAT_AS_NULL", "EXCLUDE"] = "TREAT_AS_ZERO"
    filters: list[KpiFilterInput] = Field(default_factory=list)
    calendar_id: str | None = None
    timezone: str | None = Field(default=None, max_length=64)
    dimensions: list[KpiDimensionInput] = Field(default_factory=list)
    drivers: list[KpiDriverInput] = Field(default_factory=list)
    materiality: KpiMaterialityInput | None = None
    access_policies: list[KpiAccessPolicyInput] = Field(default_factory=list)
    expected_baseline_method: str = Field(default="NOT_CONFIGURED", max_length=40)
    seasonality_expectation: str | None = Field(default=None, max_length=60)
    sparse_history_strategy: Literal[
        "PEER_BASELINE", "CATEGORY_BASELINE", "NONE", "LOW_CONFIDENCE_ONLY"
    ] = "PEER_BASELINE"
    min_history_days: int | None = Field(default=None, ge=0, le=3650)
    definition_document_id: str | None = None
    definition_document_version: int | None = Field(default=None, ge=1)
    definition_source: str | None = Field(default=None, max_length=200)
    owner_user_id: str | None = None


class KpiProposalAccept(ApiModel):
    kpi_key: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class CompanyDefinitionImport(ApiModel):
    """Import company-authored KPI definitions as governed contracts.

    An empty ``kpi_keys`` means every importable definition, which is the normal
    case: the company's registry is the configuration of record.
    """

    kpi_keys: list[str] = Field(default_factory=list)
    # Per-KPI field overrides, keyed by kpi_key, for the rare case where an
    # administrator corrects a registry row while importing it.
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class KpiTransitionRequest(ApiModel):
    reason: str | None = Field(default=None, max_length=2000)


class KpiRejectRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class KpiVersionSummary(ApiModel):
    id: str
    version: int
    status: str
    formula_expression: str
    time_grain: str
    last_validation_status: str | None
    last_validated_at: datetime | None
    approved_by: str | None
    approved_at: datetime | None
    activated_at: datetime | None
    deprecated_at: datetime | None
    created_by: str | None
    created_at: datetime
    proposal_origin: str


class KpiDefinitionOut(ApiModel):
    id: str
    kpi_key: str
    name: str
    short_description: str | None
    status: str
    current_version: int
    current_version_id: str | None
    owner_user_id: str | None
    created_at: datetime
    updated_at: datetime
    versions: list[KpiVersionSummary] = Field(default_factory=list)


class KpiPreviewRequest(ApiModel):
    """Compute a KPI over an explicit window, to sanity-check a definition.

    Bounded on purpose: this is a governance aid, not the analytics engine.
    """

    start: date | None = None
    end: date | None = None
    group_by: list[str] = Field(default_factory=list, max_length=3)
    limit: int = Field(default=20, ge=1, le=200)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class CatalogPublishRequest(ApiModel):
    note: str | None = Field(default=None, max_length=2000)


class CatalogVersionOut(ApiModel):
    id: str
    version: int
    published_at: datetime
    published_by: str | None
    note: str | None
    source_count: int
    selected_table_count: int
    profiled_table_count: int
    relationship_count: int
    document_count: int
    active_kpi_count: int
    checksum_sha256: str | None


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
class AuditLogOut(ApiModel):
    id: str
    user_id: str | None
    actor_email: str | None
    action: str
    resource_type: str
    resource_id: str | None
    resource_label: str | None
    old_version: str | None
    new_version: str | None
    outcome: str
    summary: str | None
    details: dict[str, Any]
    request_id: str | None
    occurred_at: datetime


class ExecutionLogOut(ApiModel):
    id: str
    request_id: str
    service: str
    operation: str
    http_method: str | None
    http_status: int | None
    started_at: datetime
    duration_ms: int | None
    status: str
    error: str | None
    connector: str | None
    query_count: int | None
    query_duration_ms: int | None
    rows_returned: int | None
    llm_model: str | None
    llm_calls: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_cost_usd: float | None


class SystemEventOut(ApiModel):
    id: str
    category: str
    severity: str
    title: str
    message: str | None
    occurred_at: datetime
    details: dict[str, Any]
