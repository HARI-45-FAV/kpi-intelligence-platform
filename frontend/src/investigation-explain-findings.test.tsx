/**
 * The two surfaces that close an investigation, and what anchors them.
 *
 * An explanation says what the platform can support about the node on screen; a
 * finding records what the reader concluded about it. Both are only meaningful
 * relative to *which* node, and that is what these tests pin — because the failure
 * mode here is not a crash, it is a screen where every sentence is true and
 * attached to the wrong part of the business.
 *
 * Four properties, each a way that could go wrong:
 *
 * 1. **Nothing to anchor to means nothing offered.** Before an analysis exists
 *    there is no node, so neither panel appears. An "Add a finding" button on a
 *    blank page would invite a conclusion about coordinates nobody has read.
 * 2. **The request carries the node on screen, not the controls.** The explain call
 *    sends the dimension and path of the level being *displayed*. A reader who
 *    drilled to North and asks for an explanation must not be sent the top-level
 *    breakdown's coordinates.
 * 3. **An explanation does not outlive its node.** Drilling clears it. The stub
 *    returns visibly different prose per level, so prose from the level above
 *    remaining on screen is detectable rather than a matter of inspection.
 * 4. **A finding is filed against what was read.** The POST body carries the same
 *    anchor the panel displayed, so the note and the measurement agree about their
 *    subject.
 *
 * The server re-resolves every anchor field against the KPI's approved dimensions
 * and the caller's row scope, so none of this is the access check — it is the check
 * that the client describes the screen honestly.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
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

const DIMENSIONS = {
  kpi_key: 'revenue',
  kpi_name: 'Revenue',
  kpi_version: 1,
  dimensions: [
    { name: 'region', is_default: true, hierarchy: ['channel'], approx_cardinality: 4, notes: null },
    { name: 'channel', is_default: false, hierarchy: [], approx_cardinality: 2, notes: null },
  ],
}

const EVIDENCE = {
  kpi_version: 1,
  dimension: 'region',
  additive: true,
  reference_dates: ['2026-08-14'],
  withheld_by_scope: 0,
  queries: ['SELECT region, SUM(orders.net_revenue) FROM orders GROUP BY region'],
}

function part(label: string, change: number, sharePct: number) {
  return {
    entity: label,
    label,
    actual: 21_000_000,
    expected: 30_000_000,
    change,
    share_pct: sharePct,
    absolute_share_pct: Math.abs(sharePct),
    reference_count: 12,
    matched_rows: 40,
    note: null,
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
  movement_pct: -30,
  status: 'ABNORMAL',
  comparison: 'Comparable Fridays',
  unit: 'currency',
  currency: 'INR',
  contributors: [part('North', -9_000_000, -60), part('South', -6_000_000, -40)],
  top_k: 10,
  ranked_count: 2,
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
  contributors: [part('STORE', -7_000_000, -46.7)],
  ranked_count: 1,
  next_dimensions: [],
}

const ENTITY_RESULT = {
  mode: 'entity',
  result: {
    kpi: 'Revenue',
    kpi_key: 'revenue',
    dimension: 'region',
    entity: 'South',
    target_date: '2026-08-28',
    unit: 'currency',
    currency: 'INR',
    points: [
      { date: '2026-08-27', value: 12_000_000, matched_rows: 31, note: null },
      { date: '2026-08-28', value: 8_000_000, matched_rows: 22, note: null },
    ],
    latest: 8_000_000,
    typical: 11_500_000,
    change_vs_typical: -3_500_000,
    change_pct_vs_typical: -30.4,
    observed_days: 2,
    notes: [],
  },
  evidence: { kpi_version: 1, queries: ['SELECT ...'] },
}

/** The gate: this date was analysed, so an investigation is available. */
const ENTITIES = {
  run_available: true,
  run_state: 'Recorded',
  kpi_status: 'ACTIVE',
  dimension: 'region',
  entities: [
    { entity: 'North', label: 'North', value: 21_000_000, matched_rows: 40 },
    { entity: 'South', label: 'South', value: 8_000_000, matched_rows: 22 },
  ],
}

/**
 * An explanation whose prose names the node it describes.
 *
 * The distinguishing text is the point: it is how "cleared on drill" is told apart
 * from "still showing the level above".
 */
