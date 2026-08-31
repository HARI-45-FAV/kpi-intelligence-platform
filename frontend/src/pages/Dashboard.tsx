/**
 * Overall Dashboard: date + Agent Run → KPI result cards, and a Stage Performance
 * Summary over a selectable period. Both open reusable floating panels.
 *
 * KPI identity always comes from the database (`/kpi-contracts`), never from a
 * hardcoded list and never from KPI Setup's component state — which is what
 * proves persistence actually works. The number of cards is `contracts.length`.
 *
 * Every analytical value on this screen is measured, not generated. Agent Run
 * calls `POST /run-detection/batch`, which reads each KPI's registered source with
 * its approved formula and returns actual, expected, deviation and status; the
 * Stage Performance Summary counts the detection runs the platform actually
 * stored in the selected window. There is no arithmetic in this file beyond
 * choosing a colour and tallying stored verdicts — detection happens in one
 * deterministic place on the server, and this screen displays what it decided.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, describeError } from '../api/client'
import type {
  CopilotChatResponse,
  DetectionBatchResponse,
  DetectionResult,
  DetectionRunSummary,
  KpiContract,
} from '../api/types'
import { useAuth } from '../auth/AuthContext'
import {
  formatCompact,
  formatCurrency,
  formatDate,
  formatKpiName,
  formatNumber,
} from '../components/format'
import { Alert, EmptyState, Modal, Panel, Spinner, StatusBadge } from '../components/ui'
import { useAction, useResource } from '../components/useResource'
import { useCopilot, useCopilotScreen } from '../copilot/CopilotProvider'
import KPIDetailDashboard from './KPIDetailDashboard'

/* ---------------------------------------------------------------- formatting */

function kpiValue(contract: KpiContract, value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (contract.unit === 'currency' || contract.currency) {
    return formatCurrency(value, contract.currency ?? 'INR', true)
  }
  return formatCompact(value)
}

