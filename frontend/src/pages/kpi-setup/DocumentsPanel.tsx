/** Company reference and event documents: stored, versioned, access-controlled. */

import { useState } from 'react'
import { api } from '../../api/client'
import type { CompanyDocument } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { formatBytes, formatDate, formatDateTime, titleCase } from '../../components/format'
import {
  Alert,
  DefinitionRow,
  Drawer,
  Field,
  HelpList,
  HelpSection,
  Modal,
  Panel,
  SectionHeader,
  SectionHelp,
  Spinner,
  StatusBadge,
} from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'

const ROLE_OPTIONS = ['ADMIN', 'ANALYST', 'EXECUTIVE', 'MANAGER', 'REGIONAL_MANAGER', 'VIEWER']

export default function DocumentsPanel() {
  const { companyId } = useAuth()
  const base = `/companies/${companyId}`

  const documents = useResource<CompanyDocument[]>(
    () => api.get(`${base}/documents`, { admin: true }),
    [companyId],
  )
  const types = useResource<{
    types: Array<{ value: string; label: string; document_class: string }>
    classes: Record<string, string>
  }>(() => api.get(`${base}/document-types`, { admin: true }), [companyId])

  const [uploadOpen, setUploadOpen] = useState(false)
  const [openDoc, setOpenDoc] = useState<string | null>(null)

  const reference = documents.data?.filter((d) => d.document_class === 'REFERENCE') ?? []
  const events = documents.data?.filter((d) => d.document_class === 'EVENT') ?? []

  return (
    <div className="space-y-5">
      <SectionHeader
        title="Documents"
        help={<DocumentsHelp />}
        actions={
          <button className="btn-primary btn-xs" onClick={() => setUploadOpen(true)}>
            + Upload document
          </button>
        }
      />

      {documents.loading && !documents.data ? (
        <Panel>
          <Spinner />
        </Panel>
      ) : documents.error ? (
        <Alert>{documents.error}</Alert>
      ) : !documents.data?.length ? (
        <Panel>
          <button
            type="button"
            data-bare
            className="dropzone w-full"
            onClick={() => setUploadOpen(true)}
          >
            <span className="text-[15px] font-semibold text-slate-100">
              Upload your first document
            </span>
            <span className="max-w-sm text-xs leading-relaxed text-slate-500">
              A KPI handbook, finance definitions or a pricing policy. A KPI can then cite it as the
              source of its definition.
            </span>
            <span className="btn-primary btn-xs mt-1.5">Choose a document</span>
          </button>
        </Panel>
      ) : (
        <>
          <Panel title="Reference — how the company operates" bodyClassName="">
            <DocumentGroup documents={reference} onOpen={setOpenDoc} />
          </Panel>
          {events.length > 0 && (
            <Panel title="Events — what happened" bodyClassName="">
              <DocumentGroup documents={events} onOpen={setOpenDoc} />
            </Panel>
          )}
        </>
      )}

      {uploadOpen && (
        <UploadModal
          base={base}
          types={types.data?.types ?? []}
          onClose={() => setUploadOpen(false)}
          onCreated={async () => {
            setUploadOpen(false)
            await documents.reload()
          }}
        />
      )}

      {openDoc && (
        <DocumentDrawer
          base={base}
          documentId={openDoc}
          onClose={() => setOpenDoc(null)}
          onChanged={documents.reload}
        />
      )}
    </div>
  )
}

function DocumentsHelp() {
  return (
    <SectionHelp title="About company documents">
      <HelpSection heading="What this section is">
        <p>
          The written record of how your business operates — handbooks, finance definitions, pricing
          and policy documents — stored so the platform can point at them.
        </p>
      </HelpSection>
      <HelpSection heading="What you see">
        <HelpList
          items={[
            ['Document name', 'The title you gave it.'],
            ['Type', 'What kind of document it is, which decides how it may be used.'],
            ['Version', 'Revisions are added, never overwritten.'],
            ['Status', 'Whether this document is in force.'],
            ['Access', 'Which roles are permitted to read it. Empty means every member.'],
            ['View / Manage', 'Read a version, or add a new one.'],
          ]}
        />
      </HelpSection>
      <HelpSection heading="Reference and Events">
        <p>
          <strong className="text-slate-100">Reference</strong> documents describe how the business
          works in general — they give a KPI its business meaning.{' '}
          <strong className="text-slate-100">Event</strong> documents record something that
          happened on particular dates, which is what lets an unusual figure be explained by a known
          event rather than treated as an anomaly.
        </p>
      </HelpSection>
      <HelpSection heading="Why it matters">
        <p>
          This is how business context reaches the KPI intelligence system. A KPI that cites your
          handbook is not using a definition someone invented — it is using yours, and it names the
          exact version it relied on. Because revisions never overwrite each other, a KPI approved
          against version 2 stays explainable long after version 3 exists.
        </p>
      </HelpSection>
    </SectionHelp>
  )
}

