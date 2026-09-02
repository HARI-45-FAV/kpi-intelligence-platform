/**
 * The evidence-to-action surface: what a stored result suggests someone consider
 * doing, and everything a reader needs in order to disagree with it.
 *
 * Every sentence on this panel was written by the server from stored rows. There
 * is no recommendation text in this file — no finding, no action, no owner, no
 * monitoring metric, no headline — because a screen that can author advice is a
 * screen that can author advice the evidence does not support. What this file
 * decides is ordering, emphasis and disclosure: which of the eight parts is read
 * first, what stays visible, and what sits behind "Why this recommendation?".
 *
 * Three properties the layout has to preserve, each corresponding to a way advice
 * beside a governed figure goes wrong:
 *
 *  - **A share is not a cause.** The causation note travels with every card and is
 *    never behind a disclosure, because the sentence a reader is most likely to
 *    quote out of context is the finding directly above it.
 *  - **The advice is only as sharp as the evidence.** Before a breakdown exists the
 *    server aims at the KPI and says so; the panel turns that one flag into the
 *    button that sharpens it, rather than inventing an area to name.
 *  - **Two readers, one answer.** The executive view narrows the same recommendation
 *    set — it never renders different content. Both views read from the same
 *    payload, so no toggle can produce a second conclusion.
 *
 * Feedback is the only write here. It records whether the advice was useful and how
 * far the reader's own review got; it cannot touch a verdict, a share or a figure,
 * and the server validates the recommendation key against the set it just derived,
 * so a response can never attach to advice the platform did not give.
 */

import { useMemo, useState, type ReactNode } from 'react'
import { api } from '../api/client'
import type {
  Recommendation,
  RecommendationFeedback,
  RecommendationFeedbackResponse,
  RecommendationSet,
  RecommendationsResponse,
} from '../api/types'
import { formatCompact, formatCurrency, formatNumber, titleCase } from './format'
import { Alert, Panel, Spinner } from './ui'
import { useAction, useResource } from './useResource'

/* --------------------------------------------------------------------- tones */

/**
 * Priority is the loudest thing on a card, so it carries the strongest colour.
 *
 * Rose for the one thing to do first, amber for the one that needs validating,
 * sky for the preventive card — which is advice about the parts that were *not*
 * flagged and must not look like an alarm.
 */
const PRIORITY_TONES: Record<string, string> = {
  HIGH_PRIORITY: 'border-rose-200 bg-rose-50/80 text-rose-700',
  MEDIUM_PRIORITY: 'border-amber-200 bg-amber-50/80 text-amber-700',
  PREVENTIVE_ACTION: 'border-sky-200 bg-sky-50/80 text-sky-700',
}

const IMPACT_TONES: Record<string, string> = {
  HIGH: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  MEDIUM: 'border-sky-200 bg-sky-50/80 text-sky-700',
  LOW: 'border-slate-200 bg-white/70 text-slate-500',
}

/** The same three tones the explanation card uses, so one scale reads one way. */
const CONFIDENCE_TONES: Record<string, string> = {
  HIGH: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  MEDIUM: 'border-sky-200 bg-sky-50/80 text-sky-700',
  LOW: 'border-slate-200 bg-white/70 text-slate-500',
}

/**
 * The stance banner's tone.
 *
 * `NO_ACTION` is emerald because nothing is wrong, and `EVIDENCE_FIRST` is
 * deliberately *not* rose: "we cannot judge this yet" is not a problem with the
 * business, and colouring it like one would push someone into acting on a date
 * the engine declined to judge.
 */
const STANCE_TONES: Record<string, string> = {
  ACTION: 'border-rose-200 bg-rose-50/70',
  MONITOR: 'border-emerald-200 bg-emerald-50/70',
  NO_ACTION: 'border-emerald-200 bg-emerald-50/70',
  EVIDENCE_FIRST: 'border-amber-200 bg-amber-50/70',
  UNREADABLE: 'border-slate-200 bg-white/70',
}

