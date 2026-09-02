/**
 * The governed AI explanation surface.
 *
 * One component, used by both the Result page and the Investigation Center,
 * because the guarantees are the same in both places and duplicating them is how
 * they drift apart. It renders what the server assembled and nothing else: the
 * section order comes from `order`, the headings come from the sections, and
 * there is no client-side fallback prose. If the server sent no section, the
 * screen shows no section — it does not write one.
 *
 * Three labels this card always carries, because each is part of the answer:
 *
 *  - **Who wrote the prose.** `model_written` false means the platform composed
 *    these sentences from the same governed figures a model would have been
 *    given. That is the normal case — the language model is off by default — and
 *    presenting it as an "AI answer" would misdescribe it.
 *  - **How confident, and why.** A level plus the reasons that produced it. Not a
 *    percentage: nothing upstream estimates one, so a number would be invented.
 *  - **What it could not see.** Limitations are shown at the same weight as the
 *    findings, not folded into a tooltip, because an explanation built on thin
 *    evidence and one built on complete evidence must not look alike.
 *
 * Citations are approved documents the server retrieved *after* filtering by the
 * reader's permissions and scopes. A restricted document never reaches this
 * component, so there is no permission check here to forget.
 */

import { useState, type ReactNode } from 'react'
import type { Explanation } from '../api/types'
import { formatDate } from './format'

const CONFIDENCE_TONES: Record<string, string> = {
  HIGH: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  MEDIUM: 'border-sky-200 bg-sky-50/80 text-sky-700',
  LOW: 'border-slate-200 bg-white/70 text-slate-500',
}

