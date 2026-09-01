/**
 * The monitoring dashboard counts stored rows, and says what it is not saying.
 *
 * Three properties are pinned here, each of which would be invisible if it broke:
 *
 *  1. **Withheld is not zero.** The server returns `null` for every investigation
 *     figure when the reader lacks `investigation.read` — findings tallies,
 *     `open_findings`, `has_contribution`. A dashboard is precisely where the
 *     tempting bug lives: render `{data.findings_open}` and a withheld field
 *     silently becomes "0 open", which is a claim about other people's work that
 *     this reader is not entitled to and that is probably false. So the test that
 *     matters is the *negative* one: the restricted reader's document must not
 *     contain a findings strip, an "Investigate" link, or an open-notes chip.
 *  2. **No scheduler is implied.** The server's note is rendered on the screen, and
 *     a company that has never been evaluated reads as never evaluated rather than
 *     as monitored-and-fine.
 *  3. **No arithmetic in the browser.** As in the detection-surface test, the
 *     fixture's `deviation_pct` is deliberately inconsistent with its own actual
 *     and expected, so "printed the server's field" is distinguishable from
 *     "recomputed it here".
 *
 * The fixture also carries a legacy `WATCH` verdict, because the dev database has
 * four of them: the tiles must sum to the evaluated total, which means an
 * unrecognised status has to be counted and named rather than folded into NORMAL.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import type { MonitoringMovement } from './api/types'
import MonitoringOverview from './components/MonitoringOverview'

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
    role_key: permissions.includes('investigation.read') ? 'ANALYST' : 'VIEWER',
    role_name: 'Reader',
    status: 'ACTIVE',
    is_admin_role: false,
    permissions,
  }
}

const REVENUE_MOVEMENT = {
  detection_run_id: 'run-revenue',
  kpi_id: 'kpi-revenue',
  kpi_key: 'net_revenue',
  kpi_name: 'net_revenue',
  target_date: '2026-08-28',
  status: 'ABNORMAL',
  actual_value: 6000000,
  expected_value: 10250000,
  deviation_absolute: -4250000,
  // Deliberately not derivable from the two figures above. See the file docstring.
  deviation_pct: -37.2,
  unit: 'currency',
  currency: 'INR',
  headline: 'Revenue came in well below comparable Fridays.',
  has_contribution: true,
  open_findings: 2,
}

const ORDERS_MOVEMENT = {
  detection_run_id: 'run-orders',
  kpi_id: 'kpi-orders',
  kpi_key: 'orders',
  kpi_name: 'orders',
  target_date: '2026-08-28',
  status: 'ABNORMAL',
  actual_value: 4120,
  expected_value: 4400,
  deviation_absolute: -280,
  deviation_pct: -6.4,
  unit: 'count',
  currency: null,
  headline: null,
  has_contribution: false,
  open_findings: 0,
}

/** The same fields the server withholds, withheld. */
function withheld(movement: MonitoringMovement): MonitoringMovement {
  return { ...movement, has_contribution: null, open_findings: null }
}

const FINDING = {
  id: 'finding-1',
  kpi_key: 'net_revenue',
  kpi_name: 'net_revenue',
  target_date: '2026-08-28',
  title: 'Metro West drop is a stock-out',
  note: 'Warehouse confirmed the outage.',
  status: 'IN_PROGRESS',
  dimension: 'region',
  entity: 'Metro West',
  path: [{ dimension: 'region', value: 'Metro West' }],
  scope_label: 'region: Metro West',
  detection_run_id: 'run-revenue',
  created_by_email: 'ana@aurora-retail.example.com',
  updated_by_email: 'ana@aurora-retail.example.com',
  created_at: '2026-08-29T09:00:00Z',
  updated_at: '2026-08-29T11:30:00Z',
  resolved_at: null,
}

