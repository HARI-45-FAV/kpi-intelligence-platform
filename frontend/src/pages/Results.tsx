import { useMemo, useState } from 'react'
import { api } from '../api/client'
import type { ResultHistoryResponse, ResultHistoryItem } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatCompact, formatCurrency, formatDate, formatKpiName } from '../components/format'
import { Alert, EmptyState, Modal, Panel, Spinner, StatusBadge } from '../components/ui'
import { useResource } from '../components/useResource'

const FILTERS = ['all', 'NORMAL', 'ABNORMAL', 'LOW_CONFIDENCE'] as const

/**
 * A measurement in the KPI's own unit — the same rule Monitoring applies.
 *
 * The row carries `currency` and `unit` from the stored run, so the unit is read,
 * never inferred. Guessing money by looking for "revenue" or "sales" in the KPI
 * key mislabels every other currency KPI, and pinning the symbol to USD prints
 * dollars for a company whose books are in something else.
 */
function formatValue(item: ResultHistoryItem, value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (item.currency) return formatCurrency(value, item.currency, true)
  if (item.unit === 'currency') return formatCurrency(value, 'INR', true)
  return formatCompact(value)
}

function formatDeviation(item: ResultHistoryItem): string {
  if (item.deviation_pct === null || item.deviation_pct === undefined) return '—'
  return `${item.deviation_pct >= 0 ? '+' : ''}${item.deviation_pct.toFixed(1)}%`
}

/**
 * The sentence to show for a row.
 *
 * A generated explanation is used when one exists. Nothing in the platform writes
 * them today — explanation generation belongs to the Copilot and is off by
 * default — so in practice this is the engine's deterministic headline, which is
 * stored for every run. Showing that beats the empty column this page used to
 * render on every single row.
 */
function summaryText(item: ResultHistoryItem): string | null {
  return item.ai_explanation ?? item.top_driver ?? null
}

/**
 * The small caption under a KPI's name.
 *
 * A registered KPI has both a key and a display name, and for many of them the
 * two say the same thing once the key is read as English. Printing the key only
 * when it adds something keeps the row from repeating itself, and means no raw
 * `snake_case` identifier reaches the page either way.
 */
function subtitleFor(item: ResultHistoryItem): string | null {
  const name = formatKpiName(item.kpi_name)
  const key = formatKpiName(item.kpi_key)
  return key === name ? null : key
}

