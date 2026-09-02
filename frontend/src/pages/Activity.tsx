/**
 * Activity: the audit trail and runtime telemetry.
 *
 * The trail has been written since Sprint 1; what this screen adds is the ability
 * to *read* it. An append-only log nobody can search is a compliance artefact
 * rather than an accountability tool — "who approved Revenue v2, and when" is only
 * answerable if the row can be found among thousands.
 *
 * Three decisions worth stating:
 *
 *  - **The filter values come from the server.** `/audit/options` returns the
 *    distinct actions, resource types, actors and outcomes this company actually
 *    recorded, so no control is a dead end and no action is unreachable because it
 *    fell off the end of a capped page.
 *  - **The raw action key is never replaced, only translated.** The trail's own
 *    vocabulary is the record; a prettified label that lost `detection.executed`
 *    would make the screen and the log disagree. So the readable label leads and
 *    the key travels with it — in the row's tooltip, and in the expanded detail.
 *  - **Nothing here is computed.** Every timestamp is `occurred_at` as stored, and
 *    `details` is rendered as the server scrubbed it. The screen adds no event, no
 *    inferred actor and no synthesised history.
 */

import { useMemo, useState } from 'react'
import { api } from '../api/client'
import type { AuditEntry, AuditOptionsResponse, TelemetrySummary } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime, formatNumber, formatRelative } from '../components/format'
import { Alert, EmptyState, Field, Metric, Panel, Spinner, StatusBadge } from '../components/ui'
import { useResource } from '../components/useResource'