function ConfidenceChip({ level }: { level: string }) {
  const tone = CONFIDENCE_TONES[level.toUpperCase()] ?? CONFIDENCE_TONES.LOW
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tone}`}
    >
      {level.replace(/_/g, ' ')} confidence
    </span>
  )
}

/**
 * The button that asks for an explanation.
 *
 * Separate from the card so a page can put the trigger where the reader is
 * looking and the answer where there is room for it.
 *
 * `tone` exists for the dense case. A panel header carries one of these and it is
 * the primary action there; a list of eight movement rows carries eight, where
 * eight primary buttons would shout over the movements themselves. The wording,
 * the pending text and the ✨ are the same either way, because it is the same
 * action asking the same endpoint — only the weight on the page changes.
 */
export function ExplainButton({
  label,
  pending,
  disabled,
  onClick,
  title,
  tone = 'primary',
}: {
  label: string
  pending?: boolean
  disabled?: boolean
  onClick: () => void
  title?: string
  tone?: 'primary' | 'ghost'
}) {
  return (
    <button
      type="button"
      className={`btn btn-xs shrink-0 ${tone === 'ghost' ? 'btn-ghost' : 'btn-primary'}`}
      onClick={onClick}
      disabled={pending || disabled}
      title={title}
    >
      {pending ? 'Reading the evidence…' : `✨ ${label}`}
    </button>
  )
}

/**
 * What the reader is entitled to know about the explanation's own boundaries.
 *
 * Stated on the screen rather than in a design document, because the reader is
 * the person who has to decide how much weight to put on the prose.
 */
export function ExplanationGuardrails() {
  return (
    <p className="text-[11px] leading-relaxed text-slate-500">
      Built only from this KPI&rsquo;s stored evaluation, its recorded breakdown and
      approved documents you are permitted to see. It does not recalculate the KPI,
      read the data source directly, or introduce figures from anywhere else. A
      contribution is a share of the movement, not a proven cause.
    </p>
  )
}

export default function ExplanationCard({
  explanation,
  error,
  pending,
  emptyHint,
  footer,
  showCitations = true,
}: {
  explanation: Explanation | null
  error?: string | null
  pending?: boolean
  /** Shown before anything has been asked for. */
  emptyHint?: ReactNode
  footer?: ReactNode
  /**
   * False when the page gives the retrieved documents a panel of their own.
   *
   * The Investigation Center lists them beside the explanation, under "Related
   * approved documents", so rendering them here as well would show the same
   * governed evidence twice. It suppresses the block, never the filtering: the
   * citations in `explanation` are already permission-filtered by the server, and
   * a page that hides them is not a page that gained access to more.
   */
  showCitations?: boolean
}) {
  const [showFacts, setShowFacts] = useState(false)

  if (error) {
    return (
      <div className="rounded-xl border border-rose-200 bg-rose-50/70 p-3 text-sm text-rose-700">
        {error}
      </div>
    )
  }

  if (pending && !explanation) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white/60 p-3 text-sm text-slate-500">
        Reading the stored evaluation and any approved documents you may see…
      </div>
    )
  }

  if (!explanation) {
    return (
      <div className="space-y-2 rounded-xl border border-dashed border-slate-300 bg-white/50 p-3">
        <p className="text-sm text-slate-600">{emptyHint ?? 'No explanation requested yet.'}</p>
        <ExplanationGuardrails />
      </div>
    )
  }

  const { sections, confidence, limitations, citations, model_written: modelWritten } = explanation

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <ConfidenceChip level={confidence.level} />
        {/* The provenance of the prose, not of the figures — those are the same
            either way, and saying so is the point of the distinction. */}
        <span className="chip">
          {modelWritten
            ? `Narrated by ${explanation.model ?? 'a language model'}`
            : 'Written by the platform from stored evidence'}
        </span>
        <span className="chip">{explanation.scope}</span>
      </div>

      {sections.length === 0 ? (
        <p className="text-sm text-slate-500">
          The server assembled no sections for this subject.
        </p>
      ) : (
        <div className="space-y-3">
          {sections.map((section) => (
            <section key={section.heading}>
              <h4 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-sky-700">
                {section.heading}
              </h4>
              <p className="mt-1 whitespace-pre-line text-sm leading-relaxed text-slate-700">
                {section.body}
              </p>
            </section>
          ))}
        </div>
      )}

      {confidence.reasons.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white/60 p-3">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
            Why this confidence level
          </div>
          <ul className="mt-1.5 space-y-1 text-[12px] leading-relaxed text-slate-600">
            {confidence.reasons.map((reason) => (
              <li key={reason}>· {reason}</li>
            ))}
          </ul>
        </div>
      )}

      {limitations.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-3">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-amber-700">
            What this explanation could not see
          </div>
          <ul className="mt-1.5 space-y-1 text-[12px] leading-relaxed text-amber-900">
            {limitations.map((limitation) => (
              <li key={limitation}>· {limitation}</li>
            ))}
          </ul>
        </div>
      )}

      {showCitations && citations.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white/60 p-3">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
            Supporting evidence ({citations.length})
          </div>
          <ul className="mt-2 space-y-2">
            {citations.map((citation, index) => (
              <li
                key={`${citation.document_id ?? citation.label}-${index}`}
                className="rounded-lg border border-slate-200 bg-white/70 p-2.5"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[13px] font-medium text-slate-800">
                    {citation.title ?? citation.label}
                  </span>
                  {citation.document_status && (
                    <span className="chip">{citation.document_status}</span>
                  )}
                  {citation.document_version !== null &&
                    citation.document_version !== undefined && (
                      <span className="chip">v{citation.document_version}</span>
                    )}
                  {/* The retrieval layer's own word for how this document bears on
                      the date — "effective on", "most recent before" — rather than
                      the screen guessing at relevance. */}
                  {citation.standing && <span className="chip">{citation.standing}</span>}
                </div>
                {(citation.effective_from || citation.effective_to) && (
                  <div className="mt-1 text-[11px] text-slate-500">
                    Effective {formatDate(citation.effective_from)}
                    {citation.effective_to ? ` – ${formatDate(citation.effective_to)}` : ' onwards'}
                  </div>
                )}
                {citation.snippet && (
                  <p className="mt-1.5 text-[12px] leading-relaxed text-slate-600">
                    “{citation.snippet}”
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* The same material the explanation was assembled from, so a reader can
          check the prose against the figures. Present only when the server judged
          the reader entitled to the statistics. */}
      {explanation.facts && (
        <div>
          <button
            type="button"
            className="btn btn-xs btn-ghost"
            onClick={() => setShowFacts((open) => !open)}
          >
            {showFacts ? 'Hide the figures behind this' : 'Check the figures behind this'}
          </button>
          {showFacts && (
            <pre className="mt-2 max-h-64 overflow-auto rounded-xl border border-slate-200 bg-slate-50 p-3 text-[11px] leading-relaxed text-slate-700">
              {JSON.stringify(explanation.facts, null, 2)}
            </pre>
          )}
        </div>
      )}

      <ExplanationGuardrails />
      {footer}
    </div>
  )
}
