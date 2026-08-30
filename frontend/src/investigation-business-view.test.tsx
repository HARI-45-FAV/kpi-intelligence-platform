/**
 * The investigation surface ranks parts of a business without judging them.
 *
 * Three negative requirements are pinned here, and all three drift in the same
 * direction — towards a screen that is slightly more useful and slightly untrue:
 *
 * 1. **A share is not a verdict.** The only status on the screen belongs to the
 *    KPI, carried through from detection. No contributor gets one, so the stub
 *    below deliberately sends contributors carrying `status` and
 *    `modified_z_score` fields — which the real server never returns — and the
 *    assertions check they cannot reach the document even when supplied.
 * 2. **Method stays under the technical details area.** The stub returns the full
 *    evidence block an entitled caller receives: the generated SQL, the comparable
 *    dates, the count of values a row scope withheld. So the forbidden strings are
 *    present in the data the screen received, and their absence from the business
 *    view is a real result rather than an artefact of a thin fixture. The check
 *    excludes `<details>` subtrees, because "shown on request" is the requirement,
 *    not "deleted".
 * 3. **The browser does no arithmetic.** The stub's `share_pct` for the leading
 *    contributor (−72.5%) does not agree with its own change and movement
 *    (−9,000,000 of −15,000,000, which is 60%). That is not realistic — the server
 *    derives both consistently — but it is the only way to tell "printed the
 *    server's field" apart from "recomputed it locally", and a share recomputed in
 *    a browser would be a second answer to a question that already has one.
 *
 * The drill-down test pins the fourth rule, which is about what the client *sends*:
 * coordinates only. A request carrying a movement would let the page it came from
 * decide what gets apportioned.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import { formatCurrency } from './components/format'
import Investigation from './pages/Investigation'

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

/**
 * The registry response, in the envelope the server actually sends.
 *
 * `GET /companies/{id}/kpi-contracts` returns `{company_id, contracts, count}`,
 * never a bare list. This stub used to be the bare list, which is how the page
 * came to read `contracts.data` as an array: the suite stayed green and the real
 * screen threw `.find is not a function` during render, so it never mounted.
 * A stub that disagrees with the server tests the stub.
 */
const CONTRACTS = {
  company_id: 'company-1',
  count: 1,
  contracts: [
    {
      kpi_id: 'revenue',
      kpi_definition_id: 'kpidef-1',
      kpi_version_id: 'kpiver-1',
      name: 'Revenue',
      version: 1,
      status: 'ACTIVE',
      business_definition: 'Net revenue recognised on the order date.',
      kind: 'MEASURE',
      formula: 'SUM(orders.net_revenue)',
      formula_spec: {},
      filters: [],
      is_additive: true,
      additivity_note: 'Parts sum to the whole.',
      unit: 'currency',
      currency: 'INR',
      direction: 'HIGHER_IS_BETTER',
      time_field: 'order_date',
      time_grain: 'DAY',
      source: {},
      dimensions: [],
    },
  ],
}

/** The KPI's own approved breakdowns, and the hierarchy it declared. */
const DIMENSIONS = {
  kpi_key: 'revenue',
  kpi_name: 'Revenue',
  kpi_version: 1,
  dimensions: [
    { name: 'region', is_default: true, hierarchy: ['channel'], approx_cardinality: 4, notes: null },
    { name: 'channel', is_default: false, hierarchy: [], approx_cardinality: 2, notes: null },
  ],
}

/** Everything the business view is forbidden to print, as the server sends it. */
const EVIDENCE = {
  kpi_version: 1,
  kpi_version_id: 'kpiver-1',
  detection_run_id: 'detrun-1',
  contribution_run_id: 'contrun-1',
  dimension: 'region',
  additive: true,
  reference_dates: ['2026-08-14', '2026-08-21'],
  withheld_by_scope: 0,
  queries: [
    'SELECT region, SUM(orders.net_revenue) FROM orders WHERE order_date = ? GROUP BY region',
  ],
}