/**
 * Glyph and wording for a response value.
 *
 * Presentation of an enum the server owns, not content: the option list itself
 * arrives in the payload, and anything not mapped here still renders — title-cased
 * — rather than disappearing from a control.
 */
const USEFULNESS_LABELS: Record<string, string> = {
  USEFUL: '👍 Useful',
  NOT_USEFUL: '👎 Not useful',
  NEEDS_REVIEW: '⚠ Needs review',
}

const ACTION_STATUS_LABELS: Record<string, string> = {
  NOT_STARTED: '○ Not started',
  IN_REVIEW: '◐ In review',
  ACTION_TAKEN: '● Action taken',
}

const LEVER_SOURCE_LABELS: Record<string, string> = {
  KPI_DRIVER: 'Registered driver',
  KPI_FAMILY_DEFAULT: 'Default for this KPI type',
}

/* ------------------------------------------------------------------- helpers */

/** A measurement in the KPI's own unit. The unit comes from the run, never guessed. */
function figure(
  value: number | null | undefined,
  unit?: string | null,
  currency?: string | null,
): string {
  if (value === null || value === undefined) return '—'
  if (currency) return formatCurrency(value, currency, true)
  if (unit === 'currency') return formatCurrency(value, 'INR', true)
  return formatCompact(value)
}

function share(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${Math.abs(value).toFixed(1)}%`
}

function Tag({ tone, children }: { tone: string; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tone}`}
    >
      {children}
    </span>
  )
}

function Labelled({
  label,
  children,
  className = '',
}: {
  label: string
  children: ReactNode
  className?: string
}) {
  return (
    <div className={className}>
      <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
        {label}
      </div>
      <div className="mt-1 text-[13px] leading-relaxed text-slate-800">{children}</div>
    </div>
  )
}

/* --------------------------------------------------------- the figures behind */

/**
 * The figures the advice rests on, echoed beside it.
 *
 * Not decoration: the reason a recommendation can be argued with is that the
 * movement, the expectation and the contributing share are on the same screen as
 * the suggested action. All four come from the run's own stored columns.
 */
function EvidenceStrip({ set }: { set: RecommendationSet }) {
  const summary = set.evidence_summary
  const unit = summary.unit
  const currency = summary.currency

  return (
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      <div className="surface-card px-3 py-2.5">
        <div className="stat-label">Measured</div>
        <div className="mt-1 text-[15px] font-semibold tabular-nums text-slate-800">
          {figure(summary.actual, unit, currency)}
        </div>
        <div className="mt-0.5 text-[11px] text-slate-500">
          against {figure(summary.expected, unit, currency)} expected
        </div>
      </div>
      <div className="surface-card px-3 py-2.5">
        <div className="stat-label">Movement</div>
        <div className="mt-1 text-[15px] font-semibold tabular-nums text-slate-800">
          {summary.deviation_absolute === null || summary.deviation_absolute === undefined
            ? '—'
            : `${summary.deviation_absolute > 0 ? '+' : '−'}${figure(
                Math.abs(summary.deviation_absolute),
                unit,
                currency,
              )}`}
        </div>
        <div className="mt-0.5 text-[11px] text-slate-500">
          {summary.deviation_pct === null || summary.deviation_pct === undefined
            ? 'Percentage not recorded'
            : `${summary.deviation_pct > 0 ? '+' : ''}${summary.deviation_pct.toFixed(1)}% versus expectation`}
        </div>
      </div>
      <div className="surface-card px-3 py-2.5">
        <div className="stat-label">Compared against</div>
        <div className="mt-1 text-[13px] font-semibold text-slate-800">
          {summary.comparison ? titleCase(summary.comparison) : 'Not recorded'}
        </div>
        <div className="mt-0.5 text-[11px] text-slate-500">
          {formatNumber(summary.reference_count)} comparable period
          {summary.reference_count === 1 ? '' : 's'}
        </div>
      </div>
      <div className="surface-card px-3 py-2.5">
        <div className="stat-label">Largest contributing part</div>
        <div className="mt-1 text-[13px] font-semibold text-slate-800">
          {summary.top_contributor ?? 'Not identified yet'}
        </div>
        <div className="mt-0.5 text-[11px] text-slate-500">
          {summary.top_contributor === null || summary.top_contributor === undefined
            ? 'No breakdown of this movement is stored'
            : summary.top_contributor_share_pct === null ||
                summary.top_contributor_share_pct === undefined
              ? `Ranked by size within ${summary.breakdown_dimension ?? 'the stored breakdown'}`
              : `${share(summary.top_contributor_share_pct)} of the observed movement`}
        </div>
      </div>
    </div>
  )
}

