/**
 * The KPI screen. It answers three questions, in this order:
 *
 *   1. What KPIs has the business defined?      -> the company's own registry
 *   2. Are they valid against the connected data? -> the nine governance checks
 *   3. What else could be tracked?              -> optional suggestions
 *
 * The company's definitions are the configuration of record: they are read
 * verbatim from the KPI-definition table in the connected source. Platform
 * suggestions are deliberately last and visually quieter — they supplement the
 * business's own meaning and never substitute for it.
 *
 * Selection lives in the URL (`?kpi=<id>`), not in component state, so adding a
 * KPI keeps you exactly where you were and a remount cannot lose your place.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, describeError } from '../../api/client'
import type {
  CompanyDefinitionImportResult,
  CompanyDefinitionsResponse,
  CompanyKpiDefinition,
  KpiDefinition,
  KpiDetail,
  KpiProposal,
  TableSummary,
  ValidationReport,
} from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime, formatNumber, titleCase } from '../../components/format'
import {
  Alert,
  DefinitionRow,
  Drawer,
  EmptyState,
  Field,
  HelpList,
  HelpSection,
  Modal,
  Panel,
  SectionHeader,
  SectionHelp,
  Spinner,
  StatCard,
  StatusBadge,
} from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'
import { useCopilot, useCopilotScreen } from '../../copilot/CopilotProvider'

const KPI_SELECTION_KEY = 'bi.ai.dashboard-kpis'

/**
 * The stored dashboard selection, or `null` when the administrator has never
 * made one.
 *
 * The distinction is the whole fix for the registry toggle. The stored value is
 * a list of KPI ids, and an empty list is a legitimate choice — "show none of
 * them". Collapsing "never chosen" and "chose none" into the same `[]` meant the
 * panel could not tell them apart, so it defaulted an empty selection back to
 * *every* KPI: switching the last one off silently switched them all on again,
 * and the OFF direction of the toggle was unreachable. Returning `null` for a
 * missing key keeps the default-to-all behaviour for a fresh workspace while
 * letting a deliberate empty selection stand.
 *
 * The stored format is unchanged — still a JSON array of ids — so the Dashboard
 * reads it exactly as before.
 */
function readSelectedKpis(): string[] | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(KPI_SELECTION_KEY)
    if (raw === null) return null
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : null
  } catch {
    return null
  }
}

function writeSelectedKpis(ids: string[]) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(KPI_SELECTION_KEY, JSON.stringify(ids))
  window.dispatchEvent(new CustomEvent('kpi-selection-updated', { detail: ids }))
}

export default function KpiRegistryPanel() {
  const { companyId } = useAuth()
  const base = `/companies/${companyId}`

  const registry = useResource<KpiDefinition[]>(() => api.get(`${base}/kpis`, { admin: true }), [companyId])
  const company = useResource<CompanyDefinitionsResponse>(
    () => api.get(`${base}/kpi-source-definitions`, { admin: true }),
    [companyId],
  )
  const proposals = useResource<{ proposals: KpiProposal[]; note: string }>(
    () => api.get(`${base}/kpi-proposals`, { admin: true }),
    [companyId],
  )
  const tables = useResource<TableSummary[]>(() => api.get(`${base}/tables`, { admin: true }), [companyId])

  // The open KPI is a URL concern, not component state. Two consequences that
  // matter: a remount (an auth refresh, a hot reload) reopens the same KPI, and
  // nothing in the add/import path needs to navigate to show its result.
  const [params, setParams] = useSearchParams()
  const openKpi = params.get('kpi')
  const setOpenKpi = useCallback(
    (kpiId: string | null) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current)
          if (kpiId) next.set('kpi', kpiId)
          else next.delete('kpi')
          return next
        },
        // Replace, never push: opening a KPI is not a navigation step, and Back
        // should leave the workspace rather than walk a stack of drawers.
        { replace: true },
      )
    },
    [setParams],
  )

  const [registerOpen, setRegisterOpen] = useState(false)
  // `null` until the administrator has chosen, which is what lets an empty
  // selection mean "none" instead of being reset to "all" on the next load.
  const [selectedKpis, setSelectedKpis] = useState<string[] | null>(() => readSelectedKpis())
  const [selectionSaved, setSelectionSaved] = useState(false)

  useEffect(() => {
    const valid = registry.data?.map((kpi) => kpi.id) ?? []
    if (!valid.length) return
    setSelectedKpis((current) => {
      // No stored choice yet: default to showing everything.
      if (current === null) return valid
      // Otherwise honour the choice, dropping ids that no longer exist. An
      // empty result stays empty — that is a deliberate "show none".
      const filtered = current.filter((id) => valid.includes(id))
      return filtered.length === current.length ? current : filtered
    })
  }, [registry.data])

  const saveSelection = useCallback(() => {
    const ordered = [...new Set(selectedKpis ?? [])]
    writeSelectedKpis(ordered)
    setSelectionSaved(true)
    window.setTimeout(() => setSelectionSaved(false), 1800)
  }, [selectedKpis])

  const reloadAll = useCallback(async () => {
    await Promise.all([registry.reload(), company.reload(), proposals.reload()])
  }, [registry.reload, company.reload, proposals.reload])

  const scopedTables = tables.data?.filter((t) => t.selected) ?? []
  const definitions = company.data?.definitions ?? []
  const counts = company.data?.counts
  const unregisteredProposals = proposals.data?.proposals.filter((p) => !p.already_registered) ?? []

  return (
    <div className="space-y-5">
      <SectionHeader title="KPIs" help={<KpisHelp />} />

      <CompanyDefinitionsPanel
        base={base}
        state={company}
        onImported={reloadAll}
        onOpenKpi={setOpenKpi}
      />

      <ValidationPanel
        base={base}
        registry={registry.data ?? []}
        loading={registry.loading && !registry.data}
        error={registry.error}
        definitionCount={counts?.total ?? 0}
        onOpenKpi={setOpenKpi}
        onRegister={() => setRegisterOpen(true)}
        canRegister={scopedTables.length > 0}
        onChanged={reloadAll}
        selectedKpis={selectedKpis ?? []}
        onToggleKpi={(kpiId) =>
          setSelectedKpis((current) => {
            const list = current ?? []
            return list.includes(kpiId)
              ? list.filter((id) => id !== kpiId)
              : [...list, kpiId]
          })
        }
        onSaveSelection={saveSelection}
        selectionSaved={selectionSaved}
      />

      <SuggestionsPanel
        base={base}
        proposals={unregisteredProposals}
        note={proposals.data?.note}
        loading={proposals.loading && !proposals.data}
        error={proposals.error}
        onRefresh={() => void proposals.reload()}
        companyDefinedCount={definitions.length}
        onAdded={reloadAll}
      />

      {openKpi && (
        <KpiDrawer
          base={base}
          kpiId={openKpi}
          onClose={() => setOpenKpi(null)}
          onChanged={reloadAll}
        />
      )}

      {registerOpen && (
        <RegisterKpiModal
          base={base}
          tables={scopedTables}
          onClose={() => setRegisterOpen(false)}
          onCreated={async (id) => {
            setRegisterOpen(false)
            await reloadAll()
            setOpenKpi(id)
          }}
        />
      )}
    </div>
  )
}

