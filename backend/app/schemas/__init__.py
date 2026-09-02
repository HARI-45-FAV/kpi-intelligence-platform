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
    ColumnRole,
    DataSourceType,
    DocumentType,
    DriverType,
    GrainStatus,
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
    password: str = Field(min_length=6, max_length=200)
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
    permissions: list[str] = Field(default_factory=list)
    row_scope: dict[str, Any] = Field(default_factory=dict)


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
    password: str | None = Field(default=None, min_length=6, max_length=200)
    role_key: str = Field(min_length=1, max_length=40)
    row_scope: dict[str, Any] = Field(default_factory=dict)
    denied_columns: list[str] = Field(default_factory=list)
    # Empty means unrestricted in both cases — the administrator's own scope.
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_document_scopes: list[str] = Field(default_factory=list)


class MemberUpdate(ApiModel):
    role_key: str | None = Field(default=None, max_length=40)
    status: MembershipStatus | None = None
    row_scope: dict[str, Any] | None = None
    denied_columns: list[str] | None = None
    allowed_domains: list[str] | None = None
    allowed_document_scopes: list[str] | None = None


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
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_document_scopes: list[str] = Field(default_factory=list)
    created_at: datetime


class AccessScopeOut(ApiModel):
    """The resolved access scope for the calling user inside one company.

    One place the frontend — and any later retrieval layer — can read
    "who am I here, and what may I reach", instead of inferring it from a role
    label. Produced by ``AccessContext.as_scope``, so it can never disagree with
    what the backend actually enforces.
    """

    company_id: str
    user_id: str
    roles: list[str]
    permissions: list[str]
    allowed_domains: list[str]
    allowed_data_scopes: dict[str, Any]
    allowed_document_scopes: list[str]
    denied_columns: list[str]
    is_admin: bool


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
# Substrings that mean a supposedly non-secret reference is carrying a credential.
_EMBEDDED_SECRET_HINTS = ("password=", "secret=", "apikey=", "api_key=", "token=", "pwd=")


def _reject_embedded_secret(value: str | None) -> str | None:
    """A connection reference is metadata, so it must not hold a credential.

    Refused rather than scrubbed: quietly stripping the secret would leave the
    caller believing a working reference was saved, and storing it would put a
    credential in a column that is never encrypted and is returned by the API.
    """
    if value is None:
        return value
    lowered = value.lower()
    if any(hint in lowered for hint in _EMBEDDED_SECRET_HINTS):
        raise ValueError(
            "Connection reference must not contain credentials. Supply secrets "
            "through the password or secret key fields, which are stored encrypted."
        )
    return value


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
    # Where a source the platform cannot query live actually lives: an export
    # path, a bucket key, an endpoint. Never a credential — those go through
    # encryption, and this field is rejected if it looks like one.
    connection_reference: str | None = Field(default=None, max_length=500)
    # Which governed calendar this source's periods are read against.
    business_calendar_id: str | None = Field(default=None, max_length=36)

    @field_validator("connection_reference")
    @classmethod
    def _no_secret_in_reference(cls, value: str | None) -> str | None:
        return _reject_embedded_secret(value)


class DataSourceUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    refresh_frequency: RefreshFrequency | None = None
    timezone: str | None = Field(default=None, max_length=64)
    known_limitations: str | None = None
    schema_name: str | None = Field(default=None, max_length=160)
    password: str | None = Field(default=None, max_length=500)
    service_role_key: str | None = Field(default=None, max_length=2000)
    connection_reference: str | None = Field(default=None, max_length=500)
    business_calendar_id: str | None = Field(default=None, max_length=36)

    @field_validator("connection_reference")
    @classmethod
    def _no_secret_in_reference(cls, value: str | None) -> str | None:
        return _reject_embedded_secret(value)


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
    connection_reference: str | None = None
    connection_status: str
    last_tested_at: datetime | None
    last_test_error: str | None
    refresh_frequency: str
    timezone: str
    known_limitations: str | None
    business_calendar_id: str | None = None
    last_discovered_at: datetime | None
    discovered_table_count: int = 0
    selected_table_count: int = 0

    # Governance rollup. Derived, written only by an explicit profile or health
    # check — a read of this record never recomputes it, so ``health_checked_at``
    # is what tells the reader how much the verdict is still worth.
    grain: str | None = None
    last_refresh_at: datetime | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    completeness_pct: float | None = None
    quality_score: float | None = None
    health_status: str = "UNKNOWN"
    health_checked_at: datetime | None = None
    health_reason: str | None = None

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
    # The business reading of the column. ``candidate_role`` is the profiler's
    # proposal and ``confirmed_role`` a reviewer's decision; the UI must show
    # which it is looking at rather than presenting a guess as governed truth.
    candidate_role: str = ColumnRole.UNKNOWN
    confirmed_role: str | None = None
    effective_role: str = ColumnRole.UNKNOWN
    role_status: str = "PROPOSED"
    description: str | None = None
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

    # -- governed metadata, proposed vs confirmed -------------------------
    display_name: str | None = None
    description: str | None = None
    primary_identifier_candidates: list[str] = Field(default_factory=list)
    time_field_candidates: list[str] = Field(default_factory=list)
    company_field_candidates: list[str] = Field(default_factory=list)
    candidates_status: str = "PROPOSED"
    confirmed_grain: str | None = None
    effective_grain: str | None = None
    grain_status: str = GrainStatus.PROPOSED


class SourceTableDetailOut(SourceTableOut):
    """One table with its columns and its grain evidence.

    Extends the list shape rather than redeclaring it, so a detail screen and a
    list row can never drift apart in what they call the same field.
    """

    database_name: str | None = None
    comment: str | None = None
    notes: str | None = None
    grain_columns: list[str] = Field(default_factory=list)
    grain_confidence: float | None = None
    grain_method: str | None = None
    grain_evidence: dict[str, Any] = Field(default_factory=dict)
    grain_confirmed_by: str | None = None
    grain_confirmed_at: datetime | None = None
    time_grain: str | None = None
    row_count: int | None = None
    completeness_pct: float | None = None
    quality_score: float | None = None
    withheld_column_count: int = 0
    quality_warnings: list[str] = Field(default_factory=list)
    columns: list[SourceColumnOut] = Field(default_factory=list)


class SourceHealthTableOut(ApiModel):
    """One table's contribution to its source's health verdict."""

    source_table_id: str
    table: str
    time_column: str | None = None
    freshness_status: str
    lag_seconds: int | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    row_count: int | None = None
    completeness_pct: float | None = None
    quality_score: float | None = None
    grain: str | None = None
    grain_status: str | None = None
    profiled_at: datetime | None = None
    checked_at: datetime | None = None
    note: str | None = None


class SourceHealthOut(ApiModel):
    """A deterministic health verdict plus the measurements behind it.

    ``reason`` is not decoration: a status without its evidence is a number
    somebody has to trust blindly, and the whole point of computing this without
    a model is that the arithmetic can be checked.

    ``checked_at`` is when the rollup was computed; ``measured_at`` is when the
    newest underlying measurement was taken. A read projects stored observations
    and never re-measures, so on a read the two differ — and that gap is exactly
    how stale a verdict the caller is looking at.
    """

    source_id: str
    status: str
    reason: str
    checked_at: datetime
    measured_at: datetime | None = None
    refresh_frequency: str
    last_refresh_at: datetime | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    completeness_pct: float | None = None
    quality_score: float | None = None
    grain: str | None = None
    fresh_tables: int = 0
    stale_tables: int = 0
    unknown_tables: int = 0
    unprofiled_tables: int = 0
    selected_table_count: int = 0
    known_limitations: str | None = None
    tables: list[SourceHealthTableOut] = Field(default_factory=list)


