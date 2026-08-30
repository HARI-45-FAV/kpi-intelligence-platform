/**
 * Clicking a KPI card in "KPI evaluation" opens the detail popup.
 *
 * This is the dashboard's only path from the grid to a single KPI, and it was
 * untested — which is how it came to be broken while the whole suite stayed
 * green. Three things have to line up for the click to work, and each of them is
 * an identifier agreement between two files rather than something visible on the
 * screen:
 *
 *  - `/kpi-contracts` puts the KPI's *business key* in `kpi_id` (the uuid lives in
 *    `kpi_definition_id`), while a detection result carries the same business key
 *    in `kpi_key`. The grid joins the two, so a card is only clickable when that
 *    join finds its result.
 *  - The popup renders through `Modal`, which is `fixed inset-0`, so it is outside
 *    the panel it was launched from and cannot be found by looking inside the card.
 *  - The popup asks the Copilot for prose on mount. That request is allowed to
 *    fail — the governed figures are already measured and must still be shown —
 *    so the stub below refuses it deliberately, and the numbers are asserted
 *    anyway.
 *
 * The click is dispatched on the rendered element rather than by calling the
 * handler, so an element that is not actually a control fails here.
 */

import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import { formatCurrency } from './components/format'
import { CopilotProvider } from './copilot/CopilotProvider'
import Dashboard from './pages/Dashboard'

const USER = {
  id: 'user-1',
  email: 'admin@aurora-retail.example.com',
  full_name: 'Ada Admin',
  is_active: true,
  is_platform_admin: false,
  created_at: '2026-01-01T00:00:00Z',
}

const MEMBERSHIP = {
  company_id: 'company-1',
  company_name: 'Aurora Retail',
  company_slug: 'aurora-retail',
  role_key: 'ADMIN',
  role_name: 'Administrator',
  status: 'ACTIVE',
  is_admin_role: true,
  permissions: ['analytics.read', 'detection.run', 'kpi.read'],
}

/**
 * A contract as the server exports it: `kpi_id` is the business key and
 * `kpi_definition_id` is the uuid. Getting these the wrong way round is exactly
 * the defect this file guards, so they are deliberately not interchangeable.
 */
function contract(key: string, name: string) {
  return {
    company_id: 'company-1',
    kpi_id: key,
    kpi_definition_id: `def-${key}`,
    kpi_version_id: `ver-${key}`,
    name,
    version: 1,
    status: 'ACTIVE',
    business_definition: `${name}, as governed.`,
    purpose: 'PERFORMANCE',
    kind: 'MEASURE',
    formula: `SUM(orders.${key})`,
    is_additive: true,
    additivity_note: 'Additive: safe to sum across periods and dimensions.',
    unit: 'currency',
    currency: 'INR',
    direction: 'HIGHER_IS_BETTER',
    time_field: 'order_date',
    time_grain: 'DAY',
    timezone: 'Asia/Kolkata',
    dimensions: [],
  }
}

const CONTRACTS = {
  company_id: 'company-1',
  contracts: [contract('revenue', 'Revenue'), contract('refund_value', 'Refund Value')],
  count: 2,
}

const REVENUE_RESULT = {
  kpi: 'Revenue',
  kpi_key: 'revenue',
  target_date: '2026-08-28',
  actual: 6000000,
  expected: 10250000,
  deviation_pct: -37.2,
  deviation_absolute: -4250000,
  status: 'ABNORMAL',
  comparison: 'Comparable Fridays',
  headline: 'Revenue came in well below comparable Fridays.',
  unit: 'currency',
  currency: 'INR',
}

const BATCH = {
  target_date: '2026-08-28',
  agent_run_id: 'agent-run-1',
  results: [{ result: REVENUE_RESULT, run_id: 'run-2', persisted: true }],
  skipped: [{ kpi_id: 'refund_value', reason: 'No rows for 2026-08-28 in the registered source.' }],
  counts: { evaluated: 1, skipped: 1 },
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

let batchCalls: unknown[]
let copilotCalls: unknown[]

function stubFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/auth/session')) {
      return jsonResponse({ user: USER, memberships: [MEMBERSHIP] })
    }
    if (url.includes('/kpi-contracts')) return jsonResponse(CONTRACTS)
    if (url.includes('/detection-runs')) return jsonResponse([])
    if (url.includes('/run-detection/batch')) {
      batchCalls.push(JSON.parse(String(init?.body ?? '{}')))
      return jsonResponse(BATCH)
    }
    // The Copilot is optional and may be switched off entirely. The popup must
    // still open and still show the measured figures.
    if (url.includes('/copilot/chat')) {
      copilotCalls.push(JSON.parse(String(init?.body ?? '{}')))
      return jsonResponse(
        { code: 'copilot_unavailable', message: 'No model is configured.' },
        409,
      )
    }
    return jsonResponse({})
  })
}

