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
 *
 * The findings panel reads the same way. Its lines are headlines the server
 * assembled from stored detection runs — abnormal verdicts the engine reached and
 * wrote down — over a period this panel selects for itself, separately from the
 * tally window above it. Nothing on it is generated to fill the space: a quiet
 * fortnight says so, and a movement no one has broken down names no cause and
 * says why instead.
 *
 * Any of those rows can be opened into a Copilot summary. That summary is not
 * composed here and not composed by a model from what is on screen: the row's own
 * detection run is sent back to the server, which re-reads the stored evaluation,
 * whatever breakdown exists and the approved documents this reader may see, and
 * returns the labelled sections and the recommended actions it assembled from
 * them. This screen supplies the coordinates and nothing else — which is why the
 * summary is rendered by the same two components the Result page uses rather than
 * by anything local to monitoring.
 */

import { useCallback, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, describeError } from '../api/client'
import type {
  Explanation,
  ExplanationResponse,
  MonitoringHeadline,
  MonitoringMovement,
  MonitoringResponse,
} from '../api/types'
import { useAuth } from '../auth/AuthContext'
import ExplanationCard, { ExplainButton } from '../components/Explanation'
import Recommendations from '../components/Recommendations'
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

/**
 * The findings panel's own periods, which are not the tally window's.
 *
 * A reader asking "what happened lately" works in weeks; a reader checking
 * coverage works in quarters. These four are the values the server accepts —
 * it rejects anything else — so the buttons and the API agree by construction.
 */
const FINDING_WINDOWS = [
  { days: 7, label: '7 days' },
  { days: 14, label: '14 days' },
  { days: 30, label: '1 month' },
  { days: 90, label: '3 months' },
] as const

function movementValue(
  row: { unit?: string | null; currency?: string | null },
  value: number | null | undefined,
): string {
  if (value === null || value === undefined) return '—'
  if (row.currency) return formatCurrency(value, row.currency, true)
  if (row.unit === 'currency') return formatCurrency(value, 'INR', true)
  return formatCompact(value)
}

