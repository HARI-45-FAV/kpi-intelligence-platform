/**
 * Comparison policy: which past days the detection engine treats as comparable.
 *
 * This is the governance surface for the five fixed bucket slots, and it lives
 * here — behind the admin gate — rather than on the monitoring screen, because a
 * business reader wants a verdict and an approver wants the calendar rule behind
 * it. Those are different questions and different screens.
 *
 * The flow it exposes is the whole point:
 *
 *   a company document -> extract -> read what came back -> fix or approve
 *
 * Two things it deliberately does *not* do. It does not invent a default policy
 * for a company: an extraction with nothing usable in it lands as NEEDS_REVIEW
 * with its reasons shown, and the engine keeps using its documented trailing
 * fallback until a person approves something real. And it names no weekday, week,
 * month or event anywhere in this file — every value rendered below arrives from
 * the server, extracted from *that* company's own documentation.
 */

import { useState } from 'react'
import { api } from '../../api/client'
import type {
  BucketConfigList,
  BucketConfigPreview,
  BucketConfigSummary,
  BucketExtractionResponse,
  CompanyDocument,
} from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime, monthName, titleCase, weekdayName } from '../../components/format'
import {
  Alert,
  DefinitionRow,
  Drawer,
  EmptyState,
  Field,
  Modal,
  Panel,
  Spinner,
  StatusBadge,
} from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'

/** Reference documents are the ones that describe how a company operates. */
const HANDBOOK_TYPE = 'KPI_HANDBOOK'