async function renderDashboard() {
  const view = render(
    <MemoryRouter initialEntries={['/']}>
      <AuthProvider>
        <CopilotProvider>
          <Dashboard />
        </CopilotProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
  await screen.findByText('Aurora Retail')
  return view
}

async function pressAgentRun() {
  const button = await screen.findByRole('button', { name: /agent run/i })
  await act(async () => {
    button.click()
  })
  await waitFor(() => expect(batchCalls).toHaveLength(1))
  await screen.findByText('ABNORMAL')
}

/** Click a control the way the browser does, and let React flush. */
async function click(element: HTMLElement) {
  await act(async () => {
    element.click()
  })
}

/**
 * The "KPI evaluation" panel alone. Stage Performance Summary renders a card per
 * KPI too, with the same names, so an unscoped query for "Revenue" is ambiguous
 * and would not prove which grid was clicked.
 */
function evaluationPanel(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'KPI evaluation' })
  const panel = heading.closest('section')
  if (!panel) throw new Error('The KPI evaluation panel is not on the page.')
  return panel as HTMLElement
}

/** The card for one KPI in that panel, whatever element it turned out to be. */
function card(name: string): HTMLElement {
  return within(evaluationPanel()).getByText(name).closest('button, div[class*="rounded"]') as HTMLElement
}

describe('the KPI evaluation card opens its detail popup', () => {
  beforeEach(() => {
    batchCalls = []
    copilotCalls = []
    localStorage.clear()
    localStorage.setItem(
      'bi.ai.session',
      JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
    )
    localStorage.setItem('bi.ai.company', 'company-1')
    vi.stubGlobal('fetch', stubFetch())
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('sends the business key the results are keyed by, so the card can find its result', async () => {
    await renderDashboard()
    await pressAgentRun()

    // If the dashboard sent `kpi_definition_id` here, the server would resolve
    // nothing and every card would fall through to the unclickable branch.
    expect(batchCalls[0]).toMatchObject({ kpi_ids: ['refund_value', 'revenue'] })
  })

  it('renders an evaluated KPI as a button and an unevaluated one as inert text', async () => {
    await renderDashboard()
    await pressAgentRun()

    const panel = within(evaluationPanel())
    expect(panel.getByRole('button', { name: /Revenue/ })).toBeTruthy()
    // The skipped KPI explains itself and offers nothing to click.
    expect(panel.queryByRole('button', { name: /Refund Value/ })).toBeNull()
    expect(panel.getByText(/No rows for 2026-08-28/)).toBeTruthy()
  })

  it('opens the popup on click, with the measured figures the server returned', async () => {
    await renderDashboard()
    await pressAgentRun()

    expect(screen.queryByText('Performance explained')).toBeNull()

    await click(card('Revenue'))

    // The popup itself, found at the document level because `Modal` is fixed.
    expect(await screen.findByText('Performance explained')).toBeTruthy()
    // Its figures are the server's, printed unchanged.
    expect(screen.getAllByText(formatCurrency(6000000, 'INR', true)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(formatCurrency(10250000, 'INR', true)).length).toBeGreaterThan(0)
    expect(screen.getAllByText('-37.2%').length).toBeGreaterThan(0)
  })

  /**
   * The assertion that would have caught the original defect.
   *
   * The popup was mounting correctly and holding the right figures; it was simply
   * off-screen, because `position: fixed` resolved against the app shell's frosted
   * content area (`backdrop-filter` creates a containing block) instead of the
   * viewport. jsdom does no layout, so no amount of asserting on *content* can see
   * that. What is observable here is the structural precondition: the overlay must
   * not live inside the page tree at all.
   */
  it('renders the popup outside the page tree, so fixed positioning means the viewport', async () => {
    const { container } = await renderDashboard()
    await pressAgentRun()
    await click(card('Revenue'))

    const dialog = await screen.findByText('Performance explained')
    expect(container.contains(dialog)).toBe(false)
    expect(document.body.contains(dialog)).toBe(true)
  })

  it('does nothing when a KPI that was not evaluated is clicked', async () => {
    await renderDashboard()
    await pressAgentRun()

    await click(card('Refund Value'))

    // No popup, and above all no request for prose about a figure that does not
    // exist. Silence is the correct behaviour here, not an empty modal.
    expect(screen.queryByText('Performance explained')).toBeNull()
    expect(copilotCalls).toHaveLength(0)
  })

  it('stays open and keeps showing the figures when the Copilot refuses', async () => {
    await renderDashboard()
    await pressAgentRun()
    await click(card('Revenue'))
    await screen.findByText('Performance explained')

    await waitFor(() => expect(copilotCalls).toHaveLength(1))
    // Context is read from where the user is standing, and carries no figure.
    expect(copilotCalls[0]).toMatchObject({
      context: { kpi_id: 'revenue', kpi_version: 1, selected_date: '2026-08-28' },
    })
    expect(JSON.stringify(copilotCalls[0])).not.toContain('6000000')

    // The refusal is reported, and the governed numbers survive it.
    expect(await screen.findByText(/No model is configured/)).toBeTruthy()
    expect(screen.getByText('Performance explained')).toBeTruthy()
    expect(screen.getAllByText('-37.2%').length).toBeGreaterThan(0)
  })

  it('closes on the close control', async () => {
    await renderDashboard()
    await pressAgentRun()
    await click(card('Revenue'))
    await screen.findByText('Performance explained')

    await click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByText('Performance explained')).toBeNull())
  })
})
