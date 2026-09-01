/**
 * The Results screen's business filters.
 *
 * Three properties are pinned here, each of which is invisible when it breaks:
 *
 *  1. **KPI, Date, Status and Dimension narrow on the server.** The stored list is
 *     capped, so a client-side filter over the page the browser happens to hold
 *     would leave an older date unreachable — the reader would have no control for
 *     the very row they came for. The test therefore asserts the *request*, not
 *     just the rendered table.
 *  2. **Search narrows on the client.** It is a free-text scan of what is on
 *     screen, and it must not issue a request per keystroke.
 *  3. **Dimension is offered only when the server offers it.** A VIEWER holds
 *     `analytics.read` without `investigation.read`, so the server sends no
 *     dimensions and the screen must render no dimension control — offering one
 *     would be a control whose only possible outcome is a refusal.
 *
 * The fixture's `deviation_pct` is deliberately inconsistent with its own actual
 * and expected, so "printed the server's field" stays distinguishable from
 * "recomputed it in the browser".
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import Results from './pages/Results'

const USER = {
  id: 'user-1',
  email: 'ana@aurora-retail.example.com',
  full_name: 'Ana Analyst',
  is_active: true,
  is_platform_admin: false,
  created_at: '2026-01-01T00:00:00Z',
}

function membership(permissions: string[]) {
  return {
    company_id: 'company-1',
    company_name: 'Aurora Retail',
    company_slug: 'aurora-retail',
    role_key: permissions.includes('investigation.read') ? 'ANALYST' : 'VIEWER',
    role_name: 'Reader',
    status: 'ACTIVE',
    is_admin_role: false,
    permissions,
  }
}

const REVENUE = {
  id: 'run-revenue',
  kpi_key: 'net_revenue',
  kpi_name: 'net_revenue',
  target_date: '2026-08-28',
  status: 'ABNORMAL',
  actual_value: 6000000,
  expected_value: 10250000,
  deviation_absolute: -4250000,
  // See the file docstring: not derivable from the two figures above.
  deviation_pct: -37.2,
  unit: 'currency',
  currency: 'INR',
  top_driver: 'Metro West accounts for most of the shortfall.',
  ai_explanation: null,
  explanation_status: 'NOT_GENERATED',
  explanation_generated_at: null,
  email_status: 'NOT_SENT',
  dimensions: ['region'],
  entities: ['Metro West'],
}

const ORDERS = {
  id: 'run-orders',
  kpi_key: 'orders',
  kpi_name: 'orders',
  target_date: '2026-08-21',
  status: 'NORMAL',
  actual_value: 4120,
  expected_value: 4180,
  deviation_absolute: -60,
  deviation_pct: -1.4,
  unit: 'count',
  currency: null,
  top_driver: 'In line with comparable Fridays.',
  ai_explanation: null,
  explanation_status: 'NOT_GENERATED',
  explanation_generated_at: null,
  email_status: 'NOT_SENT',
  dimensions: [],
  entities: [],
}

const OPTIONS = {
  kpis: [
    { kpi_key: 'net_revenue', kpi_name: 'net_revenue' },
    { kpi_key: 'orders', kpi_name: 'orders' },
  ],
  dates: ['2026-08-28', '2026-08-21'],
  statuses: ['ABNORMAL', 'NORMAL'],
  dimensions: ['region'],
}

function envelope(items: unknown[], options = OPTIONS) {
  return {
    summary: {
      total_runs: items.length,
      anomalies: 1,
      abnormal: 1,
      normal: 1,
      low_confidence: 0,
      kpi_count: 2,
    },
    items,
    filters: { status: null, kpi_key: null, target_date: null, dimension: null },
    options,
    // Deliberately larger than the page, so "of N stored" is a server figure.
    total_stored: 37,
  }
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

/** Every `/results` URL the page asked for, in order. */
let resultCalls: string[] = []

function setUp(permissions: string[], options = OPTIONS) {
  resultCalls = []
  localStorage.clear()
  localStorage.setItem(
    'bi.ai.session',
    JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
  )
  localStorage.setItem('bi.ai.company', 'company-1')
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/auth/session')) {
        return jsonResponse({ user: USER, memberships: [membership(permissions)] })
      }
      if (url.includes('/results')) {
        resultCalls.push(url)
        return jsonResponse(envelope([REVENUE, ORDERS], options))
      }
      return jsonResponse({}, 404)
    }),
  )
}

