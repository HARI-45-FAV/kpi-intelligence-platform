import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { ResultHistoryResponse, ResultHistoryItem } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatCompact, formatCurrency, formatDate, formatKpiName } from '../components/format'
import { Alert, EmptyState, Field, Panel, Spinner, StatusBadge } from '../components/ui'
import { useResource } from '../components/useResource'

const ALL = 'all'

/** The status buttons. `all` first, then the verdicts the engine issues. */
const STATUS_FILTERS = [ALL, 'NORMAL', 'ABNORMAL', 'LOW_CONFIDENCE'] as const

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
  const navigate = useNavigate()
  const mayView = can('analytics.read')

  // The four narrowing filters are server-side. The list is capped, so filtering
  // the page the browser already holds would leave an older date unreachable —
  // the reader would have no way to the very row they came for.
  const [statusFilter, setStatusFilter] = useState<string>(ALL)
  const [kpiFilter, setKpiFilter] = useState<string>(ALL)
  const [dateFilter, setDateFilter] = useState<string>(ALL)
  const [dimensionFilter, setDimensionFilter] = useState<string>(ALL)
  // Search stays client-side: it is a free-text scan across what is on screen,
  // not a narrowing the server can index, and keeping it local means typing does
  // not issue a request per keystroke.
  const [query, setQuery] = useState('')

  const history = useResource<ResultHistoryResponse>(() => {
    const params = new URLSearchParams()
    if (statusFilter !== ALL) params.set('status', statusFilter)
    if (kpiFilter !== ALL) params.set('kpi_key', kpiFilter)
    if (dateFilter !== ALL) params.set('target_date', dateFilter)
    if (dimensionFilter !== ALL) params.set('dimension', dimensionFilter)
    const suffix = params.toString()
    return api.get(`/companies/${companyId}/results${suffix ? `?${suffix}` : ''}`)
  }, [companyId, mayView, statusFilter, kpiFilter, dateFilter, dimensionFilter], {
    enabled: Boolean(companyId) && mayView,
  })

  const options = history.data?.options
  const kpiOptions = options?.kpis ?? []
  const dateOptions = options?.dates ?? []
  // Offered only when the server says this caller may read findings and some
  // exist, so the screen never shows a control that would return nothing.
  const dimensionOptions = options?.dimensions ?? []

  const narrowed =
    statusFilter !== ALL || kpiFilter !== ALL || dateFilter !== ALL || dimensionFilter !== ALL
  const searching = query.trim().length > 0
  const filtered = narrowed || searching

  const items = useMemo(() => {
    const base = history.data?.items ?? []
    const needle = query.trim().toLowerCase()
    if (!needle) return base
    return base.filter((item) => {
      // Both spellings are searchable: what the reader sees, and the key they
      // may know the KPI by from the registry. Recorded dimensions and entities
      // join the haystack when the caller may see them, so searching for an area
      // finds the results somebody has already marked up along it.
      const haystack = [
        formatKpiName(item.kpi_name),
        item.kpi_name,
        item.kpi_key,
        item.status,
        item.target_date,
        summaryText(item) ?? '',
        ...(item.dimensions ?? []),
        ...(item.entities ?? []),
      ]
        .join(' ')
        .toLowerCase()
      return haystack.includes(needle)
    })
  }, [history.data, query])

  function clearFilters() {
    setStatusFilter(ALL)
    setKpiFilter(ALL)
    setDateFilter(ALL)
    setDimensionFilter(ALL)
    setQuery('')
  }

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
  const totalStored = history.data?.total_stored ?? summary.total_runs

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Results</p>
          <h1 className="mt-1 text-2xl font-semibold text-slate-100">Agent run history</h1>
          <p className="mt-1 text-sm text-slate-500">
            Every stored KPI verdict. Open one for the evidence behind it.
          </p>
        </div>

        <div className="glass-nav w-fit rounded-[14px] p-1" role="group" aria-label="Status">
          {STATUS_FILTERS.map((filter) => (
            <button
              key={filter}
              type="button"
              aria-pressed={statusFilter === filter}
              onClick={() => setStatusFilter(filter)}
              className={`nav-pill px-2.5 py-1.5 text-xs ${statusFilter === filter ? 'nav-pill-active' : ''}`}
            >
              {filter === ALL ? 'All' : filter.replace('_', ' ')}
            </button>
          ))}
        </div>
      </div>

      {/* ------------------------------------------------------------- FILTERS */}
      <Panel
        title="Filters"
        actions={
          filtered ? (
            <button type="button" className="btn btn-xs btn-ghost" onClick={clearFilters}>
              Clear filters
            </button>
          ) : undefined
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Field label="KPI" hint={kpiOptions.length === 0 ? 'No results stored yet' : undefined}>
            <select
              className="field"
              value={kpiFilter}
              onChange={(event) => setKpiFilter(event.target.value)}
            >
              <option value={ALL}>All KPIs</option>
              {kpiOptions.map((option) => (
                <option key={option.kpi_key} value={option.kpi_key}>
                  {formatKpiName(option.kpi_name)}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label="Date"
            hint={dateOptions.length > 0 ? `${dateOptions.length} run dates stored` : undefined}
          >
            <select
              className="field"
              value={dateFilter}
              onChange={(event) => setDateFilter(event.target.value)}
            >
              <option value={ALL}>All dates</option>
              {dateOptions.map((value) => (
                <option key={value} value={value}>
                  {formatDate(value)}
                </option>
              ))}
            </select>
          </Field>

          {dimensionOptions.length > 0 ? (
            <Field label="Dimension" hint="Results a finding was recorded against">
              <select
                className="field"
                value={dimensionFilter}
                onChange={(event) => setDimensionFilter(event.target.value)}
              >
                <option value={ALL}>Any dimension</option>
                {dimensionOptions.map((value) => (
                  <option key={value} value={value}>
                    {formatKpiName(value)}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}

          <Field label="Search" hint="KPI, status, date or stored summary">
            <input
              className="field"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search these results"
            />
          </Field>
        </div>
      </Panel>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <Panel title="Shown" bodyClassName="p-4">
          <div className="text-2xl font-semibold text-slate-100">{items.length}</div>
          <div className="mt-1 text-xs text-slate-500">
            {filtered ? `of ${totalStored} stored` : 'Stored result rows'}
          </div>
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
            description={
              filtered
                ? 'Clear the filters to see every stored result for this company.'
                : 'Review the company’s most recent KPI runs.'
            }
            action={
              filtered ? (
                <button type="button" className="btn btn-ghost" onClick={clearFilters}>
                  Clear filters
                </button>
              ) : undefined
            }
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
                  // The row is the way into the result. Each row's id is the
                  // detection run id, so the Result page can read the same stored
                  // evaluation back — including the evidence behind its verdict,
                  // which no table cell has room for.
                  <tr
                    key={item.id}
                    onClick={() => navigate(`/results/${item.id}`)}
                    className="cursor-pointer border-b border-ink-800/80 align-top hover:bg-white/40"
                  >
                    <td className="table-cell min-w-[12rem]">
                      <div className="font-medium text-slate-100">{formatKpiName(item.kpi_name)}</div>
                      {subtitleFor(item) && (
                        <div className="mt-1 text-[11px] uppercase tracking-wider text-slate-500">
                          {subtitleFor(item)}
                        </div>
                      )}
                      {(item.dimensions ?? []).length > 0 && (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {(item.dimensions ?? []).map((value) => (
                            <span key={value} className="chip">
                              {formatKpiName(value)}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="table-cell text-slate-300">{formatDate(item.target_date)}</td>
                    <td className="table-cell font-medium tabular-nums text-slate-100">
                      {formatValue(item, item.actual_value)}
                    </td>
                    <td className="table-cell tabular-nums text-slate-300">
                      {formatValue(item, item.expected_value)}
                    </td>
                    <td className="table-cell tabular-nums text-slate-200">
                      {formatDeviation(item)}
                    </td>
                    <td className="table-cell">
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="table-cell min-w-[16rem]">
                      <div className="flex items-center gap-2">
                        <div className="line-clamp-2 max-w-md text-sm text-slate-300">
                          {summaryText(item) ?? 'No summary stored for this run.'}
                        </div>
                        <span className="btn btn-xs btn-ghost shrink-0">Open</span>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}
