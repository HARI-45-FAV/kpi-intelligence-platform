/** Activity: the audit trail and runtime telemetry, both already real in Sprint 1. */

import { useState } from 'react'
import { api } from '../api/client'
import type { AuditEntry, TelemetrySummary } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { formatDateTime, formatNumber, formatRelative } from '../components/format'
import { Alert, EmptyState, Metric, Panel, Spinner, StatusBadge } from '../components/ui'
import { useResource } from '../components/useResource'

export default function Activity() {
  const { companyId, can } = useAuth()
  const [tab, setTab] = useState<'audit' | 'telemetry'>('audit')

  const mayAudit = can('audit.read')
  const mayTelemetry = can('telemetry.read')

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Activity</h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Governance actions and runtime cost. Both are recorded from Sprint 1 onward so later
          sprints inherit accountability rather than adding it.
        </p>
      </div>

      <div className="flex gap-1 rounded-md border border-ink-700 bg-ink-850 p-1">
        {([
          ['audit', 'Audit trail'],
          ['telemetry', 'Telemetry'],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`rounded px-3 py-1.5 text-sm transition-colors ${
              tab === key
                ? 'bg-ink-700 font-medium text-slate-100'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'audit' &&
        (mayAudit ? (
          <AuditTrail companyId={companyId!} />
        ) : (
          <Alert tone="info">
            Your role does not include <code className="mono">audit.read</code>.
          </Alert>
        ))}

      {tab === 'telemetry' &&
        (mayTelemetry ? (
          <Telemetry companyId={companyId!} />
        ) : (
          <Alert tone="info">
            Your role does not include <code className="mono">telemetry.read</code>.
          </Alert>
        ))}
    </div>
  )
}

function AuditTrail({ companyId }: { companyId: string }) {
  const { data, loading, error } = useResource<AuditEntry[]>(
    () => api.get(`/companies/${companyId}/audit`, { query: { limit: 200 } }),
    [companyId],
  )

  if (loading && !data) return <Spinner />
  if (error) return <Alert>{error}</Alert>
  if (!data?.length) return <Panel><EmptyState title="No audit entries yet" /></Panel>

  return (
    <Panel title={`${data.length} entries`} bodyClassName="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-ink-700">
            <th className="table-head">When</th>
            <th className="table-head">Action</th>
            <th className="table-head">Resource</th>
            <th className="table-head">Actor</th>
            <th className="table-head">Version</th>
            <th className="table-head">Summary</th>
          </tr>
        </thead>
        <tbody>
          {data.map((entry) => (
            <tr key={entry.id} className="border-b border-ink-800 last:border-0">
              <td className="table-cell text-slate-500" title={formatDateTime(entry.occurred_at)}>
                {formatRelative(entry.occurred_at)}
              </td>
              <td className="table-cell">
                <code className="mono text-accent-soft">{entry.action}</code>
              </td>
              <td className="table-cell max-w-[16rem] truncate text-slate-300">
                {entry.resource_label ?? entry.resource_type}
              </td>
              <td className="table-cell text-slate-400">{entry.actor_email ?? 'system'}</td>
              <td className="table-cell text-slate-500">
                {entry.old_version || entry.new_version
                  ? `${entry.old_version || '—'} → ${entry.new_version || '—'}`
                  : '—'}
              </td>
              <td className="table-cell max-w-[28rem] truncate text-slate-400">
                {entry.outcome !== 'SUCCESS' && (
                  <StatusBadge status={entry.outcome === 'FAILURE' ? 'FAIL' : entry.outcome} />
                )}{' '}
                {entry.summary}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  )
}

function Telemetry({ companyId }: { companyId: string }) {
  const { data, loading, error } = useResource<TelemetrySummary>(
    () => api.get(`/companies/${companyId}/telemetry/summary`),
    [companyId],
  )

  if (loading && !data) return <Spinner />
  if (error) return <Alert>{error}</Alert>
  if (!data) return null

  return (
    <div className="space-y-5">
      <Panel title="Runtime">
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="Requests" value={formatNumber(data.requests)} hint={`${data.errors} errors`} />
          <Metric
            label="Avg latency"
            value={data.latency_ms.avg ? `${data.latency_ms.avg} ms` : '—'}
            hint={data.latency_ms.max ? `max ${data.latency_ms.max} ms` : undefined}
          />
          <Metric
            label="Source queries"
            value={formatNumber(data.connector.queries)}
            hint={`${formatNumber(data.connector.query_ms)} ms in-database`}
          />
          <Metric
            label="Rows returned"
            value={formatNumber(data.connector.rows_returned)}
            hint="aggregates, not table scans"
          />
        </div>
      </Panel>

      <Panel title="LLM versus non-LLM processing">
        <div className="grid gap-6 sm:grid-cols-4">
          <Metric label="Model calls" value={formatNumber(data.llm.calls)} tone="muted" />
          <Metric label="Prompt tokens" value={formatNumber(data.llm.prompt_tokens)} tone="muted" />
          <Metric
            label="Completion tokens"
            value={formatNumber(data.llm.completion_tokens)}
            tone="muted"
          />
          <Metric
            label="Estimated cost"
            value={`$${data.llm.estimated_cost_usd.toFixed(4)}`}
            tone="muted"
          />
        </div>

        <div className="mt-6 grid gap-5 border-t border-ink-800 pt-5 md:grid-cols-2">
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-emerald-400">
              Deterministic
            </div>
            <ul className="space-y-1">
              {data.processing_split.deterministic.map((item) => (
                <li key={item} className="flex gap-2 text-xs text-slate-400">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-emerald-600" />
                  {item}
                </li>
              ))}
            </ul>
          </div>
          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              Language model
            </div>
            {data.processing_split.llm.length === 0 ? (
              <p className="text-xs italic text-slate-600">Nothing. No model calls in Sprint 1.</p>
            ) : (
              <ul className="space-y-1">
                {data.processing_split.llm.map((item) => (
                  <li key={item} className="text-xs text-slate-400">
                    {item}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
              {data.processing_split.note}
            </p>
          </div>
        </div>
      </Panel>

      {data.by_service.length > 0 && (
        <Panel title="By service" bodyClassName="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-ink-700">
                <th className="table-head">Service</th>
                <th className="table-head">Requests</th>
                <th className="table-head">Avg ms</th>
                <th className="table-head">Max ms</th>
                <th className="table-head">Source queries</th>
              </tr>
            </thead>
            <tbody>
              {data.by_service.map((row) => (
                <tr key={row.service} className="border-b border-ink-800 last:border-0">
                  <td className="table-cell text-slate-200">{row.service}</td>
                  <td className="table-cell tabular-nums text-slate-400">
                    {formatNumber(row.requests)}
                  </td>
                  <td className="table-cell tabular-nums text-slate-400">{row.avg_ms ?? '—'}</td>
                  <td className="table-cell tabular-nums text-slate-400">{row.max_ms ?? '—'}</td>
                  <td className="table-cell tabular-nums text-slate-400">
                    {formatNumber(row.connector_queries)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  )
}
