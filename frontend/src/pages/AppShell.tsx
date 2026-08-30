/** Top navigation shell: six tabs, with KPI Setup as the governed area. */

import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import CopilotPanel from '../copilot/CopilotPanel'
import { CopilotProvider, useCopilot } from '../copilot/CopilotProvider'

const TABS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/monitoring', label: 'Monitoring' },
  { to: '/investigation', label: 'Investigation' },
  { to: '/insights', label: 'Insights' },
  { to: '/activity', label: 'Activity' },
]

export default function AppShell() {
  // The provider wraps both the header and the routed page, so the launcher in
  // the header and the screen that publishes context share one state. Pages
  // inside <Outlet /> can therefore tell the Copilot what they are showing
  // without any of them owning the panel.
  return (
    <CopilotProvider>
      <Shell />
    </CopilotProvider>
  )
}

function Shell() {
  const { user, membership, memberships, companyId, selectCompany, logout } = useAuth()
  const { openPanel } = useCopilot()

  return (
    <div className="min-h-screen px-3 py-3 sm:px-5 sm:py-5">
      <header className="glass-bar sticky top-3 z-30 mx-auto max-w-[1600px] sm:top-5">
        <div className="flex min-h-[3.75rem] items-center gap-4 px-3.5 sm:gap-6 sm:px-5">
          <div className="flex shrink-0 items-center gap-2.5">
            <span className="grid h-9 w-9 place-items-center rounded-[13px] bg-gradient-to-br from-accent to-[var(--accent-violet)] text-[13px] font-bold text-white shadow-[0_8px_18px_rgba(25,120,197,0.32),inset_0_1px_0_rgba(255,255,255,0.35)]">
              BI
            </span>
            <span className="hidden text-[15px] font-semibold tracking-tight text-slate-100 sm:block">
              BusinessIntelligence<span className="text-accent">.ai</span>
            </span>
          </div>

          {/* The nav is its own floating control inside the bar rather than a
              flat strip painted on it. */}
          <nav className="flex flex-1 items-center gap-1 overflow-x-auto">
            {TABS.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                end={tab.end}
                className={({ isActive }) => `nav-pill ${isActive ? 'nav-pill-active' : ''}`}
              >
                {tab.label}
              </NavLink>
            ))}

            {/* Visually separated: this is the governed configuration area. */}
            <span className="mx-1.5 h-5 w-px shrink-0 bg-[rgba(120,165,200,0.45)]" />
            <NavLink
              to="/kpi-setup"
              className={({ isActive }) => `nav-pill ${isActive ? 'nav-pill-active' : ''}`}
            >
              KPI Setup
            </NavLink>
          </nav>

          <div className="flex shrink-0 items-center gap-3">
            {/* Company scope is not a Copilot setting: it comes from the session
                and is re-derived from the membership row on the server. */}
            <button
              onClick={() => openPanel()}
              className="btn-ghost btn-xs"
              title="Ask about KPI definitions, documents and data profiles"
            >
              Copilot
            </button>

            {memberships.length > 1 ? (
              <select
                value={companyId ?? ''}
                onChange={(event) => selectCompany(event.target.value)}
                className="rounded-lg border border-white/80 bg-white/55 px-2 py-1 text-xs text-slate-200 shadow-sm"
                title="Switch company workspace"
              >
                {memberships.map((m) => (
                  <option key={m.company_id} value={m.company_id}>
                    {m.company_name}
                  </option>
                ))}
              </select>
            ) : (
              <span className="hidden text-xs text-slate-400 md:block">
                {membership?.company_name}
              </span>
            )}

            <div className="hidden text-right lg:block">
              <div className="text-xs font-medium text-slate-300">{user?.full_name}</div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                {membership?.role_name ?? '—'}
              </div>
            </div>

            <button onClick={logout} className="btn-ghost btn-xs">
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="app-content-shell mx-auto mt-5 max-w-[1600px] sm:mt-6">
        <Outlet />
      </main>

      <CopilotPanel />
    </div>
  )
}
