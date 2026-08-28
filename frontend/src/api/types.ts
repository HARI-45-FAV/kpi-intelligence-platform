/** Shapes returned by the Sprint 1 API, kept close to the backend schemas. */

export interface Membership {
  company_id: string
  company_name: string
  role_key: string
  role_name: string
  is_admin_role: boolean
  status: string
  permissions: string[]
  row_scope: Record<string, unknown>
}

export interface SessionInfo {
  user: { id: string; email: string; full_name: string; is_active: boolean }
  memberships: Membership[]
}

export interface AuthResult {
  access_token: string
  token_type: string
  expires_at: string
  user: SessionInfo['user']
  memberships: Membership[]
  company_id?: string | null
  permissions?: string[]
}

export interface Company {
  id: string
  company_name: string
  slug: string
  industry?: string | null
  description?: string | null
  country?: string | null
  timezone: string
  currency: string
  fiscal_year_start_month: number
  week_start_day: number
  status: string
  created_at: string
  updated_at: string
}

export interface Calendar {
  id: string
  calendar_key: string
  name: string
  timezone: string
  week_start_day: number
  fiscal_year_start_month: number
  is_default: boolean
  notes?: string | null
}

export interface RoleInfo {
  id: string
  role_key: string
  name: string
  description?: string | null
  is_admin_role: boolean
  rank: number
  permissions: string[]
  /** Presentation only — authorisation is still decided by `permissions`. */
  is_core?: boolean
  access_summary?: string | null
  access_areas?: Record<string, boolean>
}

export interface Member {
  membership_id: string
  user_id: string
  email: string
  full_name: string
  role_key: string
  role_name: string
  is_admin_role: boolean
  status: string
  row_scope: Record<string, unknown>
  denied_columns: string[]
  created_at: string
}

export interface ConnectorField {
  name: string
  label: string
  required: boolean
  kind: string
  placeholder: string
  help_text: string
  secret: boolean
}

export interface ConnectorDescriptor {
  source_type: string
  label: string
  implemented: boolean
  supports_profiling: boolean
  accepts_connection_uri: boolean
  notes: string
  fields: ConnectorField[]
}

export interface DataSource {
  id: string
  name: string
  source_type: string
  description?: string | null
  host?: string | null
  port?: number | null
  database_name?: string | null
  schema_name?: string | null
  username?: string | null
  has_credentials: boolean
  connection_status: string
  last_tested_at?: string | null
  last_test_error?: string | null
  refresh_frequency: string
  timezone: string
  last_discovered_at?: string | null
  table_count: number
  selected_table_count: number
  created_at: string
}

export interface ConnectionCheck {
  check: string
  ok: boolean
  detail?: string | null
}

export interface ConnectionTest {
  ok: boolean
  message: string
  connection_status: string
  checks: ConnectionCheck[]
  server_version?: string | null
  table_count?: number | null
  duration_ms?: number | null
  error?: string | null
}

export interface TableSummary {
  id: string
  data_source_id: string
  data_source_name: string
  schema_name: string
  table_name: string
  qualified_name: string
  table_type: string
  approx_row_count?: number | null
  column_count?: number | null
  selected: boolean
  primary_time_column?: string | null
  business_alias?: string | null
  declared_grain?: string | null
  inferred_grain?: string | null
  grain_confidence?: number | null
  quality_status?: string | null
  quality_score?: number | null
  freshness_status?: string | null
  profiled_at?: string | null
}

export interface ColumnProfileValues {
  row_count?: number | null
  null_count?: number | null
  null_pct?: number | null
  distinct_count?: number | null
  distinct_pct?: number | null
  min_value?: string | null
  max_value?: string | null
  mean_value?: number | null
  zero_count?: number | null
  negative_count?: number | null
  blank_count?: number | null
  sample_values: unknown[]
  is_unique?: boolean | null
  quality_status: string
  warnings: string[]
}

export interface ColumnDetail {
  id: string
  column_name: string
  ordinal_position: number
  data_type: string
  is_nullable: boolean
  is_primary_key: boolean
  is_foreign_key: boolean
  references_table?: string | null
  references_column?: string | null
  semantic_type: string
  classification: string
  is_pii: boolean
  is_sensitive: boolean
  is_restricted: boolean
  readable: boolean
  withheld_reason?: string | null
  profile?: ColumnProfileValues | null
}

export interface TableDetail extends TableSummary {
  columns: ColumnDetail[]
  row_count?: number | null
  warnings: string[]
  withheld_columns: number
  grain?: {
    inferred_grain?: string | null
    declared_grain?: string | null
    grain_columns: string[]
    confidence?: number | null
    method?: string | null
    is_unique?: boolean | null
    time_column?: string | null
    time_grain?: string | null
    evidence?: Record<string, unknown>
  } | null
  freshness?: {
    status: string
    lag_seconds?: number | null
    expected_interval_seconds?: number | null
    coverage_start?: string | null
    coverage_end?: string | null
    note?: string | null
  } | null
  relationships: RelationshipView[]
}