function populated(mayInvestigate: boolean) {
  return {
    window_days: 90,
    window_from: '2026-06-01',
    window_to: '2026-08-28',
    last_evaluated_at: '2026-08-29T04:00:00Z',
    counts: {
      kpis_monitored: 4,
      evaluated: 12,
      normal: 7,
      abnormal: 2,
      low_confidence: 2,
      unrecognised: 1,
      unrecognised_statuses: ['WATCH'],
      not_evaluated: 1,
    },
    kpis: [
      {
        kpi_id: 'kpi-revenue',
        kpi_key: 'net_revenue',
        kpi_name: 'net_revenue',
        lifecycle_status: 'ACTIVE',
        active_version: 1,
        latest_status: 'ABNORMAL',
        latest_target_date: '2026-08-28',
        latest_deviation_pct: -37.2,
        latest_executed_at: '2026-08-29T04:00:00Z',
        evaluated_in_window: 9,
      },
      {
        kpi_id: 'kpi-units',
        kpi_key: 'units_sold',
        kpi_name: 'units_sold',
        lifecycle_status: 'PROPOSED',
        active_version: null,
        latest_status: null,
        latest_target_date: null,
        latest_deviation_pct: null,
        latest_executed_at: null,
        evaluated_in_window: 0,
      },
    ],
    biggest_movements: mayInvestigate
      ? [REVENUE_MOVEMENT, ORDERS_MOVEMENT]
      : [withheld(REVENUE_MOVEMENT), withheld(ORDERS_MOVEMENT)],
    recent_abnormal: mayInvestigate ? [REVENUE_MOVEMENT] : [withheld(REVENUE_MOVEMENT)],
    recent_runs: [
      {
        detection_run_id: 'run-revenue',
        agent_run_id: null,
        kpi_id: 'kpi-revenue',
        kpi_key: 'net_revenue',
        kpi_name: 'net_revenue',
        target_date: '2026-08-28',
        status: 'ABNORMAL',
        deviation_pct: -37.2,
        executed_at: '2026-08-29T04:00:00Z',
      },
    ],
    findings_open: mayInvestigate ? 2 : null,
    findings_in_progress: mayInvestigate ? 1 : null,
    findings_resolved: mayInvestigate ? 3 : null,
    recent_findings: mayInvestigate ? [FINDING] : [],
    monitoring_note:
      'Detection runs when it is triggered — this platform has no scheduler in this version.',
  }
}

const NEVER_EVALUATED = {
  ...populated(true),
  window_from: null,
  window_to: null,
  last_evaluated_at: null,
  counts: {
    kpis_monitored: 3,
    evaluated: 0,
    normal: 0,
    abnormal: 0,
    low_confidence: 0,
    unrecognised: 0,
    unrecognised_statuses: [],
    not_evaluated: 3,
  },
  kpis: [],
  biggest_movements: [],
  recent_abnormal: [],
  recent_runs: [],
  findings_open: 0,
  findings_in_progress: 0,
  findings_resolved: 0,
  recent_findings: [],
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

function setUp(permissions: string[], monitoring: unknown) {
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
      if (url.includes('/monitoring')) return jsonResponse(monitoring)
      return jsonResponse({}, 404)
    }),
  )
}