class TableGovernanceUpdate(ApiModel):
    """A reviewer's decision about one table's governed metadata.

    ``confirm_candidates`` and ``confirm_grain`` are the only way anything reaches
    CONFIRMED. Setting either to false returns the field to PROPOSED, so a
    confirmation can be withdrawn when the business changes its mind.
    """

    display_name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    primary_identifier_candidates: list[str] | None = None
    time_field_candidates: list[str] | None = None
    company_field_candidates: list[str] | None = None
    confirm_candidates: bool | None = None
    confirmed_grain: str | None = Field(default=None, max_length=300)
    confirm_grain: bool | None = None


class ColumnGovernanceUpdate(ApiModel):
    """A reviewer's decision about one column's business role.

    Separate from ``ColumnClassificationUpdate``: sensitivity governs who may
    read the column, role governs what it means. Conflating them would let a
    meaning change quietly widen access.
    """

    confirmed_role: ColumnRole | None = None
    description: str | None = None
    clear_confirmed_role: bool = False


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

    @field_validator("document_type", mode="before")
    @classmethod
    def _normalise_document_type(cls, value: object) -> object:
        """Accept the UI label while persisting the canonical enum value."""
        if isinstance(value, str) and value.strip().casefold() == "kpi handbook":
            return DocumentType.KPI_HANDBOOK
        return value


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
# Detection
# ---------------------------------------------------------------------------
class BucketConfigRequest(ApiModel):
    """Create or replace a company's comparison policy.

    ``buckets`` is intentionally an open object rather than a nested Pydantic
    tree: it is validated by :func:`app.services.bucket_config.validate_bucket_config`,
    which is the same code the detection engine trusts and the same code an LLM
    draft has to pass. Declaring the shape twice would let the two drift, and the
    one that matters is the one the engine reads.
    """

    config_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    #: NULL applies the policy to every KPI; a key overrides it for one KPI.
    kpi_key: str | None = Field(default=None, max_length=80)
    buckets: dict[str, Any] = Field(default_factory=dict)


class BucketConfigApproveRequest(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)


class BucketConfigExtractRequest(ApiModel):
    """Draft a configuration from company documentation using the model.

    Exactly one of ``document_id`` or ``text`` is supplied. The result is always
    a PROPOSED configuration -- the endpoint cannot approve its own output.
    """

    config_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    name: str = Field(min_length=1, max_length=200)
    kpi_key: str | None = Field(default=None, max_length=80)
    document_id: str | None = Field(default=None, max_length=36)
    text: str | None = Field(default=None, max_length=200_000)


class RunDetectionRequest(ApiModel):
    """Evaluate one KPI on one date.

    ``company_id`` is accepted so the flat ``POST /run-detection`` form works as
    specified; the company-scoped route takes it from the path instead.
    """

    company_id: str | None = Field(default=None, max_length=36)
    kpi_id: str = Field(min_length=1, max_length=80, description="KPI key, definition id or version id")
    target_date: date
    persist: bool = True


class BatchDetectionRequest(ApiModel):
    """Evaluate several KPIs on one date, for a dashboard load."""

    company_id: str | None = Field(default=None, max_length=36)
    kpi_ids: list[str] = Field(default_factory=list, max_length=25)
    target_date: date
    persist: bool = True
    force_rerun: bool = Field(
        default=False,
        description=(
            "Execute again even though this date already has a completed Agent Run. "
            "The earlier run and its stored results are left untouched; a re-run is "
            "recorded as a new Agent Run so both readings of the day remain on file."
        ),
    )


class AgentRunOut(ApiModel):
    id: str
    company_id: str
    target_date: date
    status: str
    kpi_count: int
    processed_count: int
    normal_count: int
    abnormal_count: int
    low_confidence_count: int
    error_count: int
    errors: list[Any] = Field(default_factory=list)
    duration_ms: int | None
    executed_by_user_id: str | None
    started_at: datetime
    completed_at: datetime | None


class DetectionRunOut(ApiModel):
    id: str
    agent_run_id: str | None = None
    kpi_key: str
    kpi_name: str
    kpi_version: int
    target_date: date
    actual_value: float | None
    expected_value: float | None
    deviation_absolute: float | None
    deviation_pct: float | None
    status: str
    comparison_label: str | None
    headline: str | None
    unit: str | None
    currency: str | None
    executed_at: datetime