export interface RelationshipView {
  id: string
  from_table: string
  from_column: string
  to_table: string
  to_column: string
  relationship_type: string
  confidence?: number | null
  method?: string | null
  is_declared: boolean
  orphan_pct?: number | null
  join_safety?: {
    level: string
    reason?: string | null
    guidance?: string | null
    fan_out_factor?: number | null
    max_fan_out?: number | null
    duplicate_key_rate?: number | null
    expected_cardinality?: string | null
    observed_cardinality?: string | null
  } | null
}

export interface ReconciliationPair {
  id: string
  left_table: string
  right_table: string
  status: string
  left_grain?: string | null
  right_grain?: string | null
  left_time_grain?: string | null
  right_time_grain?: string | null
  shared_dimensions: string[]
  unmapped_dimensions: string[]
  time_overlap_days?: number | null
  reason?: string | null
  guidance?: string | null
}

export interface DocumentVersion {
  id: string
  version: number
  original_filename?: string | null
  content_type?: string | null
  size_bytes?: number | null
  checksum_sha256?: string | null
  effective_from?: string | null
  effective_to?: string | null
  is_current: boolean
  change_note?: string | null
  uploaded_by?: string | null
  uploaded_at?: string | null
  has_inline_content: boolean
}

export interface CompanyDocument {
  id: string
  document_key: string
  title: string
  description?: string | null
  document_type: string
  document_class: string
  status: string
  current_version: number
  access_scope: string[]
  tags: Record<string, unknown>
  owner_user_id?: string | null
  created_at: string
  updated_at: string
  versions: DocumentVersion[]
  retrieval_ready: boolean
}

export interface KpiVersionSummary {
  id: string
  version: number
  status: string
  formula_expression: string
  time_grain: string
  last_validation_status?: string | null
  last_validated_at?: string | null
  approved_by?: string | null
  approved_at?: string | null
  activated_at?: string | null
  deprecated_at?: string | null
  created_by?: string | null
  created_at: string
  proposal_origin: string
}

export interface KpiDefinition {
  id: string
  kpi_key: string
  name: string
  short_description?: string | null
  status: string
  current_version: number
  current_version_id?: string | null
  owner_user_id?: string | null
  created_at: string
  updated_at: string
  versions: KpiVersionSummary[]
}

export interface ValidationCheck {
  test_type: string
  label: string
  status: string
  expected?: string | null
  actual?: string | null
  message?: string | null
  is_blocking: boolean
  runtime_ms?: number | null
  evidence?: Record<string, any>
}

export interface ValidationReport {
  run_id?: string
  overall_status: string | null
  ready_for_approval?: boolean
  summary?: string | null
  duration_ms?: number | null
  passed?: number
  failed?: number
  warned?: number
  checks: ValidationCheck[]
  note?: string
}

export interface KpiContract {
  /** The stable business key (e.g. `net_revenue`), not a uuid. */
  kpi_id: string
  /** The owning definition's uuid — this is what KPI Setup selections store. */
  kpi_definition_id: string
  kpi_version_id: string
  name: string
  version: number
  status: string
  business_definition: string
  purpose?: string | null
  kind: string
  formula: string
  formula_spec: Record<string, any>
  aggregation?: string | null
  numerator?: Record<string, any> | null
  denominator?: Record<string, any> | null
  filters: unknown[]
  is_additive: boolean
  additivity_note: string
  unit?: string | null
  currency?: string | null
  direction: string
  time_field?: string | null
  time_grain: string
  timezone?: string | null
  calendar?: Record<string, any> | null
  source: Record<string, any>
  dimensions: Array<{
    dimension_name: string
    source_table?: string | null
    source_column: string
    allowed: boolean
    is_default_breakdown: boolean
    approx_cardinality?: number | null
    monitoring_note: string
  }>
  drivers: Array<{
    driver_name: string
    driver_type: string
    source_table?: string | null
    source_column?: string | null
    controllable: boolean
    measurement_method?: string | null
  }>
  materiality?: Record<string, any> | null
  access_policies: Array<{
    role_key: string
    allowed: boolean
    row_scope: Record<string, unknown>
    column_scope: string[]
    domain_scope: string[]
    aggregate_only: boolean
  }>
  lineage: Array<{
    role: string
    data_source?: string | null
    schema?: string | null
    table?: string | null
    column?: string | null
    transformation?: string | null
  }>
  behaviour?: Record<string, any>
  governance: Record<string, any>
  data_quality?: Record<string, any> | null
  freshness?: Record<string, any> | null
  is_editable?: boolean
  allowed_transitions?: string[]
}

