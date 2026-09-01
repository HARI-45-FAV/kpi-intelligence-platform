/**
 * Findings: what a person concluded, stored beside the measurement.
 *
 * This is the one place on the investigation surface where a reader writes rather
 * than reads, and it is deliberately narrow. A finding carries a title, an
 * optional note and the state of the *investigation* — OPEN, IN PROGRESS,
 * RESOLVED. It cannot carry a verdict about the KPI: detection decides that, and a
 * control here that appeared to change it would be a second, unaccountable
 * classification system.
 *
 * Three properties worth stating, because each is a decision:
 *
 *  - **The statuses come from the server.** `GET /investigation/findings` returns
 *    the transitions it will accept, so this panel can never offer one the writer
 *    would reject. A list hardcoded here would drift the first time the backend
 *    added a state.
 *  - **The anchor is passed in, never typed.** A finding is written against
 *    whatever the reader is actually looking at — the whole movement, or the node
 *    they drilled to — so nobody can file a conclusion against coordinates they
 *    were not reading. The server re-validates the anchor against the KPI's
 *    approved dimensions and the reader's row scope regardless.
 *  - **Only timestamps the database holds.** Every date shown is `created_at` or
 *    `updated_at`, rendered as itself. Nothing here synthesises an activity
 *    history, and a finding with no note shows no note rather than a placeholder
 *    sentence.
 */

import { useMemo, useState } from 'react'
import { api } from '../api/client'
import type { EntityStep, Finding, FindingResponse, FindingsResponse } from '../api/types'
import { formatDate, formatRelative } from './format'
import { Alert, EmptyState, Panel, Spinner } from './ui'
import { useAction, useResource } from './useResource'

/** Used only until the server's own list arrives; never instead of it. */
const FALLBACK_STATUSES = ['OPEN', 'IN_PROGRESS', 'RESOLVED']

