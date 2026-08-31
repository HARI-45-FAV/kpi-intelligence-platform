/**
 * The protected governance workspace.
 *
 * Everything from the Sprint 1 spec that configures the company lives behind one
 * re-authentication gate: company profile, data sources, data scope, profiling,
 * documents, the KPI registry, access rules and history. The gate is a real
 * credential check against /auth/admin-unlock, not a hidden route — an
 * unattended tab does not leave the KPI contracts editable.
 */

import { useState } from 'react'
import { Navigate, NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/AuthContext'
import { Alert, Field, Panel, PasswordInput } from '../../components/ui'
import { useAction } from '../../components/useResource'
import CompanyPanel from './CompanyPanel'
import SourcesPanel from './SourcesPanel'
import SourceGovernance from './SourceGovernance'
import DocumentsPanel from './DocumentsPanel'
import KpiRegistryPanel from './KpiRegistryPanel'
import ComparisonPolicyPanel from './ComparisonPolicyPanel'
import SecurityPanel from './SecurityPanel'
import HistoryPanel from './HistoryPanel'

// The governance order, which is also the order a company is configured in:
// who you are -> what data you connected -> the KPIs the business already
// defined -> which history those KPIs are compared against -> who may see them
// -> what was changed. Documents support a definition, so they sit alongside the
// KPIs rather than ahead of them; the comparison policy is extracted from one of
// those documents, so it follows them.
const SUB_TABS = [
  { to: '/kpi-setup', label: 'Company', end: true },
  { to: '/kpi-setup/sources', label: 'Data Sources' },
  { to: '/kpi-setup/kpis', label: 'KPIs' },
  { to: '/kpi-setup/documents', label: 'Documents' },
  { to: '/kpi-setup/comparison-policy', label: 'Comparison Policy' },
  { to: '/kpi-setup/security', label: 'Security' },
  { to: '/kpi-setup/history', label: 'History' },
]

export default function KpiSetup() {
  const { adminUnlocked, membership } = useAuth()

  if (!adminUnlocked) return <AdminUnlock />

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight text-slate-100">
            KPI Setup &amp; Governance
          </h1>
          <p className="mt-0.5 truncate text-sm text-slate-500">
            {membership?.company_name}
            {membership?.role_name ? ` · ${membership.role_name}` : ''}
          </p>
        </div>
        <LockButton />
      </div>

      <nav className="glass-nav">
        {SUB_TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.end}
            className={({ isActive }) => `nav-pill ${isActive ? 'nav-pill-active' : ''}`}
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>

      {/* Keyed on the pathname so switching tabs replays the entrance
          transition instead of swapping content instantly. */}
      <div className="anim-panel">
        <Routes>
          <Route index element={<CompanyPanel />} />
          <Route path="sources" element={<SourcesPanel />} />
          {/* Drill-down into one source. Sibling of the list rather than nested, to
              match the flat routing this workspace already uses; the Data sources
              tab has no `end` prop, so it stays active here. */}
          <Route path="sources/:sourceId" element={<SourceGovernance />} />
          <Route path="documents" element={<DocumentsPanel />} />
          <Route path="kpis" element={<KpiRegistryPanel />} />
          <Route path="comparison-policy" element={<ComparisonPolicyPanel />} />
          <Route path="security" element={<SecurityPanel />} />
          <Route path="history" element={<HistoryPanel />} />
          <Route path="*" element={<Navigate to="/kpi-setup" replace />} />
        </Routes>
      </div>
    </div>
  )
}

function LockButton() {
  const { lockAdmin } = useAuth()
  const navigate = useNavigate()
  return (
    <button
      onClick={() => {
        lockAdmin()
        navigate('/')
      }}
      className="btn-ghost btn-xs"
      title="End the elevated session and return to the dashboard"
    >
      Exit setup
    </button>
  )
}

function AdminUnlock() {
  const { unlockAdmin, user, membership } = useAuth()
  const [email, setEmail] = useState(user?.email ?? '')
  const [password, setPassword] = useState('')
  const { pending, error, run } = useAction()

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    await run(() => unlockAdmin(email, password))
  }

  return (
    <div className="mx-auto max-w-md py-10">
      <Panel>
        <div className="mb-5 text-center">
          <span className="mx-auto mb-3 block h-1 w-10 rounded-full bg-gradient-to-r from-accent to-[var(--accent-violet)]" />
          <h1 className="text-base font-semibold text-slate-100">Confirm administrator access</h1>
          <p className="mt-1 text-xs leading-relaxed text-slate-500">
            Re-enter your credentials to change what your KPIs mean and who can see them.
          </p>
        </div>

        <form onSubmit={submit} className="space-y-4">
          {error && <Alert>{error}</Alert>}

          <Field label="Email" required>
            <input
              type="email"
              className="field"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
            />
          </Field>

          <Field label="Password" required>
            <PasswordInput
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              autoFocus
              required
            />
          </Field>

          <button type="submit" className="btn-primary w-full" disabled={pending}>
            {pending ? 'Verifying…' : 'Unlock KPI Setup'}
          </button>
        </form>

        <p className="mt-4 border-t border-ink-800 pt-3 text-center text-[11px] text-slate-600">
          Requires an administrator role in {membership?.company_name ?? 'this workspace'}.
        </p>
      </Panel>
    </div>
  )
}
