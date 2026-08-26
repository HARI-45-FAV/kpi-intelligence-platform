/** Catalog versions and the governance audit trail. */

import { api } from '../../api/client'
import type { AuditEntry, CatalogVersionInfo } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime, formatNumber, formatRelative } from '../../components/format'
import { Alert, EmptyState, Panel, Spinner, StatusBadge } from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'

export default function HistoryPanel() {
  const { companyId } = useAuth()
  const base = `/companies/${companyId}`

  const versions = useResource<CatalogVersionInfo[]>(
    () => api.get(`${base}/catalog/versions`, { admin: true }),
    [companyId],
  )
  const audit = useResource<AuditEntry[]>(
    () => api.get(`${base}/audit`, { admin: true, query: { limit: 150 } }),
    [companyId],
  )
  const publish = useAction()

  return (
    <div className="space-y-5">
      <Panel
        title="Catalog versions"
        actions={
          <button
            className="btn-primary btn-xs"
            disabled={publish.pending}
            onClick={async () => {
              const ok = await publish.run(
                () => api.post(`${base}/catalog/publish`, {}, { admin: true }),
                'Catalog version published.',
              )
              if (ok) await versions.reload()
            }}
          >
            {publish.pending ? 'Publishing…' : 'Publish catalog version'}
          </button>
        }
        bodyClassName=""
      >
        <div className="border-b border-ink-800 p-4">
          <p className="text-xs leading-relaxed text-slate-500">
            Two version concepts, never conflated. A <strong>catalog version</strong> records what the
            platform knew about this company's data at a point in time; a <strong>KPI version</strong>{' '}
            records what the company meant by a metric. Publishing freezes an immutable snapshot with
            a checksum, so an insight recorded months from now stays reproducible after the schema
            moves on.
          </p>
          {publish.error && (
            <div className="mt-3">
              <Alert>{publish.error}</Alert>
            </div>
          )}
          {publish.message && (
            <div className="mt-3">
              <Alert tone="success" onDismiss={publish.reset}>
                {publish.message}
              </Alert>
            </div>
          )}
        </div>

        {versions.loading && !versions.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : versions.error ? (
          <div className="p-4">
            <Alert>{versions.error}</Alert>
          </div>
        ) : !versions.data?.length ? (
          <EmptyState
            title="No catalog version published"
            description="Publish once the sources, scope, profiling and KPI contracts are settled. Sprint 2 reads a published catalog rather than rediscovering the business each run."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-ink-700">
                  <th className="table-head">Version</th>
                  <th className="table-head">Published</th>
                  <th className="table-head">Sources</th>
                  <th className="table-head">Tables</th>
                  <th className="table-head">Profiled</th>
                  <th className="table-head">Relations</th>
                  <th className="table-head">Docs</th>
                  <th className="table-head">Active KPIs</th>
                  <th className="table-head">Checksum</th>
                </tr>
              </thead>
              <tbody>
                {versions.data.map((version) => (
                  <tr key={version.id} className="border-b border-ink-800 last:border-0">
                    <td className="table-cell font-medium text-slate-100">v{version.version}</td>
                    <td className="table-cell text-slate-400" title={version.note ?? undefined}>
                      {formatDateTime(version.published_at)}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {version.source_count}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {version.selected_table_count}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {version.profiled_table_count}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {version.relationship_count}
                    </td>
                    <td className="table-cell tabular-nums text-slate-400">
                      {version.document_count}
                    </td>
                    <td className="table-cell tabular-nums text-slate-200">
                      {version.active_kpi_count}
                    </td>
                    <td className="table-cell mono text-[11px] text-slate-600">
                      {version.checksum_sha256?.slice(0, 12) ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Governance audit trail" bodyClassName="">
        {audit.loading && !audit.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : audit.error ? (
          <div className="p-4">
            <Alert>{audit.error}</Alert>
          </div>
        ) : !audit.data?.length ? (
          <EmptyState title="No audit entries yet" />
        ) : (
          <ul>
            {audit.data.map((entry) => (
              <li
                key={entry.id}
                className="flex items-start gap-3 border-b border-ink-800 px-4 py-2.5 last:border-0"
              >
                <span
                  className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                    entry.outcome === 'SUCCESS' ? 'bg-emerald-600' : 'bg-rose-600'
                  }`}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <code className="mono text-accent-soft">{entry.action}</code>
                    {entry.resource_label && (
                      <span className="text-sm text-slate-300">{entry.resource_label}</span>
                    )}
                    {(entry.old_version || entry.new_version) && (
                      <span className="chip">
                        {entry.old_version || '—'} → {entry.new_version || '—'}
                      </span>
                    )}
                    {entry.outcome !== 'SUCCESS' && <StatusBadge status="FAIL" />}
                  </div>
                  {entry.summary && (
                    <p className="mt-0.5 text-xs leading-snug text-slate-500">{entry.summary}</p>
                  )}
                  <div className="mt-0.5 text-[11px] text-slate-600">
                    {entry.actor_email ?? 'system'}
                  </div>
                </div>
                <span
                  className="shrink-0 text-[11px] text-slate-600"
                  title={formatDateTime(entry.occurred_at)}
                >
                  {formatRelative(entry.occurred_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <p className="text-[11px] leading-relaxed text-slate-600">
        The audit trail is append-only by construction — no endpoint updates or deletes it — and
        credentials are scrubbed before an entry is written. That is what makes "who approved Revenue
        v2, and when, and against which handbook version" answerable{' '}
        {formatNumber(audit.data?.length ?? 0)} entries later.
      </p>
    </div>
  )
}
