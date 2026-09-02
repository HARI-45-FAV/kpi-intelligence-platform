/**
 * The audit trail, read rather than merely written.
 *
 * The trail has always been recorded. What makes it accountability rather than a
 * compliance artefact is whether a reader can answer "who approved Revenue v2, and
 * when" — which on a table of thousands means filtering, and on a filtered table
 * means being told honestly what was left out.
 *
 * Five properties, each a way this goes wrong quietly:
 *
 * 1. **The filter values come from the server.** Options derived from the page the
 *    client happens to hold would omit the very action a reader came to find, which
 *    is the failure that looks exactly like "that never happened".
 * 2. **A filter narrows the request, and returns to the first page.** Page 3 of a
 *    different result set is not a place; silently keeping the offset shows an empty
 *    table for a filter that matches plenty.
 * 3. **The raw action key is translated, never replaced.** A screen showing only
 *    "KPI approved" while the log says `kpi.approved` makes the two disagree, and
 *    the log is the record.
 * 4. **`details` is readable.** It is the most specific column in the trail; the
 *    previous screen dropped it entirely.
 * 5. **"No match" and "nothing recorded" are different sentences.** They render
 *    identically on an empty table and mean entirely different things — one is a
 *    filter to widen, the other is a company with no history.
 *
 * Permissions are the server's business and are already tested there; what is
 * checked here is that a reader without `audit.read` is not shown a trail to filter.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import Activity from './pages/Activity'

const USER = {
  id: 'user-1',
  email: 'admin@aurora-retail.example.com',
  full_name: 'Ada Admin',
  is_active: true,
  is_platform_admin: false,
  created_at: '2026-01-01T00:00:00Z',
}

function membership(permissions: string[]) {
  return {
    company_id: 'company-1',
    company_name: 'Aurora Retail',
    company_slug: 'aurora-retail',
    role_key: 'ADMIN',
    role_name: 'Administrator',
    status: 'ACTIVE',
    is_admin_role: true,
    permissions,
  }
}

function entry(overrides: Record<string, unknown> = {}) {
  return {
    id: 'audit-1',
    action: 'kpi.approved',
    resource_type: 'kpi_version',
    resource_id: 'kpiver-1',
    resource_label: 'Revenue v2',
    actor_email: 'ada@aurora-retail.example.com',
    outcome: 'SUCCESS',
    summary: 'Revenue v2 approved for monitoring.',
    old_version: 'DRAFT',
    new_version: 'ACTIVE',
    details: { kpi_key: 'revenue', reviewer_note: 'Formula matches the finance definition.' },
    occurred_at: '2026-08-28T09:15:00Z',
    ...overrides,
  }
}

/**
 * The whole trail this fake company recorded, and the options that describe it.
 *
 * `detection.executed` is deliberately outside the first page: it is how "the option
 * list came from the server, not from the rows on screen" is made observable.
 */
const ALL = [
  entry(),
  entry({
    id: 'audit-2',
    action: 'member.role_changed',
    resource_type: 'membership',
    resource_id: null,
    resource_label: 'Ben Analyst',
    actor_email: 'ada@aurora-retail.example.com',
    summary: 'Role changed from VIEWER to ANALYST.',
    old_version: null,
    new_version: null,
    details: {},
    occurred_at: '2026-08-27T11:00:00Z',
  }),
  entry({
    id: 'audit-3',
    action: 'detection.executed',
    resource_type: 'agent_run',
    resource_label: 'Run for 2026-08-26',
    actor_email: 'ben@aurora-retail.example.com',
    outcome: 'FAILURE',
    summary: 'Connector refused the query.',
    details: { kpis_evaluated: 0 },
    occurred_at: '2026-08-26T06:00:00Z',
  }),
]

const OPTIONS = {
  options: {
    actions: ['detection.executed', 'kpi.approved', 'member.role_changed'],
    resource_types: ['agent_run', 'kpi_version', 'membership'],
    actors: ['ada@aurora-retail.example.com', 'ben@aurora-retail.example.com'],
    outcomes: ['FAILURE', 'SUCCESS'],
  },
  total: ALL.length,
  total_unfiltered: ALL.length,
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

let auditCalls: URLSearchParams[]
let optionCalls: URLSearchParams[]

/**
 * A server that actually applies the filters.
 *
 * A stub returning a fixed page would let a broken filter pass: the assertion has to
 * be that the *screen changes* when the reader narrows, not merely that a parameter
 * was appended to a URL.
 */
function stubFetch(permissions: string[], rows = ALL) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost')
    if (url.pathname.endsWith('/auth/session')) {
      return jsonResponse({ user: USER, memberships: [membership(permissions)] })
    }

    const params = url.searchParams
    const matching = rows.filter((row) => {
      if (params.get('action') && row.action !== params.get('action')) return false
      if (params.get('outcome') && row.outcome !== params.get('outcome')) return false
      if (params.get('actor_email') && row.actor_email !== params.get('actor_email')) return false
      const q = params.get('q')
      if (q && !`${row.resource_label} ${row.summary}`.toLowerCase().includes(q.toLowerCase())) {
        return false
      }
      return true
    })

    if (url.pathname.endsWith('/audit/options')) {
      optionCalls.push(params)
      return jsonResponse({ ...OPTIONS, total: matching.length, total_unfiltered: rows.length })
    }
    if (url.pathname.endsWith('/audit')) {
      auditCalls.push(params)
      const offset = Number(params.get('offset') ?? 0)
      const limit = Number(params.get('limit') ?? 50)
      return jsonResponse(matching.slice(offset, offset + limit))
    }
    return jsonResponse({})
  })
}

