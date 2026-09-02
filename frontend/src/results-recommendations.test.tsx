/**
 * The evidence-to-action layer, as a reader of the Result page experiences it.
 *
 * The backend tests pin what the engine may *say*. These pin what the screen may
 * *do* with it — a different failure mode, and the more dangerous one, because a
 * panel can turn honest sentences into a misleading page purely by what it shows,
 * hides, or keeps on screen after the evidence changed.
 *
 * Seven properties, each a way that goes wrong:
 *
 * 1. **Advice never outruns the evidence.** With no stored breakdown the panel names
 *    no part of the business and offers the breakdown instead; running it re-aims
 *    the advice at the area the server then names. A page that kept the KPI-level
 *    wording after a region was identified would be advising on evidence it does
 *    not have.
 * 2. **All eight parts are on the card.** Evidence, area, lever, action, impact,
 *    owner, confidence, monitoring. An action without its owner or its confidence is
 *    an instruction, not a suggestion.
 * 3. **The causation note is not behind a disclosure.** It is visible before anything
 *    is expanded, because the sentence most likely to be quoted out of context is
 *    the finding immediately above it.
 * 4. **The executive view narrows, it does not contradict.** The one-line answer
 *    carries the same action, owner, impact and confidence as the detailed card.
 * 5. **A normal result recommends nothing.** No cards, no lever, no owner — just the
 *    routine monitoring it already had.
 * 6. **An unjudgeable result offers evidence steps, not an intervention.** No lever,
 *    no owner, no action anywhere on the panel.
 * 7. **A response cannot be recorded without saying whether the advice helped**, and
 *    what is recorded is the server's own recommendation key.
 *
 * Every sentence in these fixtures is server-authored, which is the point: the
 * assertions look for text the payload supplied. Nothing here proves the wording is
 * good — only that the screen does not write, hide or outlive it.
 */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import ResultDetail from './pages/ResultDetail'

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

const RUN = {
  result: {
    kpi: 'net_revenue',
    kpi_key: 'revenue',
    target_date: '2026-08-28',
    actual: 35_000_000,
    expected: 50_000_000,
    deviation_pct: -30,
    deviation_absolute: -15_000_000,
    status: 'ABNORMAL',
    comparison: 'Comparable Fridays',
    headline: 'Net Revenue is 30.0% below its comparable Fridays.',
    unit: 'currency',
    currency: 'INR',
  },
  run_id: 'run-1',
  executed_at: '2026-08-29T04:15:00Z',
}

const CAUSATION_NOTE =
  'A share of a movement is a size, not a proven cause. Contribution alone does not establish causation.'

const OPTIONS = {
  usefulness: ['USEFUL', 'NOT_USEFUL', 'NEEDS_REVIEW'],
  action_status: ['NOT_STARTED', 'IN_REVIEW', 'ACTION_TAKEN'],
}

/** The engine's evidence echo, in the shape `_evidence_summary` returns it. */
function evidenceSummary(overrides: Record<string, unknown> = {}) {
  return {
    verdict: 'ABNORMAL',
    actual: 35_000_000,
    expected: 50_000_000,
    deviation_absolute: -15_000_000,
    deviation_pct: -30,
    unit: 'currency',
    currency: 'INR',
    comparison: 'same_day_of_week',
    reference_count: 12,
    top_contributor: null,
    top_contributor_chain: null,
    top_contributor_share_pct: null,
    breakdown_dimension: null,
    ...overrides,
  }
}

const MONITORING = {
  metrics: [
    'Net Revenue against its comparable periods',
    'Order volume against comparable periods',
  ],
  window: 'Next 3 comparable periods',
}

const KPI_LEVEL_CARD = {
  key: 'order_volume|revenue',
  priority: 'MEDIUM_PRIORITY',
  priority_label: 'MEDIUM PRIORITY',
  finding:
    'Net Revenue is 30.0% below the level its comparable Fridays support. No breakdown of this movement is stored, so no part of the business is named yet.',
  why: [
    'The stored verdict for 28 Aug 2026 is ABNORMAL.',
    'Confidence is MEDIUM because no breakdown of this movement is stored.',
  ],
  target_area: null,
  lever: {
    key: 'order_volume',
    label: 'Order volume',
    source: 'KPI_DRIVER',
    note: 'Registered as a controllable driver of this KPI by your company.',
    driver_name: 'Order volume',
  },
  action:
    'Locate the affected area before acting: break this movement down along an approved dimension, then review the parts that account for the largest share of it.',
  impact: {
    level: 'MEDIUM',
    label: 'MEDIUM POTENTIAL IMPACT',
    basis:
      'Rated because this KPI is registered as HIGH business criticality and no contributing share is measured yet.',
  },
  owner: 'Operations Manager',
  confidence: {
    level: 'MEDIUM',
    meaning:
      'The evidence associates this movement with the KPI as a whole; validation is recommended before acting.',
  },
  monitoring: MONITORING,
  causation_note: CAUSATION_NOTE,
}

