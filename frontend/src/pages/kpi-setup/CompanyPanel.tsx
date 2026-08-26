/** Company identity, business context and the governed calendar. */

import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { Calendar, Company } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { monthName, weekdayName } from '../../components/format'
import { Alert, Field, Panel, Spinner, StatusBadge } from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'

const TIMEZONES = [
  'UTC', 'Asia/Kolkata', 'Asia/Singapore', 'Asia/Dubai', 'Europe/London',
  'Europe/Berlin', 'America/New_York', 'America/Los_Angeles',
]
const CURRENCIES = ['INR', 'USD', 'EUR', 'GBP', 'SGD', 'AED', 'AUD']
const MONTHS = Array.from({ length: 12 }, (_, i) => i + 1)

export default function CompanyPanel() {
  const { companyId, refresh } = useAuth()
  const company = useResource<Company>(() => api.get(`/companies/${companyId}`), [companyId])
  const calendars = useResource<Calendar[]>(
    () => api.get(`/companies/${companyId}/calendars`),
    [companyId],
  )
  const save = useAction()
  const activate = useAction()

  const [form, setForm] = useState<Partial<Company>>({})

  useEffect(() => {
    if (company.data) setForm(company.data)
  }, [company.data])

  const set = <K extends keyof Company>(key: K, value: Company[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  if (company.loading && !company.data) return <Spinner />
  if (company.error) return <Alert>{company.error}</Alert>
  if (!company.data) return null

  const dirty = (['company_name', 'industry', 'description', 'country', 'timezone',
    'currency', 'fiscal_year_start_month', 'week_start_day'] as const)
    .some((key) => form[key] !== company.data![key])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const updated = await save.run(
      () =>
        api.patch<Company>(
          `/companies/${companyId}`,
          {
            company_name: form.company_name,
            industry: form.industry,
            description: form.description,
            country: form.country,
            timezone: form.timezone,
            currency: form.currency,
            fiscal_year_start_month: form.fiscal_year_start_month,
            week_start_day: form.week_start_day,
          },
          { admin: true },
        ),
      'Company profile saved.',
    )
    if (updated) {
      company.setData(updated)
      await refresh()
    }
  }

  const defaultCalendar = calendars.data?.find((c) => c.is_default)

  return (
    <div className="space-y-5">
      <form onSubmit={submit}>
        <Panel
          title="Company profile"
          actions={
            <>
              <StatusBadge status={company.data.status} />
              <button type="submit" className="btn-primary btn-xs" disabled={save.pending || !dirty}>
                {save.pending ? 'Saving…' : 'Save changes'}
              </button>
            </>
          }
        >
          <div className="space-y-4">
            {save.error && <Alert>{save.error}</Alert>}
            {save.message && (
              <Alert tone="success" onDismiss={save.reset}>
                {save.message}
              </Alert>
            )}

            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Company name" required>
                <input
                  className="field"
                  value={form.company_name ?? ''}
                  onChange={(e) => set('company_name', e.target.value)}
                  required
                />
              </Field>
              <Field label="Industry">
                <input
                  className="field"
                  value={form.industry ?? ''}
                  onChange={(e) => set('industry', e.target.value)}
                />
              </Field>
            </div>

            <Field label="Description">
              <textarea
                className="field min-h-[4.5rem] resize-y"
                value={form.description ?? ''}
                onChange={(e) => set('description', e.target.value)}
              />
            </Field>

            <div className="grid gap-4 md:grid-cols-3">
              <Field label="Country">
                <input
                  className="field"
                  value={form.country ?? ''}
                  onChange={(e) => set('country', e.target.value)}
                />
              </Field>
              <Field label="Timezone">
                <select
                  className="field"
                  value={form.timezone ?? 'UTC'}
                  onChange={(e) => set('timezone', e.target.value)}
                >
                  {TIMEZONES.map((tz) => (
                    <option key={tz}>{tz}</option>
                  ))}
                </select>
              </Field>
              <Field label="Reporting currency">
                <select
                  className="field"
                  value={form.currency ?? 'USD'}
                  onChange={(e) => set('currency', e.target.value)}
                >
                  {CURRENCIES.map((code) => (
                    <option key={code}>{code}</option>
                  ))}
                </select>
              </Field>
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <Field
                label="Fiscal year starts"
                hint="Defines what a KPI means by 'month' and 'quarter'."
              >
                <select
                  className="field"
                  value={form.fiscal_year_start_month ?? 1}
                  onChange={(e) => set('fiscal_year_start_month', Number(e.target.value))}
                >
                  {MONTHS.map((month) => (
                    <option key={month} value={month}>
                      {monthName(month)}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Week starts on" hint="Defines weekly aggregation boundaries.">
                <select
                  className="field"
                  value={form.week_start_day ?? 1}
                  onChange={(e) => set('week_start_day', Number(e.target.value))}
                >
                  <option value={1}>Monday</option>
                  <option value={7}>Sunday</option>
                </select>
              </Field>
            </div>
          </div>
        </Panel>
      </form>

      <Panel title="Business calendar">
        {calendars.loading && !calendars.data ? (
          <Spinner />
        ) : defaultCalendar ? (
          <div className="space-y-3">
            <p className="text-xs leading-relaxed text-slate-500">
              A KPI version binds to a calendar so that "monthly revenue" has one reproducible
              meaning. The default calendar tracks the company profile above.
            </p>
            <div className="grid gap-4 text-sm sm:grid-cols-4">
              <div>
                <div className="text-[11px] uppercase tracking-wider text-slate-500">Calendar</div>
                <div className="mt-1 text-slate-200">{defaultCalendar.name}</div>
              </div>
              <div>
                <div className="text-[11px] uppercase tracking-wider text-slate-500">Timezone</div>
                <div className="mt-1 text-slate-200">{defaultCalendar.timezone}</div>
              </div>
              <div>
                <div className="text-[11px] uppercase tracking-wider text-slate-500">
                  Fiscal start
                </div>
                <div className="mt-1 text-slate-200">
                  {monthName(defaultCalendar.fiscal_year_start_month)}
                </div>
              </div>
              <div>
                <div className="text-[11px] uppercase tracking-wider text-slate-500">Week start</div>
                <div className="mt-1 text-slate-200">
                  {weekdayName(defaultCalendar.week_start_day)}
                </div>
              </div>
            </div>
          </div>
        ) : (
          <Alert tone="warn">No calendar defined yet.</Alert>
        )}
      </Panel>

      {company.data.status !== 'ACTIVE' && (
        <Panel title="Activation">
          <div className="space-y-3">
            <p className="text-xs leading-relaxed text-slate-500">
              A workspace stays in <StatusBadge status="DRAFT" /> until it is deliberately
              activated. Activation asserts that the company is configured: sources connected,
              scope chosen, and at least one KPI approved.
            </p>
            {activate.error && <Alert>{activate.error}</Alert>}
            <button
              className="btn-primary btn-xs"
              disabled={activate.pending}
              onClick={async () => {
                const updated = await activate.run(
                  () => api.post<Company>(`/companies/${companyId}/activate`, {}, { admin: true }),
                  'Workspace activated.',
                )
                if (updated) company.setData(updated)
              }}
            >
              {activate.pending ? 'Activating…' : 'Activate workspace'}
            </button>
          </div>
        </Panel>
      )}
    </div>
  )
}
