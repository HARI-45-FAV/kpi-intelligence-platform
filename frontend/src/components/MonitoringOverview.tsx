/**
 * The monitoring overview: what has actually been evaluated, and what moved.
 *
 * One governed read — `GET /companies/{id}/monitoring` — and every figure on the
 * screen is a count of stored rows or a copy of one. There is no arithmetic here
 * beyond choosing a colour, no projection, and no placeholder standing in for a
 * real number: a company that has never run detection sees zeros and is told so,
 * which is more useful than a dashboard that looks populated.
 *
 * Two honesty rules the layout exists to serve:
 *
 *  - **It must not imply a scheduler.** There isn't one in this version. The
 *    server's own note says so and is rendered at the top rather than tucked into
 *    a tooltip, and "last evaluated" is the timestamp of the most recent stored
 *    run — so a company whose last run was in March reads March, not "monitoring
 *    active".
 *  - **Withheld is not zero.** The investigation figures arrive as `null` for a
 *    reader without `investigation.read`. Those readers get no findings strip at
 *    all, rather than one reading "0 open" — which would be a claim about other
 *    people's work that this reader is not entitled to and that may be false.
 *
 * Every movement row is a way in: the result it came from, and the investigation
 * that would explain it.
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { MonitoringMovement, MonitoringResponse } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import {
  formatCompact,
  formatCurrency,
  formatDate,
  formatKpiName,
  formatNumber,
  formatRelative,
} from '../components/format'
import { Alert, EmptyState, Panel, Spinner, StatusBadge } from '../components/ui'
import { useResource } from '../components/useResource'

const WINDOWS = [
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
  { days: 365, label: '12 months' },
] as const

function movementValue(row: MonitoringMovement, value: number | null): string {
  if (value === null || value === undefined) return '—'
  if (row.currency) return formatCurrency(value, row.currency, true)
  if (row.unit === 'currency') return formatCurrency(value, 'INR', true)
  return formatCompact(value)
}

function signed(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`
}

function Tile({
  label,
  value,
  hint,
  tone = 'text-slate-100',
}: {
  label: string
  value: string | number
  hint?: string
  tone?: string
}) {
  return (
    <div className="rounded-[20px] border border-white/95 bg-white/50 p-3.5 shadow-[0_11px_22px_rgba(50,103,145,0.08)] backdrop-blur-md">
      <div className="text-[11px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`mt-1.5 text-2xl font-semibold tabular-nums ${tone}`}>{value}</div>
      {hint && <div className="mt-1 text-[11px] leading-snug text-slate-500">{hint}</div>}
    </div>
  )
}

/**
 * One movement, as a row that leads somewhere.
 *
 * The primary click is the Result page, because a movement without its verdict's
 * evidence is only a number. The secondary link is the Investigation Center, and
 * it is offered only to a reader who may actually use it.
 */
function MovementRow({
  row,
  mayInvestigate,
  showExecuted,
}: {
  row: MonitoringMovement
  mayInvestigate: boolean
  showExecuted?: string | null
}) {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5 hover:bg-white/40">
      <Link
        to={`/results/${row.detection_run_id}`}
        className="min-w-[10rem] flex-1 text-sm font-medium text-slate-100 hover:underline"
      >
        {formatKpiName(row.kpi_name)}
      </Link>
      <span className="text-xs text-slate-500">{formatDate(row.target_date)}</span>
      <span className="tabular-nums text-sm text-slate-200">
        {movementValue(row, row.actual_value)}
      </span>
      <span className="text-[11px] text-slate-500">
        vs {movementValue(row, row.expected_value)}
      </span>
      <span
        className={`tabular-nums text-sm font-medium ${
          row.status === 'ABNORMAL' ? 'text-rose-300' : 'text-slate-300'
        }`}
      >
        {signed(row.deviation_pct)}
      </span>
      <StatusBadge status={row.status} />
      {showExecuted && (
        <span className="text-[11px] text-slate-600">{formatRelative(showExecuted)}</span>
      )}
      {/* Null means "not disclosed to you", so nothing is said either way. */}
      {row.open_findings !== null && row.open_findings > 0 && (
        <span className="chip">
          {row.open_findings} open note{row.open_findings === 1 ? '' : 's'}
        </span>
      )}
      {mayInvestigate && (
        <Link
          to={`/investigation?kpi=${encodeURIComponent(row.kpi_key)}&date=${row.target_date}`}
          className="btn btn-xs btn-ghost"
        >
          {row.has_contribution ? 'Review investigation' : 'Investigate'}
        </Link>
      )}
    </li>
  )
}