class ResultSummaryOut(ApiModel):
    total_runs: int
    anomalies: int
    abnormal: int
    normal: int
    low_confidence: int
    kpi_count: int


class ResultItemOut(ApiModel):
    id: str
    kpi_key: str
    kpi_name: str
    target_date: date
    status: str
    actual_value: float | None
    expected_value: float | None
    deviation_absolute: float | None
    deviation_pct: float | None
    # Carried so the screen can render the measurement in the KPI's own unit
    # rather than guessing money from the KPI's name.
    unit: str | None = None
    currency: str | None = None
    top_driver: str | None = None
    # ``ai_explanation`` means a model wrote this sentence. It stays null when no
    # explanation was generated, and the deterministic ``top_driver`` headline is
    # what the screen falls back to -- the two are never conflated.
    ai_explanation: str | None = None
    explanation_status: str = "NOT_GENERATED"
    explanation_generated_at: datetime | None = None
    email_status: str = "NOT_SENT"


# ---------------------------------------------------------------------------
# Investigation: contribution analysis and manual dimensional analysis
# ---------------------------------------------------------------------------
class EntityStep(ApiModel):
    """One narrowing already chosen in a drill-down: a dimension and a value.

    Both are re-resolved server-side. ``dimension`` must be an approved dimension
    of the KPI version in question, and ``value`` must sit inside the caller's own
    row scope -- a value typed by hand is checked exactly as one arrived at by
    clicking is.
    """

    dimension: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=200)


class ContributionRequest(ApiModel):
    """Break one KPI's measured movement down across one approved dimension.

    There is deliberately no field for an actual, an expected value or a movement.
    The figures come from the stored detection run for this KPI and date, so a
    caller cannot state a movement and have the platform apportion it.
    """

    kpi_id: str = Field(min_length=1, max_length=80, description="KPI key, definition id or version id")
    target_date: date
    dimension: str | None = Field(
        default=None,
        max_length=120,
        description="An approved dimension of this KPI. Omitted means its default breakdown.",
    )
    path: list[EntityStep] = Field(
        default_factory=list,
        max_length=4,
        description="Ancestors already selected, outermost first.",
    )
    top_k: int | None = Field(default=None, ge=1, le=50)


class ManualAnalysisRequest(ApiModel):
    """The manual entry point: KPI, dimension, optional entity, date and lookback.

    With no ``entity`` this ranks the top contributing values of the dimension.
    With one, it profiles that single entity over the window and analyses nothing
    else -- entity-level analysis is on demand, never a sweep over every value.
    """

    kpi_id: str = Field(min_length=1, max_length=80)
    dimension: str | None = Field(default=None, max_length=120)
    entity: str | None = Field(default=None, max_length=200)
    target_date: date
    lookback_days: int = Field(default=30, ge=2, le=365)
    top_k: int | None = Field(default=None, ge=1, le=50)


# ---------------------------------------------------------------------------
# Explainability: the structured explanation of one result or one node
# ---------------------------------------------------------------------------
class ExplanationSectionOut(ApiModel):
    """One labelled section. Headings are fixed by the server, not the client."""

    heading: str
    body: str


class ExplanationCitationOut(ApiModel):
    """An approved document the explanation drew on.

    Present only for a caller holding ``document.read`` and only for documents
    their own scopes admit -- the retrieval layer applies both before any content
    is read, so a restricted document is never named here.
    """

    label: str
    title: str | None = None
    snippet: str | None = None
    document_id: str | None = None
    document_key: str | None = None
    document_version: int | None = None
    document_status: str | None = None
    #: Why this document bears on the date in question ("effective on", "most
    #: recent before", and so on). The retrieval layer's own word, not a guess.
    standing: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    score: float | None = None