const SOUTH_TARGET = {
  dimension: 'region',
  entity: 'South',
  entity_type: 'Region',
  chain: ['South'],
  chain_label: 'South',
  share_pct: -61.2,
  change: -9_180_000,
  shares_available: true,
  drill_next: ['city'],
  comparison_hint: 'Compare against regions of a similar size.',
  depth: 0,
}

const SOUTH_CARD = {
  ...KPI_LEVEL_CARD,
  key: 'order_volume|south',
  priority: 'HIGH_PRIORITY',
  priority_label: 'HIGH PRIORITY',
  finding: 'South accounts for 61.2% of the observed downward movement in Net Revenue.',
  why: [
    'The stored verdict for 28 Aug 2026 is ABNORMAL.',
    'South accounts for 61.2% of the movement in the stored region breakdown.',
    'Confidence is HIGH because twelve comparable periods were available.',
  ],
  target_area: SOUTH_TARGET,
  action:
    'Prioritise a regional performance review of South, starting with the cities that account for the largest share of the movement. Compare against regions of a similar size.',
  impact: {
    level: 'HIGH',
    label: 'HIGH POTENTIAL IMPACT',
    basis:
      'Rated because this KPI is registered as HIGH business criticality and one area accounts for most of the movement.',
  },
  owner: 'Regional Sales Manager',
  confidence: {
    level: 'HIGH',
    meaning: 'The stored evidence strongly supports prioritising a review of this area.',
  },
}

const PREVENTIVE_CARD = {
  ...KPI_LEVEL_CARD,
  key: 'inventory_availability|south',
  priority: 'PREVENTIVE_ACTION',
  priority_label: 'PREVENTIVE ACTION',
  finding:
    'The parts of the business outside South account for the remaining 38.8% of the observed movement.',
  target_area: SOUTH_TARGET,
  lever: {
    key: 'inventory_availability',
    label: 'Inventory availability',
    source: 'KPI_FAMILY_DEFAULT',
    note: 'A default for revenue KPIs, not a driver your company has registered.',
    driver_name: null,
  },
  action:
    'Check whether the same pattern is beginning in the areas that were not flagged, before it becomes material.',
  owner: 'Inventory Manager',
}

function abnormalSet(sharp: boolean) {
  return {
    result: {
      kpi: 'net_revenue',
      kpi_key: 'revenue',
      target_date: '2026-08-28',
      verdict: 'ABNORMAL',
      stance: 'ACTION',
      movement_direction: 'ADVERSE',
      headline: 'Net Revenue moved 30.0% below the level its comparable Fridays support.',
      body: sharp
        ? 'South accounts for the largest share of the observed movement.'
        : 'These suggestions are aimed at the KPI as a whole until a breakdown is stored.',
      confidence: sharp
        ? { level: 'HIGH', reasons: ['Twelve comparable periods were available.'] }
        : { level: 'MEDIUM', reasons: ['No stored breakdown narrows this movement yet.'] },
      evidence_summary: sharp
        ? evidenceSummary({
            top_contributor: 'South',
            top_contributor_chain: ['South'],
            top_contributor_share_pct: -61.2,
            breakdown_dimension: 'region',
          })
        : evidenceSummary(),
      target_area: sharp ? SOUTH_TARGET : null,
      recommendations: sharp ? [SOUTH_CARD, PREVENTIVE_CARD] : [KPI_LEVEL_CARD],
      next_steps: [] as string[],
      monitoring: MONITORING,
      limitations: [
        CAUSATION_NOTE,
        sharp
          ? 'Shares are measured against one approved dimension only.'
          : 'No breakdown is stored, so these recommendations are aimed at the KPI rather than at a part of the business.',
        'The platform measures no counterfactual, so no figure is attached to what an action is worth.',
      ],
      awaiting_breakdown: !sharp,
      causation_note: CAUSATION_NOTE,
      action_preamble: 'Based on this evidence, the following actions are recommended for review.',
      executive: {
        what_happened: 'Net Revenue moved 30.0% below the level its comparable Fridays support.',
        largest_contributor: sharp ? 'South' : null,
        largest_contributor_share: sharp ? -61.2 : null,
        top_action: sharp ? SOUTH_CARD.action : KPI_LEVEL_CARD.action,
        owner: sharp ? 'Regional Sales Manager' : 'Operations Manager',
        impact: sharp ? 'HIGH POTENTIAL IMPACT' : 'MEDIUM POTENTIAL IMPACT',
        confidence: sharp ? 'HIGH' : 'MEDIUM',
      },
    },
    run_id: 'run-1',
    feedback: [] as unknown[],
    feedback_options: OPTIONS,
    may_submit_feedback: true,
  }
}