export default function MonitoringOverview() {
  const { companyId, can } = useAuth()
  const mayView = can('analytics.read')
  const mayInvestigate = can('investigation.read')

  const [windowDays, setWindowDays] = useState<number>(90)

  const monitoring = useResource<MonitoringResponse>(
    () =>
      api.get(`/companies/${companyId}/monitoring`, { query: { window_days: windowDays } }),
    [companyId, windowDays],
    { enabled: Boolean(companyId) && mayView },
  )

  const data = monitoring.data
  const counts = data?.counts

  // Recent abnormalities the "biggest movements" list already shows are not
  // repeated: two lists with the same rows reads as more evidence than there is.
  //
  // Read defensively. The server always sends both lists, but a partial or
  // unexpected payload must degrade to an empty panel rather than take the whole
  // Monitoring page down with it — the run-detection surface below this one still
  // works, and a reader should keep it.
  const alsoAbnormal = useMemo(() => {
    const biggest = data?.biggest_movements ?? []
    const abnormal = data?.recent_abnormal ?? []
    const shown = new Set(biggest.map((row) => row.detection_run_id))
    return abnormal.filter((row) => !shown.has(row.detection_run_id))
  }, [data])

  if (!mayView) return null

  if (monitoring.loading && !data) return <Spinner label="Reading stored evaluations…" />

  if (monitoring.error) {
    return (
      <Alert tone="error">
        Unable to load the monitoring overview. ({monitoring.error})
      </Alert>
    )
  }

  if (!data || !counts) return null

  const neverEvaluated = data.last_evaluated_at === null
  // Same defensiveness as the memo above, for the same reason.
  const biggestMovements = data.biggest_movements ?? []
  const recentRuns = data.recent_runs ?? []
  const monitoredKpis = data.kpis ?? []
  const recentFindings = data.recent_findings ?? []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Overview</p>
          <h2 className="mt-1 text-lg font-semibold text-slate-100">
            What has been evaluated
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {data.window_from && data.window_to
              ? `Stored evaluations from ${formatDate(data.window_from)} to ${formatDate(
                  data.window_to,
                )}`
              : `No evaluation stored in the last ${data.window_days} days`}
            {' · '}
            {neverEvaluated
              ? 'nothing has ever been evaluated for this company'
              : `last run ${formatRelative(data.last_evaluated_at)}`}
          </p>
        </div>
        <div className="glass-nav w-fit rounded-[14px] p-1">
          {WINDOWS.map((option) => (
            <button
              key={option.days}
              type="button"
              onClick={() => setWindowDays(option.days)}
              className={`nav-pill px-2.5 py-1.5 text-xs ${
                windowDays === option.days ? 'nav-pill-active' : ''
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {/* The server's own words about what "monitoring" does and does not mean. */}
      <Alert tone="info">{data.monitoring_note}</Alert>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <Tile
          label="KPIs monitored"
          value={counts.kpis_monitored}
          hint="Registered with an active version"
        />
        <Tile
          label="Evaluated"
          value={counts.evaluated}
          hint={`Stored runs in ${data.window_days} days`}
        />
        <Tile
          label="Normal"
          value={counts.normal}
          tone="text-emerald-300"
          hint="In line with comparable history"
        />
        <Tile
          label="Abnormal"
          value={counts.abnormal}
          tone="text-rose-300"
          hint="Outside what history supports"
        />
        <Tile
          label="Low confidence"
          value={counts.low_confidence}
          tone="text-slate-300"
          hint="Too little comparable history to judge"
        />
        <Tile
          label="Not evaluated"
          value={counts.not_evaluated}
          tone="text-sky-300"
          hint="No run in this window — not a pass"
        />
      </div>

      {/* A stored row carrying a verdict from an earlier schema. Named rather than
          folded into one of the three, so the tiles still sum to the total. */}
      {counts.unrecognised > 0 && (
        <Alert tone="warn">
          {counts.unrecognised} stored evaluation{counts.unrecognised === 1 ? '' : 's'} carry a
          verdict this version does not recognise
          {counts.unrecognised_statuses.length > 0
            ? ` (${counts.unrecognised_statuses.join(', ')})`
            : ''}
          . They are counted here but not interpreted as normal, abnormal or low confidence.
        </Alert>
      )}

      {/* Rendered only when the reader is entitled to the investigation layer —
          `null` means withheld, and "0 open" would be a claim, not a blank. */}
      {mayInvestigate && data.findings_open !== null && (
        <div className="flex flex-wrap items-center gap-2 rounded-[18px] border border-white/95 bg-white/45 px-4 py-2.5">
          <span className="text-[11px] uppercase tracking-wider text-slate-500">
            Investigation findings
          </span>
          <span className="chip">{data.findings_open} open</span>
          <span className="chip">{data.findings_in_progress} in progress</span>
          <span className="chip">{data.findings_resolved} resolved</span>
          <Link to="/investigation" className="btn btn-xs btn-ghost ml-auto">
            Open the Investigation Center
          </Link>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Biggest movements" bodyClassName="">
          {biggestMovements.length === 0 ? (
            <EmptyState
              title="No movement recorded in this window"
              description="A movement appears here once detection has been run and stored a deviation for a KPI."
            />
          ) : (
            <ul className="divide-y divide-ink-800">
              {biggestMovements.map((row) => (
                <MovementRow
                  key={row.detection_run_id}
                  row={row}
                  mayInvestigate={mayInvestigate}
                />
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Recent abnormalities" bodyClassName="">
          {counts.abnormal === 0 ? (
            <EmptyState
              title="Nothing flagged in this window"
              description="No stored evaluation in this period was outside what its comparable history supports."
            />
          ) : alsoAbnormal.length === 0 ? (
            <div className="px-4 py-6 text-center text-xs leading-relaxed text-slate-500">
              All {counts.abnormal} abnormal result{counts.abnormal === 1 ? '' : 's'} in this
              window {counts.abnormal === 1 ? 'is' : 'are'} already listed under the biggest
              movements.
            </div>
          ) : (
            <ul className="divide-y divide-ink-800">
              {alsoAbnormal.map((row) => (
                <MovementRow
                  key={row.detection_run_id}
                  row={row}
                  mayInvestigate={mayInvestigate}
                />
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Recent detection runs" bodyClassName="">
          {recentRuns.length === 0 ? (
            <EmptyState
              title="No detection has been run in this window"
              description="Run detection below to evaluate the KPIs that are ready."
            />
          ) : (
            <ul className="divide-y divide-ink-800">
              {recentRuns.map((run) => (
                <li
                  key={run.detection_run_id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 hover:bg-white/40"
                >
                  <Link
                    to={`/results/${run.detection_run_id}`}
                    className="min-w-[9rem] flex-1 text-sm text-slate-200 hover:underline"
                  >
                    {formatKpiName(run.kpi_name)}
                  </Link>
                  <span className="text-xs text-slate-500">{formatDate(run.target_date)}</span>
                  <span className="tabular-nums text-xs text-slate-400">
                    {signed(run.deviation_pct)}
                  </span>
                  <StatusBadge status={run.status} />
                  {/* A real stored timestamp, not a synthesised timeline. */}
                  <span className="text-[11px] text-slate-600">
                    {formatRelative(run.executed_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Monitored KPIs" bodyClassName="">
          {monitoredKpis.length === 0 ? (
            <EmptyState
              title="No KPI is registered yet"
              description="A KPI appears here once it has been registered and approved in KPI Setup."
              action={
                <Link to="/kpi-setup/kpis" className="btn-primary btn-xs">
                  Open KPI Setup
                </Link>
              }
            />
          ) : (
            <ul className="divide-y divide-ink-800">
              {monitoredKpis.map((kpi) => (
                <li
                  key={kpi.kpi_id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2"
                >
                  <span className="min-w-[9rem] flex-1 text-sm text-slate-200">
                    {formatKpiName(kpi.kpi_name)}
                  </span>
                  <span className="chip">{kpi.lifecycle_status}</span>
                  {kpi.latest_status ? (
                    <>
                      <StatusBadge status={kpi.latest_status} />
                      <span className="text-[11px] text-slate-500">
                        {formatDate(kpi.latest_target_date)} ·{' '}
                        {formatNumber(kpi.evaluated_in_window)} run
                        {kpi.evaluated_in_window === 1 ? '' : 's'} in window
                      </span>
                    </>
                  ) : (
                    <span className="text-[11px] text-slate-500">Never evaluated</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      {mayInvestigate && recentFindings.length > 0 && (
        <Panel title="Recent findings" bodyClassName="">
          <ul className="divide-y divide-ink-800">
            {recentFindings.map((finding) => (
              <li
                key={finding.id}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2.5"
              >
                <span className="chip">{finding.status.replace(/_/g, ' ')}</span>
                <Link
                  to={`/investigation?kpi=${encodeURIComponent(finding.kpi_key)}&date=${
                    finding.target_date
                  }`}
                  className="text-sm font-medium text-slate-200 hover:underline"
                >
                  {finding.title}
                </Link>
                <span className="flex-1 text-[11px] text-slate-500">
                  {formatKpiName(finding.kpi_name)} · {formatDate(finding.target_date)}
                  {finding.scope_label ? ` · ${finding.scope_label}` : ''}
                </span>
                {/* Only timestamps the database actually holds. */}
                <span className="text-[11px] text-slate-600">
                  {finding.updated_by_email ?? finding.created_by_email ?? 'unknown author'} ·{' '}
                  {formatRelative(finding.updated_at)}
                </span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  )
}