class ExplanationConfidenceOut(ApiModel):
    """A three-level judgement with every reason that produced it.

    Deliberately not a probability: nothing in this platform estimates one, so a
    number here would be invented precision.
    """

    level: str
    reasons: list[str] = Field(default_factory=list)


class ExplanationOut(ApiModel):
    """A structured explanation, assembled from stored evidence.

    ``model_written`` is the honest label on the prose. False means these are the
    platform's own words over the same governed figures -- which is what ships
    when no language model is configured, and what a reader still gets when a
    configured model fails. The figures are identical either way.
    """

    subject: str
    scope: str
    order: list[str]
    sections: list[ExplanationSectionOut]
    text: str
    citations: list[ExplanationCitationOut] = Field(default_factory=list)
    confidence: ExplanationConfidenceOut
    limitations: list[str] = Field(default_factory=list)
    model_written: bool = False
    model: str | None = None
    #: Present only for a caller entitled to the underlying statistics. This is the
    #: same material the model was given, exposed so a reader can check the prose
    #: against the numbers rather than taking it on trust.
    facts: dict[str, Any] | None = None


class ResultExplainRequest(ApiModel):
    """Explain one stored result.

    No figures in the request. The KPI and date identify a stored detection run
    and every number comes from it, so a caller cannot supply a movement and have
    the platform explain one that was never measured.
    """

    kpi_id: str = Field(min_length=1, max_length=80)
    target_date: date
    #: Set false to skip the language model and take the deterministic assembly.
    #: Useful for a caller that wants the same explanation reproducibly.
    use_model: bool = True


class NodeExplainRequest(ApiModel):
    """Explain one node of an investigation: the whole movement, or one part.

    ``dimension`` and ``entity`` are re-resolved against the KPI version's
    approved dimensions and the caller's row scope before anything is explained.
    """

    kpi_id: str = Field(min_length=1, max_length=80)
    target_date: date
    dimension: str | None = Field(default=None, max_length=120)
    entity: str | None = Field(default=None, max_length=200)
    path: list[EntityStep] = Field(default_factory=list, max_length=4)
    use_model: bool = True


# ---------------------------------------------------------------------------
# Investigation findings: the human conclusion beside the measurement
# ---------------------------------------------------------------------------
class FindingCreate(ApiModel):
    """A note written against one movement, or one part of one.

    ``status`` is the state of the *investigation*, never of the KPI: nothing here
    can change a detection verdict, and there is no field that could be mistaken
    for one.
    """

    kpi_id: str = Field(min_length=1, max_length=80)
    target_date: date
    title: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=8000)
    status: str = Field(default="OPEN")
    dimension: str | None = Field(default=None, max_length=120)
    entity: str | None = Field(default=None, max_length=200)
    path: list[EntityStep] = Field(default_factory=list, max_length=4)


