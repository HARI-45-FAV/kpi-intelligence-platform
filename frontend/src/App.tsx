import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import { Spinner } from './components/ui'
import AppShell from './pages/AppShell'
import SignIn from './pages/SignIn'
import Onboarding from './pages/Onboarding'
import Dashboard from './pages/Dashboard'
import Monitoring from './pages/Monitoring'
import Results from './pages/Results'
import ResultDetail from './pages/ResultDetail'
import Investigation from './pages/Investigation'
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
        <Route path="results" element={<Results />} />
        {/* One stored evaluation, addressable on its own so a movement can be
            linked to from the dashboard, an alert or a colleague's message. The
            parameter is the detection run id, which is what the results list
            returns as each row's id. */}
        <Route path="results/:runId" element={<ResultDetail />} />
        <Route path="investigation" element={<Investigation />} />
        <Route path="kpi-setup/*" element={<KpiSetup />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