/* ----------------------------------------------------------- executive view */

/**
 * The five lines an executive reads.
 *
 * The same recommendation set, narrowed — every value here is a field the server
 * already computed for the detailed cards, so the two views cannot disagree. What
 * is left out is the evidence trail, not the qualifications: confidence, potential
 * impact and the causation note all survive the narrowing, because they are what
 * keep a one-line answer honest.
 */
function ExecutiveView({ set }: { set: RecommendationSet }) {
  const view = set.executive
  return (
    <div className="space-y-3">
      <div className="grid gap-3 lg:grid-cols-2">
        <Labelled label="What happened" className="surface-card px-3.5 py-3">
          {view.what_happened}
        </Labelled>
        <Labelled label="Largest contributing part" className="surface-card px-3.5 py-3">
          {view.largest_contributor ? (
            <>
              <span className="font-semibold">{view.largest_contributor}</span>
              {view.largest_contributor_share !== null &&
                view.largest_contributor_share !== undefined && (
                  <span className="text-slate-600">
                    {' '}
                    — {share(view.largest_contributor_share)} of the observed movement
                  </span>
                )}
            </>
          ) : (
            <span className="text-slate-500">
              No breakdown of this movement is stored, so no part of the business is named.
            </span>
          )}
        </Labelled>
      </div>

      {view.top_action && (
        <div className="surface-card px-3.5 py-3">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
            Top recommended action
          </div>
          <p className="mt-1 text-[13.5px] leading-relaxed text-slate-800">{view.top_action}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {view.owner && <span className="chip">Owner · {view.owner}</span>}
            {view.impact && <span className="chip">{view.impact}</span>}
            <Tag tone={CONFIDENCE_TONES[view.confidence] ?? CONFIDENCE_TONES.LOW}>
              {view.confidence.replace(/_/g, ' ')} confidence
            </Tag>
          </div>
        </div>
      )}

      <p className="text-[11px] leading-relaxed text-slate-500">{set.causation_note}</p>
    </div>
  )
}

/* ------------------------------------------------------------------ feedback */

/**
 * One reader's response to one recommendation.
 *
 * Two controls and an optional note, because the useful question ("was this
 * advice any good?") and the operational one ("did anyone act on it?") are
 * different answers and collapsing them loses both. The option values come from
 * the server's own enums via the payload, so this control cannot offer a response
 * the writer would reject.
 */
