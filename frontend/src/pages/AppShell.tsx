/** Top navigation shell: six tabs, with KPI Setup marked as protected. */

import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

const TABS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/monitoring', label: 'Monitoring' },
  { to: '/investigation', label: 'Investigation' },
  { to: '/insights', label: 'Insights' },
  { to: '/activity', label: 'Activity' },
]

export default function AppShell() {
  const { user, membership, memberships, companyId, selectCompany, logout, adminUnlocked } =
    useAuth()

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-ink-700 bg-ink-900/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1600px] items-center gap-6 px-5">
          <div className="flex items-center gap-2">
            <span className="grid h-7 w-7 place-items-center rounded bg-accent text-[13px] font-bold text-white">
              BI
            </span>
            <span className="hidden text-sm font-semibold tracking-tight text-slate-100 sm:block">
              BusinessIntelligence<span className="text-accent">.ai</span>
            </span>
          </div>

          <nav className="flex flex-1 items-center gap-1 overflow-x-auto">
            {TABS.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                end={tab.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition-colors ${
                    isActive
                      ? 'bg-ink-800 font-medium text-slate-100'
                      : 'text-slate-400 hover:bg-ink-850 hover:text-slate-200'
                  }`
                }
              >
                {tab.label}
              </NavLink>
            ))}

            {/* Visually separated: this is the governed configuration area. */}
            <span className="mx-2 h-5 w-px bg-ink-700" />
            <NavLink
              to="/kpi-setup"
              className={({ isActive }) =>
                `flex items-center gap-1.5 whitespace-nowrap rounded-md border px-3 py-1.5 text-sm transition-colors ${
                  isActive
                    ? 'border-accent bg-accent/15 font-medium text-accent-soft'
                    : 'border-ink-600 text-slate-300 hover:border-accent/50 hover:text-accent-soft'
                }`
              }
            >
              <span aria-hidden>{adminUnlocked ? '🔓' : '🔒'}</span>
              KPI Setup
            </NavLink>
          </nav>

          <div className="flex items-center gap-3">
            {memberships.length > 1 ? (
              <select
                value={companyId ?? ''}
                onChange={(event) => selectCompany(event.target.value)}
                className="rounded-md border border-ink-600 bg-ink-850 px-2 py-1 text-xs text-slate-200"
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

      <main className="mx-auto max-w-[1600px] px-5 py-6">
        <Outlet />
      </main>
    </div>
  )
}