/**
 * One document per row: name, what it is, whether it is current, who may read it.
 *
 * Storage mechanics — the internal document key, checksum, byte size, filename
 * and whether the content was pasted or uploaded — are governed and unchanged;
 * they live in the document's own drawer instead of on a list a business reader
 * scans.
 */
function DocumentGroup({
  documents,
  onOpen,
}: {
  documents: CompanyDocument[]
  onOpen: (id: string) => void
}) {
  if (!documents.length) {
    return <div className="px-4 py-6 text-sm text-slate-500">No documents in this category.</div>
  }
  return (
    <div>
      {documents.map((document) => (
        <button key={document.id} className="row-link" onClick={() => onOpen(document.id)} data-bare>
          <div className="flex flex-wrap items-center gap-3">
            <span className="min-w-[13rem] flex-1 text-[14.5px] font-medium text-slate-100">
              {document.title}
            </span>
            <span className="chip">{titleCase(document.document_type)}</span>
            <span className="chip">v{document.current_version}</span>
            <StatusBadge status={document.status} />
            <div className="flex flex-wrap gap-1">
              {document.access_scope.length ? (
                document.access_scope.map((role) => (
                  <span key={role} className="chip">
                    {titleCase(role)}
                  </span>
                ))
              ) : (
                <span className="text-[11px] text-slate-500">All roles</span>
              )}
            </div>
            <span className="text-xs font-medium text-accent">View</span>
          </div>
        </button>
      ))}
    </div>
  )
}