async function renderResults() {
  const view = render(
    <MemoryRouter initialEntries={['/results']}>
      <AuthProvider>
        <Results />
      </AuthProvider>
    </MemoryRouter>,
  )
  await screen.findByText('Agent run history')
  await waitFor(() => expect(resultCalls.length).toBeGreaterThan(0))
  return view
}

function lastCall(): string {
  return resultCalls[resultCalls.length - 1]
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

/**
 * Just the rows.
 *
 * The whole document is the wrong haystack for "this KPI is no longer listed":
 * the KPI filter's own `<option>` elements name every KPI in the company, so a
 * body-wide assertion would report a row that has in fact gone.
 */
function tableText(): string {
  return document.querySelector('tbody')?.textContent ?? ''
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the Results screen, read by an analyst', () => {
  beforeEach(() => setUp(['analytics.read', 'kpi.read', 'investigation.read']))

  it('opens with every stored result and no narrowing in the request', async () => {
    await renderResults()

    expect(lastCall()).toContain('/companies/company-1/results')
    expect(lastCall()).not.toContain('?')
    expect(tableText()).toContain('Net Revenue')
    expect(tableText()).toContain('Orders')
    // The server's own deviation, not one recomputed here.
    expect(tableText()).toContain('-37.2%')
  })

  it('asks the server for one KPI when the KPI filter is set', async () => {
    await renderResults()
    const before = resultCalls.length

    fireEvent.change(screen.getByLabelText(/^KPI/), { target: { value: 'net_revenue' } })

    await waitFor(() => expect(resultCalls.length).toBeGreaterThan(before))
    expect(lastCall()).toContain('kpi_key=net_revenue')
  })

  it('asks the server for one date when the date filter is set', async () => {
    await renderResults()
    const before = resultCalls.length

    fireEvent.change(screen.getByLabelText(/^Date/), { target: { value: '2026-08-28' } })

    await waitFor(() => expect(resultCalls.length).toBeGreaterThan(before))
    expect(lastCall()).toContain('target_date=2026-08-28')
  })

  it('asks the server for one status when a status pill is pressed', async () => {
    await renderResults()
    const before = resultCalls.length

    fireEvent.click(screen.getByRole('button', { name: /abnormal/i }))

    await waitFor(() => expect(resultCalls.length).toBeGreaterThan(before))
    expect(lastCall()).toContain('status=ABNORMAL')
  })

  it('offers the dimension filter and sends it, because findings are readable here', async () => {
    await renderResults()
    const before = resultCalls.length

    fireEvent.change(screen.getByLabelText(/^Dimension/), { target: { value: 'region' } })

    await waitFor(() => expect(resultCalls.length).toBeGreaterThan(before))
    expect(lastCall()).toContain('dimension=region')
  })

  it('searches in the browser without issuing a request', async () => {
    await renderResults()
    const before = resultCalls.length

    fireEvent.change(screen.getByLabelText(/^Search/), { target: { value: 'orders' } })

    // The row that does not match is gone, and nothing was fetched to do it.
    await waitFor(() => expect(tableText()).not.toContain('Net Revenue'))
    expect(tableText()).toContain('Orders')
    expect(resultCalls.length).toBe(before)
  })

  it('finds a result by a dimension somebody recorded a finding against', async () => {
    await renderResults()

    fireEvent.change(screen.getByLabelText(/^Search/), { target: { value: 'Metro West' } })

    await waitFor(() => expect(tableText()).not.toContain('Orders'))
    expect(tableText()).toContain('Net Revenue')
  })

  it('keeps the company total visible while narrowed, and clears back to the full list', async () => {
    await renderResults()

    fireEvent.change(screen.getByLabelText(/^KPI/), { target: { value: 'net_revenue' } })
    await waitFor(() => expect(bodyText()).toContain('of 37 stored'))

    fireEvent.click(screen.getAllByRole('button', { name: /clear filters/i })[0])

    await waitFor(() => expect(lastCall()).not.toContain('kpi_key'))
    expect(screen.queryByRole('button', { name: /clear filters/i })).toBeNull()
  })
})

describe('the Results screen, read by a viewer who may not read findings', () => {
  beforeEach(() => setUp(['analytics.read'], { ...OPTIONS, dimensions: [] }))

  it('offers no dimension filter it could not honour', async () => {
    await renderResults()

    expect(screen.queryByLabelText(/^Dimension/)).toBeNull()
    // The filters the reader does have are all still there.
    expect(screen.getByLabelText(/^KPI/)).toBeTruthy()
    expect(screen.getByLabelText(/^Date/)).toBeTruthy()
    expect(screen.getByLabelText(/^Search/)).toBeTruthy()
  })
})
