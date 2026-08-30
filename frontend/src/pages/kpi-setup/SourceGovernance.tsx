/**
 * Source governance: one source, its health, its tables, their columns.
 *
 * The screen order is the governance order — SOURCE → HEALTH → TABLES → COLUMNS
 * → PROFILE — and it holds one line throughout: a proposal is shown as a
 * proposal. Every governed field arrives with the status that says who put it
 * there, and this page renders that status rather than flattening it, because a
 * machine's guess and a reviewer's decision carry different weight downstream.
 *
 * Nothing here measures anything. Loading the page projects what was last
 * measured; only the explicit Profile and Check health buttons open a connection.
 * That is why every panel shows *when* its numbers were taken.
 */

import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../../api/client'
import type {
  DataSource,
  GovernedColumn,
  GovernedTable,
  GovernedTableDetail,
  SourceHealthReport,
  SourceProfileResult,
} from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import {
  formatCompact,
  formatDateTime,
  formatDuration,
  formatRelative,
  titleCase,
} from '../../components/format'
import {
  Alert,
  DefinitionRow,
  EmptyState,
  Field,
  Metric,
  Panel,
  Spinner,
  StatusBadge,
} from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'

// The business readings a reviewer may confirm. Kept in step with ColumnRole on
// the backend; UNKNOWN is offered so a reviewer can say "none of these" rather
// than being forced into the closest wrong answer.
const ROLE_OPTIONS = [
  'IDENTIFIER',
  'TIME',
  'MEASURE',
  'CURRENCY',
  'QUANTITY',
  'STATUS',
  'DIMENSION',
  'TEXT',
  'UNKNOWN',
] as const