export default function Results() {
  const { companyId, can } = useAuth()
  const mayView = can('analytics.read')

  const history = useResource<ResultHistoryResponse>(
    () => api.get(`/companies/${companyId}/results`),
    [companyId, mayView],
    { enabled: Boolean(companyId) && mayView },
  )

  const [statusFilter, setStatusFilter] = useState<(typeof FILTERS)[number]>('all')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<ResultHistoryItem | null>(null)

  const items = useMemo(() => {
    const base = history.data?.items ?? []
    return base.filter((item) => {
      const matchesStatus = statusFilter === 'all' || item.status === statusFilter
      // Both spellings are searchable: what the reader sees, and the key they
      // may know the KPI by from the registry.
      const haystack = `${formatKpiName(item.kpi_name)} ${item.kpi_name} ${item.kpi_key} ${item.status}`.toLowerCase()
      const matchesQuery = !query || haystack.includes(query.toLowerCase())
      return matchesStatus && matchesQuery
    })
  }, [history.data, statusFilter, query])

  if (!mayView) {
    return (
      <Alert tone="warn">
        You do not have permission to view stored result history for this company.
      </Alert>
    )
  }

  if (history.loading && !history.data) {
    return <Spinner label="Loading result history…" />
  }

  if (history.error) {
    return <Alert tone="error">Unable to load results. ({history.error})</Alert>
  }

  const summary = history.data?.summary ?? {
    total_runs: 0,
    anomalies: 0,
    abnormal: 0,
    normal: 0,
    low_confidence: 0,
    kpi_count: 0,
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Results</p>
          <h1 className="mt-1 text-2xl font-semibold text-slate-100">Agent run history</h1>
        </div>

        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search KPI or status"
            className="field max-w-xs"
          />
          <div className="glass-nav w-fit rounded-[14px] p-1">
            {FILTERS.map((filter) => (
              <button
                key={filter}
                type="button"
                onClick={() => setStatusFilter(filter)}
                className={`nav-pill px-2.5 py-1.5 text-xs ${statusFilter === filter ? 'nav-pill-active' : ''}`}
              >
                {filter === 'all' ? 'All' : filter.replace('_', ' ')}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <Panel title="Total" bodyClassName="p-4">
          <div className="text-2xl font-semibold text-slate-100">{summary.total_runs}</div>
          <div className="mt-1 text-xs text-slate-500">Stored result rows</div>
        </Panel>
        <Panel title="Anomalies" bodyClassName="p-4">
          <div className="text-2xl font-semibold text-rose-300">{summary.anomalies}</div>
          <div className="mt-1 text-xs text-slate-500">Outside tolerance</div>
        </Panel>
        <Panel title="Normal" bodyClassName="p-4">
          <div className="text-2xl font-semibold text-emerald-300">{summary.normal}</div>
          <div className="mt-1 text-xs text-slate-500">In line with history</div>
        </Panel>
        <Panel title="Low confidence" bodyClassName="p-4">
          <div className="text-2xl font-semibold text-amber-300">{summary.low_confidence}</div>
          <div className="mt-1 text-xs text-slate-500">Insufficient comparable history</div>
        </Panel>
        <Panel title="KPIs" bodyClassName="p-4">
          <div className="text-2xl font-semibold text-sky-300">{summary.kpi_count}</div>
          <div className="mt-1 text-xs text-slate-500">Distinct signals</div>
        </Panel>
      </div>

      <Panel title="Stored results" bodyClassName="p-0">
        {items.length === 0 ? (
          <EmptyState
            title="No stored results match this view"
            description="Try a different status filter or review the company’s most recent KPI runs."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full border-separate border-spacing-0">
              <thead>
                <tr>
                  <th className="table-head">KPI</th>
                  <th className="table-head">Date</th>
                  <th className="table-head">Actual</th>
                  <th className="table-head">Expected</th>
                  <th className="table-head">Deviation</th>
                  <th className="table-head">Status</th>
                  <th className="table-head">Summary</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-b border-ink-800/80 align-top hover:bg-white/40">
                    <td className="table-cell min-w-[12rem]">
                      <div className="font-medium text-slate-100">{formatKpiName(item.kpi_name)}</div>
                      {subtitleFor(item) && (
                        <div className="mt-1 text-[11px] uppercase tracking-wider text-slate-500">
                          {subtitleFor(item)}
                        </div>
                      )}
                    </td>
                    <td className="table-cell text-slate-300">{formatDate(item.target_date)}</td>
                    <td className="table-cell text-slate-200">
                      {formatValue(item, item.actual_value)}
                    </td>
                    <td className="table-cell text-slate-300">
                      {formatValue(item, item.expected_value)}
                    </td>
                    <td className="table-cell text-slate-200">{formatDeviation(item)}</td>
                    <td className="table-cell">
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="table-cell min-w-[16rem]">
                      <div className="flex items-center gap-2">
                        <div className="line-clamp-2 max-w-md text-sm text-slate-300">
                          {summaryText(item) ?? 'No summary stored for this run.'}
                        </div>
                        {summaryText(item) && (
                          <button
                            type="button"
                            className="btn btn-xs btn-ghost shrink-0"
                            onClick={() => setSelected(item)}
                          >
                            View
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Modal
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={
          selected
            ? `${formatKpiName(selected.kpi_name)} · ${formatDate(selected.target_date)}`
            : 'Result details'
        }
        width="max-w-2xl"
      >
        {selected && (
          <div className="space-y-5 text-sm text-slate-300">
            <div className="flex items-center justify-between gap-3">
              <div>
                {subtitleFor(selected) && (
                  <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">
                    {subtitleFor(selected)}
                  </div>
                )}
                <div className="mt-1 text-lg font-semibold text-slate-100">
                  {formatKpiName(selected.kpi_name)}
                </div>
              </div>
              <StatusBadge status={selected.status} />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-white/80 bg-white/45 p-3">
                <div className="text-[11px] uppercase tracking-wider text-slate-500">Actual</div>
                <div className="mt-2 text-xl font-semibold text-slate-100">
                  {formatValue(selected, selected.actual_value)}
                </div>
              </div>
              <div className="rounded-xl border border-white/80 bg-white/45 p-3">
                <div className="text-[11px] uppercase tracking-wider text-slate-500">Expected</div>
                <div className="mt-2 text-xl font-semibold text-slate-100">
                  {formatValue(selected, selected.expected_value)}
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-white/80 bg-white/45 p-3">
              <div className="text-[11px] uppercase tracking-wider text-slate-500">
                {selected.ai_explanation ? 'AI explanation' : 'What the platform found'}
              </div>
              <div className="mt-2 leading-relaxed text-slate-300">
                {summaryText(selected) ?? 'No summary is stored for this result yet.'}
              </div>
            </div>

            <div className="flex flex-wrap gap-2 text-[11px] uppercase tracking-wider text-slate-500">
              <span className="chip">Deviation {formatDeviation(selected)}</span>
              {/* Only claimed when a model really wrote one: the endpoint reports
                  NOT_GENERATED otherwise, and asserting "Explanation READY" on
                  every historical row was simply untrue. */}
              {selected.ai_explanation && (
                <span className="chip">Explanation {selected.explanation_status}</span>
              )}
              {selected.explanation_generated_at && (
                <span className="chip">
                  Generated {formatDate(selected.explanation_generated_at)}
                </span>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