export default function ComparisonPolicyPanel() {
  const { companyId } = useAuth()
  const base = `/companies/${companyId}`

  const configs = useResource<BucketConfigList>(
    () => api.get(`${base}/bucket-configs`, { admin: true }),
    [companyId],
  )
  const documents = useResource<CompanyDocument[]>(
    () => api.get(`${base}/documents`, { admin: true }),
    [companyId],
  )

  const [extractOpen, setExtractOpen] = useState(false)
  const [openConfig, setOpenConfig] = useState<string | null>(null)
  const [extraction, setExtraction] = useState<BucketExtractionResponse | null>(null)

  const rows = configs.data?.configurations ?? []
  const inForce = rows.find((row) => row.id === configs.data?.company_default_in_force)

  return (
    <div className="space-y-5">
      <Panel
        title="Comparison policy"
        actions={
          <button className="btn-primary btn-xs" onClick={() => setExtractOpen(true)}>
            Extract from a document
          </button>
        }
        bodyClassName=""
      >
        <div className="border-b border-ink-800 p-4">
          <p className="text-xs leading-relaxed text-slate-500">
            Detection needs to know which past days are comparable to the day being judged. That
            answer is company-specific, so it is read from your own documentation rather than
            assumed: a handbook that says which weekdays, weeks, months or trading events your
            business compares like-for-like. The engine reads{' '}
            <strong className="text-slate-300">approved</strong> policies only.
          </p>
          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            {inForce ? (
              <>
                In force for every KPI without its own override:{' '}
                <strong className="text-slate-300">
                  {inForce.name} v{inForce.version}
                </strong>
                .
              </>
            ) : (
              <>
                No approved policy yet. Detection still answers — it compares recent days and says
                so — but it claims no weekly, monthly or seasonal pattern until you approve one.
              </>
            )}
          </p>
        </div>

        {configs.loading && !configs.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : configs.error ? (
          <div className="p-4">
            <Alert>{configs.error}</Alert>
          </div>
        ) : !rows.length ? (
          <EmptyState
            title="No comparison policy yet"
            description="Extract one from your KPI handbook, or from any reference document that states which days your business compares like-for-like."
            action={
              <button className="btn-primary btn-xs" onClick={() => setExtractOpen(true)}>
                Extract from a document
              </button>
            }
          />
        ) : (
          rows.map((row) => (
            <button key={row.id} className="row-link" onClick={() => setOpenConfig(row.id)}>
              <div className="flex flex-wrap items-center gap-3">
                <span className="min-w-[12rem] font-medium text-slate-100">{row.name}</span>
                <StatusBadge status={row.status} />
                <span className="chip">v{row.version}</span>
                <span className="chip">
                  {row.scope === 'kpi' ? `KPI: ${row.kpi_key}` : 'All KPIs'}
                </span>
                {row.source === 'LLM_EXTRACTION' && (
                  <span className="chip" title={row.extraction_model ?? undefined}>
                    extracted
                  </span>
                )}
              </div>
              <div className="mt-1 text-[11px] text-slate-500">
                {row.enabled_slots.length
                  ? `${row.enabled_slots.length} slot(s): ${row.enabled_slots
                      .map((slot) => titleCase(slot))
                      .join(' · ')}`
                  : 'No slot enabled — this policy cannot select a comparable date yet.'}
              </div>
            </button>
          ))
        )}
      </Panel>

      {extractOpen && (
        <ExtractModal
          base={base}
          documents={documents.data ?? []}
          onClose={() => setExtractOpen(false)}
          onExtracted={async (result) => {
            setExtractOpen(false)
            setExtraction(result)
            await configs.reload()
          }}
        />
      )}

      {extraction && (
        <ExtractionResultModal
          result={extraction}
          onClose={() => setExtraction(null)}
          onReview={() => {
            setOpenConfig(extraction.id)
            setExtraction(null)
          }}
        />
      )}

      {openConfig && (
        <ConfigDrawer
          base={base}
          configId={openConfig}
          onClose={() => setOpenConfig(null)}
          onChanged={configs.reload}
        />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ extraction */

function ExtractModal({
  base,
  documents,
  onClose,
  onExtracted,
}: {
  base: string
  documents: CompanyDocument[]
  onClose: () => void
  onExtracted: (result: BucketExtractionResponse) => Promise<void>
}) {
  // A KPI handbook is the document this is for, so it sorts first — but any
  // readable reference document may state a comparison rule, so none is excluded.
  const readable = documents
    .filter((document) => document.document_class === 'REFERENCE')
    .sort((a, b) => {
      const rank = (d: CompanyDocument) => (d.document_type === HANDBOOK_TYPE ? 0 : 1)
      return rank(a) - rank(b) || a.title.localeCompare(b.title)
    })

  const [documentId, setDocumentId] = useState(readable[0]?.id ?? '')
  const [configKey, setConfigKey] = useState('comparison-policy')
  const [name, setName] = useState('Comparison policy')
  const { pending, error, run } = useAction()

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const result = await run(() =>
      api.post<BucketExtractionResponse>(
        `${base}/bucket-configs/extract`,
        { document_id: documentId, config_key: configKey, name },
        { admin: true },
      ),
    )
    if (result) await onExtracted(result)
  }

  return (
    <Modal open onClose={onClose} title="Extract a comparison policy" width="max-w-lg">
      <form onSubmit={submit} className="space-y-4">
        {error && <Alert>{error}</Alert>}

        <p className="text-xs leading-relaxed text-slate-500">
          The model reads the passages of this document that discuss comparison and returns which
          weekdays, weeks, months or trading events your business treats as comparable. It cannot
          return a KPI value, an expectation or a verdict — every number is computed by the engine —
          and the draft changes nothing until it is approved.
        </p>

        {!readable.length ? (
          <Alert>
            No readable reference document is stored yet. Upload your KPI handbook under Documents
            first.
          </Alert>
        ) : (
          <Field label="Document" required>
            <select
              className="field"
              value={documentId}
              onChange={(e) => setDocumentId(e.target.value)}
              required
            >
              {readable.map((document) => (
                <option key={document.id} value={document.id}>
                  {document.title} — {titleCase(document.document_type)}
                  {document.document_type === HANDBOOK_TYPE ? ' ★' : ''}
                </option>
              ))}
            </select>
          </Field>
        )}

        <Field label="Policy name" required>
          <input
            className="field"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>

        <Field
          label="Policy key"
          hint="Versions share a key, so a later extraction becomes v2 of the same policy."
          required
        >
          <input
            className="field"
            value={configKey}
            onChange={(e) => setConfigKey(e.target.value)}
            required
          />
        </Field>

        <div className="flex justify-end gap-2">
          <button type="button" className="btn-ghost btn-xs" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn-primary btn-xs"
            disabled={pending || !documentId || !readable.length}
          >
            {pending ? 'Reading the document…' : 'Extract'}
          </button>
        </div>
        {pending && (
          <p className="text-[11px] text-slate-600">
            A local model is reading the selected passages. This can take a minute.
          </p>
        )}
      </form>
    </Modal>
  )
}

function ExtractionResultModal({
  result,
  onClose,
  onReview,
}: {
  result: BucketExtractionResponse
  onClose: () => void
  onReview: () => void
}) {
  const retrieval = result.extraction.retrieval
  return (
    <Modal open onClose={onClose} title="What the document said" width="max-w-2xl">
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={result.status} />
          <span className="chip">
            {result.name} v{result.version}
          </span>
          {result.extraction.model && <span className="chip">{result.extraction.model}</span>}
        </div>

        {result.needs_review && (
          <Alert>
            <span className="font-medium">This extraction is not usable as it stands.</span>
            <ul className="mt-1.5 list-disc space-y-1 pl-4">
              {result.review_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </Alert>
        )}

        <SlotSummary config={result} />

        {!!result.extraction.notes.length && (
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              What happened while reading
            </div>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-xs leading-relaxed text-slate-500">
              {result.extraction.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>
        )}

        {retrieval && (
          <p className="text-[11px] leading-relaxed text-slate-600">
            {retrieval.passages_selected} of {retrieval.passages_in_document} passages (
            {retrieval.selected_characters.toLocaleString()} of{' '}
            {retrieval.document_characters.toLocaleString()} characters) reached the model, chosen
            by {retrieval.strategy}.
          </p>
        )}

        <div className="flex justify-end gap-2 border-t border-ink-800 pt-3">
          <button className="btn-ghost btn-xs" onClick={onClose}>
            Close
          </button>
          <button className="btn-primary btn-xs" onClick={onReview}>
            Review &amp; approve
          </button>
        </div>
      </div>
    </Modal>
  )
}

/* ------------------------------------------------------------- slot rendering */

/**
 * The five slots, as values. Every label here describes the *slot*; every value
 * comes from the server. `weekdayName` / `monthName` turn the company's own
 * numbers into words — they do not choose which numbers.
 */
function SlotSummary({
  config,
}: {
  config: Pick<BucketConfigSummary, 'buckets' | 'lookback_days' | 'min_reference_points' | 'max_reference_points'>
}) {
  const slots = config.buckets ?? {}
  const dow = slots.same_day_of_week
  const wom = slots.same_week_of_month
  const mos = slots.same_month_or_season
  const events = slots.business_event
  const yoy = slots.yoy_period

  return (
    <dl className="rounded-xl border border-ink-800 bg-ink-850/60 px-3 py-1">
      <SlotRow term="Same day of week" enabled={dow?.enabled}>
        {dow?.days?.length ? dow.days.map(weekdayName).join(', ') : 'no days stated'}
      </SlotRow>
      <SlotRow term="Same week of month" enabled={wom?.enabled}>
        {wom?.weeks?.length ? wom.weeks.map((w) => `week ${w}`).join(', ') : 'no weeks stated'}
      </SlotRow>
      <SlotRow term="Same month or season" enabled={mos?.enabled}>
        {mos?.months?.length ? mos.months.map(monthName).join(', ') : 'no months stated'}
      </SlotRow>
      <SlotRow term="Business event" enabled={events?.enabled}>
        {events?.events?.length ? (
          <ul className="space-y-0.5">
            {events.events.map((event) => (
              <li key={event.name}>
                <span className="text-slate-200">{event.name}</span>{' '}
                {event.dates?.length ? (
                  <span className="text-slate-500">— {event.dates.join(', ')}</span>
                ) : (
                  <span className="text-amber-600">— no dates, so it selects nothing</span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          'no events stated'
        )}
      </SlotRow>
      <SlotRow term="Year over year" enabled={yoy?.enabled}>
        {yoy?.tolerance_days != null
          ? `±${yoy.tolerance_days} day(s) around the anniversary`
          : 'no tolerance stated'}
      </SlotRow>
      <DefinitionRow term="Search budget">
        <span className="text-xs text-slate-500">
          {config.lookback_days ?? '—'} day lookback · {config.min_reference_points ?? '—'}–
          {config.max_reference_points ?? '—'} reference points. Set by the platform, not by the
          document.
        </span>
      </DefinitionRow>
    </dl>
  )
}

function SlotRow({
  term,
  enabled,
  children,
}: {
  term: string
  enabled?: boolean
  children: React.ReactNode
}) {
  return (
    <DefinitionRow term={term}>
      <div className="flex flex-wrap items-baseline gap-2">
        <span
          className={`text-[10px] font-semibold uppercase tracking-wider ${
            enabled ? 'text-emerald-700' : 'text-slate-600'
          }`}
        >
          {enabled ? 'on' : 'off'}
        </span>
        <span className="min-w-0 text-xs text-slate-400">{children}</span>
      </div>
    </DefinitionRow>
  )
}

/* ----------------------------------------------------------------- one policy */

type ConfigDetail = BucketConfigSummary & {
  normalised?: Record<string, unknown> | null
  warnings: string[]
  usable: boolean
  unusable_reason?: string | null
}

function ConfigDrawer({
  base,
  configId,
  onClose,
  onChanged,
}: {
  base: string
  configId: string
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const detail = useResource<ConfigDetail>(
    () => api.get(`${base}/bucket-configs/${configId}`, { admin: true }),
    [base, configId],
  )
  const [previewDate, setPreviewDate] = useState(() => new Date().toISOString().slice(0, 10))
  const [preview, setPreview] = useState<BucketConfigPreview | null>(null)
  const { pending, error, message, run } = useAction()

  const row = detail.data
  const can = (target: string) => row?.allowed_transitions.includes(target) ?? false

  const transition = async (verb: 'propose' | 'approve' | 'archive') => {
    await run(
      () =>
        api.post(`${base}/bucket-configs/${configId}/${verb}`, { reason: null }, { admin: true }),
      `Policy ${verb}d.`,
    )
    await detail.reload()
    await onChanged()
  }

  const runPreview = async () => {
    const result = await run(() =>
      api.get<BucketConfigPreview>(`${base}/bucket-configs/${configId}/preview`, {
        admin: true,
        query: { target_date: previewDate },
      }),
    )
    if (result) setPreview(result)
  }

  return (
    <Drawer
      open
      onClose={onClose}
      title={row ? `${row.name} v${row.version}` : 'Comparison policy'}
      subtitle={row?.description ?? undefined}
      footer={
        row && (
          <div className="flex flex-wrap justify-end gap-2">
            {can('ARCHIVED') && (
              <button
                className="btn-ghost btn-xs"
                disabled={pending}
                onClick={() => transition('archive')}
              >
                Archive
              </button>
            )}
            {can('PROPOSED') && (
              <button
                className="btn-ghost btn-xs"
                disabled={pending || !row.usable}
                title={row.usable ? undefined : (row.unusable_reason ?? undefined)}
                onClick={() => transition('propose')}
              >
                Propose
              </button>
            )}
            {can('APPROVED') && (
              <button
                className="btn-primary btn-xs"
                disabled={pending}
                onClick={() => transition('approve')}
              >
                Approve — the engine will use this
              </button>
            )}
          </div>
        )
      }
    >
      {detail.loading && !row ? (
        <Spinner />
      ) : detail.error ? (
        <Alert>{detail.error}</Alert>
      ) : !row ? null : (
        <div className="space-y-5">
          {error && <Alert>{error}</Alert>}
          {message && <p className="text-xs text-emerald-700">{message}</p>}

          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={row.status} />
            <span className="chip">{row.scope === 'kpi' ? `KPI: ${row.kpi_key}` : 'All KPIs'}</span>
            <span className="chip">{titleCase(row.source)}</span>
            {row.extraction_model && <span className="chip">{row.extraction_model}</span>}
          </div>

          {!row.usable && row.unusable_reason && (
            <Alert>
              <span className="font-medium">Not usable yet.</span> {row.unusable_reason}
            </Alert>
          )}

          <SlotSummary config={row} />

          {row.extraction_notes && (
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                Extraction notes
              </div>
              <ul className="mt-1 list-disc space-y-1 pl-4 text-xs leading-relaxed text-slate-500">
                {row.extraction_notes.split('\n').map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </div>
          )}

          {!!row.warnings.length && (
            <Alert>
              <ul className="list-disc space-y-1 pl-4">
                {row.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </Alert>
          )}

          <div className="rounded-xl border border-ink-800 p-3">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              Which past dates would this select?
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-slate-600">
              Calendar only — no KPI is measured and your source is not queried. It answers the one
              question JSON cannot: would this actually pick the days you mean?
            </p>
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <Field label="Target date">
                <input
                  type="date"
                  className="field"
                  value={previewDate}
                  onChange={(e) => setPreviewDate(e.target.value)}
                />
              </Field>
              <button
                className="btn-ghost btn-xs"
                disabled={pending || !row.usable}
                title={row.usable ? undefined : (row.unusable_reason ?? undefined)}
                onClick={runPreview}
              >
                Preview
              </button>
            </div>
            {preview && (
              <div className="mt-3 space-y-2">
                <p className="text-xs text-slate-300">
                  {preview.comparison.label} — {preview.comparable_date_count} comparable date(s)
                </p>
                <div className="flex flex-wrap gap-1">
                  {preview.comparable_dates.map((day) => (
                    <span key={day} className="chip">
                      {day}
                    </span>
                  ))}
                </div>
                <ul className="space-y-0.5 text-[11px] text-slate-500">
                  {preview.comparison.decisions.map((decision) => (
                    <li key={`${decision.bucket}-${decision.role}`}>
                      <span className="text-slate-400">{titleCase(decision.bucket)}</span> ·{' '}
                      {decision.role.toLowerCase()} · {decision.note}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          <dl>
            <DefinitionRow term="Policy key">{row.config_key}</DefinitionRow>
            <DefinitionRow term="Created">{formatDateTime(row.created_at)}</DefinitionRow>
            {row.approved_at && (
              <DefinitionRow term="Approved">{formatDateTime(row.approved_at)}</DefinitionRow>
            )}
          </dl>
        </div>
      )}
    </Drawer>
  )
}
