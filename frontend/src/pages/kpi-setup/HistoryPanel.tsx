/** Catalog versions and the governance change history. */

import { api } from '../../api/client'
import type { AuditEntry, CatalogVersionInfo } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime, formatNumber, formatRelative, titleCase } from '../../components/format'
import {
  Alert,
  EmptyState,
  HelpList,
  HelpSection,
  Panel,
  SectionHeader,
  SectionHelp,
  Spinner,
  StatCard,
  StatusBadge,
} from '../../components/ui'
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
      <SectionHeader
        title="History"
        help={<HistoryHelp />}
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
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard
          label="Catalog versions"
          value={formatNumber(versions.data?.length ?? 0)}
          caption="Immutable snapshots"
        />
        <StatCard
          label="Latest version"
          value={versions.data?.length ? `v${versions.data[0].version}` : '—'}
          caption={
            versions.data?.length ? formatRelative(versions.data[0].published_at) : 'Not published'
          }
          tone={versions.data?.length ? 'default' : 'muted'}
        />
        <StatCard
          label="Audit entries"
          value={formatNumber(audit.data?.length ?? 0)}
          caption="Recorded changes"
        />
      </div>

      {publish.error && <Alert>{publish.error}</Alert>}
      {publish.message && (
        <Alert tone="success" onDismiss={publish.reset}>
          {publish.message}
        </Alert>
      )}

      <Panel title="Catalog versions" bodyClassName="">
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
            description="Publish once your sources, scope and KPI contracts are settled."
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
                </tr>
              </thead>
              <tbody>
                {versions.data.map((version) => (
                  <tr
                    key={version.id}
                    className="border-b border-ink-800 transition-colors last:border-0 hover:bg-white/50"
                  >
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
                    <td className="table-cell tabular-nums font-medium text-slate-200">
                      {version.active_kpi_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Change history" bodyClassName="">
        {audit.loading && !audit.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : audit.error ? (
          <div className="p-4">
            <Alert>{audit.error}</Alert>
          </div>
        ) : !audit.data?.length ? (
          <EmptyState title="No changes recorded yet" />
        ) : (
          <ul>
            {audit.data.map((entry) => (
              <li
                key={entry.id}
                className="flex items-start gap-3 border-b border-ink-800 px-4 py-2.5 transition-colors last:border-0 hover:bg-white/50"
              >
                <span
                  className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                    entry.outcome === 'SUCCESS' ? 'bg-emerald-600' : 'bg-rose-500'
                  }`}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-sm font-medium text-slate-200">
                      {entry.resource_label || titleCase(entry.action.replace(/[._]/g, ' '))}
                    </span>
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
                  <div className="mt-0.5 text-[11px] text-slate-500">
                    {entry.actor_email ?? 'system'}
                  </div>
                </div>
                <span
                  className="shrink-0 text-[11px] text-slate-500"
                  title={formatDateTime(entry.occurred_at)}
                >
                  {formatRelative(entry.occurred_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}

function HistoryHelp() {
  return (
    <SectionHelp title="About history and versions">
      <HelpSection heading="What this section is">
        <p>
          The record of what this workspace looked like over time, and of every governed change made
          to it.
        </p>
      </HelpSection>
      <HelpSection heading="Two kinds of version">
        <HelpList
          items={[
            [
              'Catalog version',
              'A frozen snapshot of what the platform knew about your data — sources, tables, profiles, relationships, documents and live KPIs — at one moment.',
            ],
            [
              'KPI version',
              'What your business meant by one metric. Managed on the KPIs tab, not here.',
            ],
          ]}
        />
      </HelpSection>
      <HelpSection heading="Change history">
        <p>
          Every governed action — a KPI approved, a scope changed, a member's role edited — is
          recorded with who did it and when. Entries are only ever added, never edited or removed,
          and credentials are stripped before anything is written.
        </p>
      </HelpSection>
      <HelpSection heading="Why it matters">
        <p>
          Publishing freezes a snapshot with a checksum, so an insight recorded months from now can
          still be reproduced after your database has moved on. Together with the change history, it
          makes questions like "who approved this KPI, when, and against which handbook version"
          answerable long after the fact.
        </p>
      </HelpSection>
    </SectionHelp>
  )
}
