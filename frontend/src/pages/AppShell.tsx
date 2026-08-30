/** Top navigation shell: six tabs, with KPI Setup marked as protected. */

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
  const { user, membership, memberships, companyId, selectCompany, logout, adminUnlocked } =
    useAuth()
  const { openPanel } = useCopilot()

  return (
    <div className="min-h-screen px-3 py-3 sm:px-5 sm:py-5">
      <header className="sticky top-3 z-30 mx-auto max-w-[1600px] rounded-2xl border border-white/80 bg-white/65 shadow-[0_12px_32px_rgba(52,104,146,0.16),inset_0_1px_0_rgba(255,255,255,0.86)] backdrop-blur-xl sm:top-5">
        <div className="flex min-h-14 items-center gap-4 px-3 sm:gap-6 sm:px-5">
          <div className="flex items-center gap-2">
            <span className="grid h-8 w-8 place-items-center rounded-xl bg-accent text-[13px] font-bold text-white shadow-[0_6px_14px_rgba(25,120,197,0.28)]">
              BI
            </span>
            <span className="hidden text-sm font-semibold tracking-tight text-slate-100 sm:block">
              BusinessIntelligence<span className="text-accent">.ai</span>
            </span>
          </div>

          <nav className="flex flex-1 items-center gap-1 overflow-x-auto rounded-xl border border-white/70 bg-white/35 p-1">
            {TABS.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                end={tab.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-lg px-3 py-1.5 text-sm transition-all ${
                    isActive
                      ? 'bg-white/90 font-medium text-slate-100 shadow-sm'
                      : 'text-slate-400 hover:bg-white/65 hover:text-slate-200'
                  }`
                }
              >
                {tab.label}
              </NavLink>
            ))}

            {/* Visually separated: this is the governed configuration area. */}
            <span className="mx-1 h-5 w-px bg-ink-700/70" />
            <NavLink
              to="/kpi-setup"
              className={({ isActive }) =>
                `flex items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 py-1.5 text-sm transition-all ${
                  isActive
                    ? 'border-sky-200 bg-sky-50/90 font-medium text-accent shadow-sm'
                    : 'border-white/80 bg-white/35 text-slate-300 hover:border-sky-200 hover:bg-white/70 hover:text-accent'
                }`
              }
            >
              <span aria-hidden>{adminUnlocked ? '🔓' : '🔒'}</span>
              KPI Setup
            </NavLink>
          </nav>

          <div className="flex items-center gap-3">
            {/* Company scope is not a Copilot setting: it comes from the session
                and is re-derived from the membership row on the server. */}
            <button
              onClick={() => openPanel()}
              className="flex items-center gap-1.5 whitespace-nowrap rounded-md border border-ink-600 px-2.5 py-1 text-xs text-slate-300 transition-colors hover:border-accent/50 hover:text-accent-soft"
              title="Ask about KPI definitions, documents and data profiles"
            >
              <span aria-hidden>✨</span>
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