function FeedbackBar({
  recommendationKey,
  options,
  existing,
  pending,
  onSubmit,
}: {
  recommendationKey: string
  options: { usefulness: string[]; action_status: string[] }
  existing: RecommendationFeedback | null
  pending: boolean
  onSubmit: (payload: {
    recommendation_key: string
    usefulness: string
    action_status: string
    comment: string | null
  }) => Promise<void>
}) {
  const [usefulness, setUsefulness] = useState(existing?.usefulness ?? '')
  const [status, setStatus] = useState(existing?.action_status ?? options.action_status[0] ?? '')
  const [comment, setComment] = useState(existing?.comment ?? '')
  const [noteOpen, setNoteOpen] = useState(Boolean(existing?.comment))

  return (
    <div className="rounded-xl border border-slate-200 bg-white/55 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
          Was this recommendation useful?
        </div>
        <div className="segmented-switch">
          {options.usefulness.map((value) => (
            <button
              key={value}
              type="button"
              data-bare
              className={`segmented-option ${
                usefulness === value ? 'segmented-option-active' : ''
              }`}
              aria-pressed={usefulness === value}
              onClick={() => setUsefulness(value)}
            >
              {USEFULNESS_LABELS[value] ?? titleCase(value)}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
          Where this review stands
        </div>
        <div className="segmented-switch">
          {options.action_status.map((value) => (
            <button
              key={value}
              type="button"
              data-bare
              className={`segmented-option ${status === value ? 'segmented-option-active' : ''}`}
              aria-pressed={status === value}
              onClick={() => setStatus(value)}
            >
              {ACTION_STATUS_LABELS[value] ?? titleCase(value)}
            </button>
          ))}
        </div>
      </div>

      {noteOpen && (
        <textarea
          className="field mt-2 min-h-[3.5rem]"
          placeholder="Optional — what you checked, or why this advice missed."
          value={comment}
          onChange={(event) => setComment(event.target.value)}
        />
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn btn-xs btn-primary"
          disabled={!usefulness || pending}
          onClick={() =>
            void onSubmit({
              recommendation_key: recommendationKey,
              usefulness,
              action_status: status,
              comment: comment.trim() || null,
            })
          }
        >
          {pending ? 'Recording…' : existing ? 'Update response' : 'Record response'}
        </button>
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={() => setNoteOpen((open) => !open)}
        >
          {noteOpen ? 'Hide the note' : 'Add a note'}
        </button>
        {existing && (
          <span className="text-[11px] text-slate-500">
            Last recorded by {existing.submitted_by_email ?? 'a colleague'} —{' '}
            {USEFULNESS_LABELS[existing.usefulness] ?? titleCase(existing.usefulness)} ·{' '}
            {ACTION_STATUS_LABELS[existing.action_status] ?? titleCase(existing.action_status)}
          </span>
        )}
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- one card */

/**
 * The eight parts of one recommendation, in the order a reader needs them.
 *
 * Evidence first and action fourth, deliberately: the finding is what the advice
 * rests on, and a card that opened with "do this" would invite the action to be
 * read as an instruction from the platform rather than a suggestion derived from a
 * share of a movement. The only thing behind a disclosure is the trail — verdict,
 * deviation, comparison basis, contributor, share, confidence — because it is the
 * detail a reader reaches for second, not the qualification they need first.
 */
function RecommendationCard({
  item,
  index,
  mayRespond,
  options,
  existing,
  pending,
  onSubmit,
}: {
  item: Recommendation
  index: number
  mayRespond: boolean
  options: { usefulness: string[]; action_status: string[] }
  existing: RecommendationFeedback | null
  pending: boolean
  onSubmit: (payload: {
    recommendation_key: string
    usefulness: string
    action_status: string
    comment: string | null
  }) => Promise<void>
}) {
  const [whyOpen, setWhyOpen] = useState(false)
  const target = item.target_area

  return (
    <article className="surface-card p-3.5">
      {/* ---- 1. the evidence, and what the platform is calling it */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="grid h-6 w-6 place-items-center rounded-lg border border-white/90 bg-white/70 text-[11px] font-semibold text-slate-500">
            {index + 1}
          </span>
          <Tag tone={PRIORITY_TONES[item.priority] ?? PRIORITY_TONES.MEDIUM_PRIORITY}>
            {item.priority_label}
          </Tag>
          <Tag tone={IMPACT_TONES[item.impact.level] ?? IMPACT_TONES.LOW}>{item.impact.label}</Tag>
        </div>
        <Tag tone={CONFIDENCE_TONES[item.confidence.level] ?? CONFIDENCE_TONES.LOW}>
          {item.confidence.level.replace(/_/g, ' ')} confidence
        </Tag>
      </div>

      <p className="mt-2.5 text-[14px] font-medium leading-relaxed text-slate-800">
        {item.finding}
      </p>

      {/* ---- 2. the target area, as the drill-down that found it */}
      {target && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
            Target area
          </span>
          {target.chain.map((step, stepIndex) => (
            <span key={`${step}-${stepIndex}`} className="flex items-center gap-1.5">
              {stepIndex > 0 && <span className="text-slate-500">›</span>}
              <span className="chip font-medium">{step}</span>
            </span>
          ))}
          <span className="chip">{target.entity_type}</span>
          {target.share_pct !== null && (
            <span className="chip tabular-nums">{share(target.share_pct)} of the movement</span>
          )}
        </div>
      )}

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        {/* ---- 3. the lever, with the honest label on where it came from */}
        <Labelled label="Relevant business lever to review">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold">{item.lever.label}</span>
            <span className="chip">
              {LEVER_SOURCE_LABELS[item.lever.source] ?? titleCase(item.lever.source)}
            </span>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{item.lever.note}</p>
        </Labelled>

        {/* ---- 5, 6 and 7. what it could be worth, whose call it is, how much
                weight it carries. Confidence gets its own slot rather than living
                under the owner, where its sentence read as if it described them. */}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
          <Labelled label="Potential impact">
            <span className="font-semibold">{item.impact.label}</span>
            <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{item.impact.basis}</p>
          </Labelled>
          <Labelled label="Recommended owner">
            <span className="font-semibold">{item.owner}</span>
          </Labelled>
          <Labelled label="Confidence">
            <span className="font-semibold">{item.confidence.level.replace(/_/g, ' ')}</span>
            <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
              {item.confidence.meaning}
            </p>
          </Labelled>
        </div>
      </div>

      {/* ---- 4. the action itself */}
      <div className="mt-3 rounded-xl border border-sky-200 bg-sky-50/50 p-3">
        <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-sky-700">
          Recommended action
        </div>
        <p className="mt-1 text-[13.5px] leading-relaxed text-slate-800">{item.action}</p>
      </div>

      {/* ---- 8. what to watch to find out whether it worked */}
      {item.monitoring.metrics.length > 0 && (
        <div className="mt-3 rounded-xl border border-slate-200 bg-white/55 p-3">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
            What to monitor next
          </div>
          <ul className="mt-1.5 grid gap-1 sm:grid-cols-2">
            {item.monitoring.metrics.map((metric) => (
              <li key={metric} className="flex items-start gap-1.5 text-[12.5px] text-slate-700">
                <span className="text-emerald-600">✓</span>
                <span>{metric}</span>
              </li>
            ))}
          </ul>
          <div className="mt-2 text-[11px] text-slate-500">
            Review window · {item.monitoring.window}
          </div>
        </div>
      )}

      {/* ---- the trail, and the one sentence that never hides behind it */}
      <div className="mt-3 space-y-2">
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={() => setWhyOpen((open) => !open)}
        >
          {whyOpen ? 'Hide why this recommendation' : 'Why this recommendation?'}
        </button>
        {whyOpen && (
          <ul className="space-y-1 rounded-xl border border-slate-200 bg-white/55 p-3 text-[12px] leading-relaxed text-slate-600">
            {item.why.map((line) => (
              <li key={line}>· {line}</li>
            ))}
          </ul>
        )}
        <p className="text-[11px] leading-relaxed text-slate-500">{item.causation_note}</p>
      </div>

      {mayRespond && (
        <div className="mt-3">
          <FeedbackBar
            recommendationKey={item.key}
            options={options}
            existing={existing}
            pending={pending}
            onSubmit={onSubmit}
          />
        </div>
      )}
    </article>
  )
}

/* -------------------------------------------------------------------- panel */

export default function Recommendations({
  companyId,
  runId,
  enabled = true,
  refreshToken = 0,
  onRunBreakdown,
  breakdownPending = false,
}: {
  companyId: string
  runId: string
  /** False for a reader without `analytics.read`; the panel then asks for nothing. */
  enabled?: boolean
  /**
   * Bumped by the page when a breakdown is stored.
   *
   * The recommendation set is derived on read from whatever breakdown exists, so a
   * fresh contribution changes the answer — from "no area is named" to a named
   * region — and the panel has to ask again to show it.
   */
  refreshToken?: number
  /**
   * Runs the page's own breakdown, reused rather than reimplemented.
   *
   * Absent for a reader who may not break a movement down, in which case the panel
   * states the limitation instead of offering a button that would 403.
   */
  onRunBreakdown?: () => void | Promise<void>
  breakdownPending?: boolean
}) {
  const [view, setView] = useState<'ANALYST' | 'EXECUTIVE'>('ANALYST')

  const recommendations = useResource<RecommendationsResponse>(
    () => api.get(`/companies/${companyId}/detection-runs/${runId}/recommendations`),
    [companyId, runId, refreshToken],
    { enabled: Boolean(companyId && runId) && enabled },
  )

  const respond = useAction()
  const [respondingTo, setRespondingTo] = useState<string | null>(null)

  const data = recommendations.data
  const set = data?.result ?? null

  /** The newest response per recommendation, so a card shows one state, not a history. */
  const feedbackByKey = useMemo(() => {
    const map = new Map<string, RecommendationFeedback>()
    for (const row of data?.feedback ?? []) {
      if (!map.has(row.recommendation_key)) map.set(row.recommendation_key, row)
    }
    return map
  }, [data?.feedback])

  const submitFeedback = async (payload: {
    recommendation_key: string
    usefulness: string
    action_status: string
    comment: string | null
  }) => {
    setRespondingTo(payload.recommendation_key)
    const saved = await respond.run(
      () =>
        api.post<RecommendationFeedbackResponse>(
          `/companies/${companyId}/detection-runs/${runId}/recommendation-feedback`,
          payload,
        ),
      'Response recorded.',
    )
    setRespondingTo(null)
    if (saved) void recommendations.reload()
  }

  if (!enabled) return null

  return (
    <Panel
      title="Recommended next actions"
      actions={
        set && set.recommendations.length > 0 ? (
          <div className="segmented-switch">
            {(['ANALYST', 'EXECUTIVE'] as const).map((option) => (
              <button
                key={option}
                type="button"
                data-bare
                className={`segmented-option ${view === option ? 'segmented-option-active' : ''}`}
                aria-pressed={view === option}
                onClick={() => setView(option)}
              >
                {option === 'ANALYST' ? 'Analyst view' : 'Executive view'}
              </button>
            ))}
          </div>
        ) : undefined
      }
    >
      {recommendations.loading && !set ? (
        <Spinner label="Reading the stored evidence for this result…" />
      ) : recommendations.error ? (
        <Alert tone="error">
          Unable to derive recommendations for this result. ({recommendations.error})
        </Alert>
      ) : !set ? (
        <Alert tone="info">No recommendation set was returned for this result.</Alert>
      ) : (
        <div className="space-y-4">
          {/* ---- the stance: what the platform is willing to say at all */}
          <div className={`rounded-xl border p-3.5 ${STANCE_TONES[set.stance] ?? STANCE_TONES.UNREADABLE}`}>
            <div className="flex flex-wrap items-center gap-2">
              <Tag tone={CONFIDENCE_TONES[set.confidence.level] ?? CONFIDENCE_TONES.LOW}>
                {set.confidence.level.replace(/_/g, ' ')} confidence
              </Tag>
              <span className="chip">{titleCase(set.verdict)}</span>
              {set.movement_direction !== 'UNKNOWN' && (
                <span className="chip">{titleCase(set.movement_direction)} movement</span>
              )}
            </div>
            <h3 className="mt-2 text-[15px] font-semibold leading-snug text-slate-800">
              {set.headline}
            </h3>
            <p className="mt-1 text-[13px] leading-relaxed text-slate-700">{set.body}</p>
          </div>

          {view === 'EXECUTIVE' ? (
            <ExecutiveView set={set} />
          ) : (
            <>
              <EvidenceStrip set={set} />

              {/* ---- the one flag the page turns into a button */}
              {set.awaiting_breakdown && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-sky-200 bg-sky-50/50 p-3">
                  <p className="text-[12.5px] leading-relaxed text-slate-700">
                    These suggestions are aimed at the KPI as a whole. Breaking the movement down
                    ranks the parts of the business by how much of it each accounts for, and the
                    advice is re-aimed at the largest one.
                  </p>
                  {onRunBreakdown && (
                    <button
                      type="button"
                      className="btn btn-xs btn-primary shrink-0"
                      disabled={breakdownPending}
                      onClick={() => void onRunBreakdown()}
                    >
                      {breakdownPending ? 'Reading the source…' : 'Sharpen with a breakdown'}
                    </button>
                  )}
                </div>
              )}

              {/* ---- the cards */}
              {set.recommendations.length > 0 && (
                <div className="space-y-3">
                  <p className="text-[12px] leading-relaxed text-slate-600">
                    {set.action_preamble}
                  </p>
                  {set.recommendations.map((item, index) => (
                    <RecommendationCard
                      key={item.key}
                      item={item}
                      index={index}
                      mayRespond={Boolean(data?.may_submit_feedback)}
                      options={
                        data?.feedback_options ?? { usefulness: [], action_status: [] }
                      }
                      existing={feedbackByKey.get(item.key) ?? null}
                      pending={respond.pending && respondingTo === item.key}
                      onSubmit={submitFeedback}
                    />
                  ))}
                </div>
              )}

              {/* ---- where no action is offered, what to do instead */}
              {set.next_steps.length > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-3">
                  <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-amber-700">
                    Recommended next steps
                  </div>
                  <ol className="mt-1.5 space-y-1 text-[12.5px] leading-relaxed text-amber-900">
                    {set.next_steps.map((step, index) => (
                      <li key={step}>
                        {index + 1}. {step}
                      </li>
                    ))}
                  </ol>
                </div>
              )}

              {/* ---- routine monitoring, for a result with no cards of its own */}
              {set.recommendations.length === 0 && set.monitoring.metrics.length > 0 && (
                <div className="rounded-xl border border-slate-200 bg-white/55 p-3">
                  <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
                    What to monitor next
                  </div>
                  <ul className="mt-1.5 grid gap-1 sm:grid-cols-2">
                    {set.monitoring.metrics.map((metric) => (
                      <li
                        key={metric}
                        className="flex items-start gap-1.5 text-[12.5px] text-slate-700"
                      >
                        <span className="text-emerald-600">✓</span>
                        <span>{metric}</span>
                      </li>
                    ))}
                  </ul>
                  <div className="mt-2 text-[11px] text-slate-500">
                    Review window · {set.monitoring.window}
                  </div>
                </div>
              )}

              {/* ---- what this layer could not see, at the same weight as the advice */}
              {set.limitations.length > 0 && (
                <div className="rounded-xl border border-slate-200 bg-white/55 p-3">
                  <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
                    What these recommendations rest on, and what they cannot show
                  </div>
                  <ul className="mt-1.5 space-y-1 text-[11.5px] leading-relaxed text-slate-600">
                    {set.limitations.map((line) => (
                      <li key={line}>· {line}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}

          {respond.error && <Alert tone="error">{respond.error}</Alert>}
          {respond.message && <Alert tone="success">{respond.message}</Alert>}
        </div>
      )}
    </Panel>
  )
}