/* ------------------------------------------- 1. company-defined KPIs (primary) */

function KpisHelp() {
  return (
    <SectionHelp title="About KPIs and contracts">
      <HelpSection heading="What this section is">
        <p>
          The metrics your business has defined, and whether each one can be reliably calculated
          from the data you connected. A KPI only becomes available to the rest of the platform once
          it is live.
        </p>
      </HelpSection>
      <HelpSection heading="What you see on each card">
        <HelpList
          items={[
            ['KPI name', 'The metric as your business names it.'],
            [
              'Live / Not live',
              'Whether the platform will calculate and report this metric right now.',
            ],
            [
              'Matches the data',
              'The definition resolves against real columns in the tables you put in scope.',
            ],
            [
              'Awaiting checks',
              'Not yet verified against your data. Activate runs the checks and reports what failed.',
            ],
            ['Open contract', 'The full definition: calculation, lineage, versions and approvals.'],
            ['Show / Hide', 'Whether this KPI appears on the main dashboard.'],
          ]}
        />
      </HelpSection>
      <HelpSection heading="Why it matters">
        <p>
          A KPI here is a governed contract, not a saved query. It records what the business means
          by the metric, which columns produce it, who approved it and when — so a number reported
          months from now can still be explained and reproduced. The calculation is checked against
          your data before approval, which is what stops a plausible-looking but wrong figure from
          reaching a dashboard.
        </p>
      </HelpSection>
    </SectionHelp>
  )
}

function CompanyDefinitionsPanel({
  base,
  state,
  onImported,
  onOpenKpi,
}: {
  base: string
  state: ReturnType<typeof useResource<CompanyDefinitionsResponse>>
  onImported: () => Promise<void>
  onOpenKpi: (id: string) => void
}) {
  const importAll = useAction()
  const importOne = useAction()
  const [lastResult, setLastResult] = useState<CompanyDefinitionImportResult | null>(null)

  const data = state.data
  const definitions = data?.definitions ?? []
  const counts = data?.counts
  const table = data?.definition_table

  const runImport = async (keys: string[], action: ReturnType<typeof useAction>) => {
    const result = await action.run(() =>
      api.post<CompanyDefinitionImportResult>(
        `${base}/kpi-source-definitions/import`,
        { kpi_keys: keys },
        { admin: true },
      ),
    )
    if (!result) return
    setLastResult(result)
    // Reload in place. Nothing navigates: the imported KPIs simply appear in the
    // validation list below, and the page keeps its scroll position and context.
    await onImported()
  }

  return (
    <Panel
      title="Company-defined KPIs"
      actions={
        <button
          className="btn-primary btn-xs"
          disabled={importAll.pending || !counts?.importable}
          title={
            counts?.importable
              ? 'Register every company definition that binds to the connected data'
              : 'Nothing left to import'
          }
          onClick={() => void runImport([], importAll)}
        >
          {importAll.pending
            ? 'Importing…'
            : `Import ${counts?.importable ?? 0} into governance`}
        </button>
      }
      bodyClassName=""
    >
      {state.loading && !data ? (
        <div className="p-4">
          <Spinner />
        </div>
      ) : state.error ? (
        <div className="p-4">
          <Alert>{state.error}</Alert>
        </div>
      ) : !table ? (
        <EmptyState
          title="No KPI definition table found in the connected source"
          description="The platform looks for a table carrying both a metric-name column and a formula column — a KPI contract or semantic-contract table. Connect and discover the source that holds your KPI registry, or define KPIs by hand below."
        />
      ) : (
        <>
          <div className="grid gap-3 border-b border-ink-800 p-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Defined by the business" value={formatNumber(counts?.total ?? 0)} />
            <StatCard label="Active" value={formatNumber(counts?.active ?? 0)} />
            <StatCard
              label="Matches the data"
              value={formatNumber(counts?.resolved ?? 0)}
              tone={counts?.needs_mapping ? 'warn' : 'good'}
            />
            <StatCard
              label="Need attention"
              value={formatNumber(counts?.needs_mapping ?? 0)}
              tone={counts?.needs_mapping ? 'warn' : 'muted'}
            />
          </div>

          {importAll.error && (
            <div className="p-4">
              <Alert>{importAll.error}</Alert>
            </div>
          )}
          {lastResult && (
            <div className="p-4">
              <Alert
                tone={lastResult.counts.imported ? 'success' : 'info'}
                onDismiss={() => setLastResult(null)}
              >
                {lastResult.counts.imported} imported
                {lastResult.counts.skipped > 0 && `, ${lastResult.counts.skipped} skipped`}.
              </Alert>
            </div>
          )}

          {definitions.length === 0 ? (
            <EmptyState
              title="The KPI definition table is empty"
              description="No rows in the company registry carried both a name and a formula."
            />
          ) : (
            <div>
              {definitions.map((definition) => (
                <CompanyDefinitionRow
                  key={definition.kpi_key}
                  definition={definition}
                  pending={importOne.pending}
                  onImport={() => void runImport([definition.kpi_key], importOne)}
                  onOpen={onOpenKpi}
                />
              ))}
            </div>
          )}

          {importOne.error && (
            <div className="p-4">
              <Alert>{importOne.error}</Alert>
            </div>
          )}
        </>
      )}
    </Panel>
  )
}

