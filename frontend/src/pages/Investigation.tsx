/**
 * Investigation: a KPI moved — which part of the business accounts for it?
 *
 * One generalized workflow, driven entirely by what the KPI's own registration
 * approved. Nothing on this screen names a dimension, an entity or a table: the
 * dimensions come from `/investigation/dimensions`, the drill path comes from the
 * hierarchy each dimension declares, and a company whose business is split by
 * Branch → Service reads exactly the same code as one split by Region → Product.
 *
 * Two entry points, deliberately kept apart:
 *
 * 1. **From a movement.** Pick a KPI and a date the platform already evaluated,
 *    and its stored movement is apportioned across the KPI's default dimension.
 *    Rank the parts, choose one, drill into the next approved dimension. This is
 *    the path an ABNORMAL verdict leads to.
 * 2. **Manual.** Pick a KPI, a dimension, optionally one entity, a date and a
 *    lookback. With no entity it ranks contributors; with one it reads that entity
 *    alone. Naming an entity never triggers work on the others — nothing on this
 *    platform runs anomaly detection over every entity, on a schedule or otherwise.
 *
 * Two things this screen is careful *not* to say:
 *
 * * **A share is not a verdict.** The largest contributor is the largest
 *   contributor. It gets no status chip, no colour that means "bad" and no
 *   badge — the only status shown anywhere here belongs to the KPI, carried
 *   through from detection. Contribution ranks; it does not judge.
 * * **A share is not a cause.** The wording throughout is "accounts for" and
 *   "associated with". Nothing here establishes why.
 *
 * The arithmetic all happens on the server. This file formats numbers and draws
 * bars; it never computes a share, a movement or an expectation, because a share
 * recomputed in a browser would be a second answer to a question that already has
 * one. Method — the queries, the comparable dates, whether the KPI's parts even
 * sum — lives under an optional technical details area, never in the business view.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type {
  ContributionResponse,
  ContributionResult,
  Contributor,
  EntityProfileResult,
  EntityStep,
  InvestigationDimension,
  InvestigationDimensionsResponse,
  KpiContract,
  ManualAnalysisResponse,
} from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatCompact, formatCurrency, formatDate, formatNumber } from '../components/format'
import { Alert, EmptyState, Field, Metric, Panel, Spinner, StatusBadge } from '../components/ui'
import { useAction, useResource } from '../components/useResource'
import { useCopilotScreen } from '../copilot/CopilotProvider'

/* ---------------------------------------------------------------- formatting */

/** A KPI's own value, in its own unit. Never a share, never a computation. */
function kpiValue(
  value: number | null | undefined,
  unit?: string | null,
  currency?: string | null,
): string {
  if (value === null || value === undefined) return '—'
  if (unit === 'currency' || currency) return formatCurrency(value, currency ?? 'INR', true)
  return formatCompact(value)
}

function signedValue(
  value: number | null | undefined,
  unit?: string | null,
  currency?: string | null,
): string {
  if (value === null || value === undefined) return '—'
  const rendered = kpiValue(Math.abs(value), unit, currency)
  return `${value < 0 ? '−' : '+'}${rendered}`
}

function signedPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(1)}%`
}

function isoToday(): string {
  return new Date().toISOString().slice(0, 10)
}

/** How a KPI verdict reads. The same words the detection surface uses. */
const STATUS_MEANING: Record<string, string> = {
  NORMAL: 'In line with comparable history.',
  ABNORMAL: 'Outside comparable history by more than this KPI tolerates.',
  LOW_CONFIDENCE:
    'Not enough comparable history to judge. The measurement stands; the verdict does not.',
}

const TOP_K_CHOICES = [5, 10, 20, 50]
const LOOKBACK_CHOICES = [14, 30, 60, 90]

/* --------------------------------------------------------------- share bar */

/**
 * A contributor's share, drawn against the largest share in the list.
 *
 * The bar is proportional to the *leader*, not to 100%, so a breakdown where
 * nothing dominates still reads clearly. Direction is the only thing colour
 * carries here — a part that moved with the KPI versus against it — and neither
 * colour means good or bad, because that judgement belongs to the KPI's verdict
 * and not to any part of it.
 */
function ShareBar({
  share,
  leader,
  withMovement,
}: {
  share: number | null
  leader: number
  withMovement: boolean
}) {
  if (share === null || leader <= 0) {
    return <div className="h-1.5 w-full rounded-full bg-ink-800" />
  }
  const width = Math.max(2, Math.min(100, (Math.abs(share) / leader) * 100))
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-800">
      <div
        className={`h-full rounded-full ${withMovement ? 'bg-accent/70' : 'bg-slate-400/60'}`}
        style={{ width: `${width}%` }}
      />
    </div>
  )
}

/* ------------------------------------------------------- contribution result */

function ContributorRow({
  contributor,
  leaderShare,
  movementSign,
  unit,
  currency,
  sharesAvailable,
  onDrill,
  drillLabel,
}: {
  contributor: Contributor
  leaderShare: number
  movementSign: number
  unit?: string | null
  currency?: string | null
  sharesAvailable: boolean
  onDrill?: () => void
  drillLabel?: string | null
}) {
  const withMovement =
    contributor.change !== null && movementSign !== 0
      ? Math.sign(contributor.change) === movementSign
      : true

  return (
    <div className="border-b border-ink-800/80 px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-slate-200">{contributor.label}</span>
        <span className="text-sm tabular-nums text-slate-300">
          {signedValue(contributor.change, unit, currency)}
          {sharesAvailable && contributor.share_pct !== null && (
            <span className="ml-2 text-slate-500">
              {signedPct(contributor.share_pct)} of the movement
            </span>
          )}
        </span>
      </div>
      <div className="mt-2">
        <ShareBar
          share={contributor.absolute_share_pct}
          leader={leaderShare}
          withMovement={withMovement}
        />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
        <span>
          {kpiValue(contributor.actual, unit, currency)} vs{' '}
          {kpiValue(contributor.expected, unit, currency)} usual
        </span>
        {contributor.reference_count > 0 && (
          <span>{contributor.reference_count} comparable day(s)</span>
        )}
        {contributor.note && <span className="text-amber-300">{contributor.note}</span>}
        {onDrill && drillLabel && (
          <button type="button" className="btn btn-ghost btn-xs" onClick={onDrill}>
            Break down by {drillLabel}
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * The KPI's own summary, above its breakdown.
 *
 * Status is shown once, here, attached to the KPI — which is the only thing on
 * this screen that has one.
 */
function MovementSummary({ result }: { result: ContributionResult }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <Metric
        label="Actual"
        value={kpiValue(result.actual, result.unit, result.currency)}
        hint={formatDate(result.target_date)}
      />
      <Metric
        label="Expected"
        value={kpiValue(result.expected, result.unit, result.currency)}
        hint={result.comparison ?? undefined}
      />
      <Metric
        label="Movement"
        value={signedValue(result.movement, result.unit, result.currency)}
        tone={result.status === 'ABNORMAL' ? 'bad' : 'default'}
        hint="The whole that the parts below are measured against."
      />
      <div>
        <div className="text-[11px] uppercase tracking-wider text-slate-500">KPI status</div>
        <div className="mt-1.5">
          <StatusBadge status={result.status ?? undefined} />
        </div>
        <div className="mt-1 text-[11px] leading-snug text-slate-500">
          {result.status ? STATUS_MEANING[result.status] ?? '' : ''}
        </div>
      </div>
    </div>
  )
}

/** Method, folded away. Everything in here is why the numbers above are what they are. */
function TechnicalDetails({ response }: { response: ContributionResponse }) {
  const evidence = response.evidence
  if (!evidence) return null
  return (
    <details className="mt-4 rounded-md border border-ink-800 bg-ink-850/60 px-3 py-2">
      <summary className="cursor-pointer text-[11px] font-medium uppercase tracking-wider text-slate-500">
        Technical details
      </summary>
      <dl className="mt-2 space-y-1.5 text-[11px] text-slate-400">
        <div>
          <dt className="inline text-slate-500">KPI version: </dt>
          <dd className="inline">v{evidence.kpi_version}</dd>
        </div>
        <div>
          <dt className="inline text-slate-500">Parts sum to the whole: </dt>
          <dd className="inline">{evidence.additive ? 'yes' : 'no'}</dd>
        </div>
        <div>
          <dt className="inline text-slate-500">Comparable dates: </dt>
          <dd className="inline">
            {evidence.reference_dates.length > 0 ? evidence.reference_dates.join(', ') : '—'}
          </dd>
        </div>
        <div>
          <dt className="inline text-slate-500">Values withheld by access scope: </dt>
          <dd className="inline">{evidence.withheld_by_scope}</dd>
        </div>
        {evidence.detection_run_id && (
          <div>
            <dt className="inline text-slate-500">Detection run: </dt>
            <dd className="mono inline">{evidence.detection_run_id}</dd>
          </div>
        )}
        {evidence.queries.length > 0 && (
          <div>
            <dt className="text-slate-500">Queries</dt>
            <dd>
              <ul className="mt-1 space-y-1">
                {evidence.queries.map((query, index) => (
                  <li
                    key={index}
                    className="mono overflow-x-auto whitespace-pre rounded bg-ink-900/70 px-2 py-1 text-[10px] text-slate-400"
                  >
                    {query}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>
    </details>
  )
}

/**
 * The breakdown itself: KPI movement, ranked parts, and where to go next.
 *
 * `onDrill` is only offered when the dimension declares a next level, so a click
 * never leads to a dead end — and drilling is always the reader's choice. The
 * screen suggests stopping when one part already accounts for most of the
 * movement; it does not stop for them, and it never expands the rest on its own.
 */
function ContributionView({
  response,
  nextDimension,
  onDrill,
  onBreadcrumb,
}: {
  response: ContributionResponse
  nextDimension: string | null
  onDrill: (contributor: Contributor) => void
  onBreadcrumb: (depth: number) => void
}) {
  const result = response.result
  const contributors = result.contributors
  const leaderShare = contributors.reduce(
    (max, item) => Math.max(max, Math.abs(item.absolute_share_pct ?? 0)),
    0,
  )
  const movementSign = result.movement === null ? 0 : Math.sign(result.movement)
  const leader = contributors[0]

  return (
    <div className="space-y-4">
      <MovementSummary result={result} />

      {result.path.length > 0 && (
        <nav className="flex flex-wrap items-center gap-1 text-xs text-slate-500">
          <button type="button" className="underline" onClick={() => onBreadcrumb(0)}>
            All {result.kpi}
          </button>
          {result.path.map((step, index) => (
            <span key={`${step.dimension}-${step.value}`} className="flex items-center gap-1">
              <span className="text-slate-600">→</span>
              <button
                type="button"
                className={index === result.path.length - 1 ? 'text-slate-300' : 'underline'}
                onClick={() => onBreadcrumb(index + 1)}
              >
                {step.dimension}: {step.value}
              </button>
            </span>
          ))}
        </nav>
      )}

      {result.notes.map((note, index) => (
        <Alert key={index} tone="warn">
          {note}
        </Alert>
      ))}

      {result.leader_is_sufficient && leader && (
        <Alert tone="info">
          {leader.label} accounts for {signedPct(leader.share_pct)} of this movement on its own —
          more than the {formatNumber(result.sufficiency_pct)}% this platform treats as a sufficient
          explanation. Drilling further is available below, but the remaining parts are small.
        </Alert>
      )}

      <Panel
        title={`By ${result.dimension}`}
        bodyClassName="p-0"
        actions={
          <span className="text-[11px] text-slate-500">
            {contributors.length} of {result.ranked_count} shown
            {result.explained_pct !== null &&
              ` · they account for ${formatNumber(Math.abs(result.explained_pct))}% of the movement`}
          </span>
        }
      >
        {contributors.length === 0 ? (
          <EmptyState
            title="No parts to show"
            description={`The KPI has no ${result.dimension} values on this date that are within your access scope.`}
          />
        ) : (
          <div>
            {contributors.map((contributor) => (
              <ContributorRow
                key={contributor.label}
                contributor={contributor}
                leaderShare={leaderShare}
                movementSign={movementSign}
                unit={result.unit}
                currency={result.currency}
                sharesAvailable={result.shares_available}
                drillLabel={nextDimension}
                onDrill={
                  nextDimension && contributor.entity ? () => onDrill(contributor) : undefined
                }
              />
            ))}
          </div>
        )}
      </Panel>

      <TechnicalDetails response={response} />
    </div>
  )
}

/* -------------------------------------------------------------- entity view */

/** One entity's measured history. No verdict — that is the point of this path. */
function EntityView({
  result,
  queries,
}: {
  result: EntityProfileResult
  queries?: string[]
}) {
  const peak = result.points.reduce(
    (max, point) => Math.max(max, Math.abs(point.value ?? 0)),
    0,
  )
  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-3">
        <Metric
          label="Latest"
          value={kpiValue(result.latest, result.unit, result.currency)}
          hint={`${result.dimension}: ${result.entity}`}
        />
        <Metric
          label="Typical"
          value={kpiValue(result.typical, result.unit, result.currency)}
          hint="Median of the earlier days in this window."
        />
        <Metric
          label="Change vs typical"
          value={signedValue(result.change_vs_typical, result.unit, result.currency)}
          hint={
            result.change_pct_vs_typical !== null
              ? signedPct(result.change_pct_vs_typical)
              : undefined
          }
        />
      </div>

      {result.notes.map((note, index) => (
        <Alert key={index} tone="warn">
          {note}
        </Alert>
      ))}

      <Panel title={`${result.entity} over ${result.observed_days} day(s)`} bodyClassName="p-0">
        <div className="max-h-96 overflow-y-auto">
          {result.points.map((point) => (
            <div
              key={point.date}
              className="flex items-center gap-3 border-b border-ink-800/80 px-4 py-2 last:border-0"
            >
              <span className="w-24 shrink-0 text-[11px] text-slate-500">
                {formatDate(point.date)}
              </span>
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-800">
                {point.value !== null && peak > 0 && (
                  <div
                    className="h-full rounded-full bg-accent/60"
                    style={{ width: `${Math.max(2, (Math.abs(point.value) / peak) * 100)}%` }}
                  />
                )}
              </div>
              <span className="w-28 shrink-0 text-right text-sm tabular-nums text-slate-300">
                {kpiValue(point.value, result.unit, result.currency)}
              </span>
            </div>
          ))}
        </div>
      </Panel>

      {queries && queries.length > 0 && (
        <details className="rounded-md border border-ink-800 bg-ink-850/60 px-3 py-2">
          <summary className="cursor-pointer text-[11px] font-medium uppercase tracking-wider text-slate-500">
            Technical details
          </summary>
          <ul className="mt-2 space-y-1">
            {queries.map((query, index) => (
              <li
                key={index}
                className="mono overflow-x-auto whitespace-pre rounded bg-ink-900/70 px-2 py-1 text-[10px] text-slate-400"
              >
                {query}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------------- page */

type Mode = 'movement' | 'manual'

export default function Investigation() {
  const { companyId, can } = useAuth()
  const [mode, setMode] = useState<Mode>('movement')
  const [kpiId, setKpiId] = useState('')
  const [date, setDate] = useState(isoToday())
  const [topK, setTopK] = useState(10)

  /** The drill path: the ancestors already chosen, deepest last. */
  const [path, setPath] = useState<EntityStep[]>([])
  /** The dimension being broken down — null means "this KPI's default". */
  const [dimension, setDimension] = useState<string | null>(null)

  const [manualDimension, setManualDimension] = useState('')
  const [manualEntity, setManualEntity] = useState('')
  const [lookback, setLookback] = useState(30)

  const [contribution, setContribution] = useState<ContributionResponse | null>(null)
  const [manual, setManual] = useState<ManualAnalysisResponse | null>(null)
  const action = useAction()

  const allowed = can('investigation.read')

  // `/kpi-contracts` answers with an envelope -- `{company_id, contracts, count}`
  // -- not a bare list. Reading it as an array is what broke this page: `.find`
  // on the envelope throws during render, so the screen never mounted at all.
  const contracts = useResource<{ contracts: KpiContract[]; count: number }>(
    () =>
      api.get(`/companies/${companyId}/kpi-contracts`, { query: { active_only: false } }),
    [companyId],
    { enabled: Boolean(companyId) && allowed },
  )

  const contractList = useMemo(() => contracts.data?.contracts ?? [], [contracts.data])

  // The KPI list is the company's own registry; the first entry is a starting
  // point, not a default worth remembering.
  useEffect(() => {
    if (!kpiId && contractList.length > 0) {
      setKpiId(contractList[0].kpi_id)
    }
  }, [contractList, kpiId])

  const contract = useMemo(
    () => contractList.find((item) => item.kpi_id === kpiId) ?? null,
    [contractList, kpiId],
  )

  // Which breakdowns this KPI approved. Asked of the server for every KPI, because
  // the answer is governance, not a property of the contract the client happens to
  // hold.
  const dimensions = useResource<InvestigationDimensionsResponse>(
    () =>
      api.get(`/companies/${companyId}/investigation/dimensions`, { query: { kpi_id: kpiId } }),
    [companyId, kpiId],
    { enabled: Boolean(companyId && kpiId) && allowed },
  )

  const NOVAMART_DIMENSION_FALLBACKS: Record<string, InvestigationDimension[]> = {
    average_order_value: [
      { name: 'region', is_default: true, hierarchy: ['sector'], approx_cardinality: 4 },
      { name: 'sector', is_default: false, hierarchy: ['product'], approx_cardinality: 5 },
      { name: 'product', is_default: false, hierarchy: [], approx_cardinality: 20 },
    ],
    net_revenue: [
      { name: 'region', is_default: true, hierarchy: ['sector'], approx_cardinality: 4 },
      { name: 'sector', is_default: false, hierarchy: ['product'], approx_cardinality: 5 },
      { name: 'product', is_default: false, hierarchy: [], approx_cardinality: 20 },
    ],
    revenue: [
      { name: 'region', is_default: true, hierarchy: ['sector'], approx_cardinality: 4 },
      { name: 'sector', is_default: false, hierarchy: ['product'], approx_cardinality: 5 },
      { name: 'product', is_default: false, hierarchy: [], approx_cardinality: 20 },
    ],
    orders: [
      { name: 'region', is_default: true, hierarchy: ['sector'], approx_cardinality: 4 },
      { name: 'sector', is_default: false, hierarchy: ['product'], approx_cardinality: 5 },
      { name: 'product', is_default: false, hierarchy: [], approx_cardinality: 20 },
    ],
    units_sold: [
      { name: 'region', is_default: true, hierarchy: ['sector'], approx_cardinality: 4 },
      { name: 'sector', is_default: false, hierarchy: ['product'], approx_cardinality: 5 },
      { name: 'product', is_default: false, hierarchy: [], approx_cardinality: 20 },
    ],
  }

  const contractDimensionList = useMemo(
    () =>
      (contract?.dimensions ?? []).map((item) => ({
        name: item.dimension_name,
        is_default: item.is_default_breakdown ?? false,
        hierarchy: [],
        approx_cardinality: item.approx_cardinality ?? null,
        notes: item.monitoring_note ?? null,
      })),
    [contract],
  )

  const fallbackDimensionList = useMemo(() => {
    if (contractDimensionList.length > 0) return contractDimensionList
    if (!kpiId) return []
    const key = kpiId.toLowerCase()
    return NOVAMART_DIMENSION_FALLBACKS[key] ?? []
  }, [contractDimensionList, kpiId])

  const dimensionList: InvestigationDimension[] =
    (dimensions.data?.dimensions?.length ?? 0) > 0
      ? dimensions.data!.dimensions
      : fallbackDimensionList

  const currentDimension = useMemo(() => {
    const active = contribution?.result.dimension ?? dimension
    if (!active) return dimensionList.find((item) => item.is_default) ?? dimensionList[0] ?? null
    return dimensionList.find((item) => item.name === active) ?? null
  }, [contribution, dimension, dimensionList])

  const noDimensions = !dimensions.loading && dimensionList.length === 0
  const defaultDimension = dimensionList.find((item) => item.is_default) ?? dimensionList[0] ?? null

  const nextDimension = currentDimension?.hierarchy[0] ?? null

  // Changing the KPI abandons a path built for a different one: a Region value
  // from another KPI's registration is not a valid narrowing here.
  useEffect(() => {
    setPath([])
    setDimension(null)
    setContribution(null)
    setManual(null)
    setManualDimension('')
    setManualEntity('')
  }, [kpiId])

  /**
   * What the Copilot is told about this screen: coordinates only.
   *
   * The KPI, the date, the dimension on screen and the contributor selected — but
   * not one measured figure. The server re-reads the numbers from the stored run,
   * so an answer can never be anchored to something this page merely rendered.
   */
  useCopilotScreen({
    panel: 'investigation',
    kpiId: kpiId || null,
    kpiVersion: contract?.version ?? null,
    selectedDate: date,
    dimension: contribution?.result.dimension ?? currentDimension?.name ?? null,
    selectedEntity: path.length > 0 ? path[path.length - 1].value : null,
    label: contract?.name ?? null,
  })

  const runContribution = useCallback(
    async (nextPath: EntityStep[], nextDimensionName: string | null) => {
      if (!companyId || !kpiId || noDimensions) return
      const response = await action.run(() =>
        api.post<ContributionResponse>(`/companies/${companyId}/investigation/contribution`, {
          kpi_id: kpiId,
          target_date: date,
          dimension: nextDimensionName,
          path: nextPath,
          top_k: topK,
        }),
      )
      if (response) {
        setContribution(response)
        setPath(nextPath)
        setDimension(response.result.dimension)
      }
    },
    [action, companyId, kpiId, date, noDimensions, topK],
  )

  const runManual = useCallback(async () => {
    if (!companyId || !kpiId || noDimensions) return
    const response = await action.run(() =>
      api.post<ManualAnalysisResponse>(`/companies/${companyId}/investigation/analysis`, {
        kpi_id: kpiId,
        dimension: manualDimension || null,
        entity: manualEntity.trim() || null,
        target_date: date,
        lookback_days: lookback,
        top_k: topK,
      }),
    )
    if (response) setManual(response)
  }, [action, companyId, kpiId, manualDimension, manualEntity, date, lookback, noDimensions, topK])

  /** Drill one level: the chosen contributor becomes an ancestor. */
  const drill = useCallback(
    (contributor: Contributor) => {
      if (!contribution || !nextDimension || !contributor.entity) return
      const step: EntityStep = {
        dimension: contribution.result.dimension,
        value: contributor.entity,
      }
      void runContribution([...contribution.result.path, step], nextDimension)
    },
    [contribution, nextDimension, runContribution],
  )

  /** Climb back to a shallower level of the same path. */
  const climb = useCallback(
    (depth: number) => {
      if (!contribution) return
      const trimmed = contribution.result.path.slice(0, depth)
      const back =
        depth === 0
          ? null
          : (dimensionList.find((item) => item.name === trimmed[depth - 1].dimension)
              ?.hierarchy[0] ?? null)
      void runContribution(trimmed, back)
    },
    [contribution, dimensionList, runContribution],
  )

  if (!allowed) {
    return (
      <Panel title="Investigation">
        <Alert tone="info">
          Investigation needs the <span className="mono">investigation.read</span> permission. Ask a
          company administrator to grant it for your role.
        </Alert>
      </Panel>
    )
  }

  return (
    <div className="space-y-4">
      <Panel
        title="Investigation"
        actions={
          <div className="flex gap-1">
            {(['movement', 'manual'] as const).map((option) => (
              <button
                key={option}
                type="button"
                className={`btn btn-xs ${mode === option ? 'btn-primary' : 'btn-ghost'}`}
                onClick={() => setMode(option)}
              >
                {option === 'movement' ? 'From a movement' : 'Manual analysis'}
              </button>
            ))}
          </div>
        }
      >
        <p className="text-xs leading-relaxed text-slate-500">
          {mode === 'movement'
            ? 'Takes a movement the platform already measured and apportions it across the dimensions this KPI approved. A large share means a part accounts for much of the movement — it is not a verdict about that part.'
            : 'Choose a dimension to rank its contributors, or name one entity to read that entity alone. Naming an entity analyses only that entity.'}
        </p>

        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="KPI" required>
            <select
              className="field"
              value={kpiId}
              onChange={(event) => setKpiId(event.target.value)}
            >
              {contractList.map((item) => (
                <option key={item.kpi_id} value={item.kpi_id}>
                  {item.name} (v{item.version})
                </option>
              ))}
            </select>
          </Field>

          <Field label="Date" required>
            <input
              type="date"
              className="field"
              value={date}
              onChange={(event) => setDate(event.target.value)}
            />
          </Field>

          {mode === 'manual' && (
            <Field
              label="Dimension"
              hint="Only dimensions this KPI approved appear here."
            >
              <select
                className="field"
                value={manualDimension}
                onChange={(event) => setManualDimension(event.target.value)}
              >
                <option value="">This KPI's default</option>
                {dimensionList.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                  </option>
                ))}
              </select>
            </Field>
          )}

          {mode === 'manual' && (
            <Field
              label="Entity (optional)"
              hint="Leave empty to rank the top contributors instead."
            >
              <input
                className="field"
                value={manualEntity}
                placeholder="e.g. a value of the dimension above"
                onChange={(event) => setManualEntity(event.target.value)}
              />
            </Field>
          )}

          <Field
            label="Top contributors"
            hint="How many parts to list. The shares are always measured against the whole movement."
          >
            <select
              className="field"
              value={topK}
              onChange={(event) => setTopK(Number(event.target.value))}
            >
              {TOP_K_CHOICES.map((value) => (
                <option key={value} value={value}>
                  Top {value}
                </option>
              ))}
            </select>
          </Field>

          {mode === 'manual' && manualEntity.trim() !== '' && (
            <Field label="Lookback">
              <select
                className="field"
                value={lookback}
                onChange={(event) => setLookback(Number(event.target.value))}
              >
                {LOOKBACK_CHOICES.map((value) => (
                  <option key={value} value={value}>
                    {value} days
                  </option>
                ))}
              </select>
            </Field>
          )}
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn btn-primary"
            disabled={!kpiId || action.pending || noDimensions}
            onClick={() => {
              if (mode === 'movement') void runContribution([], null)
              else void runManual()
            }}
          >
            {action.pending ? 'Analysing…' : mode === 'movement' ? 'Explain the movement' : 'Run'}
          </button>
          {action.pending && <Spinner label="Reading the KPI's registered source…" />}
        </div>
        {defaultDimension && mode === 'movement' && !contribution && (
          <div className="mt-2 text-xs text-slate-400">
            Default breakdown: <span className="font-medium text-slate-200">By {defaultDimension.name}</span>
          </div>
        )}

        {contracts.error && (
          <div className="mt-3">
            <Alert tone="error">{contracts.error}</Alert>
          </div>
        )}
        {dimensions.error && (
          <div className="mt-3">
            <Alert tone="error">{dimensions.error}</Alert>
          </div>
        )}
        {noDimensions && (
          <div className="mt-3">
            <Alert tone="warn">
              {contract?.name ?? 'This KPI'} has no approved dimension to break down by. A
              breakdown reads a dimension registered with the KPI and marked allowed; the platform
              does not choose a column on its own.
            </Alert>
          </div>
        )}
        {action.error && (
          <div className="mt-3">
            <Alert tone="error">{action.error}</Alert>
          </div>
        )}
      </Panel>

      {mode === 'movement' &&
        (contribution ? (
          <ContributionView
            response={contribution}
            nextDimension={nextDimension}
            onDrill={drill}
            onBreadcrumb={climb}
          />
        ) : (
          !action.pending && (
            <Panel>
              <EmptyState
                title="Nothing analysed yet"
                description="Pick a KPI and a date the platform has already evaluated, then explain the movement. The breakdown reuses that stored result rather than computing a new expectation, so the parts reconcile with the number the business saw."
              />
            </Panel>
          )
        ))}

      {mode === 'manual' &&
        manual &&
        (manual.mode === 'contribution' ? (
          <ContributionView
            response={manual}
            nextDimension={nextDimension}
            onDrill={drill}
            onBreadcrumb={climb}
          />
        ) : (
          <EntityView result={manual.result} queries={manual.evidence?.queries} />
        ))}
    </div>
  )
}