function setUp(permissions: string[], rows: typeof ALL = ALL) {
  auditCalls = []
  optionCalls = []
  localStorage.clear()
  localStorage.setItem(
    'bi.ai.session',
    JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
  )
  localStorage.setItem('bi.ai.company', 'company-1')
  vi.stubGlobal('fetch', stubFetch(permissions, rows))
}

async function renderActivity() {
  const view = render(
    <MemoryRouter initialEntries={['/activity']}>
      <AuthProvider>
        <Activity />
      </AuthProvider>
    </MemoryRouter>,
  )
  await screen.findByText('Activity')
  return view
}

/** The row carrying a given resource label, so assertions read as the screen does. */
function rowFor(label: string): HTMLElement {
  return screen.getByText(label).closest('tr') as HTMLElement
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('reading the audit trail', () => {
  beforeEach(() => setUp(['audit.read', 'telemetry.read']))

  it('offers every value this company recorded, not only the ones on screen', async () => {
    await renderActivity()
    await screen.findByText('Revenue v2')

    // The action select is populated from /audit/options, so it can offer a value
    // the current page does not contain — otherwise a reader could never filter for
    // the row that fell off the end of the listing.
    await waitFor(() => expect(optionCalls.length).toBeGreaterThan(0))
    const actions = screen.getByRole('combobox', { name: /action/i }) as HTMLSelectElement
    const offered = Array.from(actions.options).map((option) => option.value)
    expect(offered).toContain('detection.executed')
    expect(offered).toContain('kpi.approved')
    // Business words in the control too: an operator should not have to read keys.
    expect(
      Array.from(actions.options).map((option) => option.textContent),
    ).toContain('KPI analysis run')
  })

  it('narrows the trail and says how many entries match', async () => {
    await renderActivity()
    await screen.findByText('Revenue v2')
    await screen.findByText('Run for 2026-08-26')

    fireEvent.change(screen.getByRole('combobox', { name: /outcome/i }), {
      target: { value: 'FAILURE' },
    })

    // The failed run remains; the successful approval is gone. Asserting the screen
    // changed, rather than that a query string was built.
    await waitFor(() => expect(screen.queryByText('Revenue v2')).toBeNull())
    await screen.findByText('Run for 2026-08-26')

    const sent = auditCalls[auditCalls.length - 1]
    expect(sent.get('outcome')).toBe('FAILURE')
    // A new filter starts at the top of its own results.
    expect(sent.get('offset')).toBe('0')
    // The count is read under the same filters, so the heading cannot claim more
    // matches than the filter admits.
    const counted = optionCalls[optionCalls.length - 1]
    expect(counted.get('outcome')).toBe('FAILURE')
    await screen.findByText(/1–1 of 1 matching/)
  })

  it('shows the action in business words and keeps the trail’s own key', async () => {
    await renderActivity()
    const row = within(rowFor('Revenue v2'))

    // Translated, not replaced: the label is readable and the key is still reachable,
    // because the log's vocabulary is the record and the screen is a rendering of it.
    row.getByText('KPI approved')
    expect(row.getByText('KPI approved').getAttribute('title')).toBe('kpi.approved')

    fireEvent.click(row.getByRole('button', { name: /detail/i }))
    await screen.findByText('kpi.approved')
    await screen.findByText('kpi_version')
    await screen.findByText('kpiver-1')
  })

  it('surfaces the stored detail instead of discarding it', async () => {
    await renderActivity()
    const row = within(rowFor('Revenue v2'))
    fireEvent.click(row.getByRole('button', { name: /detail/i }))

    // The most specific thing the trail recorded about this action, shown as stored.
    await screen.findByText('Formula matches the finance definition.')
    await screen.findByText('revenue')

    // An entry the trail recorded nothing extra about offers no expansion, rather
    // than an empty panel implying detail exists that is merely being withheld.
    const bare = within(rowFor('Ben Analyst'))
    expect(bare.queryByRole('button', { name: /detail/i })).toBeNull()
  })

  it('tells an unmatched filter apart from an empty trail', async () => {
    await renderActivity()
    await screen.findByText('Revenue v2')

    fireEvent.change(screen.getByPlaceholderText(/a KPI name, an id, a phrase/i), {
      target: { value: 'nothing recorded ever matched this' },
    })

    // A filter with no match is a filter to widen, and the screen says so — naming
    // the size of the trail it searched, which is the fact that distinguishes it.
    await screen.findByText(/No entry matches these filters/)
    await screen.findByText(/3 recorded entries/)
  })
})

describe('a trail with nothing in it', () => {
  beforeEach(() => setUp(['audit.read'], []))

  it('says the company has recorded nothing, not that a filter failed', async () => {
    await renderActivity()
    await screen.findByText(/No audit entries yet/)
    expect(screen.queryByText(/No entry matches these filters/)).toBeNull()
  })
})

describe('a reader without audit access', () => {
  beforeEach(() => setUp(['analytics.read']))

  it('is not shown a trail to filter', async () => {
    await renderActivity()
    await screen.findByText(/does not include/)
    // Not merely hidden: never asked for. A screen that renders the controls and
    // relies on a 403 has already told the reader the trail exists to be read.
    expect(auditCalls).toHaveLength(0)
    expect(optionCalls).toHaveLength(0)
  })
})
