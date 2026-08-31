import { useMemo, useState } from 'react'
import type { KpiContract } from '../api/types'
import type { DetectionRunSummary } from '../api/types'
import { formatCompact, formatCurrency, formatDate, formatKpiName } from '../components/format'
import { EmptyState, Overlay, StatusBadge } from '../components/ui'

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

type Window = { start: string; end: string }

function valueFor(contract: KpiContract, value: number | null): string {
  if (value === null || value === undefined) return '—'
  if (contract.currency || contract.unit === 'currency') {
    return formatCurrency(value, contract.currency ?? 'INR', true)
  }
  return formatCompact(value)
}

function signedPercent(value: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`
}

function monthKey(date: Date): string {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`
}

function monthLabel(key: string): string {
  const date = new Date(`${key}-01T00:00:00Z`)
  return date.toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' })
}

function shiftMonth(key: string, amount: number): string {
  const date = new Date(`${key}-01T00:00:00Z`)
  date.setUTCMonth(date.getUTCMonth() + amount)
  return monthKey(date)
}

function calendarDays(key: string): string[] {
  const first = new Date(`${key}-01T00:00:00Z`)
  const start = new Date(first)
  start.setUTCDate(1 - start.getUTCDay())
  const last = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0))
  const end = new Date(last)
  end.setUTCDate(end.getUTCDate() + (6 - end.getUTCDay()))
  const days: string[] = []
  for (const cursor = new Date(start); cursor <= end; cursor.setUTCDate(cursor.getUTCDate() + 1)) {
    days.push(cursor.toISOString().slice(0, 10))
  }
  return days
}

function statusTone(status: string): string {
  if (status === 'NORMAL') return 'bg-emerald-500'
  if (status === 'ABNORMAL') return 'bg-rose-500'
  return 'bg-slate-300'
}

function statusTileClasses(status: string): string {
  if (status === 'NORMAL') return 'border-emerald-300 bg-emerald-50 text-emerald-800'
  if (status === 'ABNORMAL') return 'border-rose-300 bg-rose-50 text-rose-800'
  return 'border-slate-300 bg-slate-100 text-slate-700'
}

