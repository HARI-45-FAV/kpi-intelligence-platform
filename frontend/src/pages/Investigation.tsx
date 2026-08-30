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
import { api } from '../api/client'
import type {
  ContributionResponse,
  ContributionResult,
  Contributor,
  EntityProfileResult,
  EntityStep,
  InvestigationDimension,
  InvestigationDimensionsResponse,
  InvestigationEntitiesResponse,
  InvestigationEntity,
  KpiContract,
  ManualAnalysisResponse,
} from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatCompact, formatCurrency, formatDate, formatNumber } from '../components/format'
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

  const body = (
    <>
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
          <span className="font-medium text-accent">Break down by {drillLabel} →</span>
        )}
      </div>
    </>
  )

  if (onDrill && drillLabel) {
    return (
      <button
        type="button"
        className="row-link block"
        onClick={onDrill}
        title={`Break down ${contributor.label} by ${drillLabel}`}
      >
        {body}
      </button>
    )
  }
  return <div className="border-b border-ink-800/80 px-4 py-3 last:border-0">{body}</div>
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
    <Panel title="KPI movement">
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
          label="Variance"
          value={signedValue(result.movement, result.unit, result.currency)}
          tone={result.status === 'ABNORMAL' ? 'bad' : 'default'}
          hint="The whole that the parts below are measured against."
        />
        <Metric
          label="Variance %"
          value={signedPct(result.movement_pct)}
          tone={result.status === 'ABNORMAL' ? 'bad' : 'default'}
          hint="Against what was expected."
        />
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
        In-depth results
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
          <span className="text-[11px] text-slate-500">
            By {result.dimension} · {contributors.length} of {result.ranked_count} shown
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
  const peak = result.points.reduce(
    (max, point) => Math.max(max, Math.abs(point.value ?? 0)),
    0,
  )
  const judged = result.status !== null
  const abnormal = result.status === 'ABNORMAL'
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
        title={`${result.dimension}: ${result.entity}`}
        actions={
          <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
            {result.kpi}
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
                hint={`How much of ${result.kpi} on this date this ${result.dimension} accounts for. A size, not a cause.`}
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

      <Panel title={`Recent trend · ${result.observed_days} day(s)`} bodyClassName="p-0">
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
 */
function RunStatus({ gate, loading }: { gate: InvestigationEntitiesResponse | null; loading: boolean }) {
  if (loading) {
    return <Spinner label="Checking whether this date has been analysed…" />
  }
  if (!gate) return null
  if (!gate.run_available) {
    return (
      <Alert tone="warn">
        <span className="font-medium">{gate.message}</span>
      </Alert>
    )
  }
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-slate-500">
      <span>
        Analysis: <span className="font-medium text-slate-300">{gate.run_state ?? 'Recorded'}</span>
      </span>
      <span className="flex items-center gap-1.5">
        KPI status: <StatusBadge status={gate.kpi_status ?? undefined} />
      </span>
      {gate.kpi_status && (
        <span className="text-slate-500">{STATUS_MEANING[gate.kpi_status] ?? ''}</span>
      )}
    </div>
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
  const leader = gate.entities.reduce(
    (max, item) => Math.max(max, Math.abs(item.value ?? 0)),
    0,
  )

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
            {gate.entities.length} shown · read from this KPI's own source
          </span>
        )
      }
    >
      {gate.entities.length === 0 ? (
        <EmptyState
          title="Nothing to choose from"
          description={`This KPI has no ${gate.dimension ?? 'dimension'} values on ${formatDate(gate.target_date)} that are within your access scope.`}
        />
      ) : (
        <div>
          {gate.entities.map((item) => {
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
  const [manualModalOpen, setManualModalOpen] = useState(false)

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

  const dimensionList: InvestigationDimension[] = dimensions.data?.dimensions ?? []

  const currentDimension = useMemo(() => {
    const active = contribution?.result.dimension ?? dimension
    if (!active) return dimensionList.find((item) => item.is_default) ?? dimensionList[0] ?? null
    return dimensionList.find((item) => item.name === active) ?? null
  }, [contribution, dimension, dimensionList])

  const noDimensions = !dimensions.loading && dimensionList.length === 0
  const defaultDimension = dimensionList.find((item) => item.is_default) ?? dimensionList[0] ?? null

  const nextDimension = currentDimension?.hierarchy[0] ?? null

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

  /** Choose one of the measured entities and analyse that entity alone (§13). */
  const investigateEntity = useCallback(
    (item: InvestigationEntity) => {
      setManualEntity(item.entity)
      setManualModalOpen(true)
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
          <div className="segmented-switch">
            {(['movement', 'manual'] as const).map((option) => (
              <button
                key={option}
                type="button"
                className={`segmented-option ${mode === option ? 'segmented-option-active' : ''}`}
                onClick={() => setMode(option)}
              >
                {option === 'movement' ? 'From a movement' : 'Manual analysis'}
              </button>
            ))}
          </div>
        }
      >
        <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-slate-500">
          <span>{mode === 'movement' ? 'Movement view' : 'Manual view'}</span>
          <span className="text-slate-600">•</span>
          <span>{mode === 'movement' ? 'Stored result' : 'Direct review'}</span>
        </div>

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
            <Field label="Dimension" hint="Approved only">
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

          <Field label="Top contributors" hint="Results shown">
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

        {/*
          The gate, before the action rather than after it. A date the platform has
          not analysed has no measured movement to apportion, so the button that
          would apportion it is not offered -- and the reason is on screen instead
          of arriving as an error once the request has already been made.
        */}
        <div className="mt-4">
          <RunStatus gate={gate.data} loading={gate.loading} />
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn btn-primary"
            disabled={!kpiId || action.pending || noDimensions || blocked}
            onClick={() => {
              if (mode === 'movement') void runContribution([], null)
              else {
                setManualModalOpen(true)
                void runManual()
              }
            }}
          >
            {action.pending ? 'Analysing…' : mode === 'movement' ? 'Explain' : 'Run'}
          </button>
          {action.pending && <Spinner label="Reading the KPI's registered source…" />}
        </div>
        {defaultDimension && mode === 'movement' && !contribution && (
          <div className="mt-2 text-[11px] uppercase tracking-[0.16em] text-slate-500">
            Default: <span className="font-medium text-slate-200">{defaultDimension.name}</span>
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

      {mode === 'manual' && manual && !manualModalOpen && (
        <Panel
          title="Latest result"
          actions={
            <button type="button" className="btn btn-xs btn-ghost" onClick={() => setManualModalOpen(true)}>
              View details
            </button>
          }
        >
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span>{manual.result.dimension}</span>
            <span className="text-slate-600">•</span>
            <span>{manual.mode === 'contribution' ? 'Contribution view' : 'Entity view'}</span>
          </div>
        </Panel>
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