class FindingUpdate(ApiModel):
    """Change a note's text or where its investigation stands.

    Every field is optional and an omitted field is left alone, so updating a
    status cannot silently blank a note.
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=8000)
    status: str | None = None


class FindingOut(ApiModel):
    id: str
    kpi_key: str
    kpi_name: str
    target_date: date
    title: str
    note: str | None
    status: str
    dimension: str | None
    entity: str | None
    path: list[dict[str, str]] = Field(default_factory=list)
    #: How this finding's anchor reads in a list that mixes several nodes.
    scope_label: str
    detection_run_id: str | None = None
    created_by_email: str | None = None
    updated_by_email: str | None = None
    created_at: datetime
    updated_at: datetime
    #: Written when the status actually became RESOLVED and cleared when it stops
    #: being RESOLVED. A timestamp that is present is one that means something.
    resolved_at: datetime | None = None


# ---------------------------------------------------------------------------
# Recommendation feedback: what a reader did with the suggested action
# ---------------------------------------------------------------------------
class RecommendationFeedbackIn(ApiModel):
    """A reader's response to one recommendation.

    Deliberately narrow. There is no field for a verdict, a status or a figure:
    this says whether the advice was useful and how far the reader's own review
    got, and nothing here can reach a detection result or an investigation note.

    ``recommendation_key`` is the engine's stable identity for one recommendation
    — its lever and its target area — so a second submission from the same reader
    corrects the first rather than stacking a duplicate opinion beside it.
    """

    recommendation_key: str = Field(min_length=1, max_length=300)
    usefulness: str = Field(default="USEFUL")
    action_status: str = Field(default="NOT_STARTED")
    comment: str | None = Field(default=None, max_length=4000)


class RecommendationFeedbackOut(ApiModel):
    recommendation_key: str
    usefulness: str
    action_status: str
    comment: str | None = None
    lever_key: str | None = None
    target_entity: str | None = None
    submitted_by_email: str | None = None
    submitted_at: datetime


# ---------------------------------------------------------------------------
# Monitoring dashboard: one read for the whole overview
# ---------------------------------------------------------------------------
class MonitoringCountsOut(ApiModel):
    """The verdict tally for the window.

    ``unrecognised`` exists because a stored row may carry a status from an
    earlier schema. Folding those into one of the three real verdicts would
    misreport them, and dropping them would make the tiles fail to sum -- so they
    are counted, named and visible.
    """

    kpis_monitored: int
    evaluated: int
    normal: int
    abnormal: int
    low_confidence: int
    unrecognised: int
    unrecognised_statuses: list[str] = Field(default_factory=list)
    #: KPIs with an active version that were not evaluated in the window at all.
    not_evaluated: int


class MonitoringMovementOut(ApiModel):
    """One KPI's largest stored movement in the window."""

    detection_run_id: str
    kpi_id: str
    kpi_key: str
    kpi_name: str
    target_date: date
    status: str
    actual_value: float | None
    expected_value: float | None
    deviation_absolute: float | None
    deviation_pct: float | None
    unit: str | None = None
    currency: str | None = None
    headline: str | None = None
    #: Whether a breakdown has been stored for this movement. The dashboard uses it
    #: to say "investigate" versus "review investigation" honestly.
    #:
    #: Null -- not False -- for a caller without ``investigation.read``. Whether
    #: anyone has analysed a movement is itself investigation information, and
    #: "you may not see this" must not be rendered as "nobody has looked".
    has_contribution: bool | None = None
    open_findings: int | None = None
    #: The leading contributor from the stored breakdown, when one has been run and
    #: this caller may see it. Copied from ``ContributionRun``, never recomputed
    #: here: the dashboard reports the apportionment the investigation reached.
    #:
    #: All four are null together -- no breakdown stored, or not disclosed to this
    #: caller. A movement with no analysis names no contributor rather than
    #: nominating the largest thing it can find.
    contributor_dimension: str | None = None
    contributor_entity: str | None = None
    contributor_share_pct: float | None = None
    #: Whether the breakdown itself judged that leader sufficient to explain the
    #: movement. A share of 30% across a long tail is not a cause, and the screen
    #: must be able to say so rather than presenting any leader as the answer.
    contributor_is_sufficient: bool | None = None
    #: Set when this movement can be opened in the existing investigation workflow:
    #: it carries a stored deviation and the caller holds ``investigation.read``.
    can_investigate: bool = False


