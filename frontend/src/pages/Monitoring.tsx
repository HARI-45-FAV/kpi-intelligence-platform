/**
 * Monitoring — the business-facing detection surface.
 *
 * Five things, and deliberately only five: KPI, Actual, Expected, Deviation,
 * Status. Plus the comparison in plain language ("Comparable Fridays"), because a
 * reader who cannot see *what* the number was compared against has no way to
 * agree or disagree with the verdict.
 *
 * What is absent is the design. This screen never shows the median, the median
 * absolute deviation, the modified z-score, the dispersion basis, the bucket slot
 * that was applied, the reference dates, the joins or the generated SQL. The
 * server does return all of that — `evidence`, to callers holding `kpi.read` —
 * and the investigation surface is where it belongs. A business owner asking
 * "did revenue behave normally on Friday?" is owed an answer, not a statistics
 * lesson, and burying the answer in method is how dashboards stop being read.
 *
 * Nothing here computes. Every number on the screen arrives from
 * `POST /run-detection/batch` or from the stored run the overview hands back;
 * there is no arithmetic in this file at all beyond choosing a colour. That is
 * the same boundary the engine enforces on the language model, applied to the
 * browser: detection happens in one deterministic place, and everything else
 * displays what it decided.
 */

import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type {
  DetectableKpi,
  DetectionBatchResponse,
  DetectionOverview,
  DetectionResult,
  DetectionRunSummary,
} from '../api/types'
import { useAuth } from '../auth/AuthContext'
import {
  formatCompact,
  formatCurrency,
  formatDate,
  formatKpiName,
  formatRelative,
} from '../components/format'
import { Alert, EmptyState, Modal, Panel, Spinner, StatusBadge } from '../components/ui'
import { useAction, useResource } from '../components/useResource'
import { useCopilotScreen } from '../copilot/CopilotProvider'

/* ------------------------------------------------------------------ presentation */

/** How a verdict reads, in the words a business owner would use. */
const STATUS_MEANING: Record<string, string> = {
  NORMAL: 'In line with comparable history.',
  ABNORMAL: 'Outside comparable history by more than this KPI tolerates.',
  LOW_CONFIDENCE:
    'Not enough comparable history to judge yet. The measurement stands; the verdict does not.',
}

/**
 * Deviation is coloured by the *verdict*, never by its sign.
 *
 * A rising number is not automatically good news — refunds, cost per order and
 * churn all get worse as they grow — and the platform already knows which
 * direction matters for this KPI, having weighed it against the KPI's own
 * tolerance. Colouring by sign would quietly contradict that judgement in green.
 */
function deviationTone(status: string): string {
  if (status === 'ABNORMAL') return 'text-rose-300'
  return 'text-slate-300'
}

function signedPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`
}

/** Currency KPIs read as money; everything else reads as a count. */
function kpiValue(result: DetectionResult, value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (result.currency) return formatCurrency(value, result.currency, true)
  if (result.unit === 'currency') return formatCurrency(value, 'INR', true)
  return formatCompact(value)
}

/* ------------------------------------------------------------------ adaptation */

/**
 * A stored run, shown in the same shape as a fresh one.
 *
 * The screen has to render before anybody presses anything, and the honest thing
 * to show is the last verdict the platform actually reached — not a blank card,
 * and certainly not a number generated in the browser to fill the space.
 */
function fromStoredRun(run: DetectionRunSummary): DetectionResult {
  return {
    kpi: run.kpi_name,
    kpi_key: run.kpi_key,
    target_date: run.target_date,
    actual: run.actual_value,
    expected: run.expected_value,
    deviation_pct: run.deviation_pct,
    deviation_absolute: run.deviation_absolute,
    status: run.status as DetectionResult['status'],
    comparison: run.comparison_label ?? null,
    headline: run.headline ?? null,
    unit: run.unit,
    currency: run.currency,
  }
}

interface KpiRow {
  kpi: DetectableKpi
  result: DetectionResult | null
  /** True when the figures are the last stored run rather than this date's. */
  stored: boolean
  executedAt?: string | null
}

function isoToday(): string {
  return new Date().toISOString().slice(0, 10)
}

/** The server evaluates at most this many KPIs per batch request. */
const BATCH_LIMIT = 25

/* ------------------------------------------------------------------- monitoring */

export default function Monitoring() {
  const { companyId, membership, can } = useAuth()
  const mayRun = can('detection.run')

  const overview = useResource<DetectionOverview>(
    () => api.get(`/companies/${companyId}/detection/overview`),
    [companyId],
    { enabled: Boolean(companyId) },
  )

  const [date, setDate] = useState(isoToday)
  const [batch, setBatch] = useState<DetectionBatchResponse | null>(null)
  const [openKpi, setOpenKpi] = useState<KpiRow | null>(null)
  const run = useAction()

  const detectable = useMemo(
    () => (overview.data?.kpis ?? []).filter((kpi) => kpi.detectable),
    [overview.data],
  )
  const blocked = useMemo(
    () => (overview.data?.kpis ?? []).filter((kpi) => !kpi.detectable),
    [overview.data],
  )

  // Fresh results win over stored ones, matched by KPI key.
  const fresh = useMemo(() => {
    const byKey = new Map<string, DetectionResult>()
    for (const item of batch?.results ?? []) byKey.set(item.result.kpi_key, item.result)
    return byKey
  }, [batch])

  const skipped = useMemo(() => {
    const byKey = new Map<string, string>()
    for (const item of batch?.skipped ?? []) byKey.set(item.kpi_id, item.reason)
    return byKey
  }, [batch])

  const rows = useMemo<KpiRow[]>(
    () =>
      detectable.map((kpi) => {
        const live = fresh.get(kpi.kpi_key)
        if (live) return { kpi, result: live, stored: false }
        const stored = kpi.latest_run ? fromStoredRun(kpi.latest_run) : null
        return {
          kpi,
          result: stored,
          stored: Boolean(stored),
          executedAt: kpi.latest_run?.executed_at ?? null,
        }
      }),
    [detectable, fresh],
  )

  const counts = useMemo(() => {
    const tally = { NORMAL: 0, ABNORMAL: 0, LOW_CONFIDENCE: 0 }
    for (const row of rows) {
      if (row.result && row.result.status in tally) {
        tally[row.result.status as keyof typeof tally] += 1
      }
    }
    return tally
  }, [rows])

  // The server evaluates at most BATCH_LIMIT KPIs in one request. Asking for the
  // whole list and letting the tail be silently dropped would leave cards showing
  // yesterday's verdict beside today's, so the cut is made here and said out loud.
  const evaluating = useMemo(() => detectable.slice(0, BATCH_LIMIT), [detectable])
  const beyondBatch = detectable.length - evaluating.length

  const runDetection = async () => {
    if (!companyId) return
    const response = await run.run(() =>
      api.post<DetectionBatchResponse>(`/companies/${companyId}/run-detection/batch`, {
        target_date: date,
        kpi_ids: evaluating.map((kpi) => kpi.kpi_key),
      }),
    )
    if (response) {
      setBatch(response)
      // The stored-run column the overview handed back is now stale.
      void overview.reload()
    }
  }

  // What the Copilot inherits from this screen: which panel is asking, the KPI
  // being read, and the date it was evaluated on. No figures — the Copilot
  // re-reads governed evidence itself rather than trusting anything this screen
  // happens to be displaying.
  useCopilotScreen({
    panel: openKpi ? 'detection_detail' : 'monitoring',
    kpiId: openKpi?.kpi.kpi_key ?? null,
    kpiVersion: openKpi?.kpi.kpi_version ?? null,
    selectedDate: openKpi?.result?.target_date ?? date,
    label: openKpi ? formatKpiName(openKpi.kpi.name) : null,
  })

  if (overview.loading && !overview.data) return <Spinner label="Loading monitored KPIs…" />
  if (overview.error)
    return <Alert>Unable to load the monitoring overview. ({overview.error})</Alert>

  const note = overview.data?.configuration.note

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">
            {membership?.company_name ? `${membership.company_name} · Monitoring` : 'Monitoring'}
          </h1>
          <p className="mt-0.5 text-sm text-slate-500">
            {detectable.length} KPI{detectable.length === 1 ? '' : 's'} ready to evaluate
            {blocked.length > 0 && ` · ${blocked.length} not ready`}
          </p>
        </div>
        {rows.some((row) => row.result) && (
          <div className="flex flex-wrap items-center gap-1.5">
            {counts.ABNORMAL > 0 && (
              <StatusBadge status="ABNORMAL" label={`${counts.ABNORMAL} abnormal`} />
            )}
            {counts.NORMAL > 0 && <StatusBadge status="NORMAL" label={`${counts.NORMAL} normal`} />}
            {counts.LOW_CONFIDENCE > 0 && (
              <StatusBadge
                status="LOW_CONFIDENCE"
                label={`${counts.LOW_CONFIDENCE} low confidence`}
              />
            )}
          </div>
        )}
      </div>

      {/* A company with no approved comparison policy still gets answers, from a
          plain recent-days window. Saying so is the point: an unstated fallback
          is the kind of thing that gets discovered during an argument. */}
      {note && <Alert tone="info">{note}</Alert>}

      <Panel
        title="Detection"
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
              onClick={() => void runDetection()}
              disabled={run.pending || detectable.length === 0 || !mayRun}
              title={mayRun ? undefined : 'Requires the detection.run permission.'}
            >
              {run.pending ? 'Evaluating…' : 'Run detection'}
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

        {detectable.length === 0 ? (
          <EmptyState
            title="No KPI is ready to evaluate yet"
            description="Detection reads the approved KPI contract — its source table, formula and time field. Register and activate a KPI, and it appears here automatically."
            action={
              <Link to="/kpi-setup/kpis" className="btn-primary btn-xs">
                Open KPI Registration
              </Link>
            }
          />
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 border-b border-ink-800 px-4 py-2.5">
              <span className="text-xs text-slate-500">
                {batch ? 'Evaluated for' : 'Last evaluated'}
              </span>
              <span className="text-sm font-medium text-slate-100">
                {formatDate(batch ? batch.target_date : rows.find((r) => r.result)?.result?.target_date)}
              </span>
              {!batch && rows.some((row) => row.result) && (
                <span className="chip">stored result</span>
              )}
              {beyondBatch > 0 && (
                <span className="text-[11px] text-slate-500">
                  · one run covers {BATCH_LIMIT} KPIs; {beyondBatch} more keep their last verdict
                </span>
              )}
            </div>

            {rows.every((row) => !row.result) ? (
              <EmptyState
                title="Choose a date and run detection"
                description={`Ready to evaluate ${detectable.length} KPI${
                  detectable.length === 1 ? '' : 's'
                } for ${formatDate(date)}.`}
              />
            ) : (
              <div className="grid gap-3 p-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
                {rows.map((row) => (
                  <VerdictCard
                    key={row.kpi.kpi_id}
                    row={row}
                    skippedReason={skipped.get(row.kpi.kpi_key)}
                    onClick={() => row.result && setOpenKpi(row)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </Panel>

      {blocked.length > 0 && (
        <Panel title="Not ready to evaluate" bodyClassName="">
          <ul className="divide-y divide-ink-800">
            {blocked.map((kpi) => (
              <li key={kpi.kpi_id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2.5">
                <span className="text-sm font-medium text-slate-200">{formatKpiName(kpi.name)}</span>
                <span className="flex-1 text-xs leading-relaxed text-slate-500">
                  {kpi.blocked_reason ?? 'Not available for detection.'}
                </span>
                <Link to="/kpi-setup/kpis" className="btn-ghost btn-xs">
                  Fix in KPI Setup
                </Link>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {openKpi?.result && (
        <VerdictModal row={openKpi} onClose={() => setOpenKpi(null)} />
      )}
    </div>
  )
}

/* ----------------------------------------------------------------- the card */

function VerdictCard({
  row,
  skippedReason,
  onClick,
}: {
  row: KpiRow
  skippedReason?: string
  onClick: () => void
}) {
  const { kpi, result } = row

  if (!result) {
    return (
      <div className="rounded-[22px] border border-white/95 bg-white/50 p-4 text-left shadow-[0_11px_22px_rgba(50,103,145,0.08)] backdrop-blur-md">
        <div className="text-sm font-medium text-slate-300">{formatKpiName(kpi.name)}</div>
        <p className="mt-3 text-xs leading-relaxed text-slate-500">
          {skippedReason ?? 'Not evaluated yet.'}
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
        <span className="text-sm font-medium text-slate-100">{formatKpiName(result.kpi)}</span>
        <StatusBadge status={result.status} />
      </div>

      <div className="mt-3 text-2xl font-semibold tabular-nums text-slate-100">
        {kpiValue(result, result.actual)}
      </div>
      <div className="mt-0.5 text-[11px] uppercase tracking-wider text-slate-500">Actual</div>

      <dl className="mt-3 space-y-1 border-t border-ink-800 pt-2 text-xs">
        <div className="flex justify-between">
          <dt className="text-slate-500">Expected</dt>
          <dd className="tabular-nums text-slate-300">{kpiValue(result, result.expected)}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-slate-500">Deviation</dt>
          <dd className={`tabular-nums font-medium ${deviationTone(result.status)}`}>
            {signedPercent(result.deviation_pct)}
          </dd>
        </div>
      </dl>

      {/* The one concession to method, and only in the words the server chose. */}
      {result.comparison && (
        <p className="mt-2.5 text-[11px] text-slate-500">Comparison: {result.comparison}</p>
      )}
      {row.stored && (
        <p className="mt-1 text-[11px] text-slate-600">
          {formatDate(result.target_date)}
          {row.executedAt ? ` · evaluated ${formatRelative(row.executedAt)}` : ''}
        </p>
      )}
    </button>
  )
}

/* ---------------------------------------------------------------- the detail */

function DetailRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between border-b border-ink-800 py-2 last:border-0">
      <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
      <span className="text-sm tabular-nums text-slate-100">{value}</span>
    </div>
  )
}

/**
 * The same five figures, larger, plus the sentence the engine wrote about them.
 *
 * Not a drill-down. There is nothing here that was hidden on the card, because
 * the technical account of *how* the verdict was reached belongs to
 * Investigation, where a reader who wants the reference window and the spread
 * can ask for it and be entitled to it.
 */
function VerdictModal({ row, onClose }: { row: KpiRow; onClose: () => void }) {
  const result = row.result!
  return (
    <Modal open onClose={onClose} title={formatKpiName(result.kpi)} width="max-w-md">
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="chip">Date: {formatDate(result.target_date)}</span>
          <StatusBadge status={result.status} />
        </div>

        <div>
          <DetailRow label="Actual" value={kpiValue(result, result.actual)} />
          <DetailRow label="Expected" value={kpiValue(result, result.expected)} />
          <DetailRow
            label="Deviation"
            value={
              <span className={deviationTone(result.status)}>
                {signedPercent(result.deviation_pct)}
              </span>
            }
          />
          <DetailRow label="Status" value={result.status.replace(/_/g, ' ')} />
        </div>

        {result.comparison && (
          <div className="rounded-xl border border-sky-200 bg-sky-50/75 p-3 text-xs text-sky-800">
            <span className="font-medium">Comparison:</span> {result.comparison}
          </div>
        )}

        {result.headline && (
          <p className="text-sm leading-relaxed text-slate-200">{result.headline}</p>
        )}

        <p className="rounded-xl border border-ink-700 bg-ink-850 p-3 text-[11px] leading-relaxed text-slate-500">
          {STATUS_MEANING[result.status] ?? 'A verdict from comparable history.'}
        </p>
      </div>
    </Modal>
  )
}