const NORMAL_SET = {
  ...abnormalSet(false),
  result: {
    ...abnormalSet(false).result,
    verdict: 'NORMAL',
    stance: 'NO_ACTION',
    movement_direction: 'FLAT',
    headline: 'No corrective action is currently recommended.',
    body: 'Performance remains within the expected range. Continue routine monitoring.',
    confidence: { level: 'HIGH', reasons: ['Twelve comparable periods were available.'] },
    evidence_summary: evidenceSummary({
      verdict: 'NORMAL',
      actual: 49_000_000,
      deviation_absolute: -1_000_000,
      deviation_pct: -2,
    }),
    recommendations: [] as unknown[],
    limitations: [CAUSATION_NOTE],
    awaiting_breakdown: false,
    executive: {
      ...abnormalSet(false).result.executive,
      what_happened: 'No corrective action is currently recommended.',
      top_action: null,
      owner: null,
      impact: null,
      confidence: 'HIGH',
    },
  },
}

const LOW_CONFIDENCE_SET = {
  ...abnormalSet(false),
  result: {
    ...abnormalSet(false).result,
    verdict: 'LOW_CONFIDENCE',
    stance: 'EVIDENCE_FIRST',
    movement_direction: 'UNKNOWN',
    headline: 'Evidence insufficient for targeted action',
    body: 'No direct intervention is recommended until additional evidence is available.',
    confidence: { level: 'LOW', reasons: ['Only one comparable period was available.'] },
    recommendations: [] as unknown[],
    next_steps: [
      'Collect additional comparable history for this KPI.',
      'Validate the dimensions this KPI can be broken down by.',
      'Check the completeness of the underlying data for this date.',
      'Review the freshness of the source this KPI reads.',
    ],
    limitations: [CAUSATION_NOTE],
    awaiting_breakdown: false,
    executive: {
      ...abnormalSet(false).result.executive,
      what_happened: 'Evidence insufficient for targeted action',
      top_action: null,
      owner: null,
      impact: null,
      confidence: 'LOW',
    },
  },
}

/** A reader without investigation access: no area named, and told why. */
const WITHHELD_SET = {
  ...abnormalSet(false),
  result: {
    ...abnormalSet(false).result,
    limitations: [
      CAUSATION_NOTE,
      'Your role does not include investigation access, so no stored breakdown was read and no part of the business is named.',
    ],
    awaiting_breakdown: false,
  },
  may_submit_feedback: false,
}

const CONTRIBUTION = {
  result: {
    kpi: 'net_revenue',
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
    contributors: [
      {
        entity: 'South',
        label: 'South',
        actual: 12_000_000,
        expected: 21_180_000,
        change: -9_180_000,
        share_pct: -61.2,
        absolute_share_pct: 61.2,
        reference_count: 12,
        matched_rows: 40,
        note: null,
      },
    ],
    top_k: 8,
    ranked_count: 1,
    explained_pct: 61.2,
    unexplained_pct: 38.8,
    leader_is_sufficient: true,
    sufficiency_pct: 61.2,
    shares_available: true,
    next_dimensions: ['city'],
    notes: [] as string[],
  },
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

let recommendationGets: string[]
let contributionPosts: unknown[]
let feedbackPosts: Record<string, unknown>[]
/** Rows the server would hold after a response, echoed back on the next read. */
let recordedRows: unknown[]
/** Flipped by a stored breakdown, exactly as the server's own answer would change. */
let sharpened: boolean
let basePayload: () => Record<string, unknown>

function stubFetch(permissions: string[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/auth/session')) {
      return jsonResponse({ user: USER, memberships: [membership(permissions)] })
    }
    // Both of these are checked before the bare run URL, which is a prefix of them.
    if (url.includes('/recommendation-feedback')) {
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
      feedbackPosts.push(body)
      const row = {
        recommendation_key: body.recommendation_key,
        usefulness: body.usefulness,
        action_status: body.action_status,
        comment: body.comment ?? null,
        lever_key: 'order_volume',
        target_entity: 'South',
        submitted_by_email: USER.email,
        submitted_at: '2026-09-02T09:00:00Z',
      }
      recordedRows = [row]
      return jsonResponse({ feedback: row })
    }
    if (url.includes('/recommendations')) {
      recommendationGets.push(url)
      return jsonResponse({ ...basePayload(), feedback: recordedRows })
    }
    if (url.includes('/investigation/contribution')) {
      contributionPosts.push(JSON.parse(String(init?.body ?? '{}')))
      sharpened = true
      return jsonResponse(CONTRIBUTION)
    }
    if (url.includes('/investigation/findings')) {
      return jsonResponse({ findings: [], statuses: ['OPEN', 'IN_PROGRESS', 'RESOLVED'] })
    }
    if (url.includes('/detection-runs/run-1')) return jsonResponse(RUN)
    return jsonResponse({})
  })
}