async function renderOverview() {
  const view = render(
    <MemoryRouter initialEntries={['/monitoring']}>
      <AuthProvider>
        <MonitoringOverview />
      </AuthProvider>
    </MemoryRouter>,
  )
  await screen.findByText('What has been evaluated')
  return view
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

function hrefs(): string[] {
  return Array.from(document.querySelectorAll('a')).map((anchor) => anchor.getAttribute('href') ?? '')
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the monitoring dashboard, read by an analyst', () => {
  beforeEach(() => setUp(['analytics.read', 'kpi.read', 'investigation.read'], populated(true)))

  it('counts stored evaluations and keeps the tiles summing to the total', async () => {
    await renderOverview()

    // 7 normal + 2 abnormal + 2 low confidence + 1 unrecognised = 12 evaluated.
    expect(screen.getByText('Evaluated').parentElement?.textContent).toContain('12')
    expect(screen.getByText('Normal').parentElement?.textContent).toContain('7')
    expect(screen.getByText('Abnormal').parentElement?.textContent).toContain('2')
    expect(screen.getByText('Low confidence').parentElement?.textContent).toContain('2')
    // A KPI with no run in the window is named as unevaluated, never as a pass.
    expect(screen.getByText('Not evaluated').parentElement?.textContent).toContain('1')
    expect(bodyText()).toContain('No run in this window — not a pass')
  })

  it('names a verdict it does not recognise instead of folding it into a real one', async () => {
    await renderOverview()

    const rendered = bodyText()
    expect(rendered).toContain('WATCH')
    expect(rendered).toContain('does not recognise')
  })

  it('states that nothing is scheduled, on the screen', async () => {
    await renderOverview()

    expect(bodyText()).toContain('has no scheduler in this version')
  })

  it('prints the deviation the server calculated rather than recomputing it', async () => {
    await renderOverview()

    expect(screen.getAllByText('-37.2%').length).toBeGreaterThan(0)
    // What a browser-side (actual - expected) / expected would have produced.
    expect(screen.queryByText('-41.5%')).toBeNull()
  })

  it('makes every movement a way into its result and its investigation', async () => {
    await renderOverview()

    const links = hrefs()
    expect(links).toContain('/results/run-revenue')
    expect(links).toContain('/investigation?kpi=net_revenue&date=2026-08-28')
    // A stored breakdown reads as "review"; one that does not exist reads as "investigate".
    expect(screen.getAllByText('Review investigation').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Investigate').length).toBeGreaterThan(0)
  })

  it('does not list the same abnormal result twice', async () => {
    await renderOverview()

    // The single recent abnormality is already under the biggest movements.
    expect(bodyText()).toContain('already listed under the biggest movements')
    expect(hrefs().filter((href) => href === '/results/run-revenue')).toHaveLength(2)
  })

  it('shows the findings tallies and the notes on a movement', async () => {
    await renderOverview()

    const rendered = bodyText()
    expect(rendered).toContain('2 open')
    expect(rendered).toContain('1 in progress')
    expect(rendered).toContain('3 resolved')
    expect(rendered).toContain('2 open notes')
    expect(screen.getByText('Metro West drop is a stock-out')).toBeTruthy()
  })
})

/**
 * The load-bearing test. A VIEWER holds `analytics.read` but not
 * `investigation.read`, so the server sends nulls; none of them may be rendered as
 * a zero, and no route into the investigation surface may be offered.
 */
describe('the monitoring dashboard, read without investigation access', () => {
  beforeEach(() => setUp(['analytics.read', 'kpi.read'], populated(false)))

  it('still shows what was evaluated and what moved', async () => {
    await renderOverview()

    expect(screen.getByText('Abnormal').parentElement?.textContent).toContain('2')
    expect(hrefs()).toContain('/results/run-revenue')
  })

  it('says nothing at all about findings rather than saying zero', async () => {
    await renderOverview()

    const rendered = bodyText()
    expect(rendered).not.toContain('Investigation findings')
    expect(rendered).not.toContain('0 open')
    expect(rendered).not.toContain('open note')
    expect(rendered).not.toContain('Recent findings')
    expect(rendered).not.toContain('Metro West')
  })

  it('offers no route into the investigation surface', async () => {
    await renderOverview()

    expect(screen.queryByText('Investigate')).toBeNull()
    expect(screen.queryByText('Review investigation')).toBeNull()
    expect(hrefs().some((href) => href.startsWith('/investigation'))).toBe(false)
  })
})

describe('a company that has never been evaluated', () => {
  beforeEach(() => setUp(['analytics.read', 'kpi.read', 'investigation.read'], NEVER_EVALUATED))

  it('reads as never evaluated, not as monitored and fine', async () => {
    await renderOverview()

    const rendered = bodyText()
    expect(rendered).toContain('nothing has ever been evaluated for this company')
    expect(rendered).toContain('No evaluation stored in the last 90 days')
    expect(rendered).toContain('No movement recorded in this window')
    expect(rendered).toContain('No detection has been run in this window')
    expect(rendered).toContain('No KPI is registered yet')
  })
})

describe('a reader without analytics access', () => {
  beforeEach(() => setUp(['company.read'], populated(true)))

  it('is shown nothing, and the dashboard is never requested', async () => {
    render(
      <MemoryRouter initialEntries={['/monitoring']}>
        <AuthProvider>
          <MonitoringOverview />
        </AuthProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(localStorage.getItem('bi.ai.company')).toBe('company-1'))

    expect(screen.queryByText('What has been evaluated')).toBeNull()
    const calls = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
    expect(calls.some((call) => String(call[0]).includes('/monitoring'))).toBe(false)
  })
})
