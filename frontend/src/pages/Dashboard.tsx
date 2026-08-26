/**
 * Overall Dashboard: date + Agent Run → dynamic KPI result cards, and a Stage
 * Performance Summary over a selectable period. Both open reusable floating
 * panels.
 *
 * KPI identity always comes from the database (`/kpi-contracts`), never from a
 * hardcoded list and never from KPI Setup's component state — which is what
 * proves persistence actually works. The number of cards is `contracts.length`.
 *
 * Analytical values here are PLACEHOLDERS. They are shaped exactly like the real
 * output (actual / expected / deviation / status, and runs / normal / abnormal)
 * so the monitoring engine can replace the generator without touching this UI.
 * They are derived deterministically from the KPI key and date, so the same
 * inputs always render the same numbers instead of reshuffling on every click.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { KpiContract } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatCompact, formatCurrency, formatDate, formatNumber } from '../components/format'
import { Alert, EmptyState, Modal, Panel, Spinner, StatusBadge } from '../components/ui'
import { useResource } from '../components/useResource'

/* ------------------------------------------------------- placeholder engine */

/** Stable 32-bit hash so mock values are reproducible per KPI and date. */
function seedOf(...parts: string[]): number {
  let hash = 2166136261
  for (const part of parts.join('|')) {
    hash ^= part.charCodeAt(0)
    hash = Math.imul(hash, 16777619)
  }
  return Math.abs(hash)
}

function pseudo(seed: number, index: number): number {
  const x = Math.sin(seed * 12.9898 + index * 78.233) * 43758.5453
  return x - Math.floor(x)
}

interface KpiResult {
  actual: number
  expected: number
  deviationPct: number
  status: 'Normal' | 'Abnormal'
}

/** Placeholder only. Replaced wholesale by the Sprint 2 monitoring engine. */
function mockResult(contract: KpiContract, date: string): KpiResult {
  const seed = seedOf(contract.kpi_id, date)
  const isRatio = contract.kind === 'RATIO'
  const base = isRatio ? 1_200 + pseudo(seed, 1) * 900 : 2_000_000 + pseudo(seed, 1) * 8_000_000
  const expected = Math.round(base)
  // Most days sit inside a normal band; a minority deviate materially.
  const abnormal = pseudo(seed, 2) < 0.3
  const swing = abnormal
    ? (pseudo(seed, 3) < 0.5 ? -1 : 1) * (0.12 + pseudo(seed, 4) * 0.22)
    : (pseudo(seed, 3) < 0.5 ? -1 : 1) * pseudo(seed, 5) * 0.045
  const actual = Math.round(expected * (1 + swing))
  return {
    actual,
    expected,
    deviationPct: expected === 0 ? 0 : ((actual - expected) / expected) * 100,
    status: abnormal ? 'Abnormal' : 'Normal',
  }
}

interface StageStats {
  totalRuns: number
  normal: number
  abnormal: number
  normalPct: number
}

function mockStage(contract: KpiContract, days: number, anchor: string): StageStats {
  const seed = seedOf(contract.kpi_id, String(days), anchor)
  // Roughly one run per weekday in the window.
  const totalRuns = Math.max(1, Math.round(days * 0.78))
  const abnormal = Math.round(pseudo(seed, 7) * Math.max(1, totalRuns * 0.14))
  const normal = totalRuns - abnormal
  return {
    totalRuns,
    normal,
    abnormal,
    normalPct: totalRuns === 0 ? 0 : (normal / totalRuns) * 100,
  }
}

/* ---------------------------------------------------------------- formatting */

function kpiValue(contract: KpiContract, value: number): string {
  if (contract.unit === 'currency' || contract.currency) {
    return formatCurrency(value, contract.currency ?? 'INR', true)
  }
  return formatCompact(value)
}

