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
  EmptyState,
  Field,
  Modal,
  Panel,
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
      <Panel
        title="Company documents"
        actions={
          <button className="btn-primary btn-xs" onClick={() => setUploadOpen(true)}>
            + Upload document
          </button>
        }
        bodyClassName=""
      >
        <div className="border-b border-ink-800 p-4">
          <p className="text-xs leading-relaxed text-slate-500">
            Sprint 1 stores, versions and access-controls documents. Chunking, embeddings and
            retrieval belong to the RAG sprint, which will read the entitlement rules proven here
            — filtering <em>before</em> search rather than censoring results afterwards.
          </p>
        </div>

        {documents.loading && !documents.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : documents.error ? (
          <div className="p-4">
            <Alert>{documents.error}</Alert>
          </div>
        ) : !documents.data?.length ? (
          <EmptyState
            title="No documents yet"
            description="Upload the KPI handbook, finance definitions or pricing policy. A KPI version can then cite a specific document version as its definition source."
          />
        ) : (
          <>
            <DocumentGroup
              label="Reference — how the company operates"
              hint={types.data?.classes.REFERENCE}
              documents={reference}
              onOpen={setOpenDoc}
            />
            <DocumentGroup
              label="Events — what happened"
              hint={types.data?.classes.EVENT}
              documents={events}
              onOpen={setOpenDoc}
            />
          </>
        )}
      </Panel>

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

function DocumentGroup({
  label,
  hint,
  documents,
  onOpen,
}: {
  label: string
  hint?: string
  documents: CompanyDocument[]
  onOpen: (id: string) => void
}) {
  if (!documents.length) return null
  return (
    <div>
      <div className="border-b border-ink-800 bg-ink-850 px-4 py-2">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          {label}
        </div>
        {hint && <div className="mt-0.5 text-[11px] text-slate-600">{hint}</div>}
      </div>
      {documents.map((document) => (
        <button key={document.id} className="row-link" onClick={() => onOpen(document.id)}>
          <div className="flex flex-wrap items-center gap-3">
            <span className="min-w-[12rem] font-medium text-slate-100">{document.title}</span>
            <span className="chip">{titleCase(document.document_type)}</span>
            <span className="chip">v{document.current_version}</span>
            <StatusBadge status={document.status} />
            <span className="flex-1" />
            <div className="flex flex-wrap gap-1">
              {document.access_scope.length ? (
                document.access_scope.map((role) => (
                  <span key={role} className="chip">
                    {role}
                  </span>
                ))
              ) : (
                <span className="text-[11px] text-slate-600">all roles</span>
              )}
            </div>
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
            <DefinitionRow term="Key">
              <span className="mono">{detail.data.document_key}</span>
            </DefinitionRow>
            <DefinitionRow term="Description">{detail.data.description ?? '—'}</DefinitionRow>
            <DefinitionRow term="Access scope">
              {detail.data.access_scope.length ? (
                <div className="flex flex-wrap gap-1">
                  {detail.data.access_scope.map((role) => (
                    <span key={role} className="chip">
                      {role}
                    </span>
                  ))}
                </div>
              ) : (
                'Every member of this company'
              )}
            </DefinitionRow>
            <DefinitionRow term="Retrieval">
              <span className="text-slate-500">
                Not indexed. Embeddings and retrieval arrive with the RAG sprint.
              </span>
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
            <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
              A revision never overwrites an earlier version. A KPI contract citing "Handbook v2"
              must stay resolvable long after v3 exists.
            </p>
          </section>
        </div>
      ) : null}
    </Drawer>
  )
}
