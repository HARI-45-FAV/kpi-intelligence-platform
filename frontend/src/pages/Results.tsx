import { useMemo, useState } from 'react'
import { api } from '../api/client'
import type { ResultHistoryResponse, ResultHistoryItem } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatCompact, formatCurrency, formatDate, formatNumber } from '../components/format'
import { Alert, EmptyState, Modal, Panel, Spinner, StatusBadge } from '../components/ui'
import { useResource } from '../components/useResource'

const FILTERS = ['all', 'NORMAL', 'ABNORMAL', 'LOW_CONFIDENCE'] as const

function formatValue(item: ResultHistoryItem): string {
  if (item.actual_value === null || item.actual_value === undefined) return '—'
  if (item.kpi_key.toLowerCase().includes('revenue') || item.kpi_key.toLowerCase().includes('sales')) {
    return formatCurrency(item.actual_value, 'USD', true)
  }
  return formatCompact(item.actual_value)
}

function formatDeviation(item: ResultHistoryItem): string {
  if (item.deviation_pct === null || item.deviation_pct === undefined) return '—'
  return `${item.deviation_pct >= 0 ? '+' : ''}${item.deviation_pct.toFixed(1)}%`
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
      const haystack = `${item.kpi_name} ${item.kpi_key} ${item.status}`.toLowerCase()
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
                  <th className="table-head">AI summary</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-b border-ink-800/80 align-top hover:bg-white/40">
                    <td className="table-cell min-w-[12rem]">
                      <div className="font-medium text-slate-100">{item.kpi_name}</div>
                      <div className="mt-1 text-[11px] uppercase tracking-wider text-slate-500">
                        {item.kpi_key}
                      </div>
                    </td>
                    <td className="table-cell text-slate-300">{formatDate(item.target_date)}</td>
                    <td className="table-cell text-slate-200">{formatValue(item)}</td>
                    <td className="table-cell text-slate-300">
                      {item.expected_value === null || item.expected_value === undefined
                        ? '—'
                        : formatNumber(item.expected_value, 2)}
                    </td>
                    <td className="table-cell text-slate-200">{formatDeviation(item)}</td>
                    <td className="table-cell">
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="table-cell min-w-[16rem]">
                      <div className="flex items-center gap-2">
                        <div className="line-clamp-2 max-w-md text-sm text-slate-300">
                          {item.ai_explanation ?? 'No explanation stored for this run.'}
                        </div>
                        {item.ai_explanation && (
                          <button
                            type="button"
                            className="btn btn-xs btn-ghost"
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
        title={selected ? `${selected.kpi_name} · ${formatDate(selected.target_date)}` : 'Result details'}
        width="max-w-2xl"
      >
        {selected && (
          <div className="space-y-5 text-sm text-slate-300">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">
                  {selected.kpi_key}
                </div>
                <div className="mt-1 text-lg font-semibold text-slate-100">{selected.kpi_name}</div>
              </div>
              <StatusBadge status={selected.status} />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-white/80 bg-white/45 p-3">
                <div className="text-[11px] uppercase tracking-wider text-slate-500">Actual</div>
                <div className="mt-2 text-xl font-semibold text-slate-100">{formatValue(selected)}</div>
              </div>
              <div className="rounded-xl border border-white/80 bg-white/45 p-3">
                <div className="text-[11px] uppercase tracking-wider text-slate-500">Expected</div>
                <div className="mt-2 text-xl font-semibold text-slate-100">
                  {selected.expected_value === null || selected.expected_value === undefined
                    ? '—'
                    : formatNumber(selected.expected_value, 2)}
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-white/80 bg-white/45 p-3">
              <div className="text-[11px] uppercase tracking-wider text-slate-500">AI explanation</div>
              <div className="mt-2 leading-relaxed text-slate-300">
                {selected.ai_explanation ?? 'No explanation is stored for this result yet.'}
              </div>
            </div>

            <div className="flex flex-wrap gap-2 text-[11px] uppercase tracking-wider text-slate-500">
              <span className="chip">Deviation {formatDeviation(selected)}</span>
              <span className="chip">Explanation {selected.explanation_status}</span>
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