/**
 * A contributor as the screen must survive receiving it: the fields the server
 * really returns, plus a verdict and a z-score it never does.
 */
function part(
  label: string,
  change: number,
  sharePct: number,
  actual: number,
  expected: number,
) {
  return {
    entity: label,
    label,
    actual,
    expected,
    change,
    share_pct: sharePct,
    absolute_share_pct: Math.abs(sharePct),
    reference_count: 12,
    matched_rows: 40,
    note: null,
    // Neither of these exists in the API contract. They are here so the assertions
    // about their absence mean something.
    status: 'ANOMALOUS',
    modified_z_score: -9.81,
  }
}

const REGION_RESULT = {
  kpi: 'Revenue',
  kpi_key: 'revenue',
  target_date: '2026-08-28',
  dimension: 'region',
  path: [],
  actual: 35_000_000,
  expected: 50_000_000,
  movement: -15_000_000,
  status: 'ABNORMAL',
  comparison: 'Comparable Fridays',
  unit: 'currency',
  currency: 'INR',
  contributors: [
    // −9,000,000 of −15,000,000 is 60%. The server says 72.5%. See the docstring.
    part('North', -9_000_000, -72.5, 21_000_000, 30_000_000),
    part('South', -4_000_000, -26.7, 8_000_000, 12_000_000),
    part('West', -1_000_000, -6.7, 3_000_000, 4_000_000),
    part('East', -1_000_000, -6.7, 3_000_000, 4_000_000),
  ],
  top_k: 10,
  ranked_count: 4,
  explained_pct: 100,
  unexplained_pct: 0,
  leader_is_sufficient: true,
  sufficiency_pct: 60,
  shares_available: true,
  next_dimensions: ['channel'],
  notes: [],
}

const CHANNEL_RESULT = {
  ...REGION_RESULT,
  dimension: 'channel',
  path: [{ dimension: 'region', value: 'North' }],
  contributors: [
    part('STORE', -7_000_000, -46.7, 12_000_000, 19_000_000),
    part('ONLINE', -2_000_000, -13.3, 9_000_000, 11_000_000),
  ],
  ranked_count: 2,
  explained_pct: 60,
  unexplained_pct: 40,
  leader_is_sufficient: false,
  next_dimensions: [],
}

const ENTITY_RESULT = {
  mode: 'entity',
  result: {
    kpi: 'Revenue',
    kpi_key: 'revenue',
    dimension: 'region',
    entity: 'South',
    unit: 'currency',
    currency: 'INR',
    points: [
      { date: '2026-08-26', value: 11_000_000, matched_rows: 30, note: null },
      { date: '2026-08-27', value: 12_000_000, matched_rows: 31, note: null },
      { date: '2026-08-28', value: 8_000_000, matched_rows: 22, note: null },
    ],
    latest: 8_000_000,
    typical: 11_500_000,
    change_vs_typical: -3_500_000,
    change_pct_vs_typical: -30.4,
    observed_days: 3,
    notes: [],
  },
  evidence: {
    kpi_version: 1,
    queries: ["SELECT SUM(orders.net_revenue) FROM orders WHERE region = 'South'"],
  },
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

let contributionCalls: any[]
let analysisCalls: any[]

function stubFetch(permissions: string[], dimensions = DIMENSIONS) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/auth/session')) {
      return jsonResponse({ user: USER, memberships: [membership(permissions)] })
    }
    if (url.includes('/kpi-contracts')) return jsonResponse(CONTRACTS)
    if (url.includes('/investigation/dimensions')) return jsonResponse(dimensions)
    if (url.includes('/investigation/contribution')) {
      const body = JSON.parse(String(init?.body ?? '{}'))
      contributionCalls.push(body)
      return jsonResponse({
        result: body.dimension === 'channel' ? CHANNEL_RESULT : REGION_RESULT,
        evidence: EVIDENCE,
      })
    }
    if (url.includes('/investigation/analysis')) {
      const body = JSON.parse(String(init?.body ?? '{}'))
      analysisCalls.push(body)
      if (body.entity) return jsonResponse(ENTITY_RESULT)
      return jsonResponse({ mode: 'contribution', result: REGION_RESULT, evidence: EVIDENCE })
    }
    return jsonResponse({})
  })
}