export default function KPIDetailDashboard({
  contract,
  runs,
  window,
  onClose,
}: {
  contract: KpiContract
  runs: DetectionRunSummary[]
  window: Window
  onClose: () => void
}) {
  const kpiRuns = useMemo(
    () => runs.filter((run) => run.kpi_key === contract.kpi_id),
    [contract.kpi_id, runs],
  )
  const [selectedId, setSelectedId] = useState<string | null>(kpiRuns[0]?.id ?? null)
  const [selectedDate, setSelectedDate] = useState<string>(kpiRuns[0]?.target_date ?? window.end)
  const [month, setMonth] = useState(() => {
    const source = kpiRuns[0]?.target_date ?? window.end
    return monthKey(new Date(`${source}T00:00:00Z`))
  })
  const [isRunModalOpen, setIsRunModalOpen] = useState(false)

  const selected = kpiRuns.find((run) => run.id === selectedId) ?? null
  const monthRuns = useMemo(
    () => new Map(kpiRuns.filter((run) => run.target_date.startsWith(month)).map((run) => [run.target_date, run])),
    [kpiRuns, month],
  )
  const chartRuns = useMemo(
    () => [...kpiRuns].sort((a, b) => a.target_date.localeCompare(b.target_date)).slice(-7),
    [kpiRuns],
  )
  const modalRuns = useMemo(
    () => [...kpiRuns].sort((a, b) => b.target_date.localeCompare(a.target_date)).slice(0, 7),
    [kpiRuns],
  )
  const maxActual = Math.max(...chartRuns.map((run) => Math.abs(run.actual_value ?? 0)), 1)

  return (
    <section className="mt-3 rounded-2xl border border-sky-200/80 bg-white/90 p-4 shadow-[0_18px_50px_rgba(38,82,120,0.18)] backdrop-blur-xl">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 pb-3">
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-700">
            KPI detail workspace
          </div>
          <h3 className="mt-1 text-lg font-semibold text-slate-900">{formatKpiName(contract.name)}</h3>
          <p className="text-xs text-slate-500">Historical Performance · stored Agent Run results</p>
        </div>
        <button className="btn-ghost btn-xs" onClick={onClose} aria-label={`Close ${formatKpiName(contract.name)} detail`}>
          Close
        </button>
      </header>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(220px,0.8fr)_minmax(0,1.6fr)]">
        <section className="rounded-xl border border-slate-200 bg-slate-50/80 p-3">
          <div className="flex items-center justify-between gap-2">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-600">Historical Runs</h4>
            <span className="text-[11px] text-slate-500">{kpiRuns.length} stored</span>
          </div>
          {kpiRuns.length === 0 ? (
            <p className="mt-4 text-xs text-slate-500">No historical runs available.</p>
          ) : (
            <div className="mt-3 max-h-56 space-y-1.5 overflow-y-auto">
              {[...kpiRuns].sort((a, b) => b.target_date.localeCompare(a.target_date)).map((run) => (
                <button
                  key={run.id}
                  className={`flex w-full items-center justify-between rounded-lg border px-2.5 py-2 text-left text-xs transition-colors ${
                    selectedId === run.id ? 'border-sky-300 bg-white shadow-sm' : 'border-transparent hover:border-slate-200 hover:bg-white'
                  }`}
                  onClick={() => {
                    setSelectedId(run.id)
                    setSelectedDate(run.target_date)
                  }}
                >
                  <span className="text-slate-700">{formatDate(run.target_date)}</span>
                  <StatusBadge status={run.status} />
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="rounded-xl border border-slate-200 bg-slate-50/80 p-3">
          <div className="flex items-center justify-between gap-2">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-600">Historical Performance</h4>
            <span className="text-[11px] text-slate-500">Actual values only</span>
          </div>
          {chartRuns.length === 0 ? (
            <p className="mt-4 text-xs text-slate-500">No historical runs available.</p>
          ) : (
            <div className="mt-4 flex h-44 items-end gap-2 overflow-x-auto border-b border-slate-300 pb-5">
              {chartRuns.map((run) => {
                const height = `${Math.max(8, (Math.abs(run.actual_value ?? 0) / maxActual) * 100)}%`
                return (
                  <button
                    key={run.id}
                    className="group flex h-full min-w-8 flex-1 flex-col items-center justify-end gap-1"
                    onClick={() => {
                      setSelectedId(run.id)
                      setSelectedDate(run.target_date)
                    }}
                    aria-label={`Performance bar ${run.target_date}`}
                    title={`${formatDate(run.target_date)} · Actual ${valueFor(contract, run.actual_value)}`}
                  >
                    <span className="invisible max-w-24 truncate text-[10px] text-slate-600 group-hover:visible">
                      {valueFor(contract, run.actual_value)}
                    </span>
                    <span className={`w-full rounded-t-md transition-all group-hover:opacity-80 ${statusTone(run.status)} ${selectedId === run.id ? 'ring-2 ring-sky-300 ring-offset-1' : ''}`} style={{ height }} />
                    <span className="text-[9px] text-slate-500">{run.target_date.slice(8)}</span>
                  </button>
                )
              })}
            </div>
          )}
          <div className="mt-2 flex gap-3 text-[10px] text-slate-500">
            <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-emerald-500" />Normal</span>
            <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-rose-500" />Abnormal</span>
            <span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-slate-300" />Low confidence</span>
          </div>
        </section>
      </div>

      <section className="mt-4 rounded-xl border border-slate-200 bg-white p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-600">KPI Load Calendar</h4>
          <div className="flex items-center gap-2">
            <button className="btn-ghost btn-xs" onClick={() => setMonth((current) => shiftMonth(current, -1))} aria-label="Previous month">‹</button>
            <span className="min-w-32 text-center text-sm font-medium text-slate-800">{monthLabel(month)}</span>
            <button className="btn-ghost btn-xs" onClick={() => setMonth((current) => shiftMonth(current, 1))} aria-label="Next month">›</button>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-7 gap-1 text-center text-[10px] font-semibold uppercase tracking-wider text-slate-400">
          {WEEKDAYS.map((day) => <span key={day}>{day}</span>)}
        </div>
        <div className="mt-1 grid grid-cols-7 gap-1">
          {calendarDays(month).map((day) => {
            const run = monthRuns.get(day)
            const outside = !day.startsWith(month)
            const tileClasses = run ? statusTileClasses(run.status) : 'border-transparent bg-slate-50/50 text-slate-500'
            return (
              <button
                key={day}
                className={`min-h-12 rounded-lg border p-1.5 text-left transition-all ${tileClasses} ${outside ? 'opacity-35' : ''} ${selected?.target_date === day ? 'ring-2 ring-sky-300 ring-offset-1' : ''}`}
                onClick={() => {
                  setSelectedDate(day)
                  if (run) {
                    setSelectedId(run.id)
                    setIsRunModalOpen(true)
                  } else {
                    setSelectedId(null)
                    setIsRunModalOpen(false)
                  }
                }}
                aria-label={`${formatDate(day)}${run ? `, ${run.status}` : ', no run available'}`}
              >
                <span className="text-[10px] font-medium">{Number(day.slice(8))}</span>
                {run ? (
                  <span className={`mt-2 block h-2.5 w-2.5 rounded-full ${statusTone(run.status)}`} />
                ) : (
                  <span className="mt-2 block h-2.5 w-2.5 rounded-full bg-slate-300/60" />
                )}
              </button>
            )
          })}
        </div>
      </section>

      {isRunModalOpen && selected && (
        <Overlay>
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4 backdrop-blur-sm">
          <div className="w-full max-w-2xl rounded-2xl border border-slate-200 bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <div>
                <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-sky-700">Run detail</div>
                <h4 className="mt-1 text-base font-semibold text-slate-900">{formatKpiName(contract.name)} · {formatDate(selected.target_date)}</h4>
              </div>
              <button className="btn-ghost btn-xs" onClick={() => setIsRunModalOpen(false)} aria-label="Close run detail">Close</button>
            </div>
            <div className="p-4">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs uppercase tracking-wider text-slate-500">Last 7 historical runs</div>
                <StatusBadge status={selected.status} />
              </div>
              <div className="mt-4 flex h-32 items-end gap-2 overflow-x-auto border-b border-slate-300 pb-4">
                {modalRuns.map((run) => {
                  const height = `${Math.max(10, (Math.abs(run.actual_value ?? 0) / Math.max(...modalRuns.map((candidate) => Math.abs(candidate.actual_value ?? 0)), 1)) * 100)}%`
                  return (
                    <button
                      key={run.id}
                      className="group flex h-full min-w-8 flex-1 flex-col items-center justify-end gap-1"
                      aria-label={`Run detail historical bar ${run.target_date}`}
                      title={`${formatDate(run.target_date)} · Actual ${valueFor(contract, run.actual_value)}`}
                      onClick={() => {
                        setSelectedDate(run.target_date)
                        setSelectedId(run.id)
                      }}
                    >
                      <span className="invisible max-w-24 truncate text-[10px] text-slate-600 group-hover:visible">
                        {valueFor(contract, run.actual_value)}
                      </span>
                      <span className={`w-full rounded-t-md transition-all ${statusTone(run.status)} ${selectedId === run.id ? 'ring-2 ring-sky-300 ring-offset-1' : ''}`} style={{ height }} />
                      <span className="text-[9px] text-slate-500">{run.target_date.slice(8)}</span>
                    </button>
                  )
                })}
              </div>
              <dl className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Actual</dt><dd className="mt-1 text-sm font-semibold text-slate-900">{valueFor(contract, selected.actual_value)}</dd></div>
                <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Expected</dt><dd className="mt-1 text-sm font-semibold text-slate-900">{valueFor(contract, selected.expected_value)}</dd></div>
                <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Deviation</dt><dd className="mt-1 text-sm font-semibold text-slate-900">{signedPercent(selected.deviation_pct)}</dd></div>
                <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Comparison</dt><dd className="mt-1 text-sm font-semibold text-slate-900">{selected.comparison_label ?? 'N/A'}</dd></div>
              </dl>
            </div>
          </div>
          </div>
        </Overlay>
      )}

      <section className="mt-4 rounded-xl border border-sky-200 bg-sky-50/70 p-3">
        {selected ? (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sm font-semibold text-slate-900">{formatKpiName(contract.name)} · {formatDate(selectedDate)}</div>
                <div className="text-[11px] text-slate-500">Persisted detection result · no recalculation</div>
              </div>
              <StatusBadge status={selected.status} />
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Actual</dt><dd className="mt-1 text-sm font-semibold text-slate-900">{valueFor(contract, selected.actual_value)}</dd></div>
              <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Expected</dt><dd className="mt-1 text-sm font-semibold text-slate-900">{valueFor(contract, selected.expected_value)}</dd></div>
              <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Deviation</dt><dd className="mt-1 text-sm font-semibold text-slate-900">{signedPercent(selected.deviation_pct)}</dd></div>
              <div><dt className="text-[10px] uppercase tracking-wider text-slate-500">Status</dt><dd className="mt-1"><StatusBadge status={selected.status} /></dd></div>
            </dl>
          </>
        ) : (
          <EmptyState title="No run available for this date" description={`${formatKpiName(contract.name)} has no persisted Agent Run result for ${formatDate(selectedDate)}.`} />
        )}
      </section>
    </section>
  )
}
