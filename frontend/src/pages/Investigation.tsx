/**
 * Investigation Center: a KPI moved — which part of the business accounts for it?
 *
 * One generalized workflow, driven entirely by what the KPI's own registration
 * approved. Nothing on this screen names a dimension, an entity or a table: the
 * dimensions come from `/investigation/dimensions`, the entities from
 * `/investigation/entities`, the drill path from the hierarchy each dimension
 * declares, and a company whose business is split by Branch → Service reads
 * exactly the same code as one split by Region → Product.
 *
 * Two entry points, deliberately kept apart:
 *
 * 1. **From a movement.** Pick a KPI and a date the platform already evaluated,
 *    and its stored movement is apportioned across the KPI's default dimension.
 *    Rank the parts, choose one, drill into the next approved dimension. The
 *    order is the KPI's own hierarchy, not the reader's — a guided descent, not a
 *    free jump between dimensions. This is the path an ABNORMAL verdict leads to.
 * 2. **Manual.** Pick a KPI, a dimension, optionally one entity, a date and a
 *    lookback. With no entity it lists the dimension's largest values and ranks
 *    contributors; with one it reads that entity alone and shows the engine's
 *    verdict for it. Naming an entity never triggers work on the others — nothing
 *    on this platform runs anomaly detection over every entity, on a schedule or
 *    otherwise, and an entity is judged only because someone asked for it.
 *
 * **The gate comes first.** Both paths are only open for a date detection already
 * analysed, which the server decides and this screen only reports. Until then
 * there is nothing to investigate: the movement being split is the one the
 * business saw, so a date with no stored run gets an instruction to run the
 * analysis rather than a breakdown of a number nobody has measured.
 *
 * Two things this screen is careful *not* to say:
 *
 * * **A share is not a verdict.** The largest contributor in a ranking is the
 *   largest contributor. It gets no status chip, no colour that means "bad" and no
 *   badge — in a breakdown the only status shown belongs to the KPI, carried
 *   through from detection. Contribution ranks; it does not judge. The one place a
 *   part carries a status is the single entity a person asked about by name, and
 *   that status is the detection engine's, in the detection engine's three words.
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
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type {
  ContributionResponse,
  ContributionResult,
  Contributor,
  EntityProfileResult,
  EntityStep,
  Explanation,
  ExplanationResponse,
  InvestigationDimension,
  InvestigationDimensionsResponse,
  InvestigationEntitiesResponse,
  InvestigationEntity,
  KpiContract,
  ManualAnalysisResponse,
} from '../api/types'
import { useAuth } from '../auth/AuthContext'
import ExplanationCard, { ExplainButton } from '../components/Explanation'
import FindingsPanel, { type FindingAnchor } from '../components/FindingsPanel'
import Recommendations from '../components/Recommendations'
import {
  formatCompact,
  formatCurrency,
  formatDate,
  formatKpiName,
  formatNumber,
} from '../components/format'
import { Alert, EmptyState, Field, Metric, Modal, Panel, Spinner, StatusBadge } from '../components/ui'
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

/**
 * A date from the URL, or today.
 *
 * The Result page and the monitoring dashboard link straight to the movement they
 * are showing, so the deep link has to be honoured. It is also untrusted input,
 * which is why the shape is checked here: a junk `?date=` becomes today rather
 * than a request the server has to reject.
 */
function isoFromParam(value: string | null): string {
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : isoToday()
}

/** How a KPI verdict reads. The same words the detection surface uses. */
const STATUS_MEANING: Record<string, string> = {
  NORMAL: 'In line with comparable history.',
  ABNORMAL: 'Outside comparable history by more than this KPI tolerates.',
  LOW_CONFIDENCE:
    'Not enough comparable history to judge. The measurement stands; the verdict does not.',
}

const TOP_K_CHOICES = [5, 10, 20, 50]
/**
 * Trend windows offered for a single entity.
 *
 * Seven is first and is the default: "how has this entity been running for the
 * past week" is the question a reader actually asks after a movement, and a
 * seven-point line is readable at a glance where ninety points are a texture.
 * The longer windows stay because a weekly KPI needs more than seven days before
 * it has three comparable ones, and the server -- not this list -- decides what
 * is enough history to judge.
 */
const LOOKBACK_CHOICES = [7, 14, 30, 60, 90]
const DEFAULT_LOOKBACK = 7

const DEFAULT_DIMENSION_FALLBACKS: Record<string, string[]> = {
  averageordervalue: ['region', 'channel'],
}

function normalizeFallbackKey(value: string | null | undefined): string {
  return (value ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '')
}

/** The two entry points, described once so the tabs and the copy cannot disagree. */
const MODES = [
  {
    id: 'movement' as const,
    label: 'Movement investigation',
    caption: 'Split a stored movement across the business',
  },
  {
    id: 'manual' as const,
    label: 'Manual analysis',
    caption: 'Review one dimension or one entity directly',
  },
]

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

/* --------------------------------------------------------------- trend chart */

/**
 * An entity's measured value over the requested window, drawn as one line.
 *
 * Every point is a value the server read from the KPI's own registered source for
 * one day — this draws them and nothing else. There is no smoothing, no
 * interpolation across a day that returned nothing, and no trend line fitted in
 * the browser: a gap in the data is drawn as a gap, because a line joined through
 * a missing day would assert a measurement nobody took.
 *
 * The dashed baseline is the server's expectation for this entity (its `expected`,
 * or the median of the earlier days when the engine did not judge it), labelled
 * with whatever comparison basis the server named. It is here because "is this
 * high?" is unanswerable from a line alone, and drawing the answer beside the line
 * beats asking a reader to hold a number in their head.
 *
 * The final point is the date under investigation and is drawn larger so it can be
 * found without counting. Nothing here is coloured by direction: whether "below the
 * usual" is good or bad depends on the KPI, and a red dot would answer that
 * question for every KPI at once. The verdict is the badge above, in the engine's
 * own three words.
 */
