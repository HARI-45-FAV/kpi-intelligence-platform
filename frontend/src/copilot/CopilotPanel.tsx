/**
 * The Copilot panel.
 *
 * A question-and-evidence surface, not a chat toy. Every answer is rendered
 * together with the governed material it was built from, because an answer you
 * cannot check is worth less than no answer at all.
 *
 * What this component deliberately does not render:
 *
 *  - **Hidden reasoning.** Only `answer` is displayed. The provider layer strips
 *    reasoning blocks server-side; this file never had a field to leak them from.
 *  - **Credentials or endpoints.** The status payload carries a provider name, a
 *    model name and a *host* — no URL, no key. Connector secrets never enter the
 *    Copilot path at all.
 *  - **Invented numbers.** Figures come from evidence or nowhere. Evidence marked
 *    `is_placeholder` is badged as such, so a dashboard stand-in can never be read
 *    as a measurement.
 *
 * Two states are normal rather than broken, and both are shown as information:
 * no model configured (`LLM_ENABLED=false`), where retrieval still returns the
 * evidence; and no matching evidence, where the honest answer is that the
 * platform does not hold the material.
 */

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { CopilotChatResponse, CopilotEvidence, CopilotStatus } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatKpiName } from '../components/format'
import { Alert, Drawer, Spinner, StatusBadge } from '../components/ui'
import { useAction, useResource } from '../components/useResource'
import { useCopilot } from './CopilotProvider'

/** Evidence source types, in the platform's own words. */
const SOURCE_LABELS: Record<string, string> = {
  kpi_contract: 'KPI contract',
  kpi_validation: 'KPI validation',
  kpi_lineage: 'KPI lineage',
  kpi_dimension: 'KPI dimension',
  kpi_driver: 'KPI driver',
  document: 'Document',
  data_source: 'Data source',
  table_profile: 'Table profile',
  column_profile: 'Column profile',
  relationship: 'Relationship',
  join_safety: 'Join safety',
  reconciliation: 'Reconciliation',
  execution_telemetry: 'Telemetry',
  platform_capability: 'Platform capability',
  detection_run: 'Detection result',
  contribution_analysis: 'Contribution breakdown',
  placeholder_notice: 'Placeholder disclosure',
}

interface Turn {
  id: number
  question: string
  response: CopilotChatResponse
}

/** Starting points, built from what the screen already knows. */
function suggestions(rawName: string | null | undefined): string[] {
  // Screens pass a readable label already; formatting again is a no-op and keeps
  // a raw key out of the prompt if some future screen forgets.
  const kpiName = rawName ? formatKpiName(rawName) : rawName
  if (kpiName) {
    return [
      `How is ${kpiName} calculated, and from which columns?`,
      `Which validation checks did ${kpiName} pass or fail?`,
      `What is the approved definition and lifecycle status of ${kpiName}?`,
    ]
  }
  return [
    'Which KPIs are active, and what does each one measure?',
    'Which tables can be joined safely, and which need aggregation first?',
    'What does our documentation say about the refund policy?',
  ]
}