function explanationFor(body: any) {
  const subject = body.entity
    ? `${body.dimension}: ${body.entity}`
    : `the ${body.dimension} breakdown`
  return {
    explanation: {
      subject,
      scope: body.entity ? 'entity' : 'breakdown',
      model_written: false,
      model: null,
      order: ['WHAT HAPPENED'],
      sections: [
        {
          heading: 'WHAT HAPPENED',
          body: `Stored evidence for ${subject} on ${body.target_date}.`,
        },
      ],
      limitations: [],
      citations: [],
      confidence: { level: 'MEDIUM', reasons: ['Twelve comparable periods were available.'] },
      facts: null,
    },
  }
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

let contributionCalls: any[]
let analysisCalls: any[]
let explainCalls: any[]
let findingsGets: string[]
let findingsPosts: any[]

function stubFetch(permissions: string[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (url.endsWith('/auth/session')) {
      return jsonResponse({ user: USER, memberships: [membership(permissions)] })
    }
    if (url.includes('/kpi-contracts')) return jsonResponse(CONTRACTS)
    if (url.includes('/investigation/dimensions')) return jsonResponse(DIMENSIONS)
    if (url.includes('/investigation/entities')) return jsonResponse(ENTITIES)
    if (url.includes('/investigation/explain')) {
      const body = JSON.parse(String(init?.body ?? '{}'))
      explainCalls.push(body)
      return jsonResponse(explanationFor(body))
    }
    if (url.includes('/investigation/findings')) {
      if (method === 'POST') {
        const body = JSON.parse(String(init?.body ?? '{}'))
        findingsPosts.push(body)
        return jsonResponse({ finding: { id: 'find-1', ...body } })
      }
      findingsGets.push(url)
      return jsonResponse({ findings: [], statuses: ['OPEN', 'IN_PROGRESS', 'RESOLVED'] })
    }
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

async function explainTheMovement() {
  await screen.findByRole('option', { name: /Revenue/ })
  const button = (await screen.findByRole('button', {
    name: /explain the movement/i,
  })) as HTMLButtonElement
  await waitFor(() => expect(button.disabled).toBe(false))
  await act(async () => {
    button.click()
  })
  await waitFor(() => expect(contributionCalls).toHaveLength(1))
  await screen.findByText('North')
}

async function press(name: RegExp) {
  const button = await screen.findByRole('button', { name })
  await act(async () => {
    button.click()
  })
  return button
}

function setUp(permissions: string[]) {
  contributionCalls = []
  analysisCalls = []
  explainCalls = []
  findingsGets = []
  findingsPosts = []
  localStorage.clear()
  localStorage.setItem(
    'bi.ai.session',
    JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
  )
  localStorage.setItem('bi.ai.company', 'company-1')
  vi.stubGlobal('fetch', stubFetch(permissions))
}

describe('explanation and findings on the investigation surface', () => {
  beforeEach(() => setUp(['analytics.read', 'investigation.read', 'kpi.read']))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('offers neither panel until a node is on screen', async () => {
    await renderInvestigation()
    await screen.findByRole('option', { name: /Revenue/ })

    // Nothing has been analysed, so there is no node — and therefore nothing to
    // explain and nothing to file a conclusion against.
    expect(screen.queryByRole('button', { name: /explain this level/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /add a finding/i })).toBeNull()
    expect(explainCalls).toHaveLength(0)
  })

  it('explains the level actually on screen, with coordinates and no figures', async () => {
    await renderInvestigation()
    await explainTheMovement()

    await press(/explain this level/i)
    await waitFor(() => expect(explainCalls).toHaveLength(1))

    const sent = explainCalls[0]
    expect(sent.kpi_id).toBe('revenue')
    expect(sent.dimension).toBe('region')
    // The top level is the whole movement broken down: no entity, no ancestors.
    expect(sent.entity).toBeNull()
    expect(sent.path).toEqual([])
    // Coordinates only. A request carrying the movement would let the page decide
    // what gets explained, which is the same rule the contribution call obeys.
    expect(Object.keys(sent).sort()).toEqual(
      ['dimension', 'entity', 'kpi_id', 'path', 'target_date'].sort(),
    )

    await screen.findByText(/Stored evidence for the region breakdown/)
    // The provenance of the prose is stated, because "written by the platform" and
    // "narrated by a model" are different claims about the same figures.
    await screen.findByText(/Written by the platform from stored evidence/)
  })

  it('clears the explanation when the reader drills to a different node', async () => {
    await renderInvestigation()
    await explainTheMovement()

    await press(/explain this level/i)
    await screen.findByText(/Stored evidence for the region breakdown/)

    const drills = await screen.findAllByRole('button', { name: /break down by channel/i })
    await act(async () => {
      drills[0].click()
    })
    await waitFor(() => expect(contributionCalls).toHaveLength(2))

    // Prose about the region breakdown must not survive under the channel heading.
    await waitFor(() =>
      expect(screen.queryByText(/Stored evidence for the region breakdown/)).toBeNull(),
    )

    // Asking again sends the node now on screen: the channel breakdown *within* North.
    await press(/explain this level/i)
    await waitFor(() => expect(explainCalls).toHaveLength(2))
    expect(explainCalls[1].dimension).toBe('channel')
    expect(explainCalls[1].path).toEqual([{ dimension: 'region', value: 'North' }])
  })

  it('files a finding against the coordinates the reader was reading', async () => {
    await renderInvestigation()
    await explainTheMovement()

    // The panel reads the stored findings for this KPI and date, and offers the
    // transitions the server said it accepts.
    await waitFor(() => expect(findingsGets.length).toBeGreaterThan(0))
    expect(findingsGets[0]).toContain('kpi_id=revenue')

    await press(/add a finding/i)
    await screen.findByText(/Filed against the region breakdown/)

    fireEvent.change(screen.getByPlaceholderText(/what did you conclude/i), {
      target: { value: 'North fell after the depot closure.' },
    })
    await press(/save finding/i)

    await waitFor(() => expect(findingsPosts).toHaveLength(1))
    const filed = findingsPosts[0]
    expect(filed.kpi_id).toBe('revenue')
    expect(filed.dimension).toBe('region')
    expect(filed.entity).toBeNull()
    expect(filed.path).toEqual([])
    expect(filed.title).toBe('North fell after the depot closure.')
    expect(filed.status).toBe('OPEN')
  })

  it('anchors both panels to one entity when the reader analysed one', async () => {
    await renderInvestigation()
    await screen.findByRole('option', { name: /Revenue/ })

    // The manual entry point, then one measured entity by name.
    await press(/manual/i)
    const entityField = await screen.findByPlaceholderText(/a value of the dimension above/i)
    fireEvent.change(entityField, { target: { value: 'South' } })
    await press(/^run$/i)
    await waitFor(() => expect(analysisCalls).toHaveLength(1))

    await press(/explain this level/i)
    await waitFor(() => expect(explainCalls).toHaveLength(1))
    // An entity is anchored with the dimension it belongs to: the server refuses an
    // entity named without one, and it would be a coordinate nobody can resolve.
    expect(explainCalls[0].dimension).toBe('region')
    expect(explainCalls[0].entity).toBe('South')

    await press(/add a finding/i)
    await screen.findByText(/Filed against region: South/)
  })
})
