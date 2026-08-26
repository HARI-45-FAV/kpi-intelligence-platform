/**
 * Data sources, analytical scope and profiling results.
 *
 * The order on this page is the governance order: connect a source, discover
 * what is in it, explicitly choose what the platform may analyse, then profile
 * only that. Discovery deliberately grants no analytical access on its own.
 */

import { useMemo, useState, type ReactNode } from 'react'
import { api } from '../../api/client'
import type {
  ConnectorDescriptor,
  ConnectionTest,
  DataSource,
  ReconciliationPair,
  RelationshipView,
  TableDetail,
  TableSummary,
} from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import {
  formatCompact,
  formatDuration,
  formatNumber,
  formatPercent,
  formatRelative,
  titleCase,
} from '../../components/format'
import {
  Alert,
  DefinitionRow,
  Drawer,
  EmptyState,
  Field,
  Metric,
  Modal,
  Panel,
  Spinner,
  StatusBadge,
} from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'

const REFRESH_OPTIONS = [
  ['REALTIME', 'Real time'],
  ['MINUTES_15', 'Every 15 minutes'],
  ['HOURLY', 'Hourly'],
  ['HOURS_2', 'Every 2 hours'],
  ['DAILY', 'Daily'],
  ['WEEKLY', 'Weekly'],
  ['UNKNOWN', 'Unknown'],
] as const