function statusLabel(value: string): string {
  const words = value.replace(/_/g, ' ').toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function statusTone(value: string): string {
  if (value === 'RESOLVED') return 'border-emerald-200 bg-emerald-50/80 text-emerald-700'
  if (value === 'IN_PROGRESS') return 'border-sky-200 bg-sky-50/80 text-sky-700'
  return 'border-amber-200 bg-amber-50/80 text-amber-700'
}

/** The coordinates a finding is filed against. */
export interface FindingAnchor {
  kpiId: string
  targetDate: string
  dimension?: string | null
  entity?: string | null
  path?: EntityStep[]
}

function anchorLabel(anchor: FindingAnchor): string {
  if (anchor.entity && anchor.dimension) return `${anchor.dimension}: ${anchor.entity}`
  if (anchor.dimension) return `the ${anchor.dimension} breakdown`
  return 'the whole movement'
}

export default function FindingsPanel({
  companyId,
  anchor,
  writable = true,
}: {
  companyId: string
  anchor: FindingAnchor
  /** False renders the notes read-only, for a surface that reads but does not file. */
  writable?: boolean
}) {
  const findings = useResource<FindingsResponse>(
    () =>
      api.get(`/companies/${companyId}/investigation/findings`, {
        query: { kpi_id: anchor.kpiId, target_date: anchor.targetDate },
      }),
    [companyId, anchor.kpiId, anchor.targetDate],
    { enabled: Boolean(companyId && anchor.kpiId && anchor.targetDate) },
  )

  const save = useAction()
  const [formOpen, setFormOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [note, setNote] = useState('')
  const [status, setStatus] = useState('OPEN')

  // A partial payload leaves an empty panel rather than throwing on a screen whose
  // other columns are working. `?? []` is the same defensiveness applied elsewhere.
  const rows: Finding[] = findings.data?.findings ?? []
  const statuses = useMemo(
    () => (findings.data?.statuses?.length ? findings.data.statuses : FALLBACK_STATUSES),
    [findings.data],
  )

  const submit = async () => {
    if (!title.trim()) return
    const created = await save.run(() =>
      api.post<FindingResponse>(`/companies/${companyId}/investigation/findings`, {
        kpi_id: anchor.kpiId,
        target_date: anchor.targetDate,
        dimension: anchor.dimension ?? null,
        entity: anchor.entity ?? null,
        path: anchor.path ?? [],
        title: title.trim(),
        note: note.trim() || null,
        status,
      }),
    )
    if (created) {
      setTitle('')
      setNote('')
      setStatus('OPEN')
      setFormOpen(false)
      void findings.reload()
    }
  }

  const move = async (finding: Finding, next: string) => {
    const updated = await save.run(() =>
      api.patch<FindingResponse>(
        `/companies/${companyId}/investigation/findings/${finding.id}`,
        { status: next },
      ),
    )
    if (updated) void findings.reload()
  }

  return (
    <Panel
      title="Findings"
      bodyClassName="p-0"
      actions={
        writable ? (
          <button
            type="button"
            className="btn btn-xs btn-ghost"
            onClick={() => setFormOpen((open) => !open)}
          >
            {formOpen ? 'Cancel' : 'Add a finding'}
          </button>
        ) : undefined
      }
    >
      {formOpen && writable && (
        <div className="space-y-2 border-b border-ink-800/80 px-4 py-3">
          {/* Said before the form, so nobody files a conclusion against coordinates
              they had not realised they were reading. */}
          <p className="text-[11px] text-slate-500">
            Filed against {anchorLabel(anchor)} on {formatDate(anchor.targetDate)}.
          </p>
          <input
            className="field"
            placeholder="What did you conclude?"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
          <textarea
            className="field min-h-[4.5rem]"
            placeholder="Optional detail — what you checked, and what it showed."
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="field w-auto px-2 py-1 text-xs"
              value={status}
              onChange={(event) => setStatus(event.target.value)}
            >
              {statuses.map((value) => (
                <option key={value} value={value}>
                  {statusLabel(value)}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-xs btn-primary"
              disabled={save.pending || !title.trim()}
              onClick={() => void submit()}
            >
              {save.pending ? 'Saving…' : 'Save finding'}
            </button>
          </div>
          {save.error && <Alert tone="error">{save.error}</Alert>}
        </div>
      )}

      {findings.loading && !findings.data ? (
        <div className="px-4 py-3">
          <Spinner label="Reading stored findings…" />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="No finding recorded yet"
          description="A finding is somebody's written conclusion about this movement. Nothing is inferred here — the panel stays empty until a person writes one."
        />
      ) : (
        <ul className="divide-y divide-ink-800/80">
          {rows.map((finding) => (
            <li key={finding.id} className="px-4 py-3">
              <div className="flex flex-wrap items-baseline gap-2">
                <span
                  className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${statusTone(
                    finding.status,
                  )}`}
                >
                  {finding.status.replace(/_/g, ' ')}
                </span>
                <span className="flex-1 text-sm font-medium text-slate-200">{finding.title}</span>
              </div>
              {finding.note && (
                <p className="mt-1.5 text-xs leading-relaxed text-slate-400">{finding.note}</p>
              )}
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
                <span>{finding.scope_label || 'whole movement'}</span>
                {/* Timestamps the database actually holds, and nothing else. */}
                <span>
                  {finding.updated_by_email ?? finding.created_by_email ?? 'unknown author'} ·{' '}
                  {formatRelative(finding.updated_at)}
                </span>
              </div>
              {writable && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {statuses
                    .filter((value) => value !== finding.status)
                    .map((value) => (
                      <button
                        key={value}
                        type="button"
                        className="btn btn-xs btn-ghost"
                        disabled={save.pending}
                        onClick={() => void move(finding, value)}
                      >
                        Mark {statusLabel(value).toLowerCase()}
                      </button>
                    ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {findings.error && (
        <div className="px-4 py-3">
          <Alert tone="error">{findings.error}</Alert>
        </div>
      )}
    </Panel>
  )
}