class MonitoringHeadlineOut(ApiModel):
    """One abnormal movement, written as a business reader would say it.

    Derived entirely from stored rows -- a ``DetectionRun`` the engine wrote and,
    where one exists, the ``ContributionRun`` an investigation stored beside it.
    Nothing here is a model's sentence: ``headline`` is assembled from the KPI's
    own name, the run's own date and the figures already computed, so the same
    inputs always produce the same line and no claim outruns its evidence.

    A movement with no stored breakdown names no contributor. That is the whole
    discipline of this shape: the alternative -- nominating whichever entity looks
    largest -- would be the platform inventing a cause. And where a contributor
    *is* named, the sentence says it "accounts for" a share of the movement, never
    that it caused or drove one: a share is a size, and this platform measures
    where a movement sits rather than why it happened.
    """

    detection_run_id: str
    kpi_id: str
    kpi_key: str
    kpi_name: str
    target_date: date
    status: str
    #: The sentence itself, ready to print.
    headline: str
    #: The movement, in the two forms the engine stored it.
    deviation_pct: float | None = None
    deviation_absolute: float | None = None
    actual_value: float | None = None
    expected_value: float | None = None
    unit: str | None = None
    currency: str | None = None
    #: "above" or "below" -- the direction of the movement, in words, so the screen
    #: does not have to re-derive it from a sign.
    direction: str | None = None
    #: The leading contributor, when a breakdown has been stored and this caller
    #: may see it. Null together otherwise; see ``MonitoringMovementOut``.
    contributor_dimension: str | None = None
    contributor_entity: str | None = None
    contributor_share_pct: float | None = None
    contributor_is_sufficient: bool | None = None
    #: Why no contributor is named, when none is: no breakdown has been run, or the
    #: caller may not see the investigation layer. Printed instead of a cause.
    contributor_note: str | None = None
    can_investigate: bool = False


class MonitoringRunOut(ApiModel):
    """One stored evaluation, most recent first."""

    detection_run_id: str
    agent_run_id: str | None = None
    kpi_id: str
    kpi_key: str
    kpi_name: str
    target_date: date
    status: str
    deviation_pct: float | None
    executed_at: datetime


class MonitoringKpiOut(ApiModel):
    """One monitored KPI and its latest stored verdict, if it has one."""

    kpi_id: str
    kpi_key: str
    kpi_name: str
    lifecycle_status: str
    active_version: int | None = None
    latest_status: str | None = None
    latest_target_date: date | None = None
    latest_deviation_pct: float | None = None
    latest_executed_at: datetime | None = None
    evaluated_in_window: int = 0


class MonitoringOut(ApiModel):
    """Everything the monitoring dashboard needs, in one governed read.

    Every figure is a count or a copy of a stored row. Nothing here is projected,
    forecast or interpolated, and a window with no runs returns zeros rather than
    a shape the screen has to guess at.
    """

    window_days: int
    window_from: date | None
    window_to: date | None
    #: Null when no evaluation has ever been stored for this company. This is what
    #: the screen must show instead of implying continuous monitoring.
    last_evaluated_at: datetime | None = None
    counts: MonitoringCountsOut
    kpis: list[MonitoringKpiOut] = Field(default_factory=list)
    biggest_movements: list[MonitoringMovementOut] = Field(default_factory=list)
    recent_abnormal: list[MonitoringMovementOut] = Field(default_factory=list)
    recent_runs: list[MonitoringRunOut] = Field(default_factory=list)
    #: The period the headline panel covers, and the periods it can be switched to.
    #: The options are sent rather than assumed by the screen, so the set of offered
    #: windows is decided in one place and a client cannot ask for a period the
    #: server does not support.
    findings_window_days: int = 7
    findings_window_options: list[int] = Field(default_factory=list)
    #: Earliest and latest *stored* target dates inside the headline window, on the
    #: same principle as ``window_from``/``window_to``: an empty period reads as
    #: empty rather than as a range in which nothing was found.
    findings_window_from: date | None = None
    findings_window_to: date | None = None
    #: Abnormal movements in that period, written as sentences and computed only
    #: from stored detection and contribution rows.
    headlines: list[MonitoringHeadlineOut] = Field(default_factory=list)
    #: How many abnormal movements the period holds in total, which is not the same
    #: as how many headlines are listed: the list is capped so the panel stays a way
    #: in to the Result page rather than a substitute for the result history.
    headline_total: int = 0
    #: The investigation tallies, and null for a caller without
    #: ``investigation.read``. A zero would assert that nobody has written a
    #: finding, which is a different claim from "this is not yours to see".
    findings_open: int | None = None
    findings_in_progress: int | None = None
    findings_resolved: int | None = None
    recent_findings: list[FindingOut] = Field(default_factory=list)
    #: Said plainly, because the platform has no scheduler in this version and a
    #: dashboard that implies one is lying about what it is showing.
    monitoring_note: str


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