export default function SourcesPanel() {
  const { companyId } = useAuth()
  const base = `/companies/${companyId}`

  const sources = useResource<DataSource[]>(() => api.get(`${base}/data-sources`, { admin: true }), [companyId])
  const tables = useResource<TableSummary[]>(() => api.get(`${base}/tables`, { admin: true }), [companyId])
  const relationships = useResource<{ relationships: RelationshipView[] }>(
    () => api.get(`${base}/analysis/relationships`, { admin: true }),
    [companyId],
  )
  const reconciliation = useResource<{ pairs: ReconciliationPair[]; note: string }>(
    () => api.get(`${base}/analysis/reconciliation`, { admin: true }),
    [companyId],
  )

  const [addOpen, setAddOpen] = useState(false)
  const [openTable, setOpenTable] = useState<string | null>(null)
  const analysis = useAction()

  const reloadAll = async () => {
    await Promise.all([
      sources.reload(),
      tables.reload(),
      relationships.reload(),
      reconciliation.reload(),
    ])
  }

  const selectedCount = tables.data?.filter((t) => t.selected).length ?? 0
  const profiledCount = tables.data?.filter((t) => t.profiled_at).length ?? 0

  return (
    <div className="space-y-5">
      <Panel
        title="Data sources"
        actions={
          <button className="btn-primary btn-xs" onClick={() => setAddOpen(true)}>
            + Add source
          </button>
        }
        bodyClassName=""
      >
        {sources.loading && !sources.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : sources.error ? (
          <div className="p-4">
            <Alert>{sources.error}</Alert>
          </div>
        ) : !sources.data?.length ? (
          <EmptyState
            title="No data source registered"
            description="Connect your Supabase project. Credentials are encrypted at rest and never returned by the API."
            action={
              <button className="btn-primary btn-xs" onClick={() => setAddOpen(true)}>
                + Add source
              </button>
            }
          />
        ) : (
          <div>
            {sources.data.map((source) => (
              <SourceRow key={source.id} source={source} base={base} onChanged={reloadAll} />
            ))}
          </div>
        )}
      </Panel>

      {(tables.data?.length ?? 0) > 0 && (
        <>
          <DataScope base={base} tables={tables.data ?? []} onSaved={reloadAll} />

          <Panel
            title="Analysis"
            actions={
              <button
                className="btn-primary btn-xs"
                disabled={analysis.pending || selectedCount === 0}
                onClick={async () => {
                  const ok = await analysis.run(
                    () => api.post(`${base}/analysis/run`, {}, { admin: true }),
                    'Analysis complete.',
                  )
                  if (ok) await reloadAll()
                }}
              >
                {analysis.pending ? 'Running…' : 'Run full analysis'}
              </button>
            }
          >
            <div className="space-y-3">
              <p className="text-xs leading-relaxed text-slate-500">
                Profiles every table in scope, then detects grain, relationships, join safety,
                freshness and cross-source compatibility. All of it is aggregate SQL pushed down to
                the source — no table is streamed into the application.
              </p>
              {analysis.error && <Alert>{analysis.error}</Alert>}
              {analysis.message && (
                <Alert tone="success" onDismiss={analysis.reset}>
                  {analysis.message}
                </Alert>
              )}
              <div className="grid gap-6 sm:grid-cols-4">
                <Metric label="In scope" value={formatCompact(selectedCount)} hint="tables" />
                <Metric label="Profiled" value={formatCompact(profiledCount)} hint="tables" />
                <Metric
                  label="Relationships"
                  value={formatCompact(relationships.data?.relationships.length ?? 0)}
                  hint="declared + inferred"
                />
                <Metric
                  label="Source pairs"
                  value={formatCompact(reconciliation.data?.pairs.length ?? 0)}
                  hint="reconciliation checked"
                />
              </div>
            </div>
          </Panel>

          <Panel title="Tables in scope" bodyClassName="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-ink-700">
                  <th className="table-head">Table</th>
                  <th className="table-head">Rows</th>
                  <th className="table-head">Cols</th>
                  <th className="table-head">Grain</th>
                  <th className="table-head">Quality</th>
                  <th className="table-head">Freshness</th>
                  <th className="table-head">Profiled</th>
                </tr>
              </thead>
              <tbody>
                {(tables.data ?? [])
                  .filter((t) => t.selected)
                  .map((table) => (
                    <tr
                      key={table.id}
                      className="cursor-pointer border-b border-ink-800 last:border-0 hover:bg-ink-850"
                      onClick={() => setOpenTable(table.id)}
                    >
                      <td className="table-cell">
                        <span className="font-medium text-slate-200">{table.table_name}</span>
                        <span className="ml-2 text-[11px] text-slate-600">
                          {table.data_source_name}
                        </span>
                      </td>
                      <td className="table-cell tabular-nums text-slate-400">
                        {formatCompact(table.approx_row_count)}
                      </td>
                      <td className="table-cell tabular-nums text-slate-400">
                        {table.column_count ?? '—'}
                      </td>
                      <td className="table-cell max-w-[18rem] truncate text-slate-400">
                        {table.inferred_grain ?? (
                          <span className="text-slate-600">not detected</span>
                        )}
                      </td>
                      <td className="table-cell">
                        <StatusBadge status={table.quality_status} />
                      </td>
                      <td className="table-cell">
                        <StatusBadge status={table.freshness_status} />
                      </td>
                      <td className="table-cell text-[11px] text-slate-600">
                        {table.profiled_at ? formatRelative(table.profiled_at) : 'never'}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </Panel>

          {(relationships.data?.relationships.length ?? 0) > 0 && (
            <RelationshipsPanel relationships={relationships.data!.relationships} />
          )}

          {(reconciliation.data?.pairs.length ?? 0) > 0 && (
            <ReconciliationPanel
              pairs={reconciliation.data!.pairs}
              note={reconciliation.data!.note}
            />
          )}
        </>
      )}

      {addOpen && (
        <AddSourceModal
          base={base}
          onClose={() => setAddOpen(false)}
          onCreated={async () => {
            setAddOpen(false)
            await reloadAll()
          }}
        />
      )}

      {openTable && (
        <TableDrawer base={base} tableId={openTable} onClose={() => setOpenTable(null)} />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ source row */

function SourceRow({
  source,
  base,
  onChanged,
}: {
  source: DataSource
  base: string
  onChanged: () => Promise<void>
}) {
  const [test, setTest] = useState<ConnectionTest | null>(null)
  const testAction = useAction()
  const discoverAction = useAction()

  return (
    <div className="border-b border-ink-800 px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-slate-200">{source.name}</span>
            <span className="chip">{titleCase(source.source_type)}</span>
            <StatusBadge status={source.connection_status} />
            {source.has_credentials && (
              <span className="chip" title="Credentials stored encrypted; never returned">
                🔑 stored
              </span>
            )}
          </div>
          <div className="mt-1 text-[11px] text-slate-600">
            {[source.host, source.database_name, source.schema_name].filter(Boolean).join(' · ') ||
              'local'}
            {' · '}
            {titleCase(source.refresh_frequency)} refresh · {source.table_count} tables discovered,{' '}
            {source.selected_table_count} in scope
          </div>
        </div>

        <div className="flex shrink-0 gap-2">
          <button
            className="btn-ghost btn-xs"
            disabled={testAction.pending}
            onClick={async () => {
              const result = await testAction.run(() =>
                api.post<ConnectionTest>(
                  `${base}/data-sources/${source.id}/test`,
                  {},
                  { admin: true },
                ),
              )
              if (result) {
                setTest(result)
                await onChanged()
              }
            }}
          >
            {testAction.pending ? 'Testing…' : 'Test connection'}
          </button>
          <button
            className="btn-ghost btn-xs"
            disabled={discoverAction.pending || source.connection_status !== 'CONNECTED'}
            title={
              source.connection_status !== 'CONNECTED'
                ? 'Test the connection first'
                : 'Read table and column metadata'
            }
            onClick={async () => {
              const ok = await discoverAction.run(
                () =>
                  api.post(`${base}/data-sources/${source.id}/discover`, {}, { admin: true }),
                'Tables discovered.',
              )
              if (ok) await onChanged()
            }}
          >
            {discoverAction.pending ? 'Discovering…' : 'Discover tables'}
          </button>
        </div>
      </div>

      {(testAction.error || discoverAction.error) && (
        <div className="mt-3">
          <Alert>{testAction.error ?? discoverAction.error}</Alert>
        </div>
      )}
      {discoverAction.message && (
        <div className="mt-3">
          <Alert tone="success" onDismiss={discoverAction.reset}>
            {discoverAction.message}
          </Alert>
        </div>
      )}

      {test && (
        <div className="mt-3 rounded-md border border-ink-700 bg-ink-850 p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-medium text-slate-300">{test.message}</span>
            <button
              onClick={() => setTest(null)}
              className="text-xs text-slate-600 hover:text-slate-400"
            >
              dismiss
            </button>
          </div>
          <ul className="space-y-1">
            {test.checks.map((check, index) => (
              <li key={index} className="flex items-start gap-2 text-xs">
                <span className={check.ok ? 'text-emerald-400' : 'text-rose-400'}>
                  {check.ok ? '✓' : '✕'}
                </span>
                <span className="text-slate-300">{check.check}</span>
                {check.detail && <span className="text-slate-600">— {check.detail}</span>}
              </li>
            ))}
          </ul>
          {test.duration_ms !== null && test.duration_ms !== undefined && (
            <div className="mt-2 text-[11px] text-slate-600">
              Completed in {test.duration_ms} ms
              {test.server_version ? ` · server ${test.server_version}` : ''}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------- add source form */

function AddSourceModal({
  base,
  onClose,
  onCreated,
}: {
  base: string
  onClose: () => void
  onCreated: () => Promise<void>
}) {
  const connectors = useResource<{ connectors: ConnectorDescriptor[] }>(
    () => api.get('/connectors'),
    [],
  )
  const create = useAction()

  const [sourceType, setSourceType] = useState('SUPABASE')
  const [useUri, setUseUri] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [name, setName] = useState('')
  const [refresh, setRefresh] = useState('DAILY')

  const descriptor = connectors.data?.connectors.find((c) => c.source_type === sourceType)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const body: Record<string, unknown> = {
      name,
      source_type: sourceType,
      refresh_frequency: refresh,
    }
    if (useUri) {
      body.connection_uri = values.connection_uri
      if (values.schema_name) body.schema_name = values.schema_name
    } else {
      for (const field of descriptor?.fields ?? []) {
        const value = values[field.name]
        if (value) body[field.name] = field.kind === 'number' ? Number(value) : value
      }
    }
    const created = await create.run(() =>
      api.post<DataSource>(`${base}/data-sources`, body, { admin: true }),
    )
    if (created) await onCreated()
  }

  return (
    <Modal open onClose={onClose} title="Add data source" width="max-w-lg">
      {connectors.loading ? (
        <Spinner />
      ) : (
        <form onSubmit={submit} className="space-y-4">
          {create.error && <Alert>{create.error}</Alert>}

          <Field label="Source name" required hint="How this source appears across the platform.">
            <input
              className="field"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="NovaMart Commerce"
              required
            />
          </Field>

          <Field label="Type" required>
            <select
              className="field"
              value={sourceType}
              onChange={(e) => {
                setSourceType(e.target.value)
                setValues({})
                setUseUri(false)
              }}
            >
              {connectors.data?.connectors.map((connector) => (
                <option key={connector.source_type} value={connector.source_type}>
                  {connector.label}
                  {connector.implemented ? '' : ' — not implemented'}
                </option>
              ))}
            </select>
          </Field>

          {descriptor && !descriptor.implemented && (
            <Alert tone="warn">
              {descriptor.notes} The interface is defined so the platform stays warehouse-ready, but
              this connector cannot read data yet.
            </Alert>
          )}

          {descriptor?.accepts_connection_uri && (
            <label className="flex items-center gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={useUri}
                onChange={(e) => setUseUri(e.target.checked)}
                className="accent-accent"
              />
              Paste a connection string instead of individual fields
            </label>
          )}

          {useUri ? (
            <>
              <Field
                label="Connection string"
                required
                hint="Supabase → Project Settings → Database → Connection string (URI)."
              >
                <input
                  className="field mono"
                  value={values.connection_uri ?? ''}
                  onChange={(e) => setValues((p) => ({ ...p, connection_uri: e.target.value }))}
                  placeholder="postgresql://postgres:••••@db.<ref>.supabase.co:5432/postgres"
                  required
                />
              </Field>
              <Field label="Schema">
                <input
                  className="field"
                  value={values.schema_name ?? ''}
                  onChange={(e) => setValues((p) => ({ ...p, schema_name: e.target.value }))}
                  placeholder="public"
                />
              </Field>
            </>
          ) : (
            (descriptor?.fields ?? []).map((field) => (
              <Field
                key={field.name}
                label={field.label}
                required={field.required}
                hint={field.help_text || undefined}
              >
                <input
                  className={`field ${field.name === 'project_url' ? 'mono' : ''}`}
                  type={field.kind === 'password' ? 'password' : field.kind === 'number' ? 'number' : 'text'}
                  value={values[field.name] ?? ''}
                  onChange={(e) => setValues((p) => ({ ...p, [field.name]: e.target.value }))}
                  placeholder={field.placeholder}
                  required={field.required}
                  autoComplete="off"
                />
              </Field>
            ))
          )}

          <Field
            label="Expected refresh cadence"
            hint="Used to judge whether this source is fresh or stale. Recorded, never assumed."
          >
            <select className="field" value={refresh} onChange={(e) => setRefresh(e.target.value)}>
              {REFRESH_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </Field>

          <div className="flex justify-end gap-2 pt-1">
            <button type="button" className="btn-ghost btn-xs" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn-primary btn-xs" disabled={create.pending}>
              {create.pending ? 'Saving…' : 'Add source'}
            </button>
          </div>

          <p className="border-t border-ink-800 pt-3 text-[11px] leading-relaxed text-slate-600">
            Credentials are encrypted with the application key before storage and are never included
            in any API response.
          </p>
        </form>
      )}
    </Modal>
  )
}

/* ---------------------------------------------------------------- data scope */

function DataScope({
  base,
  tables,
  onSaved,
}: {
  base: string
  tables: TableSummary[]
  onSaved: () => Promise<void>
}) {
  const save = useAction()
  const [draft, setDraft] = useState<Record<string, { enabled: boolean; time?: string }>>(() =>
    Object.fromEntries(
      tables.map((t) => [
        t.id,
        { enabled: t.selected, time: t.primary_time_column ?? undefined },
      ]),
    ),
  )

  const grouped = useMemo(() => {
    const map = new Map<string, TableSummary[]>()
    for (const table of tables) {
      const list = map.get(table.data_source_name) ?? []
      list.push(table)
      map.set(table.data_source_name, list)
    }
    return [...map.entries()]
  }, [tables])

  const enabledCount = Object.values(draft).filter((d) => d.enabled).length

  const submit = async () => {
    const ok = await save.run(
      () =>
        api.put(
          `${base}/data-scope`,
          {
            replace: true,
            tables: tables
              .filter((t) => draft[t.id]?.enabled)
              .map((t) => ({
                source_table_id: t.id,
                enabled: true,
                primary_time_column: draft[t.id]?.time || null,
              })),
          },
          { admin: true },
        ),
      'Data scope saved.',
    )
    if (ok) await onSaved()
  }

  return (
    <Panel
      title={`Data scope — ${enabledCount} of ${tables.length} tables enabled`}
      actions={
        <button className="btn-primary btn-xs" disabled={save.pending} onClick={submit}>
          {save.pending ? 'Saving…' : 'Save data scope'}
        </button>
      }
    >
      <div className="space-y-4">
        <p className="text-xs leading-relaxed text-slate-500">
          Only enabled tables enter profiling, the catalog and KPI registration. Nothing is analysed
          because it merely exists in the database. Set a primary time column on any table that is a
          time series — it drives freshness and cross-source reconciliation.
        </p>
        {save.error && <Alert>{save.error}</Alert>}
        {save.message && (
          <Alert tone="success" onDismiss={save.reset}>
            {save.message}
          </Alert>
        )}

        {grouped.map(([sourceName, group]) => (
          <div key={sourceName}>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              {sourceName}
            </div>
            <div className="space-y-1">
              {group.map((table) => {
                const state = draft[table.id] ?? { enabled: false }
                return (
                  <div
                    key={table.id}
                    className="flex flex-wrap items-center gap-3 rounded-md border border-ink-800 bg-ink-850 px-3 py-2"
                  >
                    <label className="flex flex-1 items-center gap-2.5">
                      <input
                        type="checkbox"
                        className="accent-accent"
                        checked={state.enabled}
                        onChange={(e) =>
                          setDraft((prev) => ({
                            ...prev,
                            [table.id]: { ...state, enabled: e.target.checked },
                          }))
                        }
                      />
                      <span className="text-sm text-slate-200">{table.table_name}</span>
                      <span className="text-[11px] text-slate-600">
                        {formatCompact(table.approx_row_count)} rows · {table.column_count} cols
                      </span>
                    </label>

                    {state.enabled && (
                      <input
                        className="w-44 rounded border border-ink-600 bg-ink-900 px-2 py-1 text-xs text-slate-200 placeholder:text-slate-600"
                        placeholder="time column (optional)"
                        value={state.time ?? ''}
                        onChange={(e) =>
                          setDraft((prev) => ({
                            ...prev,
                            [table.id]: { ...state, time: e.target.value },
                          }))
                        }
                      />
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/* ------------------------------------------------------------ relationships */

/**
 * Join safety, stated as a decision rather than as measurements.
 *
 * The numbers behind this are exactly the ones the deterministic analysis
 * produced — nothing is recomputed, rephrased or scored again here. What changes
 * is what a business reader has to read to act: which sources connect, whether
 * that connection is safe to use in a KPI, and how many need a second look.
 * Cardinality, detection method, confidence and fan-out are the analyst's
 * evidence for that verdict, so they live one click away instead of in the way.
 */

type RelationshipStatus = 'SAFE' | 'ATTENTION' | 'UNSAFE' | 'UNRATED'

const STATUS_LABEL: Record<RelationshipStatus, string> = {
  SAFE: 'Safe',
  ATTENTION: 'Needs attention',
  UNSAFE: 'Unsafe',
  UNRATED: 'Not analysed',
}

/** Derived from the stored join-safety level. One place, so the summary counts
 *  and the per-row badges can never disagree. */
function statusOf(rel: RelationshipView): RelationshipStatus {
  switch (rel.join_safety?.level) {
    case 'SAFE':
      return 'SAFE'
    case 'RISKY':
      return 'UNSAFE'
    case 'SAFE_WITH_AGGREGATION':
      return 'ATTENTION'
    default:
      return 'UNRATED'
  }
}

/** A relationship matters to KPI correctness when joining it could change a
 *  number: it is not rated SAFE, or it drops rows through orphan keys. */
function isMaterial(rel: RelationshipView): boolean {
  return statusOf(rel) !== 'SAFE' || Boolean(rel.orphan_pct)
}

function RelationshipsPanel({ relationships }: { relationships: RelationshipView[] }) {
  const [showAll, setShowAll] = useState(false)
  const [showTechnical, setShowTechnical] = useState(false)

  const counts = useMemo(() => {
    const tally = { SAFE: 0, ATTENTION: 0, UNSAFE: 0, UNRATED: 0 }
    for (const rel of relationships) tally[statusOf(rel)] += 1
    return tally
  }, [relationships])

  const material = useMemo(() => relationships.filter(isMaterial), [relationships])
  const visible = showAll ? relationships : material
  const attention = counts.ATTENTION + counts.UNSAFE + counts.UNRATED

  return (
    <Panel title="How your data connects" bodyClassName="">
      <div className="grid gap-4 border-b border-ink-800 p-4 sm:grid-cols-3">
        <Metric label="Connections checked" value={formatNumber(relationships.length)} />
        <Metric
          label="Safe to use"
          value={formatNumber(counts.SAFE)}
          tone={counts.SAFE ? 'good' : undefined}
        />
        <Metric
          label="Need attention"
          value={formatNumber(attention)}
          tone={attention ? 'warn' : 'good'}
          hint={attention ? 'These can change a KPI number if used unchecked.' : 'Nothing outstanding.'}
        />
      </div>

      {visible.length === 0 ? (
        <div className="p-4 text-sm text-slate-400">
          Every connection is safe to use in a KPI. Nothing needs a decision.
        </div>
      ) : (
        <ul className="divide-y divide-ink-800">
          {visible.map((rel) => {
            const status = statusOf(rel)
            return (
              <li key={rel.id} className="px-4 py-3">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="flex items-center gap-2 text-sm text-slate-200">
                    <span className="font-medium">{rel.from_table}</span>
                    <span aria-hidden className="text-slate-500">
                      →
                    </span>
                    <span className="font-medium">{rel.to_table}</span>
                  </span>
                  <StatusBadge
                    status={
                      status === 'SAFE'
                        ? 'SAFE'
                        : status === 'UNSAFE'
                          ? 'RISKY'
                          : status === 'ATTENTION'
                            ? 'WARNING'
                            : 'UNKNOWN'
                    }
                    label={STATUS_LABEL[status]}
                  />
                </div>

                {showTechnical && (
                  <dl className="mt-2 grid gap-x-6 gap-y-1 border-t border-ink-800 pt-2 text-[11px] sm:grid-cols-2">
                    <TechnicalRow term="Join keys">
                      <span className="mono">
                        {rel.from_table}.{rel.from_column} = {rel.to_table}.{rel.to_column}
                      </span>
                    </TechnicalRow>
                    <TechnicalRow term="Cardinality">
                      {titleCase(rel.relationship_type)}
                      {rel.join_safety?.observed_cardinality &&
                        rel.join_safety.observed_cardinality !==
                          rel.join_safety.expected_cardinality && (
                          <span className="ml-2 text-amber-400">
                            observed {titleCase(rel.join_safety.observed_cardinality)}
                          </span>
                        )}
                    </TechnicalRow>
                    <TechnicalRow term="Detection">
                      {rel.is_declared ? 'declared foreign key' : rel.method ?? '—'}
                      {rel.confidence !== null && rel.confidence !== undefined && (
                        <span className="ml-2 text-slate-500">
                          confidence {rel.confidence.toFixed(2)}
                        </span>
                      )}
                    </TechnicalRow>
                    <TechnicalRow term="Fan-out">
                      {rel.join_safety?.fan_out_factor
                        ? `×${rel.join_safety.fan_out_factor.toFixed(2)}`
                        : '—'}
                      {rel.join_safety?.max_fan_out ? (
                        <span className="ml-2 text-slate-500">
                          max ×{rel.join_safety.max_fan_out}
                        </span>
                      ) : null}
                      {rel.join_safety?.duplicate_key_rate ? (
                        <span className="ml-2 text-slate-500">
                          duplicate keys {formatPercent(rel.join_safety.duplicate_key_rate)}
                        </span>
                      ) : null}
                    </TechnicalRow>
                    {rel.orphan_pct ? (
                      <TechnicalRow term="Orphan rows">
                        {formatPercent(rel.orphan_pct)} of child rows have no parent
                      </TechnicalRow>
                    ) : null}
                    {(rel.join_safety?.guidance || rel.join_safety?.reason) && (
                      <TechnicalRow term="Analysis">
                        <span className="leading-snug text-slate-500">
                          {rel.join_safety?.guidance ?? rel.join_safety?.reason}
                        </span>
                      </TechnicalRow>
                    )}
                  </dl>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-ink-800 px-4 py-3">
        {material.length < relationships.length && (
          <button className="btn-ghost btn-xs" onClick={() => setShowAll((v) => !v)}>
            {showAll
              ? `Show only the ${material.length} that need a decision`
              : `Show all ${relationships.length} connections`}
          </button>
        )}
        <button className="btn-ghost btn-xs" onClick={() => setShowTechnical((v) => !v)}>
          {showTechnical ? 'Hide technical details' : 'Technical details'}
        </button>
      </div>
    </Panel>
  )
}

function TechnicalRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="flex gap-2">
      <dt className="w-24 shrink-0 uppercase tracking-wider text-slate-600">{term}</dt>
      <dd className="min-w-0 text-slate-300">{children}</dd>
    </div>
  )
}

/* ---------------------------------------------------------- reconciliation */

function ReconciliationPanel({
  pairs,
  note,
}: {
  pairs: ReconciliationPair[]
  note: string
}) {
  return (
    <Panel title="Cross-source reconciliation" bodyClassName="">
      <div className="border-b border-ink-800 p-4">
        <p className="text-xs leading-relaxed text-slate-500">{note}</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-ink-700">
              <th className="table-head">Pair</th>
              <th className="table-head">Verdict</th>
              <th className="table-head">Shared dimensions</th>
              <th className="table-head">Unmapped</th>
              <th className="table-head">Overlap</th>
              <th className="table-head">Guidance</th>
            </tr>
          </thead>
          <tbody>
            {pairs.map((pair) => (
              <tr key={pair.id} className="border-b border-ink-800 last:border-0">
                <td className="table-cell text-slate-300">
                  {pair.left_table} ↔ {pair.right_table}
                </td>
                <td className="table-cell">
                  <StatusBadge status={pair.status} />
                </td>
                <td className="table-cell">
                  {pair.shared_dimensions.length ? (
                    <div className="flex flex-wrap gap-1">
                      {pair.shared_dimensions.map((d) => (
                        <span key={d} className="chip">
                          {d}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-slate-600">none</span>
                  )}
                </td>
                <td className="table-cell">
                  {pair.unmapped_dimensions.length ? (
                    <div className="flex flex-wrap gap-1">
                      {pair.unmapped_dimensions.map((d) => (
                        <span key={d} className="chip text-amber-300">
                          {d}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-slate-600">—</span>
                  )}
                </td>
                <td className="table-cell tabular-nums text-slate-400">
                  {pair.time_overlap_days !== null && pair.time_overlap_days !== undefined
                    ? `${pair.time_overlap_days} d`
                    : '—'}
                </td>
                <td className="table-cell max-w-[22rem] whitespace-normal text-[11px] leading-snug text-slate-500">
                  {pair.guidance ?? pair.reason ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

/* -------------------------------------------------------------- table drawer */

function TableDrawer({
  base,
  tableId,
  onClose,
}: {
  base: string
  tableId: string
  onClose: () => void
}) {
  const detail = useResource<TableDetail>(
    () => api.get(`${base}/tables/${tableId}/profile`, { admin: true }),
    [tableId],
  )
  const profileAction = useAction()

  return (
    <Drawer
      open
      onClose={onClose}
      title={detail.data?.qualified_name ?? 'Table'}
      subtitle={detail.data ? `${detail.data.data_source_name} · ${detail.data.table_type}` : undefined}
      footer={
        <>
          <button
            className="btn-ghost btn-xs"
            disabled={profileAction.pending}
            onClick={async () => {
              const ok = await profileAction.run(() =>
                api.post(`${base}/tables/${tableId}/profile`, {}, { admin: true }),
              )
              if (ok) await detail.reload()
            }}
          >
            {profileAction.pending ? 'Profiling…' : 'Re-profile'}
          </button>
          <button className="btn-ghost btn-xs" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      {detail.loading && !detail.data ? (
        <Spinner />
      ) : detail.error ? (
        <Alert>{detail.error}</Alert>
      ) : detail.data ? (
        <div className="space-y-5">
          {profileAction.error && <Alert>{profileAction.error}</Alert>}

          <div className="grid gap-5 sm:grid-cols-4">
            <Metric label="Rows" value={formatCompact(detail.data.row_count)} />
            <Metric
              label="Quality"
              value={<StatusBadge status={detail.data.quality_status} />}
              hint={
                detail.data.quality_score !== null && detail.data.quality_score !== undefined
                  ? `score ${detail.data.quality_score}`
                  : undefined
              }
            />
            <Metric
              label="Freshness"
              value={<StatusBadge status={detail.data.freshness?.status} />}
              hint={
                detail.data.freshness?.lag_seconds !== null &&
                detail.data.freshness?.lag_seconds !== undefined
                  ? `lag ${formatDuration(detail.data.freshness.lag_seconds)}`
                  : undefined
              }
            />
            <Metric
              label="Withheld columns"
              value={formatCompact(detail.data.withheld_columns)}
              hint="not readable by you"
              tone={detail.data.withheld_columns ? 'default' : 'muted'}
            />
          </div>

          {detail.data.warnings.length > 0 && (
            <Alert tone="warn">
              <div className="mb-1 font-medium">Quality warnings — recorded, not repaired</div>
              <ul className="space-y-0.5 text-xs">
                {detail.data.warnings.map((warning, index) => (
                  <li key={index}>· {warning}</li>
                ))}
              </ul>
            </Alert>
          )}

          {detail.data.grain && (
            <section>
              <h3 className="panel-title mb-2">Grain</h3>
              <dl>
                <DefinitionRow term="Inferred">
                  {detail.data.grain.inferred_grain ?? '—'}
                </DefinitionRow>
                <DefinitionRow term="Columns">
                  <div className="flex flex-wrap gap-1">
                    {detail.data.grain.grain_columns.map((column) => (
                      <span key={column} className="chip mono">
                        {column}
                      </span>
                    ))}
                  </div>
                </DefinitionRow>
                <DefinitionRow term="Unique">
                  {detail.data.grain.is_unique ? 'Yes — one row per grain' : 'No'}
                </DefinitionRow>
                <DefinitionRow term="Method">
                  <span className="chip">{detail.data.grain.method}</span>
                  {detail.data.grain.confidence !== null &&
                    detail.data.grain.confidence !== undefined && (
                      <span className="ml-2 text-xs text-slate-500">
                        confidence {detail.data.grain.confidence.toFixed(2)}
                      </span>
                    )}
                </DefinitionRow>
                {detail.data.grain.time_column && (
                  <DefinitionRow term="Time axis">
                    <span className="mono">{detail.data.grain.time_column}</span>
                    {detail.data.grain.time_grain && (
                      <span className="ml-2 chip">{detail.data.grain.time_grain}</span>
                    )}
                  </DefinitionRow>
                )}
              </dl>
            </section>
          )}

          <section>
            <h3 className="panel-title mb-2">Columns</h3>
            <div className="overflow-x-auto rounded-md border border-ink-800">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-ink-800 bg-ink-850">
                    <th className="table-head">Column</th>
                    <th className="table-head">Semantic</th>
                    <th className="table-head">Null %</th>
                    <th className="table-head">Distinct</th>
                    <th className="table-head">Min</th>
                    <th className="table-head">Max</th>
                    <th className="table-head">Class</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data.columns.map((column) => (
                    <tr key={column.id} className="border-b border-ink-800 last:border-0">
                      <td className="table-cell">
                        <span className="mono text-slate-200">{column.column_name}</span>
                        {column.is_primary_key && <span className="ml-1.5 chip">PK</span>}
                        {column.is_foreign_key && <span className="ml-1.5 chip">FK</span>}
                      </td>
                      <td className="table-cell text-slate-400">
                        {titleCase(column.semantic_type)}
                      </td>
                      {column.readable && column.profile ? (
                        <>
                          <td className="table-cell tabular-nums text-slate-400">
                            {formatPercent(column.profile.null_pct, 2)}
                          </td>
                          <td className="table-cell tabular-nums text-slate-400">
                            {formatNumber(column.profile.distinct_count)}
                          </td>
                          <td className="table-cell max-w-[10rem] truncate text-slate-500">
                            {column.profile.min_value ?? '—'}
                          </td>
                          <td className="table-cell max-w-[10rem] truncate text-slate-500">
                            {column.profile.max_value ?? '—'}
                          </td>
                        </>
                      ) : (
                        <td className="table-cell text-[11px] italic text-slate-600" colSpan={4}>
                          withheld — {column.withheld_reason ?? 'not readable'}
                        </td>
                      )}
                      <td className="table-cell">
                        {column.is_pii ? (
                          <span className="chip text-rose-300">PII</span>
                        ) : (
                          <span className="text-[11px] text-slate-600">
                            {titleCase(column.classification)}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {detail.data.withheld_columns > 0 && (
              <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
                Withheld columns were never read. Entitlement is applied before profiling, not by
                filtering results afterwards.
              </p>
            )}
          </section>

          {detail.data.relationships.length > 0 && (
            <section>
              <h3 className="panel-title mb-2">Relationships</h3>
              <ul className="space-y-1">
                {detail.data.relationships.map((rel) => (
                  <li
                    key={rel.id}
                    className="flex flex-wrap items-center gap-2 rounded border border-ink-800 bg-ink-850 px-3 py-2 text-xs"
                  >
                    <span className="mono text-slate-300">
                      {rel.from_table}.{rel.from_column} → {rel.to_table}.{rel.to_column}
                    </span>
                    <span className="chip">{titleCase(rel.relationship_type)}</span>
                    <StatusBadge status={rel.join_safety?.level} />
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      ) : null}
    </Drawer>
  )
}
