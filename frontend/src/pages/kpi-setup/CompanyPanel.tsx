/**
 * Company identity, business context and the governed calendar.
 *
 * The page states the configuration; editing happens in a dialog. That split is
 * deliberate: a reader arriving here wants to confirm what the company is set to,
 * and a form full of inputs answers that question far worse than a row of values
 * does. Nothing about what gets saved changed — the same PATCH, the same fields.
 */

import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { Calendar, Company } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { monthName, weekdayName } from '../../components/format'
import {
  Alert,
  Field,
  HelpList,
  HelpSection,
  InfoTile,
  Modal,
  Panel,
  SectionHeader,
  SectionHelp,
  Spinner,
  StatusBadge,
} from '../../components/ui'
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
  const activate = useAction()
  const [editOpen, setEditOpen] = useState(false)

  if (company.loading && !company.data) return <Spinner />
  if (company.error) return <Alert>{company.error}</Alert>
  if (!company.data) return null

  const data = company.data
  const defaultCalendar = calendars.data?.find((c) => c.is_default)

  return (
    <div className="space-y-5">
      <SectionHeader
        title="Company"
        help={<CompanyHelp />}
        actions={
          <>
            <StatusBadge status={data.status} />
            <button className="btn-ghost btn-xs" onClick={() => setEditOpen(true)}>
              Edit details
            </button>
          </>
        }
      />

      <Panel title="Business profile">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <InfoTile label="Company" value={data.company_name} />
          <InfoTile label="Industry" value={data.industry || '—'} />
          <InfoTile label="Country" value={data.country || '—'} />
          <InfoTile label="Timezone" value={data.timezone} />
          <InfoTile label="Reporting currency" value={data.currency} />
          <InfoTile
            label="Fiscal year starts"
            value={monthName(data.fiscal_year_start_month ?? 1)}
          />
          <InfoTile label="Week starts" value={weekdayName(data.week_start_day ?? 1)} />
          <InfoTile
            label="Business calendar"
            value={
              calendars.loading && !calendars.data
                ? '…'
                : defaultCalendar
                  ? defaultCalendar.name
                  : 'Not defined'
            }
          />
        </div>
      </Panel>

      {!defaultCalendar && !calendars.loading && (
        <Alert tone="warn">No business calendar defined yet.</Alert>
      )}

      {data.status !== 'ACTIVE' && (
        <Panel title="Activation">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-slate-400">
              This workspace is a draft. Activate it once sources, scope and KPIs are configured.
            </p>
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
          {activate.error && (
            <div className="mt-3">
              <Alert>{activate.error}</Alert>
            </div>
          )}
        </Panel>
      )}

      {editOpen && (
        <EditCompanyModal
          company={data}
          onClose={() => setEditOpen(false)}
          onSaved={async (updated) => {
            company.setData(updated)
            await refresh()
            await calendars.reload()
            setEditOpen(false)
          }}
        />
      )}
    </div>
  )
}

function CompanyHelp() {
  return (
    <SectionHelp title="About company configuration">
      <HelpSection heading="What this section is">
        <p>
          Your company's business identity and its reporting calendar. Every KPI the platform
          calculates is expressed in these terms, so this is the first thing to get right.
        </p>
      </HelpSection>
      <HelpSection heading="What each setting controls">
        <HelpList
          items={[
            ['Company', 'The name shown across the platform and on every report.'],
            ['Industry', 'Business context used when suggesting which metrics to track.'],
            ['Country', 'Where the business operates. Recorded for reporting context.'],
            ['Timezone', 'Decides when a "day" starts and ends for every daily figure.'],
            [
              'Reporting currency',
              'The currency every monetary KPI is reported in.',
            ],
            [
              'Fiscal year starts',
              'Defines what your business means by a month, a quarter and a year.',
            ],
            ['Week starts', 'Defines where one week ends and the next begins.'],
            [
              'Business calendar',
              'The named calendar KPI versions bind to, built from the settings above.',
            ],
          ]}
        />
      </HelpSection>
      <HelpSection heading="Why it matters">
        <p>
          These settings give a phrase like "monthly revenue" one reproducible meaning. A company
          whose fiscal year starts in April does not mean the same thing by "Q1" as one starting in
          January — and a KPI approved today must still calculate the same way months from now.
          Changing them changes what future comparisons mean, which is why they are governed here.
        </p>
      </HelpSection>
    </SectionHelp>
  )
}

function EditCompanyModal({
  company,
  onClose,
  onSaved,
}: {
  company: Company
  onClose: () => void
  onSaved: (updated: Company) => Promise<void>
}) {
  const { companyId } = useAuth()
  const save = useAction()
  const [form, setForm] = useState<Partial<Company>>(company)

  useEffect(() => setForm(company), [company])

  const set = <K extends keyof Company>(key: K, value: Company[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const dirty = (['company_name', 'industry', 'description', 'country', 'timezone',
    'currency', 'fiscal_year_start_month', 'week_start_day'] as const)
    .some((key) => form[key] !== company[key])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const updated = await save.run(() =>
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
    )
    if (updated) await onSaved(updated)
  }

  return (
    <Modal open onClose={onClose} title="Edit company details" width="max-w-2xl">
      <form onSubmit={submit} className="space-y-4">
        {save.error && <Alert>{save.error}</Alert>}

        <div className="grid gap-4 sm:grid-cols-2">
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
            className="field min-h-[4rem] resize-y"
            value={form.description ?? ''}
            onChange={(e) => set('description', e.target.value)}
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-3">
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

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Fiscal year starts">
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
          <Field label="Week starts on">
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

        <div className="flex justify-end gap-2 border-t border-ink-800 pt-3">
          <button type="button" className="btn-ghost btn-xs" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary btn-xs" disabled={save.pending || !dirty}>
            {save.pending ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