function signed(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return '—'
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`
}

/**
 * Deviation is coloured by the verdict, never by its sign.
 *
 * A rising number is not automatically good news — refunds, cost per order and
 * churn all get worse as they grow — and the engine has already weighed the
 * movement against this KPI's own tolerance and direction. Colouring by sign
 * would quietly contradict that judgement in green.
 */
function deviationTone(status: string): string {
  if (status === 'ABNORMAL') return 'text-rose-300'
  return 'text-slate-300'
}

/** How a verdict reads, in the words a business owner would use. */
const STATUS_MEANING: Record<string, string> = {
  NORMAL: 'In line with comparable history.',
  ABNORMAL: 'Outside comparable history by more than this KPI tolerates.',
  LOW_CONFIDENCE:
    'Not enough comparable history to judge yet. The measurement stands; the verdict does not.',
}

const PERIODS = [
  ['30d', 30],
  ['60d', 60],
  ['90d', 90],
] as const

const KPI_SELECTION_KEY = 'bi.ai.dashboard-kpis'

/** The server evaluates at most this many KPIs per batch request. */
const BATCH_LIMIT = 25

/** How many stored runs the period summary reads. */
const RUN_HISTORY_LIMIT = 200

function readSelectedKpis(): string[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(KPI_SELECTION_KEY)
    if (raw === null) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

function isoToday(): string {
  return new Date().toISOString().slice(0, 10)
}

function shiftDays(iso: string, days: number): string {
  const date = new Date(`${iso}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

/* --------------------------------------------------- stored-run summarisation */

interface StageStats {
  totalRuns: number
  normal: number
  abnormal: number
  lowConfidence: number
  normalPct: number
}

const EMPTY_STAGE: StageStats = {
  totalRuns: 0,
  normal: 0,
  abnormal: 0,
  lowConfidence: 0,
  normalPct: 0,
}

/**
 * Verdicts the platform actually reached in the window, tallied per KPI.
 *
 * Nothing is inferred: a KPI that was never evaluated in the period reports zero
 * runs, which is the truth and is more useful than a plausible-looking rate.
 */
function tallyRuns(
  runs: DetectionRunSummary[],
  window: { start: string; end: string },
): Map<string, StageStats> {
  const byKpi = new Map<string, StageStats>()
  for (const run of runs) {
    if (run.target_date < window.start || run.target_date > window.end) continue
    const current = byKpi.get(run.kpi_key) ?? { ...EMPTY_STAGE }
    current.totalRuns += 1
    if (run.status === 'NORMAL') current.normal += 1
    else if (run.status === 'ABNORMAL') current.abnormal += 1
    else current.lowConfidence += 1
    byKpi.set(run.kpi_key, current)
  }
  for (const stats of byKpi.values()) {
    stats.normalPct = stats.totalRuns === 0 ? 0 : (stats.normal / stats.totalRuns) * 100
  }
  return byKpi
}

/* ------------------------------------------------------- registry consumption */

/** Versions that are no longer part of the confirmed registry. */
const RETIRED_STATUSES = new Set(['REJECTED', 'DEPRECATED'])

/**
 * One card per KPI, never one per version, and only KPIs that are live.
 *
 * A KPI reaches the dashboard by being activated in KPI Registration — that is
 * the whole contract between the two screens. Registered-but-not-yet-activated
 * KPIs are counted separately so an empty dashboard can explain itself instead of
 * just looking broken.
 */
function liveContracts(contracts: KpiContract[]): KpiContract[] {
  const best = new Map<string, KpiContract>()
  for (const contract of contracts) {
    if (contract.status !== 'ACTIVE') continue
    const current = best.get(contract.kpi_definition_id)
    if (!current || contract.version > current.version) {
      best.set(contract.kpi_definition_id, contract)
    }
  }
  return [...best.values()].sort((a, b) => a.name.localeCompare(b.name))
}

/** Registered but not live yet — the reason a dashboard can look empty. */
function awaitingActivation(contracts: KpiContract[]): number {
  const ids = new Set<string>()
  for (const contract of contracts) {
    if (contract.status === 'ACTIVE' || RETIRED_STATUSES.has(contract.status)) continue
    ids.add(contract.kpi_definition_id)
  }
  return ids.size
}

/* ------------------------------------------------------------------ dashboard */

export default function Dashboard() {
  const { companyId, membership, can } = useAuth()
  const mayRun = can('detection.run')

  // Every version, not just ACTIVE ones. A KPI is "confirmed" here once it is in
  // the registry and not rejected or deprecated — requiring full activation would
  // blank the dashboard for a registry that is still walking the approval flow.
  const kpis = useResource<{ contracts: KpiContract[]; count: number }>(
    () =>
      api.get(`/companies/${companyId}/kpi-contracts`, { query: { active_only: false } }),
    [companyId],
  )

  // The verdicts already on record. They populate the period summary, and they
  // are what the screen can honestly show before anybody presses Agent Run.
  const history = useResource<DetectionRunSummary[]>(
    () =>
      api.get(`/companies/${companyId}/detection-runs`, { query: { limit: RUN_HISTORY_LIMIT } }),
    [companyId],
    { enabled: Boolean(companyId) },
  )

  const [date, setDate] = useState(isoToday)
  const [batch, setBatch] = useState<DetectionBatchResponse | null>(null)
  const [openKpi, setOpenKpi] = useState<KpiContract | null>(null)
  const run = useAction()

  const [periodDays, setPeriodDays] = useState<number>(30)
  const [custom, setCustom] = useState<{ start: string; end: string } | null>(null)
  const [draftStart, setDraftStart] = useState(shiftDays(isoToday(), -30))
  const [draftEnd, setDraftEnd] = useState(isoToday)
  const [openStage, setOpenStage] = useState<KpiContract | null>(null)

  const contracts = useMemo(() => liveContracts(kpis.data?.contracts ?? []), [kpis.data])
  const notLiveCount = useMemo(
    () => awaitingActivation(kpis.data?.contracts ?? []),
    [kpis.data],
  )
  const [selectionVersion, setSelectionVersion] = useState(0)

  useEffect(() => {
    const syncSelection = () => setSelectionVersion((value) => value + 1)
    window.addEventListener('storage', syncSelection)
    window.addEventListener('kpi-selection-updated', syncSelection)
    return () => {
      window.removeEventListener('storage', syncSelection)
      window.removeEventListener('kpi-selection-updated', syncSelection)
    }
  }, [])

  const selectedKpis = useMemo(() => readSelectedKpis(), [selectionVersion])

  // KPI Setup stores definition uuids; a contract carries both that uuid and the
  // business key, so accept either. Anything else is a stale selection.
  const matchesSelection = useCallback(
    (contract: KpiContract) =>
      selectedKpis.includes(contract.kpi_definition_id) || selectedKpis.includes(contract.kpi_id),
    [selectedKpis],
  )

  // A selection that matches nothing in the registry is stale (a different
  // workspace, deleted KPIs, an older id scheme). Showing every KPI is the honest
  // fallback — an empty dashboard next to a "Saved (4)" badge is a contradiction.
  const selectionUsable = useMemo(
    () => selectedKpis.length > 0 && contracts.some(matchesSelection),
    [contracts, matchesSelection, selectedKpis],
  )

  const visibleContracts = useMemo(
    () => (selectionUsable ? contracts.filter(matchesSelection) : contracts),
    [contracts, matchesSelection, selectionUsable],
  )

  // The server evaluates at most BATCH_LIMIT KPIs per request. Asking for the whole
  // list and letting the tail be dropped silently would leave cards blank with no
  // explanation, so the cut is made here and said out loud.
  const evaluating = useMemo(() => visibleContracts.slice(0, BATCH_LIMIT), [visibleContracts])
  const beyondBatch = visibleContracts.length - evaluating.length

  /** Fresh detection results from this run, by KPI business key. */
  const results = useMemo(() => {
    const byKey = new Map<string, DetectionResult>()
    for (const item of batch?.results ?? []) byKey.set(item.result.kpi_key, item.result)
    return byKey
  }, [batch])

  const skipped = useMemo(() => {
    const byKey = new Map<string, string>()
    for (const item of batch?.skipped ?? []) byKey.set(item.kpi_id, item.reason)
    return byKey
  }, [batch])

  const agentRun = useCallback(async () => {
    if (!companyId) return
    const response = await run.run(() =>
      api.post<DetectionBatchResponse>(`/companies/${companyId}/run-detection/batch`, {
        target_date: date,
        kpi_ids: evaluating.map((contract) => contract.kpi_id),
      }),
    )
    if (response) {
      setBatch(response)
      // The period summary counts stored runs, and this run just added some.
      void history.reload()
    }
  }, [companyId, date, evaluating, history, run])

  const window_ = useMemo(() => {
    if (custom) return { start: custom.start, end: custom.end }
    const end = isoToday()
    return { start: shiftDays(end, -periodDays), end }
  }, [custom, periodDays])

  const windowDays = useMemo(() => {
    const ms =
      new Date(`${window_.end}T00:00:00Z`).getTime() -
      new Date(`${window_.start}T00:00:00Z`).getTime()
    return Math.max(1, Math.round(ms / 86_400_000))
  }, [window_])

  const stageStats = useMemo(
    () => tallyRuns(history.data ?? [], window_),
    [history.data, window_],
  )

  // What the Copilot inherits from this screen. The open KPI detail wins over
  // the grid, because that is what the user is reading.
  //
  // `pinnedKpi` keeps that inheritance alive after the detail modal closes:
  // asking from the modal has to dismiss it (the modal sits above the drawer),
  // and without the pin the KPI would be withdrawn a tick before the question is
  // sent. It clears when the Copilot closes.
  const { openPanel, open: copilotOpen } = useCopilot()
  const [pinnedKpi, setPinnedKpi] = useState<KpiContract | null>(null)
  useEffect(() => {
    if (!copilotOpen) setPinnedKpi(null)
  }, [copilotOpen])
  const focusKpi = openKpi ?? pinnedKpi

  // Which panel is asking, too: one KPI in focus means the detail view is the
  // subject, and otherwise the question came from the summary of every KPI on the
  // date, where reading one row's deviation against another's is the easy mistake.
  useCopilotScreen({
    panel: focusKpi ? 'detection_detail' : 'stage_performance',
    kpiId: focusKpi?.kpi_id ?? null,
    kpiVersion: focusKpi?.version ?? null,
    selectedDate: batch?.target_date ?? date,
    label: focusKpi?.name ?? null,
  })

  if (kpis.loading && !kpis.data) return <Spinner label="Loading confirmed KPIs…" />
  if (kpis.error)
    return <Alert>Unable to load KPIs. Please refresh and try again. ({kpis.error})</Alert>

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">
            {membership?.company_name ?? 'Overall Dashboard'}
          </h1>
          <p className="mt-0.5 text-sm text-slate-500">
            {visibleContracts.length} live KPI{visibleContracts.length === 1 ? '' : 's'}
            {notLiveCount > 0 && ` · ${notLiveCount} not live yet`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {notLiveCount > 0 && (
            <Link
              to="/kpi-setup/kpis"
            className="inline-flex items-center rounded-full border border-sky-200 bg-white/65 px-2.5 py-1 text-[11px] font-medium text-sky-700 shadow-sm transition-all hover:bg-white"
            >
              Activate {notLiveCount}
            </Link>
          )}
          <span
            className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-medium ${
              selectionUsable
                ? 'border-emerald-200 bg-emerald-50/80 text-emerald-700'
                : 'border-slate-200 bg-white/65 text-slate-500'
            }`}
          >
            {selectionUsable ? `Showing ${visibleContracts.length} chosen` : 'Showing all'}
          </span>
        </div>
      </div>

      {/* ---------------------------------------------- main panel: agent run */}
      <Panel
        title="KPI evaluation"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-slate-400">
              Date
              <input
                type="date"
                className="field w-auto px-2 py-1 text-xs"
                value={date}
                onChange={(event) => setDate(event.target.value)}
              />
            </label>
            <button
              className="btn-primary btn-xs"
              onClick={() => void agentRun()}
              disabled={run.pending || visibleContracts.length === 0 || !mayRun}
              title={mayRun ? undefined : 'Requires the detection.run permission.'}
            >
              {run.pending ? 'Running…' : 'Agent Run'}
            </button>
          </div>
        }
        bodyClassName=""
      >
        {run.error && (
          <div className="px-4 pt-3">
            <Alert onDismiss={run.reset}>{run.error}</Alert>
          </div>
        )}

        {visibleContracts.length === 0 ? (
          <EmptyState
            title={
              notLiveCount > 0
                ? `${notLiveCount} KPI${notLiveCount === 1 ? ' is' : 's are'} registered but not live yet`
                : 'No KPIs yet'
            }
            description={
              notLiveCount > 0
                ? 'Open KPI Registration and click Activate. Live KPIs appear here automatically.'
                : 'Register the KPIs you want to track in KPI Registration. They appear here once live.'
            }
            action={
              <Link to="/kpi-setup/kpis" className="btn-primary btn-xs">
                {notLiveCount > 0 ? 'Activate KPIs' : 'Open KPI Registration'}
              </Link>
            }
          />
        ) : !batch ? (
          <EmptyState
            title="Select a date and run the agent"
            description={`Ready to evaluate ${visibleContracts.length} confirmed KPI${
              visibleContracts.length === 1 ? '' : 's'
            } for ${formatDate(date)}.`}
          />
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 border-b border-ink-800 px-4 py-2.5">
              <span className="text-xs text-slate-500">Evaluated for</span>
              <span className="text-sm font-medium text-slate-100">
                {formatDate(batch.target_date)}
              </span>
              <span className="text-[11px] text-slate-500">
                · {batch.counts.evaluated} evaluated
                {batch.counts.skipped > 0 && `, ${batch.counts.skipped} could not be`}
              </span>
              {beyondBatch > 0 && (
                <span className="text-[11px] text-slate-500">
                  · one run covers {BATCH_LIMIT} KPIs; {beyondBatch} more were not included
                </span>
              )}
            </div>
            {/* One reusable card, mapped over the registry. */}
            <div className="grid gap-3 p-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
              {visibleContracts.map((contract) => (
                <KpiResultCard
                  key={contract.kpi_version_id}
                  contract={contract}
                  result={results.get(contract.kpi_id) ?? null}
                  skippedReason={skipped.get(contract.kpi_id)}
                  onClick={() => results.get(contract.kpi_id) && setOpenKpi(contract)}
                />
              ))}
            </div>
          </>
        )}
      </Panel>

      {/* -------------------------------------- second panel: stage performance */}
      <Panel
        title="Stage Performance Summary"
        actions={
          <div className="flex flex-wrap items-center gap-1">
            {PERIODS.map(([label, days]) => (
              <button
                key={label}
                onClick={() => {
                  setPeriodDays(days)
                  setCustom(null)
                }}
                className={`rounded px-2.5 py-1 text-xs transition-colors ${
                  !custom && periodDays === days
                    ? 'bg-accent text-white shadow-sm'
                    : 'border border-white/90 bg-white/55 text-slate-400 hover:bg-white hover:text-slate-200'
                }`}
              >
                {label}
              </button>
            ))}
            <button
              onClick={() => setCustom({ start: draftStart, end: draftEnd })}
              className={`rounded px-2.5 py-1 text-xs transition-colors ${
                custom
                  ? 'bg-accent text-white shadow-sm'
                  : 'border border-white/90 bg-white/55 text-slate-400 hover:bg-white hover:text-slate-200'
              }`}
            >
              Custom
            </button>
          </div>
        }
        bodyClassName=""
      >
        {custom && (
          <div className="flex flex-wrap items-end gap-3 border-b border-ink-800 px-4 py-3">
            <label className="text-xs text-slate-400">
              <span className="label">Start date</span>
              <input
                type="date"
                className="field w-auto px-2 py-1 text-xs"
                value={draftStart}
                onChange={(event) => setDraftStart(event.target.value)}
              />
            </label>
            <label className="text-xs text-slate-400">
              <span className="label">End date</span>
              <input
                type="date"
                className="field w-auto px-2 py-1 text-xs"
                value={draftEnd}
                onChange={(event) => setDraftEnd(event.target.value)}
              />
            </label>
            <button
              className="btn-primary btn-xs"
              onClick={() => setCustom({ start: draftStart, end: draftEnd })}
            >
              Apply
            </button>
          </div>
        )}

        <div className="border-b border-ink-800 px-4 py-2.5">
          <span className="text-xs text-slate-500">Current window</span>
          <span className="ml-2 text-sm text-slate-200">
            {formatDate(window_.start)} → {formatDate(window_.end)}
          </span>
          <span className="ml-2 text-[11px] text-slate-600">({windowDays} days)</span>
        </div>

        {history.error && (
          <div className="px-4 pt-3">
            <Alert>Unable to load stored detection runs. ({history.error})</Alert>
          </div>
        )}

        {visibleContracts.length === 0 ? (
          <EmptyState title="No confirmed KPIs to summarise" />
        ) : (
          <div className="grid gap-3 p-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {visibleContracts.map((contract) => (
              <StageCard
                key={contract.kpi_version_id}
                contract={contract}
                stats={stageStats.get(contract.kpi_id) ?? EMPTY_STAGE}
                onClick={() => setOpenStage(contract)}
              />
            ))}
          </div>
        )}
      </Panel>

      {openKpi && batch && results.get(openKpi.kpi_id) && (
        <KpiEvaluationPopup
          contract={openKpi}
          result={results.get(openKpi.kpi_id)!}
          companyId={companyId}
          onClose={() => setOpenKpi(null)}
          onOpenCopilot={(contract, question) => {
            setPinnedKpi(contract)
            setOpenKpi(null)
            openPanel(question)
          }}
        />
      )}

      {openStage && (
        <KPIDetailDashboard
          contract={openStage}
          runs={history.data ?? []}
          window={window_}
          onClose={() => setOpenStage(null)}
        />
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- KPI cards */

function KpiResultCard({
  contract,
  result,
  skippedReason,
  onClick,
}: {
  contract: KpiContract
  result: DetectionResult | null
  skippedReason?: string
  onClick: () => void
}) {
  // A KPI the engine could not evaluate says why, in the server's own words. The
  // alternative — a card with numbers in it — would be the one thing this screen
  // must never do.
  if (!result) {
    return (
      <div className="rounded-[22px] border border-white/95 bg-white/50 p-4 text-left shadow-[0_11px_22px_rgba(50,103,145,0.08)] backdrop-blur-md">
        <div className="text-sm font-medium text-slate-300">{formatKpiName(contract.name)}</div>
        <p className="mt-3 text-xs leading-relaxed text-slate-500">
          {skippedReason ?? 'Not evaluated in this run.'}
        </p>
      </div>
    )
  }

  return (
    <button
      onClick={onClick}
      className="surface-card surface-card-lift p-4 text-left"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium text-slate-100">{formatKpiName(contract.name)}</span>
        <StatusBadge status={result.status} />
      </div>

      <div className="mt-3 text-2xl font-semibold tabular-nums text-slate-100">
        {kpiValue(contract, result.actual)}
      </div>
      <div className="mt-0.5 text-[11px] uppercase tracking-wider text-slate-500">Actual</div>

      <dl className="mt-3 space-y-1 border-t border-ink-800 pt-2 text-xs">
        <div className="flex justify-between">
          <dt className="text-slate-500">Expected</dt>
          <dd className="tabular-nums text-slate-300">{kpiValue(contract, result.expected)}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-slate-500">Deviation</dt>
          <dd className={`tabular-nums font-medium ${deviationTone(result.status)}`}>
            {signed(result.deviation_pct)}
          </dd>
        </div>
      </dl>

      {/* The one concession to method, and only in the words the server chose. */}
      {result.comparison && (
        <p className="mt-2.5 text-[11px] text-slate-500">Comparison: {result.comparison}</p>
      )}
    </button>
  )
}

function StageCard({
  contract,
  stats,
  onClick,
}: {
  contract: KpiContract
  stats: StageStats
  onClick: () => void
}) {
  const healthy = stats.normalPct >= 90
  return (
    <button
      onClick={onClick}
      className="surface-card surface-card-lift p-4 text-left"
    >
      <div className="text-sm font-medium text-slate-100">{formatKpiName(contract.name)}</div>

      {stats.totalRuns === 0 ? (
        <p className="mt-3 text-xs leading-relaxed text-slate-500">
          No detection run stored in this period yet.
        </p>
      ) : (
        <>
          <div className="mt-3 flex items-baseline gap-2">
            <span
              className={`text-2xl font-semibold tabular-nums ${
                healthy ? 'text-emerald-300' : 'text-amber-300'
              }`}
            >
              {stats.normalPct.toFixed(1)}%
            </span>
            <span className="text-xs text-slate-500">normal</span>
          </div>
          <div className="mt-0.5 text-[11px] text-slate-500">
            {formatNumber(stats.totalRuns)} run{stats.totalRuns === 1 ? '' : 's'}
          </div>

          {/* Proportion bar: normal versus everything else in the window. */}
          <div className="mt-3 flex h-1.5 overflow-hidden rounded-full bg-ink-800">
            <div
              className="bg-emerald-600"
              style={{ width: `${stats.normalPct}%` }}
              aria-hidden
            />
            <div className="flex-1 bg-amber-600" aria-hidden />
          </div>

          <div className="mt-2 flex justify-between border-t border-ink-800 pt-2 text-xs">
            <span className="text-emerald-300 tabular-nums">{stats.normal} Normal</span>
            <span className="text-amber-300 tabular-nums">{stats.abnormal} Abnormal</span>
          </div>
        </>
      )}
    </button>
  )
}

/* ------------------------------------------------------------ floating panels */

function KpiEvaluationPopup({
  contract,
  result,
  companyId,
  onClose,
  onOpenCopilot,
}: {
  contract: KpiContract
  result: DetectionResult
  companyId: string | null
  onClose: () => void
  onOpenCopilot: (contract: KpiContract, question: string) => void
}) {
  const [response, setResponse] = useState<CopilotChatResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const date = result.target_date
  const question = `Give a short plain-language explanation of ${formatKpiName(contract.name)}: what it measures, how it is calculated, and its latest governed validation state.`

  useEffect(() => {
    let cancelled = false
    if (!companyId) {
      setLoading(false)
      setError('Select a company before requesting a KPI explanation.')
      return () => {
        cancelled = true
      }
    }
    setLoading(true)
    setError(null)
    setResponse(null)
    void api
      .post<CopilotChatResponse>(`/companies/${companyId}/copilot/chat`, {
        message: question,
        context: {
          kpi_id: contract.kpi_id,
          kpi_version: contract.version,
          selected_date: date,
          page: 'dashboard',
        },
      })
      .then((answer) => {
        if (!cancelled) setResponse(answer)
      })
      .catch((reason) => {
        if (!cancelled) setError(describeError(reason))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [companyId, contract.kpi_id, contract.version, date, question])

  return (
    <Modal open onClose={onClose} title="Performance explained" width="max-w-md">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-slate-100">{formatKpiName(contract.name)}</span>
          <span className="chip">Date: {formatDate(date)}</span>
          <StatusBadge status={result.status} />
        </div>

        <section className="rounded-xl border border-sky-200 bg-sky-50/75 p-3">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-sky-700">
            KPI evaluation
          </div>
          <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            <span className="text-slate-500">Actual</span>
            <span className="text-right font-medium text-slate-100">{kpiValue(contract, result.actual)}</span>
            <span className="text-slate-500">Expected</span>
            <span className="text-right font-medium text-slate-100">{kpiValue(contract, result.expected)}</span>
            <span className="text-slate-500">Deviation</span>
            <span className={`text-right font-medium ${deviationTone(result.status)}`}>
              {signed(result.deviation_pct)}
            </span>
          </div>
          {result.comparison && (
            <p className="mt-2 text-[11px] text-sky-800">
              <span className="font-medium">Comparison:</span> {result.comparison}
            </p>
          )}
        </section>

        {result.headline && (
          <p className="text-sm leading-relaxed text-slate-200">{result.headline}</p>
        )}

        <section className="rounded-xl border border-ink-700 bg-ink-850 p-3">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Copilot summary
          </div>
          <div className="mt-2 text-sm leading-relaxed text-slate-200">
            {loading && <Spinner label="Preparing a short governed explanation…" />}
            {error && <Alert>{error}</Alert>}
            {response && <p className="whitespace-pre-wrap">{response.answer}</p>}
          </div>
          {response && response.evidence.length > 0 && (
            <p className="mt-2 text-[11px] text-slate-500">
              Based on {response.evidence.length} governed evidence item{response.evidence.length === 1 ? '' : 's'}.
            </p>
          )}
        </section>

        <p className="rounded-xl border border-ink-700 bg-ink-850 p-3 text-[11px] leading-relaxed text-slate-500">
          {STATUS_MEANING[result.status] ?? 'A verdict from comparable history.'}
        </p>

        <button className="btn-ghost btn-xs w-full" onClick={() => onOpenCopilot(contract, question)}>
          Open full Copilot
        </button>
      </div>
    </Modal>
  )
}