function signed(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`
}

/**
 * The way into the existing investigation workflow for one movement.
 *
 * The Investigation Center reads the KPI and the date off the query string and
 * runs its own contribution analysis from there — this is a deep link into that
 * workflow, not a second implementation of it.
 */
function investigationHref(kpiKey: string, targetDate: string): string {
  return `/investigation?kpi=${encodeURIComponent(kpiKey)}&date=${targetDate}`
}

/**
 * What a stored breakdown found, or nothing at all.
 *
 * `contributor_is_sufficient === false` is a real answer — a breakdown ran and no
 * single entity explained the movement — so the chip says "largest of several"
 * rather than presenting the leader as the cause. Absent fields render nothing:
 * "not analysed" and "not shown to you" are indistinguishable from here, and
 * neither is worth asserting.
 */
function ContributorChip({
  dimension,
  entity,
  sharePct,
  sufficient,
}: {
  dimension?: string | null
  entity?: string | null
  sharePct?: number | null
  sufficient?: boolean | null
}) {
  if (!entity) return null
  return (
    <span className="chip" title={dimension ? `Breakdown by ${dimension}` : undefined}>
      {entity}
      {sharePct !== null && sharePct !== undefined ? ` · ${sharePct.toFixed(0)}%` : ''}
      {sufficient === false ? ' · largest of several' : ''}
    </span>
  )
}

/**
 * The coordinates a summary needs. Deliberately the narrowest shape that both row
 * types satisfy — an id to key the open row on, and the KPI and date the server
 * resolves the stored run from. No figures: nothing the screen is displaying is
 * sent back, so the summary cannot be built from a number this component rendered.
 */
interface SummarisableRow {
  detection_run_id: string
  kpi_key: string
  target_date: string
}

interface RowSummary {
  /** True for the one row whose summary is open, if any. */
  isOpen: (slot: string, runId: string) => boolean
  pending: boolean
  error: string | null
  explanation: Explanation | null
  toggle: (slot: string, row: SummarisableRow) => void
}

/**
 * One row's Copilot summary, fetched on demand.
 *
 * One at a time, and only when asked. Both halves of that matter: a movement list
 * is eight rows deep, and summarising all of them on load would mean eight
 * retrievals and eight model calls for a reader who wanted one — while keeping two
 * open at once invites reading a sentence about one movement under another's
 * heading.
 *
 * `slot` is why the open row is not identified by its run id. One detection run
 * legitimately appears more than once on this screen — as a movement, and again as
 * the headline that movement earned — and on the run id alone the headline's button
 * would read as "already open", close the movement's panel and open nothing. The
 * panel a row was rendered in is the missing half of its identity.
 *
 * The in-flight guard is the reason for the ref. Two quick clicks start two
 * requests, and without a check on arrival the slower one wins the screen: the
 * answer for the row the reader has already navigated away from would render under
 * the row they are now looking at. Every state write below is gated on the request
 * still being the open one, the error included — a failure belongs to the row that
 * caused it.
 */
function useRowSummary(companyId: string | null | undefined): RowSummary {
  const [openFor, setOpenFor] = useState<string | null>(null)
  const [explanation, setExplanation] = useState<Explanation | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const openRef = useRef<string | null>(null)

  const toggle = useCallback(
    (slot: string, row: SummarisableRow) => {
      const target = `${slot}:${row.detection_run_id}`
      if (openRef.current === target) {
        openRef.current = null
        setOpenFor(null)
        return
      }
      openRef.current = target
      setOpenFor(target)
      setExplanation(null)
      setError(null)
      setPending(true)
      void api
        .post<ExplanationResponse>(`/companies/${companyId}/results/explain`, {
          // The KPI and the date, which is all this endpoint accepts. It finds the
          // stored run itself and every figure in the answer comes from that row.
          kpi_id: row.kpi_key,
          target_date: row.target_date,
        })
        .then((response) => {
          if (openRef.current !== target) return
          setExplanation(response.explanation)
          setPending(false)
        })
        .catch((err: unknown) => {
          if (openRef.current !== target) return
          setError(describeError(err))
          setPending(false)
        })
    },
    [companyId],
  )

  const isOpen = useCallback(
    (slot: string, runId: string) => openFor === `${slot}:${runId}`,
    [openFor],
  )

  return { isOpen, pending, error, explanation, toggle }
}

/**
 * The expanded summary under one row: what the movement means, and what to consider
 * doing about it.
 *
 * Both halves are server-assembled and rendered by the components the Result page
 * uses, so a sentence a reader sees here is the same sentence they would see there.
 * The recommendation panel is given no breakdown runner: monitoring is not where a
 * breakdown is run, and the row's own Investigate link is the way to that. It will
 * therefore state that its advice is aimed at the KPI as a whole rather than offer
 * a button that does not belong on this screen.
 */
function RowSummaryPanel({
  companyId,
  runId,
  summary,
  subject,
}: {
  companyId: string
  runId: string
  summary: RowSummary
  subject: string
}) {
  return (
    <div className="mt-3 space-y-3 rounded-[18px] border border-white/95 bg-white/45 p-3.5">
      <ExplanationCard
        explanation={summary.explanation}
        error={summary.error}
        pending={summary.pending}
        emptyHint={`Reading the stored evaluation of ${subject}…`}
      />
      {/* Only once the explanation has landed. Asking for both at the same instant
          would put two spinners in a row and give the reader nothing to read while
          either resolves; the explanation is the half that answers "what happened". */}
      {summary.explanation && (
        <Recommendations companyId={companyId} runId={runId} />
      )}
    </div>
  )
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
 * it is offered only to a reader who may actually use it — the server says so per
 * row via `can_investigate`, with the reader's own permission as the fallback for
 * a payload that predates the field. The third way in stays on this screen: the
 * Copilot summary, which expands underneath.
 */
function MovementRow({
  row,
  mayInvestigate,
  showExecuted,
  companyId,
  summary,
  slot,
}: {
  row: MonitoringMovement
  mayInvestigate: boolean
  showExecuted?: string | null
  companyId: string
  summary: RowSummary
  /** Which panel this row is rendered in. See `useRowSummary`. */
  slot: string
}) {
  const canInvestigate = row.can_investigate ?? mayInvestigate
  const summaryOpen = summary.isOpen(slot, row.detection_run_id)
  return (
    <li className="px-4 py-2.5 hover:bg-white/40">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
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
        {/* What an investigation already concluded, copied from its stored breakdown. */}
        <ContributorChip
          dimension={row.contributor_dimension}
          entity={row.contributor_entity}
          sharePct={row.contributor_share_pct}
          sufficient={row.contributor_is_sufficient}
        />
        {/* Null means "not disclosed to you", so nothing is said either way. */}
        {row.open_findings !== null && row.open_findings > 0 && (
          <span className="chip">
            {row.open_findings} open note{row.open_findings === 1 ? '' : 's'}
          </span>
        )}
        <ExplainButton
          tone="ghost"
          label={summaryOpen ? 'Hide Copilot summary' : 'Copilot summary'}
          pending={summaryOpen && summary.pending}
          onClick={() => summary.toggle(slot, row)}
          title="Assembled by the server from this movement's stored evaluation, any recorded breakdown and approved documents you may see"
        />
        {canInvestigate && (
          <Link to={investigationHref(row.kpi_key, row.target_date)} className="btn btn-xs btn-ghost">
            {row.has_contribution ? 'Review investigation' : 'Investigate'}
          </Link>
        )}
      </div>
      {summaryOpen && (
        <RowSummaryPanel
          companyId={companyId}
          runId={row.detection_run_id}
          summary={summary}
          subject={`${formatKpiName(row.kpi_name)} on ${formatDate(row.target_date)}`}
        />
      )}
    </li>
  )
}

/**
 * One finding, as a business reader would hear it.
 *
 * The sentence arrives from the server already written — assembled there from the
 * KPI's own name, the run's own date and figures the detection engine computed —
 * so this component chooses a layout and nothing else. It does not compose the
 * claim, and it never fills a missing cause: where no breakdown named a
 * contributor, `contributor_note` says why and that is what shows.
 */
function HeadlineRow({
  row,
  companyId,
  summary,
}: {
  row: MonitoringHeadline
  companyId: string
  summary: RowSummary
}) {
  const summaryOpen = summary.isOpen('headline', row.detection_run_id)
  return (
    <li className="px-4 py-3 hover:bg-white/40">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <Link
          to={`/results/${row.detection_run_id}`}
          className="text-sm font-medium leading-snug text-slate-100 hover:underline"
        >
          {row.headline}
        </Link>
        <StatusBadge status={row.status} />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span>{formatKpiName(row.kpi_name)}</span>
        <span>{formatDate(row.target_date)}</span>
        <span className="tabular-nums">
          {movementValue(row, row.actual_value)} vs {movementValue(row, row.expected_value)}
        </span>
        <span
          className={`tabular-nums font-medium ${
            row.status === 'ABNORMAL' ? 'text-rose-300' : 'text-slate-400'
          }`}
        >
          {signed(row.deviation_pct)}
        </span>
        <ContributorChip
          dimension={row.contributor_dimension}
          entity={row.contributor_entity}
          sharePct={row.contributor_share_pct}
          sufficient={row.contributor_is_sufficient}
        />
        {row.contributor_note && <span className="italic">{row.contributor_note}</span>}
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <ExplainButton
            tone="ghost"
            label={summaryOpen ? 'Hide Copilot summary' : 'Copilot summary'}
            pending={summaryOpen && summary.pending}
            onClick={() => summary.toggle('headline', row)}
            title="Assembled by the server from this movement's stored evaluation, any recorded breakdown and approved documents you may see"
          />
          {row.can_investigate && (
            <Link
              to={investigationHref(row.kpi_key, row.target_date)}
              className="btn btn-xs btn-ghost"
            >
              {row.contributor_entity ? 'Review investigation' : 'Investigate'}
            </Link>
          )}
        </span>
      </div>
      {summaryOpen && (
        <RowSummaryPanel
          companyId={companyId}
          runId={row.detection_run_id}
          summary={summary}
          subject={`${formatKpiName(row.kpi_name)} on ${formatDate(row.target_date)}`}
        />
      )}
    </li>
  )
}

export default function MonitoringOverview() {
  const { companyId, can } = useAuth()
  const mayView = can('analytics.read')
  const mayInvestigate = can('investigation.read')

  const [windowDays, setWindowDays] = useState<number>(90)
  // The findings panel's period is separate state: widening the tally window to a
  // year should not silently turn "what happened this week" into a year of news.
  const [findingsWindowDays, setFindingsWindowDays] = useState<number>(7)

  // One summary across the whole screen, so opening a headline closes whichever
  // movement was open. Declared here rather than per row because "one at a time"
  // is a property of the screen, and a hook per row could not enforce it.
  const summary = useRowSummary(companyId)

  const monitoring = useResource<MonitoringResponse>(
    () =>
      api.get(`/companies/${companyId}/monitoring`, {
        query: { window_days: windowDays, findings_window_days: findingsWindowDays },
      }),
    [companyId, windowDays, findingsWindowDays],
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
  const headlines = data.headlines ?? []
  // Offer only the periods this server accepts. It validates the parameter and
  // rejects anything else, so a button it would refuse should not exist.
  const serverWindows = data.findings_window_options ?? []
  const findingWindowOptions =
    serverWindows.length > 0
      ? FINDING_WINDOWS.filter((option) => serverWindows.includes(option.days))
      : [...FINDING_WINDOWS]
  const headlineTotal = data.headline_total ?? headlines.length

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
                  companyId={companyId ?? ''}
                  summary={summary}
                  slot="movement"
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
                  companyId={companyId ?? ''}
                  summary={summary}
                  slot="abnormal"
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

      {/* Findings, as headlines, over their own period.
          Every line here is derived from a stored detection run — an abnormal
          verdict the engine reached and wrote down. Nothing is generated for the
          panel, so an empty period is reported as empty rather than filled. */}
      <Panel
        title="Recent findings"
        bodyClassName=""
        actions={
          <div className="glass-nav w-fit rounded-[14px] p-1">
            {findingWindowOptions.map((option) => (
              <button
                key={option.days}
                type="button"
                onClick={() => setFindingsWindowDays(option.days)}
                className={`nav-pill px-2.5 py-1.5 text-xs ${
                  findingsWindowDays === option.days ? 'nav-pill-active' : ''
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        }
      >
        {headlines.length === 0 ? (
          <EmptyState
            title={`Nothing abnormal was recorded in the last ${findingsWindowDays} days`}
            description="A finding appears here once a stored detection run has flagged a movement as abnormal in this period."
          />
        ) : (
          <>
            <p className="px-4 pt-3 text-[11px] text-slate-500">
              {headlineTotal} abnormal movement{headlineTotal === 1 ? '' : 's'} in the last{' '}
              {data.findings_window_days ?? findingsWindowDays} days
              {data.findings_window_from && data.findings_window_to
                ? ` · ${formatDate(data.findings_window_from)} to ${formatDate(
                    data.findings_window_to,
                  )}`
                : ''}
              {headlineTotal > headlines.length
                ? ` · showing the ${headlines.length} largest`
                : ''}
            </p>
            <ul className="divide-y divide-ink-800">
              {headlines.map((row) => (
                <HeadlineRow
                  key={row.detection_run_id}
                  row={row}
                  companyId={companyId ?? ''}
                  summary={summary}
                />
              ))}
            </ul>
          </>
        )}
      </Panel>

      {/* People's own notes, kept separate from the derived headlines above: one is
          what the engine measured, the other is what a colleague wrote. */}
      {mayInvestigate && recentFindings.length > 0 && (
        <Panel title="Investigation notes" bodyClassName="">
          <ul className="divide-y divide-ink-800">
            {recentFindings.map((finding) => (
              <li
                key={finding.id}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2.5"
              >
                <span className="chip">{finding.status.replace(/_/g, ' ')}</span>
                <Link
                  to={investigationHref(finding.kpi_key, finding.target_date)}
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
