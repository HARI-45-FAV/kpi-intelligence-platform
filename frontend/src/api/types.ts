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
  /** Named to match the API exactly — it counts what discovery found, not what is in scope. */
  discovered_table_count: number
  selected_table_count: number
  created_at: string
  /** Where a source with no driver lives — a path, endpoint or export name. */
  connection_reference?: string | null
  business_calendar_id?: string | null
  known_limitations?: string | null
  /**
   * Derived governance rollup. Written only by an explicit profile or health
   * check, so `health_checked_at` is what says how much of it is still true.
   * All null means never measured, which is a real answer, not a missing one.
   */
  grain?: string | null
  last_refresh_at?: string | null
  coverage_start?: string | null
  coverage_end?: string | null
  completeness_pct?: number | null
  quality_score?: number | null
  health_status: string
  health_checked_at?: string | null
  health_reason?: string | null
}

/** One table's contribution to its source's health verdict. */
export interface SourceHealthTable {
  source_table_id: string
  table: string
  time_column?: string | null
  freshness_status: string
  lag_seconds?: number | null
  coverage_start?: string | null
  coverage_end?: string | null
  row_count?: number | null
  completeness_pct?: number | null
  quality_score?: number | null
  grain?: string | null
  grain_status?: string | null
  profiled_at?: string | null
  checked_at?: string | null
  note?: string | null
}

/**
 * A deterministic health verdict and the measurements behind it.
 *
 * `checked_at` is when the arithmetic ran; `measured_at` is when the newest
 * underlying measurement was taken. A GET projects stored observations rather
 * than re-measuring, so the gap between the two is how stale the verdict is.
 */
export interface SourceHealthReport {
  source_id: string
  status: string
  reason: string
  checked_at: string
  measured_at?: string | null
  refresh_frequency: string
  last_refresh_at?: string | null
  coverage_start?: string | null
  coverage_end?: string | null
  completeness_pct?: number | null
  quality_score?: number | null
  grain?: string | null
  fresh_tables: number
  stale_tables: number
  unknown_tables: number
  unprofiled_tables: number
  selected_table_count: number
  known_limitations?: string | null
  tables: SourceHealthTable[]
}

/**
 * A registered table as the governance screens see it.
 *
 * Candidate lists and `*_status` fields travel together on purpose: a proposal
 * has to be displayable as a proposal, never as settled fact.
 */
export interface GovernedTable {
  id: string
  data_source_id: string
  schema_name: string
  table_name: string
  qualified_name: string
  table_type: string
  approx_row_count?: number | null
  column_count?: number | null
  discovered_at?: string | null
  selected: boolean
  business_alias?: string | null
  declared_grain?: string | null
  primary_time_column?: string | null
  inferred_grain?: string | null
  quality_status?: string | null
  freshness_status?: string | null
  profiled_at?: string | null
  display_name?: string | null
  description?: string | null
  primary_identifier_candidates: string[]
  time_field_candidates: string[]
  company_field_candidates: string[]
  candidates_status: string
  confirmed_grain?: string | null
  effective_grain?: string | null
  grain_status: string
}

/** A registered column with its proposed and confirmed business role. */
export interface GovernedColumn {
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
  candidate_role: string
  confirmed_role?: string | null
  effective_role: string
  role_status: string
  description?: string | null
  classification: string
  is_pii: boolean
  is_sensitive: boolean
  is_restricted: boolean
  readable: boolean
  withheld_reason?: string | null
}

export interface GovernedTableDetail extends GovernedTable {
  database_name?: string | null
  comment?: string | null
  notes?: string | null
  grain_columns: string[]
  grain_confidence?: number | null
  grain_method?: string | null
  grain_evidence: Record<string, unknown>
  grain_confirmed_by?: string | null
  grain_confirmed_at?: string | null
  time_grain?: string | null
  row_count?: number | null
  completeness_pct?: number | null
  quality_score?: number | null
  withheld_column_count: number
  quality_warnings: string[]
  columns: GovernedColumn[]
}