export interface KpiDetail {
  definition: KpiDefinition
  version: KpiContract
  validation: ValidationReport | null
}

export interface KpiProposal {
  kpi_key: string
  name: string
  kind: string
  business_definition: string
  formula_expression: string
  source_table_id: string
  source_table: string
  time_field?: string | null
  time_grain: string
  unit?: string | null
  confidence: number
  rationale: string
  already_registered: boolean
  dimensions: Array<{ dimension_name: string; source_column: string; approx_cardinality?: number }>
  drivers: Array<{ driver_name: string; driver_type: string; source_column?: string | null }>
  evidence: Record<string, any>
  warnings: string[]
}

/** One row of the company's own KPI registry, read from the connected source. */
export interface CompanyKpiDefinition {
  kpi_key: string
  name: string
  business_definition: string
  source_formula: string
  resolution_status: 'RESOLVED' | 'NEEDS_MAPPING'
  formula_expression?: string | null
  source_table_id?: string | null
  source_table?: string | null
  time_field?: string | null
  time_grain: string
  kind?: string | null
  unit?: string | null
  direction: string
  owner?: string | null
  is_active: boolean
  declared_grain?: string | null
  declared_source?: string | null
  dimensions: Array<{ dimension_name: string; source_column: string }>
  materiality_threshold_pct?: number | null
  issues: string[]
  already_registered: boolean
  registered_kpi_id?: string | null
  importable: boolean
}

export interface CompanyDefinitionsResponse {
  definition_table: {
    source_table_id: string
    data_source_name?: string | null
    schema: string
    table: string
    role_columns: Record<string, string>
    matched_roles: number
    row_count?: number | null
    detection_method: string
  } | null
  other_candidate_tables: Array<{ table: string; matched_roles: number }>
  definitions: CompanyKpiDefinition[]
  counts: {
    total: number
    active: number
    resolved: number
    needs_mapping: number
    registered: number
    importable: number
  }
  note: string
}

export interface CompanyDefinitionImportResult {
  imported: KpiDefinition[]
  skipped: Array<{ kpi_key: string; reason: string; issues?: string[] }>
  counts: { imported: number; skipped: number }
}

export interface RelationshipSummary {
  checked: number
  safe: number
  needs_attention: number
  unsafe: number
  unrated: number
  material_relationship_ids: string[]
  material_count: number
}

export interface AuditEntry {
  id: string
  action: string
  resource_type: string
  resource_id?: string | null
  resource_label?: string | null
  actor_email?: string | null
  outcome: string
  summary?: string | null
  old_version?: string | null
  new_version?: string | null
  details: Record<string, unknown>
  occurred_at: string
}

export interface TelemetrySummary {
  requests: number
  errors: number
  latency_ms: { avg?: number | null; max?: number | null }
  connector: { queries: number; query_ms: number; rows_returned: number }
  llm: {
    calls: number
    prompt_tokens: number
    completion_tokens: number
    estimated_cost_usd: number
  }
  by_service: Array<{
    service: string
    requests: number
    avg_ms?: number | null
    max_ms?: number | null
    connector_queries: number
  }>
  processing_split: { deterministic: string[]; llm: string[]; note: string }
}

export interface SystemEvent {
  id: string
  category: string
  severity: string
  title: string
  message?: string | null
  occurred_at: string
}

export interface Dashboard {
  company: { id: string; name: string; status: string; currency: string; timezone: string }
  system_status: {
    data_sources: { total: number; connected: number }
    selected_tables: number
    profiled_tables: number
    kpis: { total: number; active: number; by_version_status: Record<string, number> }
    documents: number
    catalog_version?: number | null
    data_quality: {
      status: string
      avg_score?: number | null
      tables_by_status: Record<string, number>
    }
    freshness: {
      stale_tables: string[]
      last_source_data_at?: string | null
      checked_tables: number
    }
    checked_at: string
  }
  kpi_summary: Array<{
    kpi_id: string
    kpi_version_id: string
    name: string
    version: number
    formula: string
    unit?: string | null
    currency?: string | null
    time_grain: string
    dimensions: string[]
    value: number | null
    value_note: string
  }>
  recent_activity: SystemEvent[]
  sprint_scope: { delivered: string; not_yet: string }
}

export interface CatalogVersionInfo {
  id: string
  version: number
  published_at: string
  published_by?: string | null
  note?: string | null
  source_count: number
  selected_table_count: number
  profiled_table_count: number
  relationship_count: number
  document_count: number
  active_kpi_count: number
  checksum_sha256?: string | null
}