export default function Activity() {
  const { companyId, can } = useAuth()
  const [tab, setTab] = useState<'audit' | 'telemetry'>('audit')

  const mayAudit = can('audit.read')
  const mayTelemetry = can('telemetry.read')

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Activity</h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Governance actions and runtime cost. Both are recorded from Sprint 1 onward so later
          sprints inherit accountability rather than adding it.
        </p>
      </div>

      <div className="flex gap-1 rounded-md border border-ink-700 bg-ink-850 p-1">
        {([
          ['audit', 'Audit trail'],
          ['telemetry', 'Telemetry'],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`rounded px-3 py-1.5 text-sm transition-colors ${
              tab === key
                ? 'bg-ink-700 font-medium text-slate-100'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'audit' &&
        (mayAudit ? (
          <AuditTrail companyId={companyId!} />
        ) : (
          <Alert tone="info">
            Your role does not include <code className="mono">audit.read</code>.
          </Alert>
        ))}

      {tab === 'telemetry' &&
        (mayTelemetry ? (
          <Telemetry companyId={companyId!} />
        ) : (
          <Alert tone="info">
            Your role does not include <code className="mono">telemetry.read</code>.
          </Alert>
        ))}
    </div>
  )
}

/* ------------------------------------------------------------- audit reading */

/** Entries per page. Small enough to read, large enough to be worth filtering. */
const PAGE_SIZE = 50

/**
 * Action keys whose generic reading is wrong or unhelpfully terse.
 *
 * Only the exceptions. `_readableAction` derives the rest from the key itself, so a
 * new action added to the backend gets a sensible label without an edit here — the
 * alternative, a complete map, silently prints nothing for anything it has not been
 * taught, which is the worse failure for a log that gains new action types.
 */
const ACTION_LABELS: Record<string, string> = {
  'detection.executed': 'KPI analysis run',
  AGENT_RUN: 'Agent run',
  'detection.run_summary_emailed': 'Run summary emailed',
  'user.logged_in': 'Signed in',
  'user.registered': 'Account registered',
  'member.role_changed': 'Member role changed',
  'member.scope_updated': 'Member data scope changed',
  'source.tables_discovered': 'Source tables discovered',
  'profiling.executed': 'Data profiled',
  'kpi.imported_from_source': 'KPI imported from source',
  'kpi.submitted_for_review': 'KPI submitted for review',
  'investigation.contribution_analysed': 'Contribution analysed',
  'investigation.entity_analysed': 'Entity analysed',
  'explainability.result_explained': 'Result explained',
  'explainability.node_explained': 'Investigation node explained',
}

/** Domain words the platform writes as acronyms; anything else sentence-cases. */
const ACRONYMS = new Set(['kpi', 'sql', 'llm', 'ai', 'csv', 'api', 'rls', 'utc'])

function word(value: string): string {
  return ACRONYMS.has(value.toLowerCase()) ? value.toUpperCase() : value
}

/**
 * The trail's own key, in business words.
 *
 * `kpi.approved` reads "KPI approved"; `profiling.grain_detected` reads "Profiling
 * grain detected". The derivation is generic on purpose — a new action added to the
 * backend gets a readable label without an edit to `ACTION_LABELS`, whereas a
 * lookup-only approach prints nothing for anything it has not been taught.
 */
function readableAction(action: string): string {
  const mapped = ACTION_LABELS[action]
  if (mapped) return mapped
  const words = action.split(/[._]/).filter(Boolean).map(word)
  if (words.length === 0) return action
  const [first, ...rest] = words
  const lead = ACRONYMS.has(first.toLowerCase())
    ? first
    : first.charAt(0).toUpperCase() + first.slice(1).toLowerCase()
  return [lead, ...rest.map((part) => (ACRONYMS.has(part.toLowerCase()) ? part : part.toLowerCase()))].join(' ')
}

/** A resource type or detail key in the same register: `source_table` → "Source table". */
function readableResourceType(value: string): string {
  return readableAction(value)
}

interface AuditFilters {
  action: string
  resource_type: string
  actor_email: string
  outcome: string
  since: string
  until: string
  q: string
}

const NO_FILTERS: AuditFilters = {
  action: '',
  resource_type: '',
  actor_email: '',
  outcome: '',
  since: '',
  until: '',
  q: '',
}

/** Only the filters that are set, so an empty control sends nothing. */
function activeQuery(filters: AuditFilters): Record<string, string> {
  return Object.fromEntries(
    Object.entries(filters).filter(([, value]) => value.trim() !== ''),
  ) as Record<string, string>
}

function AuditTrail({ companyId }: { companyId: string }) {
  const [filters, setFilters] = useState<AuditFilters>(NO_FILTERS)
  const [page, setPage] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)

  // One serialised form of the filters keys both requests, so the page and the
  // count it is described by can never be read under different conditions.
  const query = useMemo(() => activeQuery(filters), [filters])
  const queryKey = useMemo(() => JSON.stringify(query), [query])

  const entries = useResource<AuditEntry[]>(
    () =>
      api.get(`/companies/${companyId}/audit`, {
        query: { ...query, limit: PAGE_SIZE, offset: page * PAGE_SIZE },
      }),
    [companyId, queryKey, page],
  )

  const meta = useResource<AuditOptionsResponse>(
    () => api.get(`/companies/${companyId}/audit/options`, { query }),
    [companyId, queryKey],
  )

  const rows = entries.data ?? []
  const total = meta.data?.total ?? null
  const options = meta.data?.options
  const filtered = Object.keys(query).length > 0

  /** Changing any filter returns to the first page: page 3 of a new result is not a place. */
  const update = (patch: Partial<AuditFilters>) => {
    setFilters((current) => ({ ...current, ...patch }))
    setPage(0)
    setExpanded(null)
  }

  const from = page * PAGE_SIZE
  const hasMore = total !== null ? from + rows.length < total : rows.length === PAGE_SIZE

  return (
    <div className="space-y-4">
      <Panel
        title="Filters"
        actions={
          filtered ? (
            <button
              type="button"
              className="btn btn-xs btn-ghost"
              onClick={() => {
                setFilters(NO_FILTERS)
                setPage(0)
              }}
            >
              Clear all
            </button>
          ) : undefined
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          <Field label="Action" hint="Recorded by this company">
            <select
              className="field"
              value={filters.action}
              onChange={(event) => update({ action: event.target.value })}
            >
              <option value="">Any action</option>
              {(options?.actions ?? []).map((value) => (
                <option key={value} value={value}>
                  {readableAction(value)}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Resource" hint="What was acted on">
            <select
              className="field"
              value={filters.resource_type}
              onChange={(event) => update({ resource_type: event.target.value })}
            >
              <option value="">Any resource</option>
              {(options?.resource_types ?? []).map((value) => (
                <option key={value} value={value}>
                  {readableResourceType(value)}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Actor" hint="Who performed it">
            <select
              className="field"
              value={filters.actor_email}
              onChange={(event) => update({ actor_email: event.target.value })}
            >
              <option value="">Anyone</option>
              {(options?.actors ?? []).map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Outcome" hint="Whether it succeeded">
            <select
              className="field"
              value={filters.outcome}
              onChange={(event) => update({ outcome: event.target.value })}
            >
              <option value="">Any outcome</option>
              {(options?.outcomes ?? []).map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>

          <Field label="From" hint="Inclusive">
            <input
              type="date"
              className="field"
              value={filters.since}
              max={filters.until || undefined}
              onChange={(event) => update({ since: event.target.value })}
            />
          </Field>

          <Field label="To" hint="Inclusive of the whole day">
            <input
              type="date"
              className="field"
              value={filters.until}
              min={filters.since || undefined}
              onChange={(event) => update({ until: event.target.value })}
            />
          </Field>

          <Field label="Search" hint="Resource or summary text">
            <input
              className="field"
              placeholder="A KPI name, an id, a phrase…"
              value={filters.q}
              onChange={(event) => update({ q: event.target.value })}
            />
          </Field>
        </div>
      </Panel>

      {entries.error && <Alert>{entries.error}</Alert>}
      {meta.error && <Alert tone="warn">Could not read the filter options. ({meta.error})</Alert>}

      {entries.loading && !entries.data ? (
        <Panel>
          <Spinner label="Reading the audit trail…" />
        </Panel>
      ) : rows.length === 0 ? (
        <Panel>
          {/* Two different facts that look identical on an empty table, told apart. */}
          <EmptyState
            title={filtered ? 'No entry matches these filters' : 'No audit entries yet'}
            description={
              filtered
                ? `Nothing in this company's ${formatNumber(
                    meta.data?.total_unfiltered ?? 0,
                  )} recorded entries matches. Widen the dates, or clear a filter.`
                : 'Governance actions are recorded as they happen. Nothing has been recorded for this company yet.'
            }
          />
        </Panel>
      ) : (
        <Panel
          title={
            total !== null
              ? `${formatNumber(from + 1)}–${formatNumber(from + rows.length)} of ${formatNumber(total)}${
                  filtered ? ' matching' : ''
                }`
              : `${rows.length} entries`
          }
          actions={
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                className="btn btn-xs btn-ghost"
                disabled={page === 0 || entries.loading}
                onClick={() => {
                  setPage((value) => Math.max(0, value - 1))
                  setExpanded(null)
                }}
              >
                Newer
              </button>
              <button
                type="button"
                className="btn btn-xs btn-ghost"
                disabled={!hasMore || entries.loading}
                onClick={() => {
                  setPage((value) => value + 1)
                  setExpanded(null)
                }}
              >
                Older
              </button>
            </div>
          }
          bodyClassName="overflow-x-auto p-0"
        >
          <table className="w-full">
            <thead>
              <tr className="border-b border-ink-700">
                <th className="table-head">When</th>
                <th className="table-head">Action</th>
                <th className="table-head">Resource</th>
                <th className="table-head">Actor</th>
                <th className="table-head">Version</th>
                <th className="table-head">Summary</th>
                <th className="table-head" />
              </tr>
            </thead>
            <tbody>
              {rows.map((entry) => (
                <AuditRow
                  key={entry.id}
                  entry={entry}
                  open={expanded === entry.id}
                  onToggle={() => setExpanded((current) => (current === entry.id ? null : entry.id))}
                />
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  )
}

/**
 * One entry, with its stored detail available rather than discarded.
 *
 * `details` is where the trail keeps what actually happened — the KPI key, the
 * confidence level, the recipient count of a summary mail — already scrubbed of
 * credentials server-side. The old screen dropped it entirely, which meant the most
 * specific column in the log was the one column nobody could read.
 */
function AuditRow({
  entry,
  open,
  onToggle,
}: {
  entry: AuditEntry
  open: boolean
  onToggle: () => void
}) {
  const detailEntries = Object.entries(entry.details ?? {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== '',
  )
  const hasDetail = detailEntries.length > 0 || Boolean(entry.resource_id)

  return (
    <>
      <tr className="border-b border-ink-800 last:border-0">
        <td className="table-cell text-slate-500" title={formatDateTime(entry.occurred_at)}>
          {formatRelative(entry.occurred_at)}
        </td>
        <td className="table-cell">
          {/* The readable label leads; the trail's own key is what the tooltip and
              the expanded detail carry, so the screen never replaces the record. */}
          <span className="text-slate-200" title={entry.action}>
            {readableAction(entry.action)}
          </span>
        </td>
        <td className="table-cell max-w-[16rem] truncate text-slate-300" title={entry.resource_label ?? undefined}>
          {entry.resource_label ?? readableResourceType(entry.resource_type)}
        </td>
        <td className="table-cell text-slate-400">{entry.actor_email ?? 'system'}</td>
        <td className="table-cell text-slate-500">
          {entry.old_version || entry.new_version
            ? `${entry.old_version || '—'} → ${entry.new_version || '—'}`
            : '—'}
        </td>
        <td className="table-cell max-w-[28rem] truncate text-slate-400" title={entry.summary ?? undefined}>
          {entry.outcome !== 'SUCCESS' && (
            <StatusBadge status={entry.outcome === 'FAILURE' ? 'FAIL' : entry.outcome} />
          )}{' '}
          {entry.summary}
        </td>
        <td className="table-cell">
          {hasDetail && (
            <button
              type="button"
              className="btn btn-xs btn-ghost"
              aria-expanded={open}
              onClick={onToggle}
            >
              {open ? 'Hide' : 'Detail'}
            </button>
          )}
        </td>
      </tr>
      {open && hasDetail && (
        <tr className="border-b border-ink-800 last:border-0">
          <td colSpan={7} className="px-4 py-3">
            <div className="grid gap-3 lg:grid-cols-2">
              <dl className="space-y-1.5">
                <DetailLine label="Recorded" value={formatDateTime(entry.occurred_at)} />
                <DetailLine label="Action key" value={entry.action} mono />
                <DetailLine label="Resource type" value={entry.resource_type} mono />
                {entry.resource_id && (
                  <DetailLine label="Resource id" value={entry.resource_id} mono />
                )}
                <DetailLine label="Outcome" value={entry.outcome} />
              </dl>
              {detailEntries.length > 0 && (
                <dl className="space-y-1.5">
                  {detailEntries.map(([key, value]) => (
                    <DetailLine
                      key={key}
                      label={readableResourceType(key)}
                      value={typeof value === 'object' ? JSON.stringify(value) : String(value)}
                    />
                  ))}
                </dl>
              )}
            </div>
            <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
              Recorded when the action happened, and never edited afterwards. Credentials are
              removed before an entry is written, so a detail shown here is one the trail stored.
            </p>
          </td>
        </tr>
      )}
    </>
  )
}

function DetailLine({
  label,
  value,
  mono = false,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex flex-wrap gap-x-2 text-[11px]">
      <dt className="min-w-[7.5rem] uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className={`flex-1 break-words text-slate-300 ${mono ? 'mono' : ''}`}>{value}</dd>
    </div>
  )
}

function Telemetry({ companyId }: { companyId: string }) {
  const { data, loading, error } = useResource<TelemetrySummary>(
    () => api.get(`/companies/${companyId}/telemetry/summary`),
    [companyId],
  )

  if (loading && !data) return <Spinner />
  if (error) return <Alert>{error}</Alert>
  if (!data) return null

  return (
    <div className="space-y-5">
      <Panel title="Runtime">
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="Requests" value={formatNumber(data.requests)} hint={`${data.errors} errors`} />
          <Metric
            label="Avg latency"
            value={data.latency_ms.avg ? `${data.latency_ms.avg} ms` : '—'}
            hint={data.latency_ms.max ? `max ${data.latency_ms.max} ms` : undefined}
          />
          <Metric
            label="Source queries"
            value={formatNumber(data.connector.queries)}
            hint={`${formatNumber(data.connector.query_ms)} ms in-database`}
          />
          <Metric
            label="Rows returned"
            value={formatNumber(data.connector.rows_returned)}
            hint="aggregates, not table scans"
          />
        </div>
      </Panel>

      <Panel title="LLM versus non-LLM processing">
        <div className="grid gap-6 sm:grid-cols-4">
          <Metric label="Model calls" value={formatNumber(data.llm.calls)} tone="muted" />
          <Metric label="Prompt tokens" value={formatNumber(data.llm.prompt_tokens)} tone="muted" />
          <Metric
            label="Completion tokens"
            value={formatNumber(data.llm.completion_tokens)}
            tone="muted"
          />
          <Metric
            label="Estimated cost"
            value={`$${data.llm.estimated_cost_usd.toFixed(4)}`}
            tone="muted"
          />
        </div>

        <div className="mt-6 grid gap-5 border-t border-ink-800 pt-5 md:grid-cols-2">
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-emerald-400">
              Deterministic
            </div>
            <ul className="space-y-1">
              {data.processing_split.deterministic.map((item) => (
                <li key={item} className="flex gap-2 text-xs text-slate-400">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-emerald-600" />
                  {item}
                </li>
              ))}
            </ul>
          </div>
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              Language model
            </div>
            {data.processing_split.llm.length === 0 ? (
              <p className="text-xs italic text-slate-600">Nothing. No model calls in Sprint 1.</p>
            ) : (
              <ul className="space-y-1">
                {data.processing_split.llm.map((item) => (
                  <li key={item} className="text-xs text-slate-400">
                    {item}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
              {data.processing_split.note}
            </p>
          </div>
        </div>
      </Panel>

      {data.by_service.length > 0 && (
        <Panel title="By service" bodyClassName="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-ink-700">
                <th className="table-head">Service</th>
                <th className="table-head">Requests</th>
                <th className="table-head">Avg ms</th>
                <th className="table-head">Max ms</th>
                <th className="table-head">Source queries</th>
              </tr>
            </thead>
            <tbody>
              {data.by_service.map((row) => (
                <tr key={row.service} className="border-b border-ink-800 last:border-0">
                  <td className="table-cell text-slate-200">{row.service}</td>
                  <td className="table-cell tabular-nums text-slate-400">
                    {formatNumber(row.requests)}
                  </td>
                  <td className="table-cell tabular-nums text-slate-400">{row.avg_ms ?? '—'}</td>
                  <td className="table-cell tabular-nums text-slate-400">{row.max_ms ?? '—'}</td>
                  <td className="table-cell tabular-nums text-slate-400">
                    {formatNumber(row.connector_queries)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  )
}