async function renderResult() {
  const view = render(
    <MemoryRouter initialEntries={['/results/run-1']}>
      <AuthProvider>
        <Routes>
          <Route path="/results/:runId" element={<ResultDetail />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
  await screen.findByText('Recommended next actions')
  return view
}

async function press(name: RegExp | string) {
  const button = await screen.findByRole('button', { name })
  await act(async () => {
    button.click()
  })
  return button
}

async function pressFirst(name: RegExp | string) {
  const buttons = await screen.findAllByRole('button', { name })
  await act(async () => {
    buttons[0].click()
  })
  return buttons[0]
}

function setUp(permissions: string[], payload: () => Record<string, unknown>) {
  recommendationGets = []
  contributionPosts = []
  feedbackPosts = []
  recordedRows = []
  sharpened = false
  basePayload = payload
  localStorage.clear()
  localStorage.setItem(
    'bi.ai.session',
    JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
  )
  localStorage.setItem('bi.ai.company', 'company-1')
  vi.stubGlobal('fetch', stubFetch(permissions))
}

const FULL_ACCESS = ['analytics.read', 'investigation.read', 'kpi.read']

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('recommendations on an abnormal result', () => {
  beforeEach(() => setUp(FULL_ACCESS, () => abnormalSet(sharpened)))

  it('names no part of the business until a breakdown exists, then re-aims at the one it names', async () => {
    await renderResult()

    // KPI-level: the server named no area, and the panel does not choose one.
    // "South" is in the *contribution* fixture, not in this payload.
    // Said twice on purpose: once as the stance, once beside the button that fixes it.
    expect((await screen.findAllByText(/aimed at the KPI as a whole/)).length).toBe(2)
    expect(screen.queryByText('South')).toBeNull()
    expect(screen.queryByText(/61.2% of the movement/)).toBeNull()

    await press(/sharpen with a breakdown/i)
    await waitFor(() => expect(contributionPosts).toHaveLength(1))
    // The set is derived on read, so a stored breakdown means asking again.
    await waitFor(() => expect(recommendationGets.length).toBeGreaterThanOrEqual(2))

    await screen.findByText(/South accounts for 61.2% of the observed downward movement/)
    expect((await screen.findAllByText('Region')).length).toBeGreaterThan(0)
    expect((await screen.findAllByText(/61.2% of the movement/)).length).toBeGreaterThan(0)
    // And the wording that applied before an area was known is gone.
    expect(screen.queryByText(/Locate the affected area before acting/)).toBeNull()
  })

  it('carries all eight parts of a recommendation, with the causation note in the open', async () => {
    setUp(FULL_ACCESS, () => abnormalSet(true))
    await renderResult()

    // 1 evidence, 2 target area, 3 lever, 4 action, 5 impact, 6 owner,
    // 7 confidence, 8 monitoring.
    await screen.findByText(/South accounts for 61.2% of the observed downward movement/)
    expect(screen.getAllByText('Target area').length).toBe(2)
    expect(screen.getAllByText('Relevant business lever to review').length).toBe(2)
    expect(screen.getAllByText('Order volume').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Recommended action').length).toBe(2)
    await screen.findByText(/Prioritise a regional performance review of South/)
    expect(screen.getAllByText('Potential impact').length).toBe(2)
    expect(screen.getAllByText('HIGH POTENTIAL IMPACT').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Recommended owner').length).toBe(2)
    await screen.findByText('Regional Sales Manager')
    expect(screen.getAllByText(/HIGH confidence/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('What to monitor next').length).toBe(2)
    expect(screen.getAllByText(/Review window · Next 3 comparable periods/).length).toBe(2)

    // The priority tiers, with the preventive card labelled as such rather than as an alarm.
    await screen.findByText('HIGH PRIORITY')
    await screen.findByText('PREVENTIVE ACTION')
    // A lever the company never registered is labelled a default, not a fact about it.
    await screen.findByText('Default for this KPI type')

    // Visible before anything is expanded — the trail behind it is not.
    expect(screen.getAllByText(CAUSATION_NOTE).length).toBeGreaterThan(0)
    expect(screen.queryByText(/South accounts for 61.2% of the movement in the stored/)).toBeNull()

    await pressFirst(/why this recommendation/i)
    await screen.findByText(/South accounts for 61.2% of the movement in the stored/)
  })

  it('narrows to the executive view without changing the answer', async () => {
    setUp(FULL_ACCESS, () => abnormalSet(true))
    await renderResult()
    await screen.findByText(/Prioritise a regional performance review of South/)

    await press(/executive view/i)

    // The same action, owner and impact — one copy of it, not a second opinion.
    await screen.findByText('Top recommended action')
    expect(screen.getAllByText(/Prioritise a regional performance review of South/).length).toBe(1)
    await screen.findByText('Owner · Regional Sales Manager')
    await screen.findByText('HIGH POTENTIAL IMPACT')
    // The detail is put away; the qualification is not.
    expect(screen.queryByText('What to monitor next')).toBeNull()
    expect(screen.queryByRole('button', { name: /why this recommendation/i })).toBeNull()
    expect(screen.getAllByText(CAUSATION_NOTE).length).toBeGreaterThan(0)

    await press(/analyst view/i)
    expect(screen.getAllByText('What to monitor next').length).toBe(2)
  })

  it('records a response only once the reader has said whether the advice helped', async () => {
    setUp(FULL_ACCESS, () => abnormalSet(true))
    await renderResult()
    expect((await screen.findAllByText('Was this recommendation useful?')).length).toBe(2)

    const record = (await screen.findAllByRole('button', { name: /record response/i }))[0]
    expect((record as HTMLButtonElement).disabled).toBe(true)

    await pressFirst(/👍/)
    await pressFirst(/Action taken/)
    await act(async () => {
      record.click()
    })

    await waitFor(() => expect(feedbackPosts).toHaveLength(1))
    // The server's own key for the card, so a response cannot attach to advice
    // the platform did not give.
    expect(feedbackPosts[0]).toEqual({
      recommendation_key: 'order_volume|south',
      usefulness: 'USEFUL',
      action_status: 'ACTION_TAKEN',
      comment: null,
    })
    await screen.findByText(/Response recorded/)
    // Read back from the server rather than assumed from the click.
    await screen.findByText(/Last recorded by admin@aurora-retail.example.com/)
  })
})

describe('recommendations on results the engine will not act on', () => {
  it('recommends nothing for a normal result, and still says what to watch', async () => {
    setUp(FULL_ACCESS, () => NORMAL_SET)
    await renderResult()

    await screen.findByText('No corrective action is currently recommended.')
    await screen.findByText(/Performance remains within the expected range/)
    // No card, so no lever, no owner and no action.
    expect(screen.queryByText('Recommended action')).toBeNull()
    expect(screen.queryByText('Relevant business lever to review')).toBeNull()
    expect(screen.queryByText('Recommended owner')).toBeNull()
    expect(screen.queryByRole('button', { name: /executive view/i })).toBeNull()
    // Routine monitoring survives, because that is what a normal result is owed.
    await screen.findByText('What to monitor next')
    await screen.findByText(/Review window · Next 3 comparable periods/)
  })

  it('offers evidence steps rather than an intervention when the date cannot be judged', async () => {
    setUp(FULL_ACCESS, () => LOW_CONFIDENCE_SET)
    await renderResult()

    await screen.findByText('Evidence insufficient for targeted action')
    await screen.findByText(/No direct intervention is recommended until additional evidence/)
    await screen.findByText('Recommended next steps')
    await screen.findByText(/1\. Collect additional comparable history/)
    await screen.findByText(/4\. Review the freshness of the source/)

    expect(screen.queryByText('Recommended action')).toBeNull()
    expect(screen.queryByText('Recommended owner')).toBeNull()
    expect(screen.queryByText('Relevant business lever to review')).toBeNull()
    expect(screen.queryByText('Was this recommendation useful?')).toBeNull()
  })

  it('offers no breakdown and no response to a reader without investigation access', async () => {
    setUp(['analytics.read', 'kpi.read'], () => WITHHELD_SET)
    await renderResult()

    await screen.findByText(/Your role does not include investigation access/)
    expect(screen.queryByRole('button', { name: /sharpen with a breakdown/i })).toBeNull()
    expect(screen.queryByText('Was this recommendation useful?')).toBeNull()
    expect(contributionPosts).toHaveLength(0)
  })
})