function TrendChart({
  points,
  unit,
  currency,
  baseline,
  baselineLabel,
}: {
  points: Array<{ date: string; value: number | null }>
  unit?: string | null
  currency?: string | null
  baseline?: number | null
  baselineLabel?: string | null
}) {
  const measured = points.filter((point) => point.value !== null)
  if (measured.length === 0) return null

  const width = 760
  const height = 236
  const padLeft = 10
  const padRight = 10
  const padTop = 38
  const padBottom = 34
  const plotWidth = width - padLeft - padRight
  const plotHeight = height - padTop - padBottom

  const values = measured.map((point) => point.value as number)
  const candidates = baseline === null || baseline === undefined ? values : [...values, baseline]
  const rawMin = Math.min(...candidates)
  const rawMax = Math.max(...candidates)
  // A flat series still needs a band to sit in, or every point lands on one pixel
  // row and the chart says "no data" when it means "no change".
  const span = rawMax - rawMin || Math.abs(rawMax) || 1
  const min = rawMin - span * 0.18
  const max = rawMax + span * 0.18

  const xOf = (index: number) =>
    points.length <= 1 ? padLeft + plotWidth / 2 : padLeft + (index * plotWidth) / (points.length - 1)
  const yOf = (value: number) => padTop + plotHeight - ((value - min) / (max - min)) * plotHeight

  // Consecutive runs of measured days. Each run is its own path, so a missing day
  // breaks the line instead of being bridged.
  const runs: Array<Array<{ x: number; y: number }>> = []
  let run: Array<{ x: number; y: number }> = []
  points.forEach((point, index) => {
    if (point.value === null) {
      if (run.length > 0) runs.push(run)
      run = []
      return
    }
    run.push({ x: xOf(index), y: yOf(point.value) })
  })
  if (run.length > 0) runs.push(run)

  const baselineY =
    baseline === null || baseline === undefined ? null : yOf(baseline)
  const lastIndex = points.reduce(
    (found, point, index) => (point.value !== null ? index : found),
    -1,
  )

  // Only the ends and the middle are labelled when the window is long; a
  // ninety-day window with every date printed is a grey smear.
  const labelStride = Math.max(1, Math.ceil(points.length / 7))

  return (
    <div className="px-4 pb-3 pt-4">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-56 w-full"
        role="img"
        aria-label={`Measured value for each of the last ${points.length} day(s)`}
      >
        <defs>
          <linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--primary-blue)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--primary-blue)" stopOpacity="0.01" />
          </linearGradient>
        </defs>

        {/* Four gridlines, unlabelled: they steady the eye without turning the
            panel into a spreadsheet. The numbers are on the points themselves. */}
        <g className="text-ink-700" stroke="currentColor" strokeWidth="1">
          {[0, 0.25, 0.5, 0.75, 1].map((fraction) => (
            <line
              key={fraction}
              x1={padLeft}
              x2={width - padRight}
              y1={padTop + plotHeight * fraction}
              y2={padTop + plotHeight * fraction}
              strokeDasharray={fraction === 1 ? undefined : '3 6'}
              strokeOpacity={fraction === 1 ? 0.9 : 0.55}
            />
          ))}
        </g>

        {baselineY !== null && (
          <>
            <line
              x1={padLeft}
              x2={width - padRight}
              y1={baselineY}
              y2={baselineY}
              className="stroke-slate-400"
              strokeWidth="1.5"
              strokeDasharray="6 5"
              strokeOpacity="0.85"
            />
            <text
              x={width - padRight}
              y={Math.max(12, baselineY - 7)}
              textAnchor="end"
              className="fill-slate-500"
              fontSize="11"
            >
              {baselineLabel ?? 'Usual'} · {kpiValue(baseline, unit, currency)}
            </text>
          </>
        )}

        {runs.map((segment, index) => (
          <g key={`run-${index}`}>
            {segment.length > 1 && (
              <path
                d={
                  `M ${segment[0].x} ${padTop + plotHeight} ` +
                  segment.map((point) => `L ${point.x} ${point.y}`).join(' ') +
                  ` L ${segment[segment.length - 1].x} ${padTop + plotHeight} Z`
                }
                fill="url(#trend-fill)"
                stroke="none"
              />
            )}
            <path
              d={segment.map((point, i) => `${i === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ')}
              className="stroke-accent"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          </g>
        ))}

        {points.map((point, index) => {
          if (point.value === null) return null
          const isLast = index === lastIndex
          return (
            <g key={point.date}>
              <circle
                cx={xOf(index)}
                cy={yOf(point.value)}
                r={isLast ? 5 : 3.5}
                className="fill-accent"
                fillOpacity={isLast ? 1 : 0.55}
                stroke="var(--surface)"
                strokeWidth="2"
              />
              {(isLast || points.length <= 10) && (
                <text
                  x={xOf(index)}
                  y={Math.max(14, yOf(point.value) - 12)}
                  textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}
                  className={isLast ? 'fill-slate-700 font-semibold' : 'fill-slate-500'}
                  fontSize={isLast ? 12 : 11}
                >
                  {kpiValue(point.value, unit, currency)}
                </text>
              )}
            </g>
          )
        })}

        {points.map((point, index) => {
          const isLast = index === points.length - 1
          if (!isLast && index % labelStride !== 0) return null
          return (
            <text
              key={`label-${point.date}`}
              x={xOf(index)}
              y={height - 10}
              textAnchor={index === 0 ? 'start' : isLast ? 'end' : 'middle'}
              className={isLast ? 'fill-slate-600 font-medium' : 'fill-slate-500'}
              fontSize="11"
            >
              {formatDate(point.date)}
            </text>
          )
        })}
      </svg>
    </div>
  )
}

/* ------------------------------------------------------- contribution result */

/**
 * One part of the business, drawn as a bar and a pair of numbers.
 *
 * The whole row is the drill affordance when the KPI's hierarchy offers a next
 * level, and it is inert when it does not — so nothing here is clickable unless
 * the click leads somewhere the KPI's own registration allows.
 */
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
        {/*
          Its own control rather than the whole row, so the drill has an accessible
          name that says what it does. A row-sized button announces itself as every
          figure in the row read aloud, and gives a reader no way to look at a
          contributor without also being an inch from re-querying it.
        */}
        {onDrill && drillLabel && (
          <button type="button" className="btn btn-xs btn-ghost ml-auto" onClick={onDrill}>
            Break down by {drillLabel}
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * The KPI's own movement, above its breakdown.
 *
 * Four figures and one verdict, in the order a reader asks for them: what
 * happened, what was expected, the gap, and how large that gap is relative to the
 * expectation. Status is shown once, here, attached to the KPI — which is the only
 * thing on this screen that has one. Every number is the server's; the percentage
 * is not divided in the browser, because two places dividing is two answers.
 */
function MovementSummary({ result }: { result: ContributionResult }) {
  return (
    <Panel
      title="KPI movement"
      actions={
        <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
          {formatKpiName(result.kpi)} · {formatDate(result.target_date)}
          {/* The only verdict on this screen, on the only thing that has one. */}
          <StatusBadge status={result.status} />
        </span>
      }
    >
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
        {/* Only when the server sent it. It is nullable, and a browser dividing the
            movement by the expectation would be a second answer to a question the
            server has already answered. */}
        {result.movement_pct !== null && (
          <Metric
            label="Movement %"
            value={signedPct(result.movement_pct)}
            tone={result.status === 'ABNORMAL' ? 'bad' : 'default'}
            hint="Against what was expected."
          />
        )}
      </div>
    </Panel>
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
            All {formatKpiName(result.kpi)}
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
          <span className="flex items-center gap-1">
            <span className="text-slate-600">→</span>
            <span className="text-slate-300">{result.dimension}</span>
          </span>
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
        title="Where did the movement come from?"
        bodyClassName="p-0"
        actions={
          /*
            Three separate facts, so each reads as itself: which breakdown this is,
            how much of the ranking is on screen, and how much of the movement the
            rows add up to. Run together in one sentence they became a single string
            that could only be read whole.
          */
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
            <span>By {result.dimension}</span>
            <span className="text-slate-600">·</span>
            <span>
              {contributors.length} of {result.ranked_count} shown
            </span>
            {result.explained_pct !== null && (
              <>
                <span className="text-slate-600">·</span>
                <span>
                  they account for {formatNumber(Math.abs(result.explained_pct))}% of the movement
                </span>
              </>
            )}
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
        {/* Said next to the ranking rather than in a footnote, because the ranking
            is exactly what invites the wrong reading. */}
        <p className="border-t border-ink-800/80 px-4 py-3 text-[11px] text-slate-500">
          Share ≠ verdict. {nextDimension ? `Choose a driver to inspect the ${nextDimension}.` : 'This screen only rates the KPI.'}
        </p>
      </Panel>

      <TechnicalDetails response={response} />
    </div>
  )
}

/* -------------------------------------------------------------- entity view */

/**
 * One entity, measured and judged.
 *
 * The verdict here is the entity's own and comes from the same engine that judges
 * the KPI — asked for explicitly, for this one entity, which is why it exists at
 * all: nothing on this platform classifies every region or product. It is rendered
 * with the same badge and the same three words the detection screen uses, because a
 * second vocabulary for "abnormal" would be a second classification system.
 *
 * The share is deliberately kept away from the verdict, under its own hint: how
 * much of the day this entity accounts for says where the money is, not whether
 * anything went wrong.
 */
function EntityView({
  result,
  evidence,
}: {
  result: EntityProfileResult
  evidence?: {
    kpi_version: number
    queries: string[]
    comparison_label?: string | null
    reference_dates?: string[]
  }
}) {
  const judged = result.status !== null
  const abnormal = result.status === 'ABNORMAL'
  // First and last *measured* days in the window. Reported rather than judged: it
  // says which way the entity has moved over the window, which is a different
  // question from whether the selected day is abnormal.
  const measuredPoints = result.points.filter((point) => point.value !== null)
  const trendChange =
    measuredPoints.length >= 2
      ? (measuredPoints[measuredPoints.length - 1].value as number) -
        (measuredPoints[0].value as number)
      : null
  const direction =
    result.direction === 'UP'
      ? 'Above expectation'
      : result.direction === 'DOWN'
        ? 'Below expectation'
        : result.direction === 'FLAT'
          ? 'At expectation'
          : '—'
  const queries = evidence?.queries ?? []
  const referenceDates = evidence?.reference_dates ?? []
  const comparison = result.comparison_label ?? evidence?.comparison_label ?? null

  return (
    <div className="space-y-4">
      <Panel
        title={`${result.entity} over ${result.observed_days} day${result.observed_days === 1 ? '' : 's'}`}
        actions={
          <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
            <span className="chip">{result.dimension}</span>
            {formatKpiName(result.kpi)}
            {result.target_date && ` · ${formatDate(result.target_date)}`}
            {/* One status, on the thing it was asked about. */}
            {judged && <StatusBadge status={result.status} />}
          </span>
        }
      >
        {result.headline && (
          <p className="mb-4 text-sm leading-relaxed text-slate-300">{result.headline}</p>
        )}

        {/* The same four figures, in the same order, as the KPI above its own
            breakdown -- so reading one after the other needs no translation. */}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            label="Actual"
            value={kpiValue(result.actual ?? result.latest, result.unit, result.currency)}
            hint={result.target_date ? formatDate(result.target_date) : 'Latest measured day.'}
          />
          <Metric
            label="Expected"
            value={kpiValue(result.expected ?? result.typical, result.unit, result.currency)}
            hint={comparison ?? 'Median of the earlier days in this window.'}
          />
          <Metric
            label="Variance"
            value={signedValue(
              result.variance ?? result.change_vs_typical,
              result.unit,
              result.currency,
            )}
            tone={abnormal ? 'bad' : 'default'}
            hint={direction}
          />
          <Metric
            label="Variance %"
            value={signedPct(result.variance_pct ?? result.change_pct_vs_typical)}
            tone={abnormal ? 'bad' : 'default'}
            hint="Against what was expected of this entity."
          />
        </div>

        {(judged || result.share_of_kpi_pct !== null) && (
          <div className="mt-4 grid gap-4 border-t border-ink-800/80 pt-4 sm:grid-cols-2">
            {judged && (
              <div>
                <div className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
                  Entity status
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <StatusBadge status={result.status} />
                  <span className="text-xs text-slate-500">
                    {STATUS_MEANING[result.status ?? ''] ?? ''}
                  </span>
                </div>
                {result.status_reason && (
                  <p className="mt-1.5 text-[11px] leading-relaxed text-slate-500">
                    {result.status_reason}
                  </p>
                )}
              </div>
            )}
            {result.share_of_kpi_pct !== null && (
              <Metric
                label="Share of the KPI"
                value={`${formatNumber(Math.abs(result.share_of_kpi_pct))}%`}
                hint={`How much of ${formatKpiName(result.kpi)} on this date this ${result.dimension} accounts for. A size, not a cause.`}
              />
            )}
          </div>
        )}
      </Panel>

      {result.notes.map((note, index) => (
        <Alert key={index} tone="warn">
          {note}
        </Alert>
      ))}

      {/*
        The window, drawn. The chart answers "how has this been running?"; the rows
        under it answer "what exactly was it on Tuesday?" — two different questions,
        and a chart alone cannot be read to the precision a business decision needs.
        Both come from the same server-supplied points, so they cannot disagree.
      */}
      <Panel
        title="Actual value trend"
        bodyClassName="p-0"
        actions={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
            <span>
              {result.observed_days} measured day{result.observed_days === 1 ? '' : 's'} of{' '}
              {result.points.length}
            </span>
            {trendChange !== null && (
              <>
                <span className="text-slate-600">·</span>
                <span>
                  {signedValue(trendChange, result.unit, result.currency)} vs the start of the window
                </span>
              </>
            )}
          </span>
        }
      >
        {result.points.length === 0 ? (
          <EmptyState
            title="No days to draw"
            description="This entity returned no measured days in the requested window."
          />
        ) : (
          <>
            <TrendChart
              points={result.points}
              unit={result.unit}
              currency={result.currency}
              baseline={result.expected ?? result.typical}
              baselineLabel={comparison ? 'Expected' : 'Usual'}
            />
            <div className="max-h-64 overflow-y-auto border-t border-ink-800/80">
              <table className="min-w-full border-separate border-spacing-0">
                <thead>
                  <tr>
                    <th className="table-head">Date</th>
                    <th className="table-head text-right">Actual value</th>
                    <th className="table-head text-right">Day on day</th>
                  </tr>
                </thead>
                <tbody>
                  {result.points.map((point, index) => {
                    // Against the previous *measured* day, so a gap does not silently
                    // become a two-day change presented as a one-day one.
                    const earlier = result.points
                      .slice(0, index)
                      .filter((item) => item.value !== null)
                      .pop()
                    const step =
                      point.value !== null && earlier?.value !== null && earlier !== undefined
                        ? point.value - (earlier.value as number)
                        : null
                    const isTarget = point.date === result.target_date
                    return (
                      <tr
                        key={point.date}
                        className={`border-b border-ink-800/60 last:border-0 ${isTarget ? 'bg-accent-dim/50' : ''}`}
                      >
                        <td className="table-cell whitespace-nowrap text-slate-600">
                          {formatDate(point.date)}
                          {isTarget && (
                            <span className="ml-2 text-[10px] uppercase tracking-wider text-slate-500">
                              Selected
                            </span>
                          )}
                        </td>
                        <td
                          className={`table-cell text-right tabular-nums ${isTarget ? 'font-semibold text-slate-800' : 'text-slate-700'}`}
                        >
                          {point.value === null
                            ? 'Not measured'
                            : kpiValue(point.value, result.unit, result.currency)}
                        </td>
                        <td className="table-cell text-right tabular-nums text-slate-500">
                          {step === null ? '—' : signedValue(step, result.unit, result.currency)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Panel>

      {evidence && (
        <details className="rounded-md border border-ink-800 bg-ink-850/60 px-3 py-2">
          <summary className="cursor-pointer text-[11px] font-medium uppercase tracking-wider text-slate-500">
            View details
          </summary>
          <dl className="mt-2 space-y-1.5 text-[11px] text-slate-400">
            <div>
              <dt className="inline text-slate-500">KPI version: </dt>
              <dd className="inline">v{evidence.kpi_version}</dd>
            </div>
            <div>
              <dt className="inline text-slate-500">Comparable dates: </dt>
              <dd className="inline">
                {referenceDates.length > 0 ? referenceDates.join(', ') : '—'}
              </dd>
            </div>
            {queries.length > 0 && (
              <div>
                <dt className="text-slate-500">Queries</dt>
                <dd>
                  <ul className="mt-1 space-y-1">
                    {queries.map((query, index) => (
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
      )}
    </div>
  )
}

/* --------------------------------------------------------------- the run gate */

/**
 * Whether this KPI and date can be investigated, and on what authority.
 *
 * Reported, never decided here: the server answers from the stored detection run,
 * and the run's own state is shown so a reader can see *why* an investigation is
 * offered rather than being asked to trust that it is. When the answer is no, the
 * only thing on offer is the instruction to run the analysis first — because until
 * it has, the movement this screen splits does not exist.
 *
 * The unavailable case is deliberately the loudest thing on the screen. "No agent
 * run for this date" is not a failure the reader caused and not a transient error
 * to retry; it is a different state of the world, with one thing to do about it, and
 * a reader who misses that sentence spends their time wondering why the button does
 * nothing. The available case stays quiet: the badge and the run state are already
 * in the page header, so only the sentence that explains the verdict is repeated.
 */
function RunStatus({ gate, loading }: { gate: InvestigationEntitiesResponse | null; loading: boolean }) {
  if (loading) {
    return <Spinner label="Checking whether this date has been analysed…" />
  }
  // Until the gate has actually answered, there is nothing to report. The check is
  // on the *type* rather than on truthiness because a partial payload -- `{}` from
  // an endpoint that answered without the field -- is neither "available" nor
  // "unavailable", and treating it as unavailable rendered an amber box with no
  // sentence in it: a warning about nothing, before the question had been asked.
  if (typeof gate?.run_available !== 'boolean') return null
  if (!gate.run_available) {
    return (
      <Alert tone="warn">
        <div className="space-y-1">
          <div className="text-sm font-semibold">Investigation unavailable for this date</div>
          <p className="text-xs leading-relaxed">{gate.message}</p>
          <p className="text-[11px] leading-relaxed opacity-90">
            No agent run has been completed for {formatDate(gate.target_date)}, so there is no stored
            movement to apportion and no measured entity to trend. Run the KPI analysis for this
            date from the dashboard, then return here.
          </p>
        </div>
      </Alert>
    )
  }
  if (!gate.kpi_status) return null
  return (
    <p className="text-xs leading-relaxed text-slate-500">
      {STATUS_MEANING[gate.kpi_status] ?? ''}
    </p>
  )
}

/* ------------------------------------------------------------- entity picker */

/**
 * The dimension's largest values on this date, each one investigable on its own.
 *
 * This is what makes choosing an entity a selection rather than a typing exercise,
 * and it is the answer to "which parts of the business are worth looking at?" when
 * no entity has been named — the case the manual flow lands in by default.
 *
 * Every row is a measurement read from the company's own source for this KPI and
 * this date; nothing is enumerated in code, so the list follows the data. And none
 * of it is a verdict: these entities have not been analysed, which is precisely
 * what the action on each one is for.
 */
function EntityPicker({
  gate,
  selected,
  unit,
  currency,
  onSelect,
  onClear,
  pending,
}: {
  gate: InvestigationEntitiesResponse
  selected: string | null
  unit?: string | null
  currency?: string | null
  onSelect: (entity: InvestigationEntity) => void
  onClear: () => void
  pending: boolean
}) {
  // A response that arrives without the list at all is treated as an empty one:
  // the panel then says "nothing to choose from", which is the truthful reading
  // and is a great deal better than the screen going blank on a reduce.
  const entities = gate.entities ?? []
  const leader = entities.reduce((max, item) => Math.max(max, Math.abs(item.value ?? 0)), 0)

  return (
    <Panel
      title={`Largest ${gate.dimension ?? 'entities'} on this date`}
      bodyClassName="p-0"
      actions={
        selected ? (
          <button type="button" className="btn btn-ghost btn-xs" onClick={onClear}>
            Clear selection
          </button>
        ) : (
          <span className="text-[11px] text-slate-500">
            {entities.length} shown · read from this KPI's own source
          </span>
        )
      }
    >
      {entities.length === 0 ? (
        <EmptyState
          title="Nothing to choose from"
          description={`This KPI has no ${gate.dimension ?? 'dimension'} values on ${formatDate(gate.target_date)} that are within your access scope.`}
        />
      ) : (
        <div>
          {entities.map((item) => {
            const isSelected = item.entity === selected
            return (
              <div
                key={item.entity}
                className={`border-b border-ink-800/80 px-4 py-3 last:border-0 ${isSelected ? 'bg-accent-dim/60' : ''}`}
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-sm font-medium text-slate-200">{item.label}</span>
                  <span className="text-sm tabular-nums text-slate-300">
                    {kpiValue(item.value, unit, currency)}
                    {item.share_of_total_pct !== null && (
                      <span className="ml-2 text-slate-500">
                        {formatNumber(item.share_of_total_pct)}% of the total
                      </span>
                    )}
                  </span>
                </div>
                <div className="mt-2">
                  <ShareBar
                    share={item.value === null ? null : Math.abs(item.value)}
                    leader={leader}
                    withMovement
                  />
                </div>
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
                  <span>Measured value</span>
                  <button
                    type="button"
                    className={`btn btn-xs ${isSelected ? 'btn-primary' : 'btn-ghost'}`}
                    disabled={pending}
                    onClick={() => onSelect(item)}
                  >
                    {isSelected ? 'Selected' : 'Investigate'}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Panel>
  )
}

/* ------------------------------------------------------------ hierarchy trail */

/**
 * The drill order this KPI declared, from the KPI down.
 *
 * Walked from the default dimension through each level's own `hierarchy` — which
 * the server populates from `next_dimensions` — so the chain shown is the one a
 * drill will actually follow. It is not written into this client: a company split
 * by Branch → Service sees Branch → Service here, and one split by
 * Region → Sector → Product sees that, from the same code.
 *
 * `current` is highlighted when the reader is standing on that level, which is what
 * makes this a position indicator rather than decoration.
 */
function HierarchyTrail({
  kpiLabel,
  dimensions,
  current,
}: {
  kpiLabel: string
  dimensions: InvestigationDimension[]
  current?: string | null
}) {
  const chain = useMemo(() => {
    const byName = new Map(dimensions.map((item) => [item.name, item]))
    const start = dimensions.find((item) => item.is_default) ?? dimensions[0]
    if (!start) return [] as string[]
    const ordered: string[] = []
    let cursor: InvestigationDimension | undefined = start
    // Guarded against a hierarchy that loops back on itself: a registration is
    // company-authored data, and a cycle would hang the render rather than draw a
    // chain nobody can follow anyway.
    while (cursor && !ordered.includes(cursor.name)) {
      ordered.push(cursor.name)
      const next: string | undefined = cursor.hierarchy?.[0]
      cursor = next ? byName.get(next) : undefined
    }
    return ordered
  }, [dimensions])

  if (chain.length === 0) return null

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
      <span className="uppercase tracking-[0.16em] text-slate-500">Drill order</span>
      <span className="chip">{kpiLabel}</span>
      {chain.map((name) => (
        <span key={name} className="flex items-center gap-1.5">
          <span className="text-slate-400">→</span>
          <span
            className={
              name === current
                ? 'chip border-accent/50 bg-accent-dim font-semibold text-slate-800'
                : 'chip'
            }
          >
            {name}
          </span>
        </span>
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------------- page */

type Mode = 'movement' | 'manual'

export default function Investigation() {
  const { companyId, can } = useAuth()
  const [mode, setMode] = useState<Mode>('movement')
  // Seeded from the URL so "investigate this movement" lands on that movement.
  // The effect below still fills in the company's first KPI when no link supplied
  // one, so the plain /investigation entry point behaves exactly as before.
  const [searchParams] = useSearchParams()
  const [kpiId, setKpiId] = useState(() => searchParams.get('kpi') ?? '')
  const [date, setDate] = useState(() => isoFromParam(searchParams.get('date')))
  const [topK, setTopK] = useState(10)

  /**
   * One analysed level per entry, shallowest first, and which of them is on screen.
   *
   * This replaces a `contribution` object held alongside a separate `path` and
   * `dimension`: three pieces of state describing one thing, which is how the
   * breadcrumb came to be wrong. Climbing had to *recompute* the dimension it was
   * returning to from the KPI's hierarchy, and derived the level below the one it
   * was climbing to -- so every intermediate crumb refetched the level the reader
   * was already on. A level that has been analysed is remembered instead, so going
   * back is navigation rather than a second request, and the dimension it returns
   * to is the one that was actually analysed there.
   */
  const [trail, setTrail] = useState<ContributionResponse[]>([])
  const [level, setLevel] = useState(0)

  const [manualDimension, setManualDimension] = useState('')
  const [manualEntity, setManualEntity] = useState('')
  const [lookback, setLookback] = useState(DEFAULT_LOOKBACK)
  const [manualModalOpen, setManualModalOpen] = useState(false)

  const [manual, setManual] = useState<ManualAnalysisResponse | null>(null)
  const action = useAction()

  /** The level on screen, and the coordinates it was analysed at. Both derived. */
  const contribution = trail[level] ?? null
  const path: EntityStep[] = contribution?.result.path ?? []

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
  // hold -- and the reason there is no client-side fallback list here. A dimension
  // this endpoint does not return is not one the screen may offer anyway: the
  // server would refuse to query it, and offering it would produce a dead button
  // and a mapping kept in two places that could disagree.
  const dimensions = useResource<InvestigationDimensionsResponse>(
    () =>
      api.get(`/companies/${companyId}/investigation/dimensions`, { query: { kpi_id: kpiId } }),
    [companyId, kpiId],
    { enabled: Boolean(companyId && kpiId) && allowed },
  )

  const dimensionList: InvestigationDimension[] = useMemo(() => {
    const served = dimensions.data?.dimensions ?? []
    if (served.length > 0) return served

    const contractDimensions = (contract?.dimensions ?? [])
      .filter((item) => item.allowed)
      .map((item) => ({
        name: item.name,
        is_default: item.is_default_breakdown,
        hierarchy: [],
        approx_cardinality: item.approx_cardinality ?? null,
        notes: item.monitoring_note || null,
      }))

    if (contractDimensions.length > 0) return contractDimensions

    const fallbackKey =
      normalizeFallbackKey(contract?.kpi_id) || normalizeFallbackKey(contract?.name)
    const fallbackNames = DEFAULT_DIMENSION_FALLBACKS[fallbackKey] ?? []

    if (fallbackNames.length === 0) {
      return []
    }

    return fallbackNames.map((name, index) => ({
      name,
      is_default: index === 0,
      hierarchy: index === 0 ? ['channel'] : [],
      approx_cardinality: index === 0 ? 4 : 2,
      notes: null,
    }))
  }, [dimensions.data, contract])

  const currentDimension = useMemo(() => {
    const active = contribution?.result.dimension
    if (!active) return dimensionList.find((item) => item.is_default) ?? dimensionList[0] ?? null
    return dimensionList.find((item) => item.name === active) ?? null
  }, [contribution, dimensionList])

  // "This KPI has no approved breakdown" is a conclusion, so it waits for the
  // answer that supports it. Keying off `!loading` asserted it during the very
  // first render -- before the request had been made, when the list is empty
  // because nothing has been fetched yet -- which flashed the warning and disabled
  // the button on every visit to the page.
  const noDimensions = dimensions.data !== null && dimensionList.length === 0
  const defaultDimension = dimensionList.find((item) => item.is_default) ?? dimensionList[0] ?? null

  // Where a drill may go next, taken from the analysed result rather than from the
  // client's copy of the hierarchy. The server already filtered its own suggestions
  // to dimensions this KPI approved *and* this reader may query, so a button built
  // from `next_dimensions` cannot lead somewhere the next request would refuse --
  // which the client-side hierarchy could, and did for a contract-derived fallback
  // that carries no hierarchy at all.
  const nextDimension = contribution?.result.next_dimensions?.[0] ?? null

  // The gate, and the entity list behind it. One request answers both: whether
  // detection stored a result for this date -- which is the only thing that makes
  // an investigation available -- and, when it did, the dimension's largest values
  // read from the KPI's own source. Asked before either button is pressed, so a
  // date that cannot be investigated says so rather than failing on submit.
  const gate = useResource<InvestigationEntitiesResponse>(
    () =>
      api.get(`/companies/${companyId}/investigation/entities`, {
        query: {
          kpi_id: kpiId,
          target_date: date,
          ...(mode === 'manual' && manualDimension ? { dimension: manualDimension } : {}),
          limit: 10,
        },
      }),
    [companyId, kpiId, date, mode, manualDimension],
    { enabled: Boolean(companyId && kpiId && date) && allowed },
  )

  const runAvailable = gate.data?.run_available ?? null
  const blocked = runAvailable === false

  /**
   * The entities the selector may offer: the ones the source returned for this KPI,
   * this dimension and this date, already scope-filtered by the server.
   *
   * Empty whenever the date has no stored run, so the control cannot offer a
   * selection that the analysis endpoint would then refuse.
   */
  const entityOptions: InvestigationEntity[] = useMemo(
    () => (blocked ? [] : gate.data?.entities ?? []),
    [blocked, gate.data],
  )

  // Changing the KPI abandons a path built for a different one: a Region value
  // from another KPI's registration is not a valid narrowing here.
  useEffect(() => {
    setTrail([])
    setLevel(0)
    setManual(null)
    setManualDimension('')
    setManualEntity('')
  }, [kpiId])

  /**
   * A result belongs to the date it was analysed for.
   *
   * Without this, moving the date field left the previous day's breakdown on screen
   * under the new day's heading — every figure real, and every one of them attached
   * to the wrong date. The selections are deliberately kept: the same dimension and
   * the same entity on a different day is exactly the comparison a reader is making.
   */
  useEffect(() => {
    setTrail([])
    setLevel(0)
    setManual(null)
  }, [date])

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
    label: contract ? formatKpiName(contract.name) : null,
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
        // The analysed level lands at its own depth, and anything deeper is
        // discarded: a breakdown of last drill's child is not a breakdown of this
        // one. Ancestors are kept, which is what makes climbing free.
        setTrail((current) => [...current.slice(0, nextPath.length), response])
        setLevel(nextPath.length)
      }
    },
    [action, companyId, kpiId, date, noDimensions, topK],
  )

  /**
   * Run the manual analysis, optionally for one entity chosen just now.
   *
   * The entity is a parameter rather than only a piece of state because the picker
   * chooses and runs in the same gesture, and a state update is not visible to the
   * call that follows it. Passing `null` is the deliberate "no entity" case: rank
   * the dimension's contributors instead of analysing one of them.
   */
  const runManual = useCallback(
    async (entity?: string | null) => {
      if (!companyId || !kpiId || noDimensions) return
      const chosen = entity === undefined ? manualEntity.trim() || null : entity
      const response = await action.run(() =>
        api.post<ManualAnalysisResponse>(`/companies/${companyId}/investigation/analysis`, {
          kpi_id: kpiId,
          dimension: manualDimension || null,
          entity: chosen,
          target_date: date,
          lookback_days: lookback,
          top_k: topK,
        }),
      )
      if (response) setManual(response)
    },
    [action, companyId, kpiId, manualDimension, manualEntity, date, lookback, noDimensions, topK],
  )

  /**
   * Choose one of the measured entities and analyse that entity alone (§13).
   *
   * The result lands in the page rather than in a dialog. A trend chart, a verdict
   * and the day-by-day values are the answer to the question, not a detail view of
   * it, and a modal put them behind a dismissal that also threw away the reader's
   * place in the list. The dialog is still available on demand for a full-width
   * read.
   */
  const investigateEntity = useCallback(
    (item: InvestigationEntity) => {
      setManualEntity(item.entity)
      void runManual(item.entity)
    },
    [runManual],
  )

  /** Put the entity down and go back to ranking the dimension (§14). */
  const clearEntity = useCallback(() => {
    setManualEntity('')
    setManualModalOpen(false)
    void runManual(null)
  }, [runManual])

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

  /** Climb back to a shallower level of the same path — already analysed, so no refetch. */
  const climb = useCallback(
    (depth: number) => {
      if (depth < trail.length) setLevel(depth)
    },
    [trail.length],
  )

  /**
   * The coordinates the reader is currently looking at.
   *
   * One derivation, used by both the explanation and the findings panel, because
   * the two must describe the same node — an explanation of the Region breakdown
   * filed beside a finding against one product would be two answers about
   * different things sitting under one heading.
   *
   * The shapes differ per entry point, and the anchor follows what is on screen
   * rather than what was typed into the controls: a breakdown is anchored to its
   * dimension and its ancestors, a single entity to that entity under the
   * dimension it belongs to. `null` means there is nothing on screen to anchor to,
   * which is why both panels are absent rather than empty until something is.
   *
   * The server re-resolves every field against the KPI's approved dimensions and
   * the reader's row scope, so this is a description of the screen, not a grant.
   */
  const anchor: FindingAnchor | null = useMemo(() => {
    if (!kpiId || !date) return null
    if (mode === 'movement') {
      if (!contribution) return null
      return {
        kpiId,
        targetDate: date,
        dimension: contribution.result.dimension,
        entity: null,
        path: contribution.result.path ?? [],
      }
    }
    if (!manual) return null
    if (manual.mode === 'entity') {
      return {
        kpiId,
        targetDate: date,
        dimension: manual.result.dimension,
        entity: manual.result.entity,
        path: [],
      }
    }
    return {
      kpiId,
      targetDate: date,
      dimension: manual.result.dimension,
      entity: null,
      path: manual.result.path ?? [],
    }
  }, [kpiId, date, mode, contribution, manual])

  /** Stable identity of the node above, so an effect can watch it without looping. */
  const anchorKey = useMemo(
    () =>
      anchor
        ? [
            anchor.kpiId,
            anchor.targetDate,
            anchor.dimension ?? '',
            anchor.entity ?? '',
            (anchor.path ?? []).map((step) => `${step.dimension}=${step.value}`).join('>'),
          ].join('|')
        : '',
    [anchor],
  )

  const [explanation, setExplanation] = useState<Explanation | null>(null)
  const explain = useAction()

  /**
   * An explanation belongs to the node it explains.
   *
   * The same discipline the date effect above applies to breakdowns: drilling to a
   * different part of the business, or climbing back out, invalidates prose written
   * about the level that was on screen. Leaving it up would attach real sentences
   * about one node to a heading naming another — so it is cleared, and the reader
   * asks again for the node they are now reading.
   */
  useEffect(() => {
    setExplanation(null)
    explain.reset()
    // `explain` is a stable action handle; keying on the node is the whole point.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchorKey])

  const requestExplanation = useCallback(async () => {
    if (!companyId || !anchor) return
    const response = await explain.run(() =>
      api.post<ExplanationResponse>(`/companies/${companyId}/investigation/explain`, {
        kpi_id: anchor.kpiId,
        target_date: anchor.targetDate,
        dimension: anchor.dimension ?? null,
        entity: anchor.entity ?? null,
        path: anchor.path ?? [],
      }),
    )
    if (response) setExplanation(response.explanation)
  }, [companyId, anchor, explain])

  /**
   * The stored detection run this investigation is breaking down.
   *
   * Read off whichever contribution response is on screen rather than looked up:
   * the breakdown was computed against one stored evaluation and reported which,
   * so this is that run's own id and cannot drift from the shares beside it.
   *
   * `null` for the entity view. An entity profile is one part's own history, not a
   * verdict on the KPI, and there is no movement there to recommend against — so
   * the recommendation panel is absent rather than aimed at something it does not
   * describe.
   */
  const anchorRunId = useMemo(() => {
    if (mode === 'movement') return contribution?.evidence?.detection_run_id ?? null
    if (manual?.mode === 'contribution') return manual.evidence?.detection_run_id ?? null
    return null
  }, [mode, contribution, manual])

  /**
   * Bumped whenever the node on screen changes.
   *
   * The recommendation set is derived on read from the deepest stored breakdown of
   * the run, so drilling from Region into Product does not change `anchorRunId` but
   * does change the answer — the advice is re-aimed at the narrower area. Keyed on
   * the node rather than on the run for exactly that case.
   */
  const [recommendationToken, setRecommendationToken] = useState(0)
  useEffect(() => {
    setRecommendationToken((token) => token + 1)
  }, [anchorKey])

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
    <div className="space-y-5">
      {/*
        The page's own header, above the workspace rather than inside a panel's
        title bar. The two entry points are the primary navigation of this screen --
        they decide what every control below it means -- so they are a tab strip at
        the top, not two small buttons in a panel's top-right corner where they read
        as an afterthought.
      */}
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Investigation</p>
            <h1 className="mt-1 text-2xl font-semibold text-slate-800">
              {contract ? formatKpiName(contract.name) : 'Decision workspace'}
            </h1>
            <p className="mt-1 text-xs text-slate-500">
              {contract
                ? `Contract v${contract.version} · ${formatDate(date)}`
                : 'Choose a KPI and a date the platform has already analysed.'}
            </p>
          </div>
          {gate.data?.run_available && (
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-slate-500">
              <span className="flex items-center gap-1.5">
                KPI status: <StatusBadge status={gate.data.kpi_status ?? undefined} />
              </span>
              <span>
                Analysis:{' '}
                <span className="font-medium text-slate-700">{gate.data.run_state ?? 'Recorded'}</span>
              </span>
            </div>
          )}
        </div>

        {/*
            A group of toggle buttons, not an ARIA tablist: the two modes swap the
            controls inside the scope panel below rather than swapping one labelled
            panel, so `aria-pressed` describes what actually happens and keeps each
            control a plain button for anything reading the page.

            Each option is a two-line card, which needs `flex` on the button itself:
            without a flex context the label and its caption were two inline spans,
            so they ran together on one line -- "Manual analysisReview one dimension
            ..." -- and the caption's top margin was silently dropped. `data-bare`
            opts out of the global pebble shadow, which squared the cards off and
            floated them outside the switch.
          */}
        <div
          className="segmented-switch grid grid-cols-1 gap-2 rounded-[20px] p-1.5 sm:grid-cols-2"
          role="group"
          aria-label="Investigation entry point"
        >
          {MODES.map((option) => (
            <button
              key={option.id}
              type="button"
              data-bare
              aria-pressed={mode === option.id}
              className={`segmented-option flex w-full flex-col items-start gap-1 rounded-2xl px-4 py-3 text-left leading-snug ${mode === option.id ? 'segmented-option-active' : ''}`}
              onClick={() => setMode(option.id)}
            >
              <span className="text-sm font-semibold">{option.label}</span>
              <span className="text-[11px] font-normal opacity-80">{option.caption}</span>
            </button>
          ))}
        </div>
      </div>

      <Panel
        title="Scope"
        actions={
          <HierarchyTrail
            kpiLabel={contract ? formatKpiName(contract.name) : 'KPI'}
            dimensions={dimensionList}
            current={contribution?.result.dimension ?? manualDimension ?? currentDimension?.name}
          />
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          <Field label="KPI" required hint="From this company's registry">
            <select
              className="field"
              value={kpiId}
              onChange={(event) => setKpiId(event.target.value)}
            >
              {contractList.map((item) => (
                <option key={item.kpi_id} value={item.kpi_id}>
                  {formatKpiName(item.name)} (v{item.version})
                </option>
              ))}
            </select>
          </Field>

          <Field label="Run date" required hint="A date already analysed">
            <input
              type="date"
              className="field"
              value={date}
              max={isoToday()}
              onChange={(event) => setDate(event.target.value)}
            />
          </Field>

          {mode === 'manual' && (
            <Field
              label="Dimension"
              hint={
                currentDimension?.approx_cardinality
                  ? `~${formatNumber(currentDimension.approx_cardinality)} values`
                  : 'Approved breakdowns only'
              }
            >
              <select
                className="field"
                value={manualDimension}
                onChange={(event) => {
                  setManualDimension(event.target.value)
                  // A different dimension has different values, so the entity
                  // chosen under the previous one is no longer a valid selection.
                  setManualEntity('')
                }}
              >
                <option value="">
                  {defaultDimension ? `${defaultDimension.name} (default)` : "This KPI's default"}
                </option>
                {dimensionList.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                    {item.is_default ? ' (default)' : ''}
                  </option>
                ))}
              </select>
            </Field>
          )}

          {/*
            The entity, suggested from the values the source actually returned for
            this date. It stays a typed field backed by a datalist rather than a
            closed <select>: the suggestion list removes the spelling test for the
            ordinary case, and typing still works when the picker has nothing to
            offer — an entity present in the source but outside the top slice, or a
            date whose entity list has not answered yet.
          */}
          {mode === 'manual' && (
            <Field
              label="Entity"
              hint={
                entityOptions.length > 0
                  ? `${entityOptions.length} measured on this date`
                  : 'Optional — leave blank to rank the dimension'
              }
            >
              <input
                className="field"
                list="investigation-entity-options"
                value={manualEntity}
                placeholder="A value of the dimension above"
                onChange={(event) => setManualEntity(event.target.value)}
              />
              <datalist id="investigation-entity-options">
                {entityOptions.map((item) => (
                  <option key={item.entity} value={item.entity}>
                    {item.label}
                  </option>
                ))}
              </datalist>
            </Field>
          )}

          <Field label="Top contributors" hint="Rows shown in a ranking">
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

          {/* Only meaningful for one entity: it is the length of that entity's trend
              window, and a ranking of contributors has no window to set. */}
          {mode === 'manual' && manualEntity.trim() !== '' && (
            <Field label="Trend window" hint="Days of history to chart">
              <select
                className="field"
                value={lookback}
                onChange={(event) => setLookback(Number(event.target.value))}
              >
                {LOOKBACK_CHOICES.map((value) => (
                  <option key={value} value={value}>
                    Last {value} days
                  </option>
                ))}
              </select>
            </Field>
          )}
        </div>

        {/*
          The gate, before the action rather than after it. A date the platform has
          not analysed has no measured movement to apportion, so the button that
          would apportion it is not offered -- and the reason is on screen instead
          of arriving as an error once the request has already been made.
        */}
        <div className="mt-4">
          <RunStatus gate={gate.data} loading={gate.loading} />
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-ink-800/70 pt-4">
          <button
            type="button"
            className="btn btn-primary"
            disabled={!kpiId || action.pending || noDimensions || blocked}
            onClick={() => {
              if (mode === 'movement') void runContribution([], null)
              else void runManual()
            }}
          >
            {action.pending ? 'Analysing…' : mode === 'movement' ? 'Explain the movement' : 'Run'}
          </button>
          {action.pending && <Spinner label="Reading the KPI's registered source…" />}
          {/*
            Which breakdown the run will use, named before it runs and in the same
            words the result carries afterwards ("By region"), so the answer is not
            the first place the reader learns what was split. It gives way to the
            result's own heading once there is one, rather than repeating it.
          */}
          {!action.pending && mode === 'movement' && defaultDimension && !contribution && (
            <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
              <span className="chip">By {defaultDimension.name}</span>
              <span>first, then down this KPI's own hierarchy.</span>
            </span>
          )}
        </div>

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
              {contract ? formatKpiName(contract.name) : 'This KPI'} has no approved dimension to break down by. A
              breakdown reads a dimension registered with the KPI and marked allowed; the platform
              does not choose a column on its own.
            </Alert>
          </div>
        )}
        {gate.error && (
          <div className="mt-3">
            <Alert tone="error">{gate.error}</Alert>
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
                title={blocked ? 'Nothing to investigate on this date' : 'Nothing analysed yet'}
                description={
                  blocked
                    ? 'Select a date that has already been analysed.'
                    : 'Choose a KPI and a date with an approved run.'
                }
              />
            </Panel>
          )
        ))}

      {/*
        The dimension's own values, measured, each investigable on its own. Offered
        whenever the date has been analysed -- so "no entity named" is a starting
        point rather than a dead end -- and never a list written into the client.
      */}
      {mode === 'manual' && !blocked && gate.data && (
        <EntityPicker
          gate={gate.data}
          selected={manualEntity || null}
          unit={contract?.unit ?? null}
          currency={contract?.currency ?? null}
          onSelect={investigateEntity}
          onClear={clearEntity}
          pending={action.pending}
        />
      )}

      {/*
        The manual result, in the page. Rendered inline so the trend, the verdict and
        the day-by-day values are readable alongside the picker that chose the
        entity; the dialog below shows the identical content full-width for a reader
        who wants it larger, and nothing is computed twice to do it.
      */}
      {mode === 'manual' && manual && !manualModalOpen && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.16em] text-slate-500">
              <span>{manual.mode === 'contribution' ? 'Contribution ranking' : 'Entity analysis'}</span>
              <span className="text-slate-400">•</span>
              <span>{manual.result.dimension}</span>
            </div>
            <button
              type="button"
              className="btn btn-xs btn-ghost"
              onClick={() => setManualModalOpen(true)}
            >
              Expand
            </button>
          </div>
          {manual.mode === 'contribution' ? (
            <ContributionView
              response={manual}
              nextDimension={nextDimension}
              onDrill={drill}
              onBreadcrumb={climb}
            />
          ) : (
            <EntityView result={manual.result} evidence={manual.evidence} />
          )}
        </div>
      )}

      {mode === 'manual' && blocked && !action.pending && (
        <Panel>
          <EmptyState
            title="Nothing to investigate on this date"
            description="No agent run has been completed for this date, so there is no entity to trend and no dimension to rank. Run the KPI analysis for this date first."
          />
        </Panel>
      )}

      {mode === 'manual' && !manual && !blocked && !action.pending && gate.data && (
        <Panel>
          <EmptyState
            title="Nothing analysed yet"
            description={
              entityOptions.length > 0
                ? 'Pick an entity above and analyse it, or rank the dimension’s contributors.'
                : 'Run the analysis to rank this dimension’s contributors.'
            }
          />
        </Panel>
      )}

      {/*
        The two surfaces that close an investigation, and the reason they sit
        together at the foot of the page: an explanation is what the platform can
        say about the node on screen, and a finding is what the reader concluded
        about it. Reading the first and writing the second is one gesture.

        Both are anchored to `anchor` — the node actually on screen — so neither can
        be filed against, or written about, coordinates the reader was not reading.
        They appear only once something has been analysed, because both endpoints
        need the stored run that the analysis proves exists.
      */}
      {anchor && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Panel
            title="What this means"
            actions={
              <ExplainButton
                label="Explain this level"
                pending={explain.pending}
                onClick={() => void requestExplanation()}
                title="Assembled from this KPI's stored evaluation, its recorded breakdown and approved documents you may see"
              />
            }
          >
            <ExplanationCard
              explanation={explanation}
              error={explain.error}
              pending={explain.pending}
              emptyHint={
                anchor.entity
                  ? `Ask for an explanation of ${anchor.entity} on this date, in the platform's own words.`
                  : `Ask for an explanation of the ${anchor.dimension ?? 'current'} breakdown on this date, in the platform's own words.`
              }
            />
          </Panel>

          <FindingsPanel companyId={companyId ?? ''} anchor={anchor} />
        </div>
      )}

      {/*
        And what to consider doing about it.

        The same panel the Result page renders, against the same stored run, so the
        advice a reader is given in an investigation is the advice they would be
        given anywhere else about that movement — there is one recommendation set
        per result, derived by the server from the breakdown that now exists.

        No breakdown runner is passed: this page *is* the breakdown runner, so the
        "sharpen with a breakdown" button would only ask the reader to do again what
        they are already doing. `recommendationToken` covers the same ground from the
        other side — every drill re-asks, so the advice on screen always describes
        the level on screen rather than the one the reader arrived from.
      */}
      {anchorRunId && companyId && (
        <Recommendations
          companyId={companyId}
          runId={anchorRunId}
          enabled={can('analytics.read')}
          refreshToken={recommendationToken}
        />
      )}

      <Modal
        open={manualModalOpen && Boolean(manual)}
        onClose={() => setManualModalOpen(false)}
        title={manual ? (manual.mode === 'contribution' ? 'Contribution detail' : `${manual.result.dimension}: ${manual.result.entity ?? 'Result'}`) : 'Detail'}
        width="max-w-5xl"
      >
        {manual &&
          (manual.mode === 'contribution' ? (
            <ContributionView
              response={manual}
              nextDimension={nextDimension}
              onDrill={drill}
              onBreadcrumb={climb}
            />
          ) : (
            <EntityView result={manual.result} evidence={manual.evidence} />
          ))}
      </Modal>
    </div>
  )
}