function signed(pct: number): string {
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`
}

function deviationTone(pct: number): string {
  if (Math.abs(pct) < 5) return 'text-slate-300'
  return pct < 0 ? 'text-rose-300' : 'text-emerald-300'
}

const PERIODS = [
  ['30d', 30],
  ['60d', 60],
  ['90d', 90],
] as const

const KPI_SELECTION_KEY = 'bi.ai.dashboard-kpis'

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

function hasSavedSelection(): boolean {
  if (typeof window === 'undefined') return false
  return window.localStorage.getItem(KPI_SELECTION_KEY) !== null
}

function isoToday(): string {
  return new Date().toISOString().slice(0, 10)
}

function shiftDays(iso: string, days: number): string {
  const date = new Date(`${iso}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

/* ------------------------------------------------------------------ dashboard */

export default function Dashboard() {
  const { companyId, membership } = useAuth()

  const kpis = useResource<{ contracts: KpiContract[]; count: number }>(
    () =>
      api.get(`/companies/${companyId}/kpi-contracts`, { query: { active_only: true } }),
    [companyId],
  )

  const [date, setDate] = useState(isoToday)
  const [runDate, setRunDate] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [openKpi, setOpenKpi] = useState<KpiContract | null>(null)

  const [periodDays, setPeriodDays] = useState<number>(30)
  const [custom, setCustom] = useState<{ start: string; end: string } | null>(null)
  const [draftStart, setDraftStart] = useState(shiftDays(isoToday(), -30))
  const [draftEnd, setDraftEnd] = useState(isoToday)
  const [openStage, setOpenStage] = useState<KpiContract | null>(null)

  const contracts = kpis.data?.contracts ?? []
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
  const savedSelectionApplied = useMemo(() => hasSavedSelection(), [selectionVersion])
  const visibleContracts = useMemo(() => {
    if (!savedSelectionApplied) return contracts
    return contracts.filter((contract) => selectedKpis.includes(contract.kpi_id))
  }, [contracts, savedSelectionApplied, selectedKpis])

  const agentRun = useCallback(() => {
    setRunning(true)
    // A short delay so the UI reads as an evaluation rather than an instant
    // toggle. No calculation happens here — this is placeholder data.
    window.setTimeout(() => {
      setRunDate(date)
      setRunning(false)
    }, 550)
  }, [date])

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
            {visibleContracts.length} confirmed KPI{visibleContracts.length === 1 ? '' : 's'} · read from the
            governed registry
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-medium ${
              savedSelectionApplied
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
                : 'border-slate-700 bg-slate-900 text-slate-300'
            }`}
          >
            {savedSelectionApplied ? `Saved (${selectedKpis.length})` : 'Using all KPIs'}
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
                className="rounded-md border border-ink-600 bg-ink-850 px-2 py-1 text-xs text-slate-100"
                value={date}
                onChange={(event) => setDate(event.target.value)}
              />
            </label>
            <button
              className="btn-primary btn-xs"
              onClick={agentRun}
              disabled={running || contracts.length === 0}
            >
              {running ? 'Running…' : 'Agent Run'}
            </button>
          </div>
        }
        bodyClassName=""
      >
        {visibleContracts.length === 0 ? (
          <EmptyState
            title="No KPI cards selected"
            description="Use KPI Registration to include the KPIs you want on the dashboard, then click Save changes."
            action={
              <Link to="/kpi-setup/kpis" className="btn-primary btn-xs">
                Open KPI Registration
              </Link>
            }
          />
        ) : !runDate ? (
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
              <span className="text-sm font-medium text-slate-100">{formatDate(runDate)}</span>
              <span className="chip">placeholder values</span>
            </div>
            {/* One reusable card, mapped over the registry. */}
            <div className="grid gap-px bg-ink-800 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
              {visibleContracts.map((contract) => (
                <KpiResultCard
                  key={contract.kpi_version_id}
                  contract={contract}
                  result={mockResult(contract, runDate)}
                  onClick={() => setOpenKpi(contract)}
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
                    ? 'bg-accent text-white'
                    : 'border border-ink-600 text-slate-400 hover:text-slate-200'
                }`}
              >
                {label}
              </button>
            ))}
            <button
              onClick={() => setCustom({ start: draftStart, end: draftEnd })}
              className={`rounded px-2.5 py-1 text-xs transition-colors ${
                custom
                  ? 'bg-accent text-white'
                  : 'border border-ink-600 text-slate-400 hover:text-slate-200'
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
                className="rounded-md border border-ink-600 bg-ink-850 px-2 py-1 text-xs text-slate-100"
                value={draftStart}
                onChange={(event) => setDraftStart(event.target.value)}
              />
            </label>
            <label className="text-xs text-slate-400">
              <span className="label">End date</span>
              <input
                type="date"
                className="rounded-md border border-ink-600 bg-ink-850 px-2 py-1 text-xs text-slate-100"
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

        {visibleContracts.length === 0 ? (
          <EmptyState title="No confirmed KPIs to summarise" />
        ) : (
          <div className="grid gap-px bg-ink-800 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {visibleContracts.map((contract) => (
              <StageCard
                key={contract.kpi_version_id}
                contract={contract}
                stats={mockStage(contract, windowDays, window_.start)}
                onClick={() => setOpenStage(contract)}
              />
            ))}
          </div>
        )}
      </Panel>

      <p className="text-[11px] leading-relaxed text-slate-600">
        KPI names and the number of cards come from the confirmed registry, so both panels change
        automatically when a KPI is added, edited or deprecated. The analytical values are
        placeholders shaped like the real output — the monitoring engine replaces the generator
        without changing this screen.
      </p>

      {openKpi && runDate && (
        <KpiDetailPanel
          contract={openKpi}
          date={runDate}
          result={mockResult(openKpi, runDate)}
          onClose={() => setOpenKpi(null)}
        />
      )}

      {openStage && (
        <StageDetailPanel
          contract={openStage}
          window={window_}
          stats={mockStage(openStage, windowDays, window_.start)}
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
  onClick,
}: {
  contract: KpiContract
  result: KpiResult
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="bg-ink-900 p-4 text-left transition-colors hover:bg-ink-850"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium text-slate-100">{contract.name}</span>
        <StatusBadge status={result.status === 'Normal' ? 'GOOD' : 'WARNING'} label={result.status} />
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
          <dd className={`tabular-nums font-medium ${deviationTone(result.deviationPct)}`}>
            {signed(result.deviationPct)}
          </dd>
        </div>
      </dl>
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
      className="bg-ink-900 p-4 text-left transition-colors hover:bg-ink-850"
    >
      <div className="text-sm font-medium text-slate-100">{contract.name}</div>

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
        {formatNumber(stats.totalRuns)} runs
      </div>

      {/* Proportion bar: normal versus abnormal share of the window. */}
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
    </button>
  )
}

/* ------------------------------------------------------------ floating panels */

function FuturePlaceholder({ items }: { items: string[] }) {
  return (
    <section className="rounded-md border border-dashed border-ink-600 bg-ink-850 p-3">
      <div className="panel-title mb-2">Coming in later sprints</div>
      <ul className="space-y-1">
        {items.map((item) => (
          <li key={item} className="flex gap-2 text-xs text-slate-600">
            <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-ink-600" />
            {item}
          </li>
        ))}
      </ul>
    </section>
  )
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between border-b border-ink-800 py-2 last:border-0">
      <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
      <span className="text-sm tabular-nums text-slate-100">{value}</span>
    </div>
  )
}

function KpiDetailPanel({
  contract,
  date,
  result,
  onClose,
}: {
  contract: KpiContract
  date: string
  result: KpiResult
  onClose: () => void
}) {
  return (
    <Modal open onClose={onClose} title={contract.name} width="max-w-lg">
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="chip">Date: {formatDate(date)}</span>
          <StatusBadge
            status={result.status === 'Normal' ? 'GOOD' : 'WARNING'}
            label={result.status}
          />
        </div>

        <div>
          <Row label="Actual" value={kpiValue(contract, result.actual)} />
          <Row label="Expected" value={kpiValue(contract, result.expected)} />
          <Row
            label="Deviation"
            value={
              <span className={deviationTone(result.deviationPct)}>
                {signed(result.deviationPct)}
              </span>
            }
          />
          <Row label="Status" value={result.status} />
        </div>

        {/* Plain-language definition the business owner already confirmed during
            registration. No formula or internals — this screen stays business-facing. */}
        <div className="rounded-md border border-ink-700 bg-ink-850 p-3 text-[11px] leading-relaxed text-slate-500">
          <span className="font-medium text-slate-400">What this measures.</span>{' '}
          {contract.business_definition}
        </div>

        <FuturePlaceholder
          items={[
            'Why this is normal or abnormal',
            'Driver analysis and contribution',
            'Traceable evidence with source freshness',
            'Trend and expected range',
            'Recommended action',
          ]}
        />
      </div>
    </Modal>
  )
}

function StageDetailPanel({
  contract,
  window,
  stats,
  onClose,
}: {
  contract: KpiContract
  window: { start: string; end: string }
  stats: StageStats
  onClose: () => void
}) {
  return (
    <Modal open onClose={onClose} title={contract.name} width="max-w-lg">
      <div className="space-y-4">
        <span className="chip">
          Period: {formatDate(window.start)} → {formatDate(window.end)}
        </span>

        <div>
          <Row label="Total runs" value={formatNumber(stats.totalRuns)} />
          <Row
            label="Normal runs"
            value={<span className="text-emerald-300">{formatNumber(stats.normal)}</span>}
          />
          <Row
            label="Abnormal runs"
            value={<span className="text-amber-300">{formatNumber(stats.abnormal)}</span>}
          />
          <Row label="Normal percentage" value={`${stats.normalPct.toFixed(1)}%`} />
        </div>

        <FuturePlaceholder
          items={[
            'Run-by-run history with outcomes',
            'Which dimensions drove abnormal runs',
            'Detection accuracy and analyst feedback',
            'Drift in expected behaviour over the period',
          ]}
        />
      </div>
    </Modal>
  )
}
