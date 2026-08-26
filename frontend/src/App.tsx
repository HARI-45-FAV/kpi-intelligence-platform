import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import { Spinner } from './components/ui'
import AppShell from './pages/AppShell'
import SignIn from './pages/SignIn'
import Onboarding from './pages/Onboarding'
import Dashboard from './pages/Dashboard'
import Monitoring from './pages/Monitoring'
import Investigation from './pages/Investigation'
import Insights from './pages/Insights'
import Activity from './pages/Activity'
import KpiSetup from './pages/kpi-setup/KpiSetup'

export default function App() {
  const { ready, user, companyId } = useAuth()

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Starting BusinessIntelligence.ai…" />
      </div>
    )
  }

  if (!user) return <SignIn />

  // A signed-in user with no company yet goes straight to onboarding: the whole
  // platform is scoped to a company, so there is nothing to show without one.
  if (!companyId) return <Onboarding />

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Dashboard />} />
        <Route path="monitoring" element={<Monitoring />} />
        <Route path="investigation" element={<Investigation />} />
        <Route path="insights" element={<Insights />} />
        <Route path="activity" element={<Activity />} />
        <Route path="kpi-setup/*" element={<KpiSetup />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