export default function CopilotPanel() {
  const { companyId, membership } = useAuth()
  const { open, closePanel, seed, clearSeed, screen, page, requestContext } = useCopilot()

  const [question, setQuestion] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const ask = useAction()

  // Consumed once: a launcher that opened the panel with a question pre-filled
  // should not keep re-filling it after the user edits or clears it.
  useEffect(() => {
    if (seed === null) return
    setQuestion(seed)
    clearSeed()
  }, [seed, clearSeed])

  const status = useResource<CopilotStatus>(
    () => api.get(`/companies/${companyId}/copilot/status`),
    [companyId],
    { enabled: Boolean(companyId) && open },
  )

  const available = status.data?.available ?? false
  const hints = useMemo(() => suggestions(screen.label), [screen.label])

  const submit = async () => {
    const message = question.trim()
    if (!message) return
    const response = await ask.run(() =>
      api.post<CopilotChatResponse>(`/companies/${companyId}/copilot/chat`, {
        message,
        // Sent as hints. The server re-resolves every field against this
        // company before any of it reaches the model.
        context: requestContext,
      }),
    )
    if (!response) return
    setTurns((current) => [{ id: current.length + 1, question: message, response }, ...current])
    setQuestion('')
  }

  return (
    <Drawer
      open={open}
      onClose={closePanel}
      title="Copilot"
      subtitle={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span>{membership?.company_name}</span>
          <span className="text-slate-700">·</span>
          <span>Answers from this company's governed knowledge only</span>
        </span>
      }
      width="max-w-2xl"
    >
      <div className="space-y-4">
        <ContextStrip page={page} label={screen.label} screen={screen} />

        {status.loading && !status.data && <Spinner label="Checking Copilot availability…" />}
        {status.error && <Alert tone="warn">Could not read Copilot status. ({status.error})</Alert>}

        {/* ------------------------------------------------------- composer */}
        <div className="copilot-composer">
          <textarea
            className="copilot-input"
            rows={3}
            maxLength={4000}
            placeholder={
              screen.label
                ? `Ask about ${screen.label}, its definition, lineage or validation…`
                : 'Ask about a KPI definition, a document, or your data profiles…'
            }
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void submit()
            }}
          />
          <div className="copilot-composer-actions">
            <span className="copilot-hint">
              {available ? '⌘/Ctrl + Enter to send' : 'Evidence only'}
            </span>
            <button
              className="btn-primary btn-xs"
              onClick={() => void submit()}
              disabled={ask.pending || !question.trim()}
            >
              {ask.pending ? 'Working…' : available ? 'Ask' : 'Retrieve'}
            </button>
          </div>
        </div>

        {ask.error && <Alert onDismiss={ask.reset}>{ask.error}</Alert>}
        {ask.pending && <Spinner label="Retrieving governed evidence…" />}

        {turns.length === 0 && !ask.pending && (
          <div className="space-y-3">
            <div className="rounded-2xl border border-white/80 bg-[rgba(255,255,255,0.22)] p-4 text-center shadow-[0_10px_22px_rgba(38,88,130,0.08)]">
              <div className="text-sm font-medium text-slate-100">Nothing asked yet</div>
              <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
                The Copilot reads KPI contracts, validation results, lineage, documents and data
                profiles for this company. It cannot run SQL or change governance.
              </p>
            </div>
            <div className="grid gap-2 sm:grid-cols-3">
              {hints.map((hint) => (
                <button
                  key={hint}
                  onClick={() => setQuestion(hint)}
                  className="suggestion-pill"
                >
                  {hint}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Newest first: the answer just asked for is the one being read. */}
        {turns.map((turn) => (
          <TurnView key={turn.id} turn={turn} />
        ))}
      </div>
    </Drawer>
  )
}

/* --------------------------------------------------------------- sub-sections */

function ContextStrip({
  page,
  label,
  screen,
}: {
  page: string
  label?: string | null
  screen: { kpiVersion?: number | null; selectedDate?: string | null }
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="chip">Page: {page}</span>
      {label && <span className="chip">KPI: {label}</span>}
      {screen.kpiVersion != null && <span className="chip">v{screen.kpiVersion}</span>}
      {screen.selectedDate && <span className="chip">Date: {screen.selectedDate}</span>}
    </div>
  )
}

function TurnView({ turn }: { turn: Turn }) {
  const { response } = turn
  const [showEvidence, setShowEvidence] = useState(true)
  const notes = response.context.notes ?? []

  return (
    <article className="copilot-turn">
      <header>
        <div className="text-[11px] uppercase tracking-wider text-slate-500">Question</div>
        <p className="question-bubble">{turn.question}</p>
      </header>

      <div>
        <div className="flex items-center justify-between gap-2">
          <div className="text-[11px] uppercase tracking-wider text-slate-500">
            {response.llm_available ? 'Answer' : 'Platform response'}
          </div>
          {response.model && (
            <span className="text-[10px] text-slate-600">{response.model}</span>
          )}
        </div>
        <p className="answer-bubble">{response.answer}</p>
      </div>

      {response.truncated && (
        <Alert tone="warn">
          The assistant reached its tool-call limit for this question, so the answer may rest on
          incomplete evidence.
        </Alert>
      )}

      {notes.length > 0 && (
        <Alert tone="info">
          <ul className="space-y-1 text-xs">
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </Alert>
      )}

      {response.caveats.length > 0 && (
        <Alert tone="warn">
          <ul className="space-y-1 text-xs leading-relaxed">
            {response.caveats.map((caveat) => (
              <li key={caveat}>{caveat}</li>
            ))}
          </ul>
        </Alert>
      )}

      {response.tool_calls.length > 0 && (
        <div className="text-[11px] text-slate-500">
          <span className="uppercase tracking-wider">Governed tools used</span>
          <ul className="mt-1 space-y-0.5">
            {response.tool_calls.map((call, index) => (
              <li key={`${call.tool}-${index}`} className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-slate-400">{call.tool}</span>
                <StatusBadge status={call.ok ? 'PASS' : 'FAIL'} label={call.ok ? 'ok' : 'refused'} />
                {call.error && <span className="text-rose-300">{call.error}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {response.evidence.length > 0 ? (
        <div>
          <button
            onClick={() => setShowEvidence((value) => !value)}
            className="text-[11px] uppercase tracking-wider text-slate-500 hover:text-slate-300"
          >
            {showEvidence ? '▾' : '▸'} Evidence ({response.evidence.length})
          </button>
          {showEvidence && (
            <ul className="mt-2 space-y-2">
              {response.evidence.map((item) => (
                <EvidenceCard key={item.evidence_id} item={item} />
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="text-[11px] text-slate-600">
          No governed evidence matched this question, so nothing was sent to a model.
        </p>
      )}
    </article>
  )
}

function EvidenceCard({ item }: { item: CopilotEvidence }) {
  const provenance = Object.entries(item.metadata ?? {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== '',
  )
  return (
    <li
      className={`rounded-xl border px-3 py-2 shadow-[0_6px_16px_rgba(38,88,130,0.06)] ${
        item.is_placeholder
          ? // The remapped amber pair. `amber-950/20` and `amber-900/70` are not in
            // the light theme's override list, so they rendered as raw dark Tailwind
            // amber — a muddy brown tile on a sky-blue card.
            'border-amber-800 bg-amber-950/50'
          : 'border-ink-700 bg-ink-850'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] text-accent-soft">[{item.evidence_id}]</span>
        <span className="text-xs font-medium text-slate-200">{item.title}</span>
        <span className="chip">{SOURCE_LABELS[item.source_type] ?? item.source_type}</span>
        {item.is_placeholder && <StatusBadge status="WARNING" label="Not a measurement" />}
      </div>
      <p className="mt-1.5 whitespace-pre-wrap text-[11px] leading-relaxed text-slate-400">
        {item.content}
      </p>
      {provenance.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {provenance.map(([key, value]) => (
            <span key={key} className="text-[10px] text-slate-600">
              {key}={String(value)}
            </span>
          ))}
        </div>
      )}
    </li>
  )
}