async function renderInvestigation() {
  const view = render(
    <MemoryRouter initialEntries={['/investigation']}>
      <AuthProvider>
        <Investigation />
      </AuthProvider>
    </MemoryRouter>,
  )
  await screen.findByText('Investigation')
  return view
}

async function press(name: RegExp) {
  const button = await screen.findByRole('button', { name })
  await act(async () => {
    button.click()
  })
  return button
}

async function explainTheMovement() {
  await screen.findByRole('option', { name: /Revenue/ })
  const button = (await screen.findByRole('button', {
    name: /explain the movement/i,
  })) as HTMLButtonElement
  // The button is disabled until the KPI registry has answered. Clicking early
  // would send nothing and the assertions would fail for the wrong reason.
  await waitFor(() => expect(button.disabled).toBe(false))
  await act(async () => {
    button.click()
  })
  await waitFor(() => expect(contributionCalls).toHaveLength(1))
  await screen.findByText('North')
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

/**
 * What the screen shows without being asked. `<details>` subtrees are removed
 * rather than the whole document scanned, because the requirement is that method
 * is available on request — not that it is unavailable.
 */
function businessText(): string {
  const clone = document.body.cloneNode(true) as HTMLElement
  clone.querySelectorAll('details').forEach((node) => node.remove())
  return clone.textContent ?? ''
}

function setUp(permissions: string[]) {
  contributionCalls = []
  analysisCalls = []
  localStorage.clear()
  localStorage.setItem(
    'bi.ai.session',
    JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
  )
  localStorage.setItem('bi.ai.company', 'company-1')
  vi.stubGlobal('fetch', stubFetch(permissions))
}

describe('the investigation surface', () => {
  beforeEach(() => setUp(['analytics.read', 'investigation.read', 'kpi.read']))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the KPI movement and the parts that account for it', async () => {
    await renderInvestigation()
    await explainTheMovement()

    // The whole, as detection measured it.
    expect(screen.getByText(formatCurrency(35_000_000, 'INR', true))).toBeTruthy()
    expect(screen.getByText(formatCurrency(50_000_000, 'INR', true))).toBeTruthy()
    expect(screen.getAllByText(/Comparable Fridays/).length).toBeGreaterThan(0)

    // The parts, ranked as the server ranked them.
    for (const label of ['North', 'South', 'West', 'East']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    expect(screen.getByText('By region')).toBeTruthy()
    expect(screen.getAllByText(/4 of 4 shown/).length).toBeGreaterThan(0)
  })

  it('keeps the empty-dimension state stable and does not call the API', async () => {
    contributionCalls = []
    analysisCalls = []
    vi.stubGlobal('fetch', stubFetch(['analytics.read', 'investigation.read', 'kpi.read'], {
      kpi_key: 'revenue',
      kpi_name: 'Revenue',
      kpi_version: 1,
      dimensions: [],
    }))

    await renderInvestigation()

    const button = (await screen.findByRole('button', {
      name: /explain the movement/i,
    })) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(await screen.findByText(/has no approved dimension to break down by/i)).toBeTruthy()

    await act(async () => {
      button.click()
    })

    expect(contributionCalls).toHaveLength(0)
  })

  it('uses the hardcoded NovaMart dimension map when the KPI has no registered dimensions', async () => {
    const contractWithNoDimensions = {
      ...CONTRACTS,
      contracts: [{ ...CONTRACTS.contracts[0], kpi_id: 'average_order_value', name: 'average_order_value', dimensions: [] }],
    }

    contributionCalls = []
    analysisCalls = []
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/auth/session')) {
        return jsonResponse({ user: USER, memberships: [membership(['analytics.read', 'investigation.read', 'kpi.read'])] })
      }
      if (url.includes('/kpi-contracts')) return jsonResponse(contractWithNoDimensions)
      if (url.includes('/investigation/dimensions')) return jsonResponse({
        kpi_key: 'average_order_value',
        kpi_name: 'average_order_value',
        kpi_version: 1,
        dimensions: [],
      })
      if (url.includes('/investigation/contribution')) {
        const body = JSON.parse(String(init?.body ?? '{}'))
        contributionCalls.push(body)
        return jsonResponse({
          result: body.dimension === 'channel' ? CHANNEL_RESULT : REGION_RESULT,
          evidence: EVIDENCE,
        })
      }
      return jsonResponse({})
    })

    await renderInvestigation()

    const button = (await screen.findByRole('button', {
      name: /explain the movement/i,
    })) as HTMLButtonElement
    await waitFor(() => expect(button.disabled).toBe(false))
    expect(await screen.findByText('By region')).toBeTruthy()

    await act(async () => {
      button.click()
    })

    expect(contributionCalls).toHaveLength(1)
    expect(contributionCalls[0].dimension).toBeNull()
  })

  it('uses the saved KPI contract dimensions when the investigation endpoint is empty', async () => {
    const contractWithDimensions = {
      ...CONTRACTS,
      contracts: [
        {
          ...CONTRACTS.contracts[0],
          dimensions: [
            {
              name: 'region',
              table: 'orders',
              column: 'region',
              allowed: true,
              is_default_breakdown: true,
              approx_cardinality: 4,
              monitoring_note: 'valid breakdown',
            },
            {
              name: 'channel',
              table: 'orders',
              column: 'channel',
              allowed: true,
              is_default_breakdown: false,
              approx_cardinality: 3,
              monitoring_note: 'valid breakdown',
            },
          ],
        },
      ],
    }

    contributionCalls = []
    analysisCalls = []
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/auth/session')) {
        return jsonResponse({ user: USER, memberships: [membership(['analytics.read', 'investigation.read', 'kpi.read'])] })
      }
      if (url.includes('/kpi-contracts')) return jsonResponse(contractWithDimensions)
      if (url.includes('/investigation/dimensions')) return jsonResponse({
        kpi_key: 'revenue',
        kpi_name: 'Revenue',
        kpi_version: 1,
        dimensions: [],
      })
      if (url.includes('/investigation/contribution')) {
        const body = JSON.parse(String(init?.body ?? '{}'))
        contributionCalls.push(body)
        return jsonResponse({
          result: body.dimension === 'channel' ? CHANNEL_RESULT : REGION_RESULT,
          evidence: EVIDENCE,
        })
      }
      return jsonResponse({})
    })

    await renderInvestigation()

    const button = (await screen.findByRole('button', {
      name: /explain the movement/i,
    })) as HTMLButtonElement
    await waitFor(() => expect(button.disabled).toBe(false))
    expect(await screen.findByText('By region')).toBeTruthy()

    await act(async () => {
      button.click()
    })

    expect(contributionCalls).toHaveLength(1)
    expect(contributionCalls[0].dimension).toBeNull()
  })

  /**
   * The load-bearing test. Every part below carries a `status` and a
   * `modified_z_score` in the response the screen received.
   */
  it('gives no contributor a verdict, and shows the KPI status exactly once', async () => {
    await renderInvestigation()
    await explainTheMovement()

    expect(screen.getAllByText('ABNORMAL')).toHaveLength(1)

    const rendered = bodyText()
    expect(rendered).not.toContain('ANOMALOUS')
    expect(rendered).not.toContain('9.81')
    for (const term of ['anomal', 'z-score', 'z_score', 'caused by', 'because of']) {
      expect(rendered.toLowerCase()).not.toContain(term)
    }
  })

  it('prints the share the server calculated rather than recomputing it', async () => {
    await renderInvestigation()
    await explainTheMovement()

    expect(screen.getAllByText(/72\.5% of the movement/).length).toBe(1)
    // What a browser-side change / movement would have produced for North.
    expect(businessText()).not.toContain('60.0% of the movement')
  })

  it('keeps the queries and the comparable dates out of the business view', async () => {
    await renderInvestigation()
    await explainTheMovement()

    const business = businessText().toLowerCase()
    for (const term of [
      'select',
      'group by',
      'net_revenue',
      'order_date',
      'median',
      'withheld by access scope',
      'detrun-1',
      '2026-08-14',
    ]) {
      expect(business).not.toContain(term)
    }

    // And they are one disclosure away, not deleted.
    const details = bodyText()
    expect(details).toContain('Technical details')
    expect(details).toContain('GROUP BY region')
    expect(details).toContain('detrun-1')
  })

  it('says one part is enough to stop at without calling it a problem', async () => {
    await renderInvestigation()
    await explainTheMovement()

    const rendered = bodyText()
    expect(rendered).toContain('accounts for')
    expect(rendered).toContain('sufficient explanation')
    // The suggestion is to stop reading, not a finding about North.
    expect(rendered.toLowerCase()).not.toContain('north is')
  })

  it('drills down by sending coordinates and no figures', async () => {
    await renderInvestigation()
    await explainTheMovement()

    // Every listed part offers the same drill, one per row. The leader's is the
    // one taken here, which is what makes the path assertion below meaningful.
    const drills = await screen.findAllByRole('button', { name: /break down by channel/i })
    expect(drills).toHaveLength(4)
    await act(async () => {
      drills[0].click()
    })
    await waitFor(() => expect(contributionCalls).toHaveLength(2))

    expect(contributionCalls[1]).toEqual({
      kpi_id: 'revenue',
      target_date: expect.any(String),
      dimension: 'channel',
      path: [{ dimension: 'region', value: 'North' }],
      top_k: 10,
    })
    // Nothing measured is sent: the server re-reads the movement from the run.
    for (const key of ['actual', 'expected', 'movement', 'status', 'share_pct']) {
      expect(contributionCalls[1]).not.toHaveProperty(key)
    }

    await screen.findByText('By channel')
    expect(screen.getByText('STORE')).toBeTruthy()
    // The whole is still the KPI's movement, so the parts here explain part of it.
    expect(screen.getAllByText(/60% of the movement/).length).toBeGreaterThan(0)
    expect(screen.getByText(/region: North/)).toBeTruthy()
  })

  it('analyses one named entity and nothing else', async () => {
    await renderInvestigation()
    await press(/manual analysis/i)

    const entity = await screen.findByPlaceholderText(/a value of the dimension above/i)
    fireEvent.change(entity, { target: { value: 'South' } })
    await press(/^run$/i)
    await waitFor(() => expect(analysisCalls).toHaveLength(1))

    expect(analysisCalls[0].entity).toBe('South')
    await screen.findByText(/South over 3 day/)

    // Its own measured history, with no verdict attached to it.
    expect(screen.getAllByText(formatCurrency(8_000_000, 'INR', true)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(formatCurrency(11_500_000, 'INR', true)).length).toBe(1)
    expect(screen.queryByText('ABNORMAL')).toBeNull()

    // And no other part of the business was touched or shown.
    const rendered = bodyText()
    for (const other of ['North', 'West', 'East', 'STORE', 'ONLINE']) {
      expect(rendered).not.toContain(other)
    }
    expect(contributionCalls).toHaveLength(0)
  })
})

describe('a reader without the investigation permission', () => {
  beforeEach(() => setUp(['analytics.read', 'kpi.read']))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('is told what is missing instead of being shown an empty breakdown', async () => {
    await renderInvestigation()

    expect(await screen.findByText(/investigation.read/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /explain the movement/i })).toBeNull()
    expect(contributionCalls).toHaveLength(0)
  })
})