function UploadModal({
  base,
  types,
  onClose,
  onCreated,
}: {
  base: string
  types: Array<{ value: string; label: string; document_class: string }>
  onClose: () => void
  onCreated: () => Promise<void>
}) {
  const create = useAction()
  const [title, setTitle] = useState('')
  const [documentType, setDocumentType] = useState('KPI_HANDBOOK')
  const [description, setDescription] = useState('')
  const [effectiveFrom, setEffectiveFrom] = useState('')
  const [scope, setScope] = useState<string[]>(['ADMIN', 'ANALYST', 'EXECUTIVE'])
  const [mode, setMode] = useState<'file' | 'text'>('text')
  const [inline, setInline] = useState('')
  const [file, setFile] = useState<File | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const form = new FormData()
    form.append(
      'metadata',
      JSON.stringify({
        title,
        document_type: documentType,
        description: description || null,
        access_scope: scope,
        effective_from: effectiveFrom || null,
        inline_content: mode === 'text' ? inline : null,
      }),
    )
    if (mode === 'file' && file) form.append('file', file)

    const created = await create.run(() =>
      api.upload<CompanyDocument>(`${base}/documents`, form, { admin: true }),
    )
    if (created) await onCreated()
  }

  return (
    <Modal open onClose={onClose} title="Upload document" width="max-w-xl">
      <form onSubmit={submit} className="space-y-4">
        {create.error && <Alert>{create.error}</Alert>}

        <Field label="Title" required>
          <input
            className="field"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="NovaMart KPI Handbook"
            required
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Document type" required>
            <select
              className="field"
              value={documentType}
              onChange={(e) => setDocumentType(e.target.value)}
            >
              {types.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Effective from" hint="When this version takes effect.">
            <input
              type="date"
              className="field"
              value={effectiveFrom}
              onChange={(e) => setEffectiveFrom(e.target.value)}
            />
          </Field>
        </div>

        <Field label="Description">
          <input
            className="field"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Finance-approved KPI definitions."
          />
        </Field>

        <Field
          label="Access scope"
          hint="Roles permitted to retrieve this document. Empty means every member."
        >
          <div className="flex flex-wrap gap-2">
            {ROLE_OPTIONS.map((role) => (
              <label
                key={role}
                className={`cursor-pointer rounded border px-2 py-1 text-xs transition-colors ${
                  scope.includes(role)
                    ? 'border-accent bg-accent/15 text-accent-soft'
                    : 'border-ink-600 text-slate-400 hover:border-ink-600'
                }`}
              >
                <input
                  type="checkbox"
                  className="hidden"
                  checked={scope.includes(role)}
                  onChange={(e) =>
                    setScope((prev) =>
                      e.target.checked ? [...prev, role] : prev.filter((r) => r !== role),
                    )
                  }
                />
                {role}
              </label>
            ))}
          </div>
        </Field>

        <div className="flex gap-1 rounded-md border border-ink-700 bg-ink-850 p-1">
          {([
            ['text', 'Paste content'],
            ['file', 'Upload file'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setMode(key)}
              className={`flex-1 rounded px-3 py-1.5 text-sm transition-colors ${
                mode === key
                  ? 'bg-ink-700 font-medium text-slate-100'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {mode === 'text' ? (
          <Field label="Content">
            <textarea
              className="field min-h-[7rem] resize-y"
              value={inline}
              onChange={(e) => setInline(e.target.value)}
              placeholder="Revenue is the sum of order_value across all orders in the period…"
            />
          </Field>
        ) : (
          <Field label="File">
            <input
              type="file"
              className="field"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </Field>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className="btn-ghost btn-xs" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary btn-xs" disabled={create.pending}>
            {create.pending ? 'Uploading…' : 'Upload'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

function DocumentDrawer({
  base,
  documentId,
  onClose,
  onChanged,
}: {
  base: string
  documentId: string
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const detail = useResource<CompanyDocument>(
    () => api.get(`${base}/documents/${documentId}`, { admin: true }),
    [documentId],
  )
  const revise = useAction()
  const [note, setNote] = useState('')
  const [content, setContent] = useState('')
  const [showRevise, setShowRevise] = useState(false)

  return (
    <Drawer
      open
      onClose={onClose}
      title={detail.data?.title ?? 'Document'}
      subtitle={
        detail.data
          ? `${titleCase(detail.data.document_type)} · ${titleCase(detail.data.document_class)} · v${detail.data.current_version}`
          : undefined
      }
      footer={
        <>
          <button className="btn-ghost btn-xs" onClick={() => setShowRevise((v) => !v)}>
            {showRevise ? 'Cancel revision' : 'Add new version'}
          </button>
          <button className="btn-ghost btn-xs" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      {detail.loading && !detail.data ? (
        <Spinner />
      ) : detail.error ? (
        <Alert>{detail.error}</Alert>
      ) : detail.data ? (
        <div className="space-y-5">
          <dl>
            <DefinitionRow term="Description">{detail.data.description ?? '—'}</DefinitionRow>
            <DefinitionRow term="Access scope">
              {detail.data.access_scope.length ? (
                <div className="flex flex-wrap gap-1">
                  {detail.data.access_scope.map((role) => (
                    <span key={role} className="chip">
                      {titleCase(role)}
                    </span>
                  ))}
                </div>
              ) : (
                'Every member of this company'
              )}
            </DefinitionRow>
          </dl>

          {showRevise && (
            <section className="rounded-md border border-accent/30 bg-accent/5 p-3">
              <div className="panel-title mb-2">New version</div>
              {revise.error && <Alert>{revise.error}</Alert>}
              <div className="space-y-3">
                <Field label="Change note">
                  <input
                    className="field"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Clarified that AOV is not additive across periods."
                  />
                </Field>
                <Field label="Content">
                  <textarea
                    className="field min-h-[6rem] resize-y"
                    value={content}
                    onChange={(e) => setContent(e.target.value)}
                  />
                </Field>
                <button
                  className="btn-primary btn-xs"
                  disabled={revise.pending}
                  onClick={async () => {
                    const form = new FormData()
                    form.append(
                      'metadata',
                      JSON.stringify({
                        title: detail.data!.title,
                        document_type: detail.data!.document_type,
                        change_note: note || null,
                        inline_content: content,
                      }),
                    )
                    const ok = await revise.run(
                      () =>
                        api.upload(`${base}/documents/${documentId}/versions`, form, {
                          admin: true,
                        }),
                      'New version stored.',
                    )
                    if (ok) {
                      setShowRevise(false)
                      setNote('')
                      setContent('')
                      await detail.reload()
                      await onChanged()
                    }
                  }}
                >
                  {revise.pending ? 'Saving…' : 'Store version'}
                </button>
              </div>
            </section>
          )}

          <section>
            <h3 className="panel-title mb-2">Versions</h3>
            <ul className="divide-y divide-ink-800 rounded-md border border-ink-800">
              {detail.data.versions
                .slice()
                .reverse()
                .map((version) => (
                  <li key={version.id} className="flex flex-wrap items-center gap-3 px-3 py-2">
                    <span className="font-medium text-slate-200">v{version.version}</span>
                    {version.is_current && <StatusBadge status="ACTIVE" label="current" />}
                    <span className="flex-1 truncate text-xs text-slate-500">
                      {version.change_note ?? version.original_filename ?? 'inline content'}
                    </span>
                    <span className="text-[11px] text-slate-600">
                      {version.effective_from
                        ? `effective ${formatDate(version.effective_from)}`
                        : formatDateTime(version.uploaded_at)}
                    </span>
                    {version.size_bytes && (
                      <span className="text-[11px] text-slate-600">
                        {formatBytes(version.size_bytes)}
                      </span>
                    )}
                    <a
                      className="text-xs text-accent-soft hover:underline"
                      href={`/api/v1${base}/documents/${documentId}/content?version=${version.version}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      view
                    </a>
                  </li>
                ))}
            </ul>
          </section>
        </div>
      ) : null}
    </Drawer>
  )
}