export default function SourceGovernance() {
  const { companyId } = useAuth()
  const { sourceId } = useParams<{ sourceId: string }>()
  const base = `/companies/${companyId}`

  const source = useResource<DataSource>(
    () => api.get(`${base}/data-sources/${sourceId}`, { admin: true }),
    [companyId, sourceId],
  )
  const health = useResource<SourceHealthReport>(
    () => api.get(`${base}/data-sources/${sourceId}/health`, { admin: true }),
    [companyId, sourceId],
  )
  const tables = useResource<GovernedTable[]>(
    () => api.get(`${base}/tables?data_source_id=${sourceId}`, { admin: true }),
    [companyId, sourceId],
  )

  const [openTable, setOpenTable] = useState<string | null>(null)
  const [lastProfile, setLastProfile] = useState<SourceProfileResult | null>(null)
  const healthCheck = useAction()
  const profile = useAction()

  const reloadAll = async () => {
    await Promise.all([source.reload(), health.reload(), tables.reload()])
  }

  if (source.loading && !source.data) return <Spinner label="Loading source" />
  if (source.error) return <Alert>{source.error}</Alert>
  if (!source.data) return null

  const record = source.data
  const inScope = tables.data?.filter((table) => table.selected) ?? []

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-slate-100">{record.name}</h2>
            <span className="chip">{titleCase(record.source_type)}</span>
            <StatusBadge status={record.connection_status} />
            <StatusBadge status={record.health_status} />
          </div>
          <p className="mt-0.5 text-xs text-slate-500">
            {[record.host, record.database_name, record.schema_name, record.connection_reference]
              .filter(Boolean)
              .join(' · ') || 'no connection coordinates recorded'}
          </p>
        </div>
        <Link className="btn-ghost btn-xs" to="/kpi-setup/sources">
          ← All sources
        </Link>
      </div>

      {/* ------------------------------------------------------------ source */}
      <Panel
        title="Source"
        actions={
          <div className="flex gap-2">
            <button
              className="btn-ghost btn-xs"
              disabled={healthCheck.pending}
              onClick={async () => {
                const ok = await healthCheck.run(
                  () =>
                    api.post<SourceHealthReport>(
                      `${base}/data-sources/${sourceId}/health`,
                      undefined,
                      { admin: true },
                    ),
                  'Health measured.',
                )
                if (ok) await reloadAll()
              }}
            >
              {healthCheck.pending ? 'Checking…' : 'Check health'}
            </button>
            <button
              className="btn-primary btn-xs"
              disabled={profile.pending || inScope.length === 0}
              title={
                inScope.length === 0
                  ? 'No table is in analytical scope. Select tables under Data scope first.'
                  : 'Read statistics from the source. This opens a connection.'
              }
              onClick={async () => {
                const result = await profile.run<SourceProfileResult>(() =>
                  api.post<SourceProfileResult>(
                    `${base}/data-sources/${sourceId}/profile`,
                    undefined,
                    { admin: true },
                  ),
                )
                if (result) {
                  setLastProfile(result)
                  await reloadAll()
                }
              }}
            >
              {profile.pending ? 'Profiling…' : 'Profile source'}
            </button>
          </div>
        }
      >
        <div className="space-y-3">
          {healthCheck.error && <Alert>{healthCheck.error}</Alert>}
          {profile.error && <Alert>{profile.error}</Alert>}
          {healthCheck.message && (
            <Alert tone="success" onDismiss={healthCheck.reset}>
              {healthCheck.message}
            </Alert>
          )}
          {lastProfile && (
            <Alert tone="success" onDismiss={() => setLastProfile(null)}>
              <div className="text-xs leading-relaxed">
                Profiled {lastProfile.profiled_table_count} table
                {lastProfile.profiled_table_count === 1 ? '' : 's'}
                {lastProfile.withheld_column_count > 0 && (
                  <>
                    {' · '}
                    {lastProfile.withheld_column_count} column
                    {lastProfile.withheld_column_count === 1 ? '' : 's'} withheld by your
                    entitlement and never read
                  </>
                )}
                . Health is now {lastProfile.health.status}.
                <div className="mt-1 opacity-80">{lastProfile.note}</div>
              </div>
            </Alert>
          )}

          <dl>
            <DefinitionRow term="Description">{record.description || '—'}</DefinitionRow>
            <DefinitionRow term="Grain">
              {record.grain ?? <span className="text-slate-600">not measured</span>}
            </DefinitionRow>
            <DefinitionRow term="Refresh cadence">
              {titleCase(record.refresh_frequency)}
              <span className="ml-2 text-xs text-slate-600">
                declared, not observed — freshness is judged against it
              </span>
            </DefinitionRow>
            <DefinitionRow term="Last refresh">
              {record.last_refresh_at ? formatDateTime(record.last_refresh_at) : '—'}
            </DefinitionRow>
            <DefinitionRow term="Coverage">
              <CoverageRange start={record.coverage_start} end={record.coverage_end} />
            </DefinitionRow>
            <DefinitionRow term="Timezone">{record.timezone}</DefinitionRow>
            <DefinitionRow term="Business calendar">
              {record.business_calendar_id ? (
                <span className="mono text-xs">{record.business_calendar_id}</span>
              ) : (
                <span className="text-slate-600">company default</span>
              )}
            </DefinitionRow>
            <DefinitionRow term="Tables">
              {record.discovered_table_count} discovered · {record.selected_table_count} in
              analytical scope
            </DefinitionRow>
            <DefinitionRow term="Last discovery">
              {record.last_discovered_at ? formatRelative(record.last_discovered_at) : 'never'}
            </DefinitionRow>
            <DefinitionRow term="Known limitations">
              {record.known_limitations || <span className="text-slate-600">none recorded</span>}
            </DefinitionRow>
          </dl>
        </div>
      </Panel>

      {/* ------------------------------------------------------------ health */}
      <SourceHealthPanel report={health.data} loading={health.loading} error={health.error} />

      {/* ------------------------------------------------------------ tables */}
      <Panel title="Tables" bodyClassName="">
        {tables.loading && !tables.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : tables.error ? (
          <div className="p-4">
            <Alert>{tables.error}</Alert>
          </div>
        ) : !tables.data?.length ? (
          <EmptyState
            title="No tables registered"
            description="Run Discover tables on the source list to read its table and column metadata."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-ink-800 bg-ink-850">
                  <th className="table-head">Table</th>
                  <th className="table-head">Scope</th>
                  <th className="table-head">Grain</th>
                  <th className="table-head">Rows</th>
                  <th className="table-head">Quality</th>
                  <th className="table-head">Freshness</th>
                  <th className="table-head">Profiled</th>
                  <th className="table-head" />
                </tr>
              </thead>
              <tbody>
                {tables.data.map((table) => (
                  <tr key={table.id} className="border-b border-ink-800 last:border-0">
                    <td className="table-cell">
                      <span className="mono text-slate-200">{table.qualified_name}</span>
                      {table.display_name && (
                        <span className="ml-2 text-xs text-slate-500">{table.display_name}</span>
                      )}
                    </td>
                    <td className="table-cell">
                      {table.selected ? (
                        <span className="chip">in scope</span>
                      ) : (
                        <span className="text-[11px] text-slate-600">not selected</span>
                      )}
                    </td>
                    <td className="table-cell">
                      <GrainCell table={table} />
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {formatCompact(table.approx_row_count)}
                    </td>
                    <td className="table-cell">
                      <StatusBadge status={table.quality_status} />
                    </td>
                    <td className="table-cell">
                      <StatusBadge status={table.freshness_status} />
                    </td>
                    <td className="table-cell text-[11px] text-slate-500">
                      {table.profiled_at ? formatRelative(table.profiled_at) : 'never'}
                    </td>
                    <td className="table-cell text-right">
                      <button
                        className="btn-ghost btn-xs"
                        onClick={() =>
                          setOpenTable(openTable === table.id ? null : table.id)
                        }
                      >
                        {openTable === table.id ? 'Hide' : 'Review'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {openTable && (
        <TableGovernance
          base={base}
          tableId={openTable}
          onClose={() => setOpenTable(null)}
          onChanged={reloadAll}
        />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------- health */

function SourceHealthPanel({
  report,
  loading,
  error,
}: {
  report: SourceHealthReport | null
  loading: boolean
  error: string | null
}) {
  if (loading && !report) {
    return (
      <Panel title="Health">
        <Spinner />
      </Panel>
    )
  }
  if (error) {
    return (
      <Panel title="Health">
        <Alert>{error}</Alert>
      </Panel>
    )
  }
  if (!report) return null

  // A read projects stored observations. When the newest measurement predates the
  // rollup, saying so is the whole point — otherwise the verdict looks live.
  const projected =
    report.measured_at !== null && report.measured_at !== undefined
      ? `measurements from ${formatRelative(report.measured_at)}`
      : 'no measurement has been taken yet'

  return (
    <Panel title="Health">
      <div className="space-y-4">
        <div className="grid gap-5 sm:grid-cols-4">
          <Metric label="Status" value={<StatusBadge status={report.status} />} hint={projected} />
          <Metric
            label="Completeness"
            value={
              report.completeness_pct !== null && report.completeness_pct !== undefined
                ? `${report.completeness_pct}%`
                : '—'
            }
            hint="mean across tables in scope"
          />
          <Metric
            label="Quality"
            value={
              report.quality_score !== null && report.quality_score !== undefined
                ? String(report.quality_score)
                : '—'
            }
            hint="the weakest table sets this"
          />
          <Metric
            label="Tables"
            value={`${report.fresh_tables} fresh`}
            hint={`${report.stale_tables} stale · ${report.unknown_tables} unknown · ${report.unprofiled_tables} unprofiled`}
          />
        </div>

        <Alert tone={report.status === 'HEALTHY' ? 'success' : 'warn'}>
          <div className="text-xs leading-relaxed">{report.reason}</div>
        </Alert>

        <dl>
          <DefinitionRow term="Coverage">
            <CoverageRange start={report.coverage_start} end={report.coverage_end} />
            <span className="ml-2 text-[11px] text-slate-600">
              intersection across tables — an analysis is only trustworthy where every table has
              data
            </span>
          </DefinitionRow>
          <DefinitionRow term="Verdict computed">{formatDateTime(report.checked_at)}</DefinitionRow>
          <DefinitionRow term="Newest measurement">
            {report.measured_at ? formatDateTime(report.measured_at) : 'never measured'}
          </DefinitionRow>
          {report.known_limitations && (
            <DefinitionRow term="Known limitations">{report.known_limitations}</DefinitionRow>
          )}
        </dl>

        {report.tables.length > 0 && (
          <div className="overflow-x-auto rounded-md border border-ink-800">
            <table className="w-full">
              <thead>
                <tr className="border-b border-ink-800 bg-ink-850">
                  <th className="table-head">Table</th>
                  <th className="table-head">Time column</th>
                  <th className="table-head">Freshness</th>
                  <th className="table-head">Lag</th>
                  <th className="table-head">Rows</th>
                  <th className="table-head">Complete</th>
                  <th className="table-head">Quality</th>
                </tr>
              </thead>
              <tbody>
                {report.tables.map((line) => (
                  <tr key={line.source_table_id} className="border-b border-ink-800 last:border-0">
                    <td className="table-cell mono text-slate-300">{line.table}</td>
                    <td className="table-cell mono text-[11px] text-slate-500">
                      {line.time_column ?? '—'}
                    </td>
                    <td className="table-cell">
                      <StatusBadge status={line.freshness_status} />
                    </td>
                    <td className="table-cell text-[11px] text-slate-500">
                      {formatDuration(line.lag_seconds)}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {formatCompact(line.row_count)}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {line.completeness_pct !== null && line.completeness_pct !== undefined
                        ? `${line.completeness_pct}%`
                        : '—'}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {line.quality_score !== null && line.quality_score !== undefined
                        ? line.quality_score
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="text-[11px] leading-relaxed text-slate-600">
          Computed by arithmetic over recorded measurements — never by a model. Freshness outranks
          quality: a source that stopped loading makes its own quality figures out of date.
        </p>
      </div>
    </Panel>
  )
}

/* ------------------------------------------------------- table review screen */

function TableGovernance({
  base,
  tableId,
  onClose,
  onChanged,
}: {
  base: string
  tableId: string
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const detail = useResource<GovernedTableDetail>(
    () => api.get(`${base}/tables/${tableId}`, { admin: true }),
    [tableId],
  )
  const review = useAction()
  const [displayName, setDisplayName] = useState<string | null>(null)
  const [description, setDescription] = useState<string | null>(null)

  const table = detail.data
  const patch = async (body: Record<string, unknown>, message: string) => {
    const ok = await review.run(
      () => api.patch<GovernedTableDetail>(`${base}/tables/${tableId}`, body, { admin: true }),
      message,
    )
    if (ok) {
      await detail.reload()
      await onChanged()
    }
  }

  return (
    <Panel
      title={table ? `Review · ${table.qualified_name}` : 'Review table'}
      actions={
        <button className="btn-ghost btn-xs" onClick={onClose}>
          Close
        </button>
      }
    >
      {detail.loading && !table ? (
        <Spinner />
      ) : detail.error ? (
        <Alert>{detail.error}</Alert>
      ) : table ? (
        <div className="space-y-5">
          {review.error && <Alert>{review.error}</Alert>}
          {review.message && (
            <Alert tone="success" onDismiss={review.reset}>
              {review.message}
            </Alert>
          )}

          <div className="grid gap-5 sm:grid-cols-4">
            <Metric label="Rows" value={formatCompact(table.row_count)} />
            <Metric
              label="Completeness"
              value={
                table.completeness_pct !== null && table.completeness_pct !== undefined
                  ? `${table.completeness_pct}%`
                  : '—'
              }
            />
            <Metric
              label="Quality"
              value={<StatusBadge status={table.quality_status} />}
              hint={
                table.quality_score !== null && table.quality_score !== undefined
                  ? `score ${table.quality_score}`
                  : undefined
              }
            />
            <Metric
              label="Withheld columns"
              value={formatCompact(table.withheld_column_count)}
              hint="not readable by you"
              tone={table.withheld_column_count ? 'default' : 'muted'}
            />
          </div>

          {table.quality_warnings.length > 0 && (
            <Alert tone="warn">
              <div className="mb-1 font-medium">Quality warnings — recorded, not repaired</div>
              <ul className="space-y-0.5 text-xs">
                {table.quality_warnings.map((warning, index) => (
                  <li key={index}>· {warning}</li>
                ))}
              </ul>
            </Alert>
          )}

          {/* ------------------------------------------------- naming */}
          <section className="space-y-3">
            <h3 className="panel-title">Business naming</h3>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Display name" hint="What the business calls this table.">
                <input
                  className="field"
                  value={displayName ?? table.display_name ?? ''}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder={table.table_name}
                />
              </Field>
              <Field label="Description" hint="What it holds, in your own words.">
                <input
                  className="field"
                  value={description ?? table.description ?? ''}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </Field>
            </div>
            <button
              className="btn-ghost btn-xs"
              disabled={review.pending || (displayName === null && description === null)}
              onClick={async () => {
                const body: Record<string, unknown> = {}
                if (displayName !== null) body.display_name = displayName
                if (description !== null) body.description = description
                await patch(body, 'Naming saved.')
                setDisplayName(null)
                setDescription(null)
              }}
            >
              {review.pending ? 'Saving…' : 'Save naming'}
            </button>
          </section>

          {/* ---------------------------------------------- candidates */}
          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="panel-title">Governed candidates</h3>
              <StatusChip status={table.candidates_status} />
            </div>
            <p className="text-[11px] leading-relaxed text-slate-600">
              Proposed deterministically from structure and measured cardinality. Lists rather than
              single answers, because collapsing them would hide the ambiguity you are here to
              resolve. Confirming freezes them against every later profiling pass.
            </p>
            <dl>
              <DefinitionRow term="Row identifier">
                <ChipList values={table.primary_identifier_candidates} />
              </DefinitionRow>
              <DefinitionRow term="Time axis">
                <ChipList values={table.time_field_candidates} />
              </DefinitionRow>
              <DefinitionRow term="Company scope">
                <ChipList values={table.company_field_candidates} />
              </DefinitionRow>
            </dl>
            <div className="flex gap-2">
              {table.candidates_status === 'CONFIRMED' ? (
                <button
                  className="btn-ghost btn-xs"
                  disabled={review.pending}
                  onClick={() =>
                    patch({ confirm_candidates: false }, 'Confirmation withdrawn.')
                  }
                >
                  Withdraw confirmation
                </button>
              ) : (
                <button
                  className="btn-primary btn-xs"
                  disabled={review.pending}
                  onClick={() => patch({ confirm_candidates: true }, 'Candidates confirmed.')}
                >
                  Confirm candidates
                </button>
              )}
            </div>
          </section>

          {/* --------------------------------------------------- grain */}
          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="panel-title">Grain — what one row represents</h3>
              <StatusChip status={table.grain_status} />
            </div>
            <dl>
              <DefinitionRow term="Confirmed">
                {table.confirmed_grain ?? <span className="text-slate-600">not confirmed</span>}
              </DefinitionRow>
              <DefinitionRow term="Declared">
                {table.declared_grain ?? <span className="text-slate-600">none</span>}
              </DefinitionRow>
              <DefinitionRow term="Inferred">
                {table.inferred_grain ?? <span className="text-slate-600">not detected</span>}
              </DefinitionRow>
              <DefinitionRow term="Columns">
                <ChipList values={table.grain_columns} />
              </DefinitionRow>
              <DefinitionRow term="Method">
                {table.grain_method ? <span className="chip">{table.grain_method}</span> : '—'}
                {table.grain_confidence !== null && table.grain_confidence !== undefined && (
                  <span className="ml-2 text-xs text-slate-500">
                    confidence {table.grain_confidence.toFixed(2)}
                  </span>
                )}
              </DefinitionRow>
              {table.grain_confirmed_at && (
                <DefinitionRow term="Confirmed at">
                  {formatDateTime(table.grain_confirmed_at)}
                </DefinitionRow>
              )}
            </dl>
            <div className="flex gap-2">
              {table.grain_status === 'CONFIRMED' ? (
                <button
                  className="btn-ghost btn-xs"
                  disabled={review.pending}
                  onClick={() => patch({ confirm_grain: false }, 'Grain confirmation withdrawn.')}
                >
                  Withdraw confirmation
                </button>
              ) : (
                <button
                  className="btn-primary btn-xs"
                  disabled={review.pending || !table.effective_grain}
                  title={
                    table.effective_grain
                      ? 'Freeze this grain as the business truth'
                      : 'Nothing to confirm — run grain detection first'
                  }
                  onClick={() => patch({ confirm_grain: true }, 'Grain confirmed.')}
                >
                  Confirm grain
                </button>
              )}
            </div>
          </section>

          {/* ------------------------------------------------- columns */}
          <section className="space-y-2">
            <h3 className="panel-title">Columns</h3>
            <div className="overflow-x-auto rounded-md border border-ink-800">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-ink-800 bg-ink-850">
                    <th className="table-head">Column</th>
                    <th className="table-head">Type</th>
                    <th className="table-head">Semantic</th>
                    <th className="table-head">Role</th>
                    <th className="table-head">Class</th>
                    <th className="table-head" />
                  </tr>
                </thead>
                <tbody>
                  {table.columns.map((column) => (
                    <ColumnRow
                      key={column.id}
                      base={base}
                      column={column}
                      onChanged={async () => {
                        await detail.reload()
                        await onChanged()
                      }}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            {table.withheld_column_count > 0 && (
              <p className="text-[11px] leading-relaxed text-slate-600">
                Withheld columns were never read. Entitlement is applied before profiling, not by
                filtering results afterwards.
              </p>
            )}
          </section>
        </div>
      ) : null}
    </Panel>
  )
}

/* --------------------------------------------------------------- column row */

function ColumnRow({
  base,
  column,
  onChanged,
}: {
  base: string
  column: GovernedColumn
  onChanged: () => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [role, setRole] = useState(column.effective_role)
  const save = useAction()

  const send = async (body: Record<string, unknown>) => {
    const ok = await save.run(() =>
      api.patch<GovernedColumn>(`${base}/columns/${column.id}/role`, body, { admin: true }),
    )
    if (ok) {
      setEditing(false)
      await onChanged()
    }
  }

  return (
    <>
      <tr className="border-b border-ink-800 last:border-0">
        <td className="table-cell">
          <span className="mono text-slate-200">{column.column_name}</span>
          {column.is_primary_key && <span className="ml-1.5 chip">PK</span>}
          {column.is_foreign_key && <span className="ml-1.5 chip">FK</span>}
          {column.description && (
            <div className="mt-0.5 text-[11px] text-slate-600">{column.description}</div>
          )}
        </td>
        <td className="table-cell mono text-[11px] text-slate-500">{column.data_type}</td>
        <td className="table-cell text-slate-400">{titleCase(column.semantic_type)}</td>
        <td className="table-cell">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="chip">{titleCase(column.effective_role)}</span>
            <StatusChip status={column.role_status} />
          </div>
          {column.confirmed_role && column.confirmed_role !== column.candidate_role && (
            <div className="mt-0.5 text-[11px] text-slate-600">
              proposed {titleCase(column.candidate_role)}
            </div>
          )}
        </td>
        <td className="table-cell">
          {column.is_pii ? (
            <span className="chip text-rose-300">PII</span>
          ) : (
            <span className="text-[11px] text-slate-600">{titleCase(column.classification)}</span>
          )}
          {!column.readable && (
            <div className="mt-0.5 text-[11px] italic text-slate-600">
              withheld — {column.withheld_reason ?? 'not readable'}
            </div>
          )}
        </td>
        <td className="table-cell text-right">
          <button
            className="btn-ghost btn-xs"
            onClick={() => {
              setRole(column.effective_role)
              setEditing(!editing)
            }}
          >
            {editing ? 'Cancel' : 'Set role'}
          </button>
        </td>
      </tr>
      {editing && (
        <tr className="border-b border-ink-800 bg-ink-850 last:border-0">
          <td className="table-cell" colSpan={6}>
            {save.error && (
              <div className="mb-2">
                <Alert>{save.error}</Alert>
              </div>
            )}
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Business role">
                <select
                  className="field"
                  value={role}
                  onChange={(event) => setRole(event.target.value)}
                >
                  {ROLE_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {titleCase(option)}
                    </option>
                  ))}
                </select>
              </Field>
              <button
                className="btn-primary btn-xs"
                disabled={save.pending}
                onClick={() => send({ confirmed_role: role })}
              >
                {save.pending ? 'Saving…' : 'Confirm role'}
              </button>
              {column.confirmed_role && (
                <button
                  className="btn-ghost btn-xs"
                  disabled={save.pending}
                  onClick={() => send({ clear_confirmed_role: true })}
                  title="Hand this column back to the profiler's proposal"
                >
                  Clear confirmation
                </button>
              )}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
              Role is what the column <em>means</em>. It is deliberately separate from
              classification, which decides who may read it — one change must never quietly widen
              access.
            </p>
          </td>
        </tr>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ helpers */

function StatusChip({ status }: { status?: string | null }) {
  if (!status) return null
  // CONFIRMED is the only state a person put there, so it is the only one that
  // reads as settled. Everything else is visibly provisional.
  const settled = status === 'CONFIRMED'
  return (
    <span
      className={`chip ${settled ? 'text-emerald-300' : 'text-amber-300'}`}
      title={
        settled
          ? 'Confirmed by a reviewer — automated passes will not overwrite it'
          : 'Proposed or declared — not yet confirmed by a reviewer'
      }
    >
      {titleCase(status)}
    </span>
  )
}

function ChipList({ values }: { values: string[] }) {
  if (!values.length) return <span className="text-slate-600">none</span>
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((value) => (
        <span key={value} className="chip mono">
          {value}
        </span>
      ))}
    </div>
  )
}

function GrainCell({ table }: { table: GovernedTable }) {
  if (!table.effective_grain) return <span className="text-[11px] text-slate-600">unknown</span>
  return (
    <div className="space-y-0.5">
      <div className="text-xs text-slate-400">{table.effective_grain}</div>
      <StatusChip status={table.grain_status} />
    </div>
  )
}

function CoverageRange({
  start,
  end,
}: {
  start?: string | null
  end?: string | null
}) {
  if (!start && !end) return <span className="text-slate-600">not measured</span>
  return (
    <span className="tabular-nums text-xs text-slate-400">
      {start ? formatDateTime(start) : '?'} → {end ? formatDateTime(end) : '?'}
    </span>
  )
}