export interface SourceProfileResult {
  source_id: string
  profiled_table_count: number
  withheld_column_count: number
  /** One entry per profiled table, shaped by the profiler's own outcome record. */
  tables: Record<string, unknown>[]
  health: SourceHealthReport
  note: string
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

/* ------------------------------------------------------------------- copilot */

/**
 * What the user is looking at when they ask a question.
 *
 * Every field is a *hint*. The server re-resolves each one inside the caller's
 * own company, so a stale or foreign `kpi_id` resolves to nothing rather than to
 * someone else's KPI. Note what is absent: no company, no SQL, no filter, no
 * tool choice, and no way to hand the model a KPI value or definition to trust.
 * The company comes from the session; the meaning of a KPI comes from the
 * governed registry.
 */
/**
 * What the user is looking at. Coordinates only — no measurement.
 *
 * There is deliberately no field for an actual, an expected value, a deviation or
 * a status, even though every panel that opens the Copilot has them on screen. The
 * server re-reads those from the run it stored, because a client that could state
 * the actual could state a false one and have it explained as fact.
 */
export interface CopilotRequestContext {
  /**
   * Which panel asked: `stage_performance`, `detection_detail`, `historical_run`,
   * `investigation`, `future_action`. Decides which verified result the answer is
   * anchored to. Unrecognised values are ignored server-side.
   */
  panel?: string | null
  kpi_id?: string | null
  kpi_version?: number | null
  selected_date?: string | null
  /** Approved dimension being viewed. Checked against the KPI's own registry. */
  dimension?: string | null
  /** Value selected within that dimension. Checked against the caller's row scope. */
  selected_entity?: string | null
  /** Run whose stored results are on screen, so a past answer stays reproducible. */
  agent_run_id?: string | null
  /** Router path the question came from. Audit trail only. */
  page?: string | null
}

export interface CopilotChatRequest {
  message: string
  context?: CopilotRequestContext
}

/** One governed item the answer was built from, citable as `[E1]`. */
export interface CopilotEvidence {
  evidence_id: string
  source_type: string
  source_id?: string | null
  title: string
  content: string
  /** True for material derived from dashboard placeholders, never a measurement. */
  is_placeholder: boolean
  metadata: Record<string, unknown>
}

export interface CopilotToolCall {
  tool: string
  arguments: Record<string, unknown>
  ok: boolean
  error?: string | null
  caveats: string[]
}

export interface CopilotChatResponse {
  answer: string
  evidence: CopilotEvidence[]
  /** The context the *server* resolved — not the hints that were sent. */
  context: {
    company_id: string
    company_name: string
    role: string
    kpi_definition_id?: string | null
    kpi_key?: string | null
    kpi_name?: string | null
    kpi_version_id?: string | null
    kpi_version?: number | null
    selected_date?: string | null
    agent_run_id?: string | null
    notes: string[]
  }
  llm_available: boolean
  model?: string | null
  tool_calls: CopilotToolCall[]
  caveats: string[]
  iterations: number
  usage: Record<string, number>
  /** Set when no model is configured. A supported state, not an error. */
  unavailable_reason?: string | null
  truncated: boolean
}

export interface CopilotStatus {
  enabled: boolean
  available: boolean
  provider: string
  model?: string | null
  /** Host only — never the full endpoint URL, and never the API key. */
  endpoint_host?: string | null
  unavailable_reason?: string | null
  tools_available: string[]
  knowledge_sources: string[]
  planned_capabilities: string[]
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

/* ----------------------------------------------------------------- detection */

/**
 * A detection verdict as the business reads it.
 *
 * This mirrors the server's `business_view()` exactly, and the omissions are the
 * point: there is no median, no MAD, no modified z-score, no dispersion basis, no
 * bucket slot name, no reference dates and no SQL. The server does return that
 * evidence to callers holding `kpi.read`, but the business surface has no type for
 * it, so the statistics cannot leak into this screen by accident.
 *
 * `comparison` is the only trace of the calendar logic, and it is already prose
 * the server wrote for a reader ("Comparable Fridays"), not a bucket identifier.
 */
export interface DetectionResult {
  kpi: string
  kpi_key: string
  target_date: string
  actual: number | null
  expected: number | null
  deviation_pct: number | null
  deviation_absolute: number | null
  status: 'NORMAL' | 'ABNORMAL' | 'LOW_CONFIDENCE'
  comparison: string | null
  headline: string | null
  unit?: string | null
  currency?: string | null
}

export interface DetectionRunResponse {
  result: DetectionResult
  run_id?: string | null
  agent_run_id?: string | null
  persisted: boolean
}

export interface DetectionBatchResponse {
  target_date: string
  agent_run_id: string
  agent_run: AgentRunSummary
  results: DetectionRunResponse[]
  skipped: Array<{ kpi_id: string; reason: string }>
  counts: { evaluated: number; skipped: number }
}

export interface AgentRunSummary {
  id: string
  company_id: string
  target_date: string
  status: string
  kpi_count: number
  processed_count: number
  normal_count: number
  abnormal_count: number
  low_confidence_count: number
  error_count: number
  errors: Array<{ kpi_id?: string; reason?: string }>
  duration_ms?: number | null
  executed_by_user_id?: string | null
  started_at: string
  completed_at?: string | null
}

/** The last stored verdict for a KPI, as listed by the overview. */
export interface DetectionRunSummary {
  id: string
  kpi_key: string
  kpi_name: string
  kpi_version: number
  target_date: string
  actual_value: number | null
  expected_value: number | null
  deviation_absolute: number | null
  deviation_pct: number | null
  status: string
  comparison_label?: string | null
  headline?: string | null
  unit?: string | null
  currency?: string | null
  executed_at: string
}

/* ------------------------------------------------------------- investigation */

/**
 * One dimension a KPI may be broken down by, as its registration approved it.
 *
 * The list comes from the server, never from the client: a dimension absent here
 * is not one the screen may hide, it is one the platform will refuse to query.
 * `hierarchy` is where a drill-down may go next, already filtered to dimensions
 * that are themselves approved.
 */
export interface InvestigationDimension {
  name: string
  is_default: boolean
  hierarchy: string[]
  approx_cardinality?: number | null
  notes?: string | null
}

export interface InvestigationDimensionsResponse {
  kpi_key: string
  kpi_name: string
  kpi_version: number
  dimensions: InvestigationDimension[]
}

/** One step already taken in a drill-down: an approved dimension and a value. */
export interface EntityStep {
  dimension: string
  value: string
}

/**
 * One part of the business and what it did.
 *
 * Deliberately no status field. A contributor has a movement and a share of the
 * KPI's movement; it does not have a verdict, and the largest share is not an
 * anomaly. `share_pct` is `null` when the KPI is a ratio, an average or a
 * distinct count, because no share of such a movement is arithmetic.
 */
export interface Contributor {
  entity: string | null
  label: string
  actual: number | null
  expected: number | null
  change: number | null
  share_pct: number | null
  absolute_share_pct: number | null
  reference_count: number
  matched_rows: number | null
  note?: string | null
}

/** A measured KPI movement, split across one approved dimension. */
export interface ContributionResult {
  kpi: string
  kpi_key: string
  target_date: string
  dimension: string
  path: EntityStep[]
  actual: number | null
  expected: number | null
  movement: number | null
  status: string | null
  comparison: string | null
  unit?: string | null
  currency?: string | null
  contributors: Contributor[]
  top_k: number
  ranked_count: number
  explained_pct: number | null
  unexplained_pct: number | null
  /** The leader alone accounts for most of the movement — a reason to stop. */
  leader_is_sufficient: boolean
  sufficiency_pct: number
  shares_available: boolean
  next_dimensions: string[]
  notes: string[]
}

/** How the breakdown was produced. Technical details area only. */
export interface ContributionEvidence {
  kpi_version: number
  kpi_version_id: string
  detection_run_id?: string | null
  /** The stored investigation this response was written to, for audit follow-up. */
  contribution_run_id?: string | null
  dimension: string
  additive: boolean
  reference_dates: string[]
  withheld_by_scope: number
  queries: string[]
}

export interface ContributionResponse {
  result: ContributionResult
  evidence?: ContributionEvidence
}

/** One entity's own history, produced on demand for the entity asked about. */
export interface EntityProfileResult {
  kpi: string
  kpi_key: string
  dimension: string
  entity: string
  unit?: string | null
  currency?: string | null
  points: Array<{
    date: string
    value: number | null
    matched_rows: number | null
    note?: string | null
  }>
  latest: number | null
  typical: number | null
  change_vs_typical: number | null
  change_pct_vs_typical: number | null
  observed_days: number
  notes: string[]
}

/**
 * The manual entry point returns one of two shapes, tagged by `mode`.
 *
 * No entity named → a ranked breakdown, the same one the automatic flow produces.
 * An entity named → that entity's history alone. Nothing on this platform
 * analyses every entity, so asking about one never triggers work on the rest.
 */
export type ManualAnalysisResponse =
  | ({ mode: 'contribution' } & ContributionResponse)
  | {
      mode: 'entity'
      result: EntityProfileResult
      evidence?: { kpi_version: number; queries: string[] }
    }

/**
 * One KPI on the monitoring screen.
 *
 * `blocked_reason` carries the governance obstacle — unapproved, no time field,
 * no source binding, the wrong grain — because that is something the reader can
 * act on, where a silently missing row is not.
 */
export interface DetectableKpi {
  kpi_id: string
  kpi_key: string
  name: string
  detectable: boolean
  blocked_reason?: string | null
  unit?: string | null
  currency?: string | null
  kpi_version?: number | null
  latest_run?: DetectionRunSummary | null
}

export interface DetectionOverview {
  kpis: DetectableKpi[]
  counts: { total: number; detectable: number }
  configuration: {
    /** Named only; the slot values behind it stay off the business screen. */
    company_default?: { config_key: string; name: string; version: number } | null
    kpi_overrides: Array<{ config_key: string; name: string; kpi_key?: string | null }>
    note?: string | null
  }
}

/* -------------------------------------------------- comparison configuration */

/**
 * The five comparison slots the detection engine knows how to fill.
 *
 * The slot *names* are fixed — they are part of the algorithm — and everything
 * inside them is the company's own. Nothing in this file names a weekday, a week,
 * a month or an event: those arrive from the server, extracted from that
 * company's documentation or typed by an administrator, so this type describes
 * the shape of a policy and never one company's policy.
 *
 * This lives on the governance surface only. The business detection screen shows
 * KPI, actual, expected, deviation and status — never a slot.
 */
export interface BucketSlots {
  same_day_of_week?: { enabled: boolean; days?: number[] }
  same_week_of_month?: { enabled: boolean; weeks?: number[] }
  same_month_or_season?: { enabled: boolean; months?: number[] }
  business_event?: {
    enabled: boolean
    events?: Array<{ name: string; dates?: string[] }>
  }
  yoy_period?: { enabled: boolean; tolerance_days?: number }
  lookback_days?: number
  min_reference_points?: number
  max_reference_points?: number
}

/**
 * `NEEDS_REVIEW` is the honest landing place for an extraction that produced
 * something but not something usable. It is deliberately not `PROPOSED` — that
 * would invite an approval click on a policy that cannot select a single
 * comparable date — and deliberately not an error, because the partial result and
 * its reasons are what a reviewer needs to finish the job by hand.
 */
export type BucketConfigStatus =
  | 'DRAFT'
  | 'NEEDS_REVIEW'
  | 'PROPOSED'
  | 'APPROVED'
  | 'ARCHIVED'

export interface BucketConfigSummary {
  id: string
  config_key: string
  name: string
  description?: string | null
  kpi_key?: string | null
  scope: 'company' | 'kpi'
  status: BucketConfigStatus
  version: number
  buckets: BucketSlots
  enabled_slots: string[]
  lookback_days?: number | null
  min_reference_points?: number | null
  max_reference_points?: number | null
  source: 'MANUAL' | 'LLM_EXTRACTION'
  source_document_id?: string | null
  extraction_model?: string | null
  extraction_notes?: string | null
  approved_by_user_id?: string | null
  approved_at?: string | null
  approval_reason?: string | null
  allowed_transitions: string[]
  created_at: string
  updated_at: string
}

export interface BucketConfigList {
  configurations: BucketConfigSummary[]
  company_default_in_force?: string | null
  note?: string | null
}

/** How much of the document reached the model, and by what strategy. */
export interface ExtractionRetrieval {
  strategy: string
  passages_in_document: number
  passages_selected: number
  document_characters: number
  selected_characters: number
}

export interface BucketExtractionResponse extends BucketConfigSummary {
  extraction: {
    model?: string | null
    notes: string[]
    raw_keys: string[]
    rejected_keys: string[]
    needs_review: boolean
    review_reasons: string[]
    retrieval?: ExtractionRetrieval | null
  }
  needs_review: boolean
  review_reasons: string[]
  warnings: string[]
  note?: string | null
}

/** The calendar consequence of a policy, computed without touching the source. */
export interface BucketConfigPreview {
  config_id: string
  status: BucketConfigStatus
  target_date: string
  comparison: {
    label: string
    bucket_applied: string
    buckets_applied: string[]
    decisions: Array<{
      bucket: string
      role: string
      reference_count: number
      note: string
    }>
  }
  comparable_dates: string[]
  comparable_date_count: number
  warnings: string[]
  note?: string | null
}