function CompanyDefinitionRow({
  definition,
  pending,
  onImport,
  onOpen,
}: {
  definition: CompanyKpiDefinition
  pending: boolean
  onImport: () => void
  onOpen: (id: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const resolved = definition.resolution_status === 'RESOLVED'

  return (
    <div className="border-b border-ink-800 px-4 py-3 last:border-0 transition-colors hover:bg-white/45">
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-[10rem] flex-1 font-medium text-slate-100">{definition.name}</span>
        {!definition.is_active && <span className="chip text-slate-500">inactive in source</span>}
        <StatusBadge
          status={resolved ? 'ACTIVE' : 'WARNING'}
          label={resolved ? 'Matches the data' : 'Needs attention'}
        />

        {definition.already_registered && definition.registered_kpi_id ? (
          <button
            className="btn-ghost btn-xs"
            onClick={() => onOpen(definition.registered_kpi_id!)}
          >
            View contract
          </button>
        ) : (
          <button
            className="btn-primary btn-xs"
            disabled={pending || !definition.importable}
            title={
              definition.importable
                ? 'Register this company definition as a governed contract'
                : 'Resolve the issues below first'
            }
            onClick={onImport}
          >
            Add to governance
          </button>
        )}
        <button className="btn-ghost btn-xs" onClick={() => setExpanded((v) => !v)}>
          {expanded ? 'Hide' : 'Details'}
        </button>
      </div>

      {definition.issues.length > 0 && !expanded && (
        <p className="mt-1.5 text-[11px] text-amber-700">{definition.issues[0]}</p>
      )}

      {expanded && (
        <div className="mt-3 rounded-xl border border-white/85 bg-white/70 p-3">
          <p className="mb-2 text-xs leading-snug text-slate-400">
            {definition.business_definition}
          </p>
          <dl className="text-xs">
            <DefinitionRow term="Formula in source">
              <span className="mono">{definition.source_formula}</span>
            </DefinitionRow>
            <DefinitionRow term="Bound calculation">
              {definition.formula_expression ? (
                <span className="mono text-slate-100">{definition.formula_expression}</span>
              ) : (
                <span className="text-amber-300">not bound to catalog columns yet</span>
              )}
            </DefinitionRow>
            <DefinitionRow term="Source">
              <span className="mono">{definition.source_table ?? definition.declared_source ?? '—'}</span>
              {definition.time_field && (
                <span className="ml-2 text-slate-500">
                  time: <span className="mono">{definition.time_field}</span>
                </span>
              )}
            </DefinitionRow>
            <DefinitionRow term="Declared grain">
              {definition.declared_grain ?? '—'}
              <span className="mx-2 text-slate-600">·</span>
              resolved as {titleCase(definition.time_grain)}
            </DefinitionRow>
            {definition.owner && (
              <DefinitionRow term="Owner">{definition.owner}</DefinitionRow>
            )}
            {definition.dimensions.length > 0 && (
              <DefinitionRow term="Dimensions">
                <div className="flex flex-wrap gap-1">
                  {definition.dimensions.map((d) => (
                    <span key={d.dimension_name} className="chip">
                      {d.dimension_name}
                    </span>
                  ))}
                </div>
              </DefinitionRow>
            )}
            {definition.materiality_threshold_pct !== null &&
              definition.materiality_threshold_pct !== undefined && (
                <DefinitionRow term="Material change">
                  {definition.materiality_threshold_pct}%
                </DefinitionRow>
              )}
          </dl>

          {definition.issues.length > 0 && (
            <div className="mt-3">
              <Alert tone="warn">
                <ul className="space-y-0.5 text-xs">
                  {definition.issues.map((issue, index) => (
                    <li key={index}>· {issue}</li>
                  ))}
                </ul>
              </Alert>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------- 2. validation status */

/* --------------------------------------------------- 2. validation & approval */

/**
 * Take a KPI live in one step.
 *
 * The governed path is validate → approve → activate, and approval is refused
 * until validation passes. Exposing that as three separate clicks in a drawer
 * footer meant nobody could find it, so this runs the whole path and reports the
 * one thing that matters: is it live, and if not, why not.
 */
async function activateVersion(base: string, versionId: string): Promise<string | null> {
  const report = await api.post<ValidationReport>(
    `${base}/kpi-versions/${versionId}/validate`,
    {},
    { admin: true },
  )
  if (!report.ready_for_approval) {
    const blocking = report.checks?.filter((check) => check.status === 'FAIL') ?? []
    const detail = blocking.map((check) => check.message || check.label).filter(Boolean).join('; ')
    return detail || report.summary || 'This KPI did not pass its data checks.'
  }
  await api.post(`${base}/kpi-versions/${versionId}/approve`, {}, { admin: true })
  return null
}

function ValidationPanel({
  base,
  registry,
  loading,
  error,
  definitionCount,
  onOpenKpi,
  onRegister,
  canRegister,
  selectedKpis,
  onToggleKpi,
  onSaveSelection,
  selectionSaved,
  onChanged,
}: {
  base: string
  registry: KpiDefinition[]
  loading: boolean
  error: string | null
  definitionCount: number
  onOpenKpi: (id: string) => void
  onRegister: () => void
  canRegister: boolean
  selectedKpis: string[]
  onToggleKpi: (id: string) => void
  onSaveSelection: () => void
  selectionSaved: boolean
  onChanged: () => Promise<void>
}) {
  const activation = useAction()
  const [busyKpi, setBusyKpi] = useState<string | null>(null)
  const [blocked, setBlocked] = useState<Record<string, string>>({})

  const liveVersion = (kpi: KpiDefinition) =>
    kpi.versions.find((v) => v.status === 'ACTIVE') ?? kpi.versions[kpi.versions.length - 1]

  const pending = registry.filter((kpi) => liveVersion(kpi)?.status !== 'ACTIVE')
  const liveCount = registry.length - pending.length

  const takeLive = async (kpis: KpiDefinition[]) => {
    const failures: Record<string, string> = {}
    await activation.run(async () => {
      for (const kpi of kpis) {
        const version = liveVersion(kpi)
        if (!version || version.status === 'ACTIVE') continue
        setBusyKpi(kpi.id)
        try {
          const reason = await activateVersion(base, version.id)
          if (reason) failures[kpi.id] = reason
        } catch (err) {
          failures[kpi.id] = describeError(err)
        }
      }
      setBusyKpi(null)
      const live = kpis.length - Object.keys(failures).length
      if (Object.keys(failures).length) {
        throw new Error(
          `${live} of ${kpis.length} went live. ${Object.keys(failures).length} could not be activated — see the notes below.`,
        )
      }
      return live
    }, kpis.length === 1 ? 'Now live on the dashboard.' : `${kpis.length} KPIs are now live on the dashboard.`)
    setBlocked(failures)
    setBusyKpi(null)
    await onChanged()
  }

  return (
    <Panel
      title={`KPI registry — ${liveCount} of ${registry.length} live`}
      actions={
        <div className="flex items-center gap-2">
          {pending.length > 0 && (
            <button
              className="btn-primary btn-xs"
              onClick={() => takeLive(pending)}
              disabled={activation.pending}
            >
              {activation.pending ? 'Activating…' : `Activate ${pending.length}`}
            </button>
          )}
          <button
            className="btn-ghost btn-xs"
            onClick={onSaveSelection}
            disabled={!registry.length}
          >
            {selectionSaved ? 'Saved' : 'Save dashboard choice'}
          </button>
          <button
            className="btn-ghost btn-xs"
            onClick={onRegister}
            disabled={!canRegister}
            title={canRegister ? undefined : 'Add a table to the data scope first'}
          >
            + Add a KPI
          </button>
        </div>
      }
      bodyClassName=""
    >
      {selectionSaved && (
        <div className="border-b border-ink-800 px-4 py-2">
          <Alert tone="success">Dashboard selection updated.</Alert>
        </div>
      )}
      {activation.message && (
        <div className="border-b border-ink-800 px-4 py-2">
          <Alert tone="success" onDismiss={activation.reset}>
            {activation.message}
          </Alert>
        </div>
      )}
      {activation.error && (
        <div className="border-b border-ink-800 px-4 py-2">
          <Alert onDismiss={activation.reset}>{activation.error}</Alert>
        </div>
      )}
      {loading ? (
        <div className="p-4">
          <Spinner />
        </div>
      ) : error ? (
        <div className="p-4">
          <Alert>{error}</Alert>
        </div>
      ) : !registry.length ? (
        <EmptyState
          title="No KPIs yet"
          description={
            definitionCount > 0
              ? 'Import your business KPIs above. Each one is checked against your data before it can go live.'
              : 'Import your business KPIs above, add one by hand, or take a suggestion below.'
          }
        />
      ) : (
        <div className="grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-3">
          {registry.map((kpi) => {
            const live = liveVersion(kpi)
            const isLive = live?.status === 'ACTIVE'
            const selected = selectedKpis.includes(kpi.id)
            const blockedReason = blocked[kpi.id]
            return (
              <div
                key={kpi.id}
                className={`surface-card surface-card-lift flex flex-col p-4 ${
                  selected ? 'border-accent/50' : ''
                }`}
              >
                {/* Name and contract status only. The formula, lineage, grain and
                    the nine validation checks are all in the contract drawer. */}
                <div className="flex items-start justify-between gap-2">
                  <button
                    type="button"
                    data-bare
                    className="min-w-0 flex-1 text-left"
                    onClick={() => onOpenKpi(kpi.id)}
                  >
                    <div className="truncate text-[15px] font-semibold text-slate-100">
                      {kpi.name}
                    </div>
                  </button>
                  <StatusBadge
                    status={isLive ? 'ACTIVE' : 'DRAFT'}
                    label={isLive ? 'Live' : 'Not live'}
                  />
                </div>

                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <StatusBadge
                    status={isLive ? 'GOOD' : 'UNDER_REVIEW'}
                    label={isLive ? 'Matches the data' : 'Awaiting checks'}
                  />
                  <span className="text-[11px] text-slate-500">
                    {selected ? 'On dashboard' : 'Hidden'}
                  </span>
                </div>

                {blockedReason && (
                  <p className="mt-2.5 rounded-lg border border-amber-200 bg-amber-50/80 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700">
                    {blockedReason}
                  </p>
                )}

                <div className="mt-3.5 flex flex-wrap items-center gap-2 border-t border-ink-800 pt-3">
                  {!isLive && (
                    <button
                      type="button"
                      className="btn-primary btn-xs"
                      disabled={activation.pending}
                      onClick={() => takeLive([kpi])}
                    >
                      {busyKpi === kpi.id ? 'Activating…' : 'Activate'}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn-ghost btn-xs"
                    onClick={() => onOpenKpi(kpi.id)}
                  >
                    Open contract
                  </button>
                  <button
                    type="button"
                    className="btn-ghost btn-xs ml-auto"
                    aria-pressed={selected}
                    title={
                      selected ? 'Remove from the dashboard' : 'Show this KPI on the dashboard'
                    }
                    onClick={() => onToggleKpi(kpi.id)}
                  >
                    {selected ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Panel>
  )
}

/* -------------------------------------------- 3. optional suggestions (last) */

function SuggestionsPanel({
  base,
  proposals,
  note,
  loading,
  error,
  onRefresh,
  companyDefinedCount,
  onAdded,
}: {
  base: string
  proposals: KpiProposal[]
  note?: string
  loading: boolean
  error: string | null
  onRefresh: () => void
  companyDefinedCount: number
  onAdded: () => Promise<void>
}) {
  // Null means "follow the data": collapsed once the business has its own
  // definitions, expanded when it has none and suggestions are the only route in.
  // Held as an override rather than a `useState` initial value, which would
  // otherwise freeze whatever was true before the first response arrived.
  const [override, setOverride] = useState<boolean | null>(null)
  const open = override ?? companyDefinedCount === 0

  return (
    <section className="rounded-md border border-dashed border-ink-700 bg-ink-900/40">
      <header className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
        <button
          className="flex items-center gap-2 text-left"
          onClick={() => setOverride(!open)}
          aria-expanded={open}
        >
          <span className="text-slate-500">{open ? '▾' : '▸'}</span>
          <span className="text-sm font-medium text-slate-300">
            Optional additional KPI suggestions
          </span>
          <span className="chip">{proposals.length}</span>
        </button>
        {open && (
          <button className="btn-ghost btn-xs" onClick={onRefresh}>
            Refresh
          </button>
        )}
      </header>

      {open && (
        <div className="border-t border-ink-800">
          <p className="px-4 py-3 text-xs leading-relaxed text-slate-500">
            {note ??
              'Deterministic candidates derived from column profiles. Accepting one is your decision, not the platform’s.'}
          </p>
          {loading ? (
            <div className="p-4">
              <Spinner />
            </div>
          ) : error ? (
            <div className="p-4">
              <Alert>{error}</Alert>
            </div>
          ) : proposals.length === 0 ? (
            <p className="px-4 pb-4 text-xs text-slate-600">
              No further candidates. Profile more tables to widen the search.
            </p>
          ) : (
            <div className="border-t border-ink-800">
              {proposals.map((proposal) => (
                <ProposalRow
                  key={proposal.kpi_key}
                  base={base}
                  proposal={proposal}
                  onAccepted={onAdded}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function ProposalRow({
  base,
  proposal,
  onAccepted,
}: {
  base: string
  proposal: KpiProposal
  onAccepted: () => Promise<void>
}) {
  const [expanded, setExpanded] = useState(false)
  const [definition, setDefinition] = useState(proposal.business_definition)
  const accept = useAction()

  return (
    <div className="border-b border-ink-800 px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-[9rem] font-medium text-slate-100">{proposal.name}</span>
        <span className="mono flex-1 truncate text-slate-400">{proposal.formula_expression}</span>
        <span className="chip">{proposal.kind === 'RATIO' ? 'ratio' : 'simple'}</span>
        <button className="btn-ghost btn-xs" onClick={() => setExpanded((v) => !v)}>
          {expanded ? 'Hide' : 'Review'}
        </button>
        <button
          className="btn-ghost btn-xs"
          disabled={accept.pending}
          onClick={async () => {
            const created = await accept.run(() =>
              api.post<KpiDefinition>(
                `${base}/kpi-proposals/accept`,
                {
                  kpi_key: proposal.kpi_key,
                  overrides:
                    definition !== proposal.business_definition
                      ? { business_definition: definition }
                      : {},
                },
                { admin: true },
              ),
            )
            // Refresh the lists and stay put. The accepted KPI appears under
            // Validation; nothing opens over the page and nothing navigates.
            if (created) await onAccepted()
          }}
        >
          {accept.pending ? 'Adding…' : 'Accept suggestion'}
        </button>
      </div>

      {accept.error && (
        <div className="mt-3">
          <Alert>{accept.error}</Alert>
        </div>
      )}

      {expanded && (
        <div className="mt-3 space-y-3 rounded-md border border-ink-700 bg-ink-850 p-3">
          <Field
            label="Business definition"
            hint="The platform proposes; you own the meaning. Edit before accepting if this is not how your business defines it."
          >
            <textarea
              className="field min-h-[3.5rem] resize-y"
              value={definition}
              onChange={(e) => setDefinition(e.target.value)}
            />
          </Field>

          <dl className="text-xs">
            <DefinitionRow term="Source">
              <span className="mono">{proposal.source_table}</span>
              {proposal.time_field && (
                <span className="ml-2 text-slate-500">
                  time: <span className="mono">{proposal.time_field}</span>
                </span>
              )}
            </DefinitionRow>
            <DefinitionRow term="Dimensions">
              <div className="flex flex-wrap gap-1">
                {proposal.dimensions.map((d) => (
                  <span key={d.dimension_name} className="chip">
                    {d.dimension_name}
                    {d.approx_cardinality ? ` (${formatNumber(d.approx_cardinality)})` : ''}
                  </span>
                ))}
              </div>
            </DefinitionRow>
            <DefinitionRow term="Candidate drivers">
              <div className="flex flex-wrap gap-1">
                {proposal.drivers.length ? (
                  proposal.drivers.map((d) => (
                    <span key={d.driver_name} className="chip">
                      {d.driver_name}
                    </span>
                  ))
                ) : (
                  <span className="text-slate-600">none detected</span>
                )}
              </div>
            </DefinitionRow>
            <DefinitionRow term="Why proposed">
              <span className="text-slate-400">
                {proposal.evidence?.reason ?? proposal.rationale ?? '—'}
              </span>
              <span className="ml-2 chip" title="Deterministic score from column profiles, naming and grain">
                {(proposal.confidence * 100).toFixed(0)}% match
              </span>
            </DefinitionRow>
          </dl>

          {proposal.warnings.length > 0 && (
            <Alert tone="warn">
              <ul className="space-y-0.5 text-xs">
                {proposal.warnings.map((warning, index) => (
                  <li key={index}>· {warning}</li>
                ))}
              </ul>
            </Alert>
          )}
        </div>
      )}
    </div>
  )
}

/* --------------------------------------------------------------- KPI drawer */

function KpiDrawer({
  base,
  kpiId,
  onClose,
  onChanged,
}: {
  base: string
  kpiId: string
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const [versionNumber, setVersionNumber] = useState<number | undefined>(undefined)
  const detail = useResource<KpiDetail>(
    () =>
      api.get(`${base}/kpis/${kpiId}`, {
        admin: true,
        query: versionNumber ? { version: versionNumber } : undefined,
      }),
    [kpiId, versionNumber],
  )
  const validate = useAction()
  const lifecycle = useAction()
  const [reason, setReason] = useState('')

  const contract = detail.data?.version
  const validation = detail.data?.validation
  const readyToApprove = validation?.ready_for_approval ?? false
  // Older registered contracts can legitimately predate optional governance
  // fields. Treat absent collections as empty so opening one never blanks the
  // whole KPI workspace while its data is being upgraded.
  const dimensions = contract?.dimensions ?? []
  const drivers = contract?.drivers ?? []
  const accessPolicies = contract?.access_policies ?? []
  const lineage = contract?.lineage ?? []
  const validationChecks = validation?.checks ?? []
  const versions = detail.data?.definition.versions ?? []

  // The Copilot inherits the exact version on screen, so "explain this" means
  // this contract rather than whichever version happens to be live. It explains;
  // it cannot validate, approve, deprecate or edit — those are the buttons below,
  // and they stay human.
  const { openPanel } = useCopilot()
  useCopilotScreen({
    panel: 'kpi_setup',
    kpiId,
    kpiVersion: contract?.version ?? null,
    label: detail.data?.definition.name ?? null,
  })

  const refresh = async () => {
    await detail.reload()
    await onChanged()
  }

  return (
    <Drawer
      open
      onClose={onClose}
      width="max-w-4xl"
      title={
        <span className="flex items-center gap-2">
          {detail.data?.definition.name ?? 'KPI'}
          {contract && <StatusBadge status={contract.status} />}
          {contract && <span className="chip">v{contract.version}</span>}
        </span>
      }
      subtitle={contract ? <span className="mono">{contract.formula}</span> : undefined}
      footer={
        contract && (
          <>
            <button
              className="btn-ghost btn-xs"
              disabled={validate.pending}
              onClick={async () => {
                const report = await validate.run(() =>
                  api.post<ValidationReport>(
                    `${base}/kpi-versions/${contract.kpi_version_id}/validate`,
                    {},
                    { admin: true },
                  ),
                )
                if (report) await refresh()
              }}
            >
              {validate.pending ? 'Validating…' : 'Validate'}
            </button>

            {['DRAFT', 'PROPOSED', 'UNDER_REVIEW'].includes(contract.status) && (
              <button
                className="btn-primary btn-xs"
                disabled={lifecycle.pending || !readyToApprove}
                title={
                  readyToApprove
                    ? 'Approve and make this version live'
                    : 'Validation must pass before approval'
                }
                onClick={async () => {
                  const ok = await lifecycle.run(
                    () =>
                      api.post(
                        `${base}/kpi-versions/${contract.kpi_version_id}/approve`,
                        { reason: reason || undefined },
                        { admin: true },
                      ),
                    'Approved and activated.',
                  )
                  if (ok) await refresh()
                }}
              >
                {lifecycle.pending ? 'Working…' : 'Approve & activate'}
              </button>
            )}

            {contract.status === 'ACTIVE' && (
              <button
                className="btn-danger btn-xs"
                disabled={lifecycle.pending}
                onClick={async () => {
                  const ok = await lifecycle.run(
                    () =>
                      api.post(
                        `${base}/kpi-versions/${contract.kpi_version_id}/deprecate`,
                        { reason: reason || undefined },
                        { admin: true },
                      ),
                    'Deprecated.',
                  )
                  if (ok) await refresh()
                }}
              >
                Deprecate
              </button>
            )}
          </>
        )
      }
    >
      {detail.loading && !detail.data ? (
        <Spinner />
      ) : detail.error ? (
        <Alert>{detail.error}</Alert>
      ) : contract ? (
        <div className="space-y-6">
          {validate.error && <Alert>{validate.error}</Alert>}
          {lifecycle.error && <Alert>{lifecycle.error}</Alert>}
          {lifecycle.message && (
            <Alert tone="success" onDismiss={lifecycle.reset}>
              {lifecycle.message}
            </Alert>
          )}

          <section>
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="panel-title">Contract</h3>
              <button
                className="btn-ghost btn-xs"
                onClick={() =>
                  openPanel(
                    `Explain the ${detail.data?.definition.name ?? 'KPI'} contract at version ` +
                      `${contract.version}: what it measures, how it is calculated, its lineage, ` +
                      `and what its validation found.`,
                  )
                }
              >
                ✨ Explain with Copilot
              </button>
            </div>
            <dl>
              <DefinitionRow term="Business definition">
                {contract.business_definition}
              </DefinitionRow>
              <DefinitionRow term="Calculation">
                <span className="mono text-slate-100">{contract.formula}</span>
              </DefinitionRow>
              <DefinitionRow term="Additivity">
                {contract.is_additive ? (
                  <span className="text-emerald-300">Additive</span>
                ) : (
                  <span className="text-amber-300">Not additive</span>
                )}
                <span className="ml-2 text-xs text-slate-500">{contract.additivity_note}</span>
              </DefinitionRow>
              <DefinitionRow term="Source">
                <span className="mono">
                  {contract.source?.schema}.{contract.source?.table}
                </span>
                {contract.source?.data_source && (
                  <span className="ml-2 text-xs text-slate-500">
                    via {contract.source.data_source}
                  </span>
                )}
              </DefinitionRow>
              <DefinitionRow term="Time">
                <span className="mono">{contract.time_field ?? '—'}</span>
                <span className="mx-2 text-slate-600">·</span>
                {titleCase(contract.time_grain)}
                {contract.calendar?.name && (
                  <>
                    <span className="mx-2 text-slate-600">·</span>
                    {contract.calendar.name}
                  </>
                )}
                {contract.timezone && (
                  <>
                    <span className="mx-2 text-slate-600">·</span>
                    {contract.timezone}
                  </>
                )}
              </DefinitionRow>
              <DefinitionRow term="Dimensions">
                <div className="flex flex-wrap gap-1">
                  {dimensions.map((d, index) => (
                    <span key={`${d.dimension_name}-${d.source_column}-${index}`} className="chip" title={d.monitoring_note}>
                      {d.dimension_name}
                    </span>
                  ))}
                </div>
                {dimensions.length > 0 && (
                  <p className="mt-1.5 text-[11px] leading-snug text-slate-600">
                    A declared dimension authorises a breakdown. It does not schedule per-entity
                    monitoring.
                  </p>
                )}
              </DefinitionRow>
              <DefinitionRow term="Drivers">
                <div className="flex flex-wrap gap-1">
                  {drivers.length ? (
                    drivers.map((d, index) => (
                      <span
                        key={`${d.driver_name}-${d.source_column ?? ''}-${index}`}
                        className="chip"
                        title={d.controllable ? 'Controllable lever' : 'Observed factor'}
                      >
                        {d.controllable && '⚙ '}
                        {d.driver_name}
                      </span>
                    ))
                  ) : (
                    <span className="text-slate-600">none</span>
                  )}
                </div>
              </DefinitionRow>
              {contract.materiality && (
                <DefinitionRow term="Materiality">
                  Relative {contract.materiality.relative_threshold_pct ?? '—'}%
                  <span className="mx-2 text-slate-600">·</span>
                  Absolute {formatNumber(contract.materiality.absolute_threshold)}
                  <span className="mx-2 text-slate-600">·</span>
                  {titleCase(contract.materiality.business_criticality)}
                  <p className="mt-1 text-[11px] text-slate-600">
                    Stored for Sprint 2. No monitoring runs against it yet.
                  </p>
                </DefinitionRow>
              )}
              <DefinitionRow term="Access">
                <div className="flex flex-wrap gap-1">
                  {accessPolicies.map((policy, index) => (
                    <span
                      key={`${policy.role_key}-${index}`}
                      className={`chip ${policy.allowed ? '' : 'text-slate-600 line-through'}`}
                    >
                      {policy.role_key}
                      {Object.keys(policy.row_scope ?? {}).length > 0 && ' (scoped)'}
                      {policy.aggregate_only && ' · aggregate only'}
                    </span>
                  ))}
                </div>
              </DefinitionRow>
              <DefinitionRow term="Lineage">
                <ul className="space-y-0.5">
                  {lineage.map((item, index) => (
                    <li key={`${item.role}-${item.table ?? ''}-${item.column ?? ''}-${index}`} className="text-xs">
                      <span className="inline-block w-24 text-slate-500">{item.role}</span>
                      <span className="mono text-slate-300">
                        {item.table}
                        {item.column ? `.${item.column}` : ''}
                      </span>
                      {item.transformation && (
                        <span className="ml-2 text-slate-600">{item.transformation}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </DefinitionRow>
            </dl>
          </section>

          <section>
            <h3 className="panel-title mb-2">Validation</h3>
            {!validation || !validationChecks.length ? (
              <Alert tone="info">
                Not yet validated. Nine governance checks must run — including executing the KPI
                against the source — before this version can be approved.
              </Alert>
            ) : (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-3">
                  <StatusBadge status={validation.overall_status ?? undefined} />
                  <span className="text-sm text-slate-300">{validation.summary}</span>
                  <span className="text-[11px] text-slate-600">
                    {validation.passed ?? 0} passed · {validation.warned ?? 0} warned ·{' '}
                    {validation.failed ?? 0} failed
                    {validation.duration_ms ? ` · ${validation.duration_ms} ms` : ''}
                  </span>
                </div>

                <ul className="divide-y divide-ink-800 rounded-md border border-ink-800">
                  {validationChecks.map((check, index) => (
                    <li key={`${check.test_type}-${index}`} className="flex items-start gap-3 px-3 py-2">
                      <span
                        className={
                          check.status === 'PASS'
                            ? 'text-emerald-400'
                            : check.status === 'WARN'
                              ? 'text-amber-400'
                              : check.status === 'FAIL'
                                ? 'text-rose-400'
                                : 'text-slate-600'
                        }
                      >
                        {check.status === 'PASS' ? '✓' : check.status === 'FAIL' ? '✕' : '!'}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-baseline gap-2">
                          <span className="text-sm text-slate-200">{check.label}</span>
                          {!check.is_blocking && (
                            <span className="text-[10px] uppercase tracking-wider text-slate-600">
                              advisory
                            </span>
                          )}
                        </div>
                        {check.message && (
                          <p className="mt-0.5 text-xs leading-snug text-slate-500">
                            {check.message}
                          </p>
                        )}
                        {check.evidence?.sql && (
                          <pre className="mono mt-1.5 overflow-x-auto rounded bg-ink-950 px-2 py-1.5 text-[11px] text-slate-500">
                            {String(check.evidence.sql)}
                          </pre>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          {['DRAFT', 'PROPOSED', 'UNDER_REVIEW', 'ACTIVE'].includes(contract.status) && (
            <section>
              <Field
                label="Decision note"
                hint="Recorded in the audit trail with your identity and the timestamp."
              >
                <input
                  className="field"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Matches KPI Handbook v2; Finance signed off."
                />
              </Field>
            </section>
          )}

          <section>
            <h3 className="panel-title mb-2">Version history</h3>
            <ul className="divide-y divide-ink-800 rounded-md border border-ink-800">
              {versions.map((version) => (
                <li
                  key={version.id}
                  className={`flex flex-wrap items-center gap-3 px-3 py-2 ${
                    version.version === contract.version ? 'bg-ink-850' : ''
                  }`}
                >
                  <button
                    className="text-sm font-medium text-accent-soft hover:underline"
                    onClick={() => setVersionNumber(version.version)}
                  >
                    v{version.version}
                  </button>
                  <StatusBadge status={version.status} />
                  <span className="mono flex-1 truncate text-xs text-slate-500">
                    {version.formula_expression}
                  </span>
                  <span className="chip">{titleCase(version.proposal_origin)}</span>
                  <span className="text-[11px] text-slate-600">
                    {version.approved_at
                      ? `approved ${formatDateTime(version.approved_at)}`
                      : `created ${formatDateTime(version.created_at)}`}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
              Editing an active KPI creates the next version in DRAFT; the live version keeps serving
              until the new one is approved. Insights recorded earlier stay bound to the version that
              produced them.
            </p>
          </section>

          <section className="rounded-md border border-ink-700 bg-ink-850 p-3">
            <div className="panel-title mb-1">Governance</div>
            <dl className="text-xs">
              <DefinitionRow term="Created by">
                {contract.governance?.created_by ?? '—'}
              </DefinitionRow>
              <DefinitionRow term="Approved by">
                {contract.governance?.approved_by ?? 'not yet approved'}
                {contract.governance?.approved_at && (
                  <span className="ml-2 text-slate-500">
                    {formatDateTime(contract.governance.approved_at)}
                  </span>
                )}
              </DefinitionRow>
              {contract.governance?.definition_source && (
                <DefinitionRow term="Definition source">
                  {contract.governance.definition_source}
                  {contract.governance.definition_document_version && (
                    <span className="ml-2 chip">
                      doc v{contract.governance.definition_document_version}
                    </span>
                  )}
                  <p className="mt-1 text-[11px] text-slate-600">
                    A reference document supports the definition. It is governance evidence, never
                    the quantitative source.
                  </p>
                </DefinitionRow>
              )}
              <DefinitionRow term="Origin">
                {contract.governance?.proposal_origin === 'COMPANY'
                  ? 'Company KPI registry'
                  : titleCase(contract.governance?.proposal_origin ?? 'MANUAL')}
              </DefinitionRow>
            </dl>
          </section>
        </div>
      ) : null}
    </Drawer>
  )
}

/* ------------------------------------------------------- manual registration */

const AGGREGATIONS = ['SUM', 'COUNT', 'COUNT(DISTINCT …)', 'AVG', 'MIN', 'MAX']

function RegisterKpiModal({
  base,
  tables,
  onClose,
  onCreated,
}: {
  base: string
  tables: TableSummary[]
  onClose: () => void
  onCreated: (kpiId: string) => Promise<void>
}) {
  const create = useAction()
  const [form, setForm] = useState({
    name: '',
    business_definition: '',
    formula_expression: '',
    source_table_id: tables[0]?.id ?? '',
    time_field: '',
    time_grain: 'DAY',
    unit: '',
  })
  const [dimensions, setDimensions] = useState('')
  const [drivers, setDrivers] = useState('')

  const table = useMemo(
    () => tables.find((t) => t.id === form.source_table_id),
    [tables, form.source_table_id],
  )
  const columns = useResource<Array<{ column_name: string; semantic_type: string }>>(
    () => api.get(`${base}/tables/${form.source_table_id}/columns`, { admin: true }),
    [form.source_table_id],
    { enabled: Boolean(form.source_table_id) },
  )

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const timeCandidates =
    columns.data?.filter((c) => ['DATE', 'TIMESTAMP'].includes(c.semantic_type)) ?? []
  const dimensionCandidates =
    columns.data?.filter((c) => ['CATEGORICAL', 'BOOLEAN_FLAG'].includes(c.semantic_type)) ?? []

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const created = await create.run(() =>
      api.post<KpiDefinition>(
        `${base}/kpis`,
        {
          ...form,
          time_field: form.time_field || null,
          unit: form.unit || null,
          dimensions: dimensions
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean)
            .map((column) => ({ dimension_name: column, source_column: column })),
          drivers: drivers
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean)
            .map((name) => ({ driver_name: name })),
        },
        { admin: true },
      ),
    )
    if (created) await onCreated(created.id)
  }

  return (
    <Modal open onClose={onClose} title="Define a KPI by hand" width="max-w-2xl">
      <form onSubmit={submit} className="space-y-4">
        {create.error && <Alert>{create.error}</Alert>}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="KPI name" required>
            <input
              className="field"
              value={form.name}
              onChange={(e) => set('name', e.target.value)}
              placeholder="Revenue"
              required
            />
          </Field>
          <Field label="Unit" hint="currency, count, ratio, percent…">
            <input
              className="field"
              value={form.unit}
              onChange={(e) => set('unit', e.target.value)}
              placeholder="currency"
            />
          </Field>
        </div>

        <Field label="Business definition" required hint="What this number means to the business.">
          <textarea
            className="field min-h-[3.5rem] resize-y"
            value={form.business_definition}
            onChange={(e) => set('business_definition', e.target.value)}
            placeholder="Total recognised sales revenue across all orders."
            required
          />
        </Field>

        <Field label="Source table" required>
          <select
            className="field"
            value={form.source_table_id}
            onChange={(e) => set('source_table_id', e.target.value)}
            required
          >
            {tables.map((t) => (
              <option key={t.id} value={t.id}>
                {t.table_name} ({t.data_source_name})
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Formula"
          required
          hint={
            <>
              Accepted forms: <code className="mono">{AGGREGATIONS.join(', ')}</code> and a single
              division of two aggregates. The formula is parsed into a governed contract, not
              executed as free-text SQL — which is what makes validation and lineage possible.
            </>
          }
        >
          <input
            className="field mono"
            value={form.formula_expression}
            onChange={(e) => set('formula_expression', e.target.value)}
            placeholder="SUM(order_value)  ·  COUNT(DISTINCT order_id)  ·  SUM(order_value) / COUNT(DISTINCT order_id)"
            required
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Time field" hint="Column that places a row in time.">
            <select
              className="field"
              value={form.time_field}
              onChange={(e) => set('time_field', e.target.value)}
            >
              <option value="">— none —</option>
              {timeCandidates.map((c) => (
                <option key={c.column_name} value={c.column_name}>
                  {c.column_name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Time grain">
            <select
              className="field"
              value={form.time_grain}
              onChange={(e) => set('time_grain', e.target.value)}
            >
              {['HOUR', 'DAY', 'WEEK', 'MONTH', 'QUARTER', 'YEAR'].map((grain) => (
                <option key={grain} value={grain}>
                  {titleCase(grain)}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field
          label="Dimensions"
          hint={
            dimensionCandidates.length
              ? `Comma-separated column names. Detected in ${table?.table_name}: ${dimensionCandidates
                  .map((c) => c.column_name)
                  .join(', ')}`
              : 'Comma-separated column names.'
          }
        >
          <input
            className="field mono"
            value={dimensions}
            onChange={(e) => setDimensions(e.target.value)}
            placeholder="region, channel"
          />
        </Field>

        <Field label="Drivers" hint="Candidate explanatory factors, for the later investigation engine.">
          <input
            className="field"
            value={drivers}
            onChange={(e) => setDrivers(e.target.value)}
            placeholder="volume, price, mix, marketing"
          />
        </Field>

        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className="btn-ghost btn-xs" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary btn-xs" disabled={create.pending}>
            {create.pending ? 'Creating…' : 'Create draft'}
          </button>
        </div>

        <p className="border-t border-ink-800 pt-3 text-[11px] leading-relaxed text-slate-600">
          The KPI is created in DRAFT. It must pass all nine validation checks and receive explicit
          approval before it becomes ACTIVE.
        </p>
      </form>
    </Modal>
  )
}
