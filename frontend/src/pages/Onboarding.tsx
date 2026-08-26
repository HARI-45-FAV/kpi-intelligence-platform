/** First-run: create the company workspace everything else is scoped to. */

import { useState } from 'react'
import { api } from '../api/client'
import type { Company } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { Alert, Field } from '../components/ui'
import { useAction } from '../components/useResource'

const TIMEZONES = [
  'UTC',
  'Asia/Kolkata',
  'Asia/Singapore',
  'Asia/Dubai',
  'Europe/London',
  'Europe/Berlin',
  'America/New_York',
  'America/Los_Angeles',
]
const CURRENCIES = ['INR', 'USD', 'EUR', 'GBP', 'SGD', 'AED', 'AUD']
const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

export default function Onboarding() {
  const { refresh, selectCompany, logout, user } = useAuth()
  const { pending, error, run } = useAction()

  const [form, setForm] = useState({
    company_name: '',
    industry: '',
    country: '',
    timezone: 'Asia/Kolkata',
    currency: 'INR',
    fiscal_year_start_month: 4,
    week_start_day: 1,
  })

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const company = await run(() => api.post<Company>('/companies', form))
    if (company) {
      await refresh()
      selectCompany(company.id)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-xl">
        <div className="mb-6">
          <h1 className="text-lg font-semibold text-slate-100">Create your company workspace</h1>
          <p className="mt-1 text-sm text-slate-400">
            Signed in as {user?.email}. Every data source, document and KPI belongs to a company,
            so this comes first.
          </p>
        </div>

        <form onSubmit={submit} className="panel space-y-4 p-5">
          {error && <Alert>{error}</Alert>}

          <Field label="Company name" required>
            <input
              className="field"
              value={form.company_name}
              onChange={(e) => set('company_name', e.target.value)}
              placeholder="NovaMart"
              required
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Industry">
              <input
                className="field"
                value={form.industry}
                onChange={(e) => set('industry', e.target.value)}
                placeholder="E-commerce"
              />
            </Field>
            <Field label="Country">
              <input
                className="field"
                value={form.country}
                onChange={(e) => set('country', e.target.value)}
                placeholder="India"
              />
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Timezone" required>
              <select
                className="field"
                value={form.timezone}
                onChange={(e) => set('timezone', e.target.value)}
              >
                {TIMEZONES.map((tz) => (
                  <option key={tz}>{tz}</option>
                ))}
              </select>
            </Field>
            <Field label="Reporting currency" required>
              <select
                className="field"
                value={form.currency}
                onChange={(e) => set('currency', e.target.value)}
              >
                {CURRENCIES.map((code) => (
                  <option key={code}>{code}</option>
                ))}
              </select>
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Fiscal year starts"
              hint="Gives 'monthly revenue' a governed meaning later."
            >
              <select
                className="field"
                value={form.fiscal_year_start_month}
                onChange={(e) => set('fiscal_year_start_month', Number(e.target.value))}
              >
                {MONTHS.map((month, index) => (
                  <option key={month} value={index + 1}>
                    {month}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Week starts on">
              <select
                className="field"
                value={form.week_start_day}
                onChange={(e) => set('week_start_day', Number(e.target.value))}
              >
                <option value={1}>Monday</option>
                <option value={7}>Sunday</option>
              </select>
            </Field>
          </div>

          <div className="flex items-center justify-between pt-1">
            <button type="button" onClick={logout} className="btn-ghost btn-xs">
              Sign out
            </button>
            <button type="submit" className="btn-primary" disabled={pending}>
              {pending ? 'Creating…' : 'Create workspace'}
            </button>
          </div>
        </form>

        <p className="mt-3 text-center text-[11px] text-slate-600">
          You become this workspace's administrator. A default business calendar is created from
          the timezone and fiscal year above.
        </p>
      </div>
    </div>
  )
}
