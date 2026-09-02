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
 *  4. **No sentence composed here.** The findings panel prints the headline the
 *     server assembled from stored runs, verbatim. The fixture's headline says
 *     something the component could not have derived from the other fields, so
 *     "printed the server's sentence" is distinguishable from "wrote one". Where a
 *     movement has no stored breakdown, the panel must say so — a gap where a cause
 *     belongs is an invitation to borrow the neighbouring row's.
 *
 * The fixture also carries a legacy `WATCH` verdict, because the dev database has
 * four of them: the tiles must sum to the evaluated total, which means an
 * unrecognised status has to be counted and named rather than folded into NORMAL.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import type { MonitoringHeadline, MonitoringMovement } from './api/types'
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
  // What the stored breakdown concluded. Copied by the server, not recomputed.
  contributor_dimension: 'region',
  contributor_entity: 'Metro West',
  contributor_share_pct: 61,
  contributor_is_sufficient: true,
  can_investigate: true,
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
  // Nobody has broken this one down, so there is no contributor to name.
  contributor_dimension: null,
  contributor_entity: null,
  contributor_share_pct: null,
  contributor_is_sufficient: null,
  can_investigate: true,
}

/** The same fields the server withholds, withheld. */
function withheld(movement: MonitoringMovement): MonitoringMovement {
  return {
    ...movement,
    has_contribution: null,
    open_findings: null,
    contributor_dimension: null,
    contributor_entity: null,
    contributor_share_pct: null,
    contributor_is_sufficient: null,
    can_investigate: false,
  }
}

/**
 * Two headlines the server wrote from stored runs.
 *
 * The first names a contributor because a breakdown found one; the second names
 * none and says why. Both sentences are the server's, so the screen has to print
 * them rather than compose its own.
 */
const REVENUE_HEADLINE = {
  detection_run_id: 'run-revenue',
  kpi_id: 'kpi-revenue',
  kpi_key: 'net_revenue',
  kpi_name: 'net_revenue',
  target_date: '2026-08-28',
  status: 'ABNORMAL',
  headline:
    'Net Revenue moved 37.2% below expectation on 28 Aug, with Metro West accounting for most of it (61.0% of the movement).',
  deviation_pct: -37.2,
  deviation_absolute: -4250000,
  actual_value: 6000000,
  expected_value: 10250000,
  unit: 'currency',
  currency: 'INR',
  direction: 'below',
  contributor_dimension: 'region',
  contributor_entity: 'Metro West',
  contributor_share_pct: 61,
  contributor_is_sufficient: true,
  contributor_note: null,
  can_investigate: true,
}

const ORDERS_HEADLINE = {
  detection_run_id: 'run-orders',
  kpi_id: 'kpi-orders',
  kpi_key: 'orders',
  kpi_name: 'orders',
  target_date: '2026-08-28',
  status: 'ABNORMAL',
  headline: 'Orders moved 6.4% below expectation on 28 Aug.',
  deviation_pct: -6.4,
  deviation_absolute: -280,
  actual_value: 4120,
  expected_value: 4400,
  unit: 'count',
  currency: null,
  direction: 'below',
  contributor_dimension: null,
  contributor_entity: null,
  contributor_share_pct: null,
  contributor_is_sufficient: null,
  contributor_note: 'No breakdown has been run for this movement yet.',
  can_investigate: true,
}

/**
 * A headline as a reader without `investigation.read` receives it.
 *
 * The movement itself is `analytics.read` and stays; what disappears is the
 * apportionment — so the sentence the server writes for this reader names no
 * contributor, and the note gives entitlement as the reason. "Nobody has looked"
 * is never implied to someone who simply may not see it.
 */
function withheldHeadline(row: MonitoringHeadline, sentence: string): MonitoringHeadline {
  return {
    ...row,
    headline: sentence,
    contributor_dimension: null,
    contributor_entity: null,
    contributor_share_pct: null,
    contributor_is_sufficient: null,
    contributor_note: 'Contributor analysis is not visible to your role.',
    can_investigate: false,
  }
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

/**
 * The summary the server assembles for one row, keyed on the KPI it was asked about.
 *
 * The prose names its own subject, which is how "the summary of the row I clicked"
 * is told apart from "the summary still on screen from the previous row".
 */
function explanationFor(body: { kpi_id: string; target_date: string }) {
  return {
    explanation: {
      subject: `${body.kpi_id} on ${body.target_date}`,
      scope: 'result',
      model_written: false,
      model: null,
      order: ['WHAT HAPPENED'],
      sections: [
        {
          heading: 'WHAT HAPPENED',
          body: `Stored evaluation of ${body.kpi_id} on ${body.target_date}, read from the run.`,
        },
      ],
      limitations: [],
      citations: [],
      confidence: { level: 'MEDIUM', reasons: ['Twelve comparable periods were available.'] },
      facts: null,
    },
  }
}

const CAUSATION_NOTE =
  'A share of a movement is a size, not a proven cause. Contribution alone does not establish causation.'

/** The recommendation set for one run, as the server derives it. */
function recommendationSetFor(runId: string) {
  return {
    result: {
      kpi: 'net_revenue',
      kpi_key: 'net_revenue',
      target_date: '2026-08-28',
      verdict: 'ABNORMAL',
      stance: 'ACTION',
      movement_direction: 'ADVERSE',
      headline: `Recommended actions derived from ${runId}.`,
      body: 'Metro West accounts for the largest share of the observed movement.',
      confidence: { level: 'HIGH', reasons: ['Twelve comparable periods were available.'] },
      evidence_summary: {
        verdict: 'ABNORMAL',
        actual: 6_000_000,
        expected: 10_250_000,
        deviation_absolute: -4_250_000,
        deviation_pct: -37.2,
        unit: 'currency',
        currency: 'INR',
        comparison: 'same_day_of_week',
        reference_count: 12,
        top_contributor: 'Metro West',
        top_contributor_chain: ['Metro West'],
        top_contributor_share_pct: 61,
        breakdown_dimension: 'region',
      },
      target_area: null,
      recommendations: [] as unknown[],
      next_steps: [] as string[],
      monitoring: { metrics: ['Net Revenue against its comparable periods'], window: 'Next 3' },
      limitations: [CAUSATION_NOTE],
      awaiting_breakdown: false,
      causation_note: CAUSATION_NOTE,
      action_preamble: 'Based on this evidence, the following actions are recommended for review.',
      executive: {
        what_happened: `Recommended actions derived from ${runId}.`,
        largest_contributor: 'Metro West',
        largest_contributor_share: 61,
        top_action: null,
        owner: null,
        impact: null,
        confidence: 'HIGH',
      },
    },
    run_id: runId,
    feedback: [] as unknown[],
    feedback_options: {
      usefulness: ['USEFUL', 'NOT_USEFUL', 'NEEDS_REVIEW'],
      action_status: ['NOT_STARTED', 'IN_REVIEW', 'ACTION_TAKEN'],
    },
    may_submit_feedback: true,
  }
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
    findings_window_days: 7,
    findings_window_options: [7, 14, 30, 90],
    findings_window_from: '2026-08-28',
    findings_window_to: '2026-08-28',
    headlines: mayInvestigate
      ? [REVENUE_HEADLINE, ORDERS_HEADLINE]
      : [
          withheldHeadline(
            REVENUE_HEADLINE,
            'Net Revenue moved 37.2% below expectation on 28 Aug.',
          ),
          withheldHeadline(ORDERS_HEADLINE, 'Orders moved 6.4% below expectation on 28 Aug.'),
        ],
    headline_total: 2,
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
  findings_window_from: null,
  findings_window_to: null,
  headlines: [],
  headline_total: 0,
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

/** Every monitoring URL the screen asked for, so the window buttons are checkable. */
const requested: string[] = []
/** Every summary the screen asked for, so what it sent is checkable. */
const explainBodies: Record<string, unknown>[] = []
/** Every recommendation URL, so the run each summary aimed at is checkable. */
const recommendationUrls: string[] = []

function setUp(permissions: string[], monitoring: unknown) {
  localStorage.clear()
  requested.length = 0
  explainBodies.length = 0
  recommendationUrls.length = 0
  localStorage.setItem(
    'bi.ai.session',
    JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
  )
  localStorage.setItem('bi.ai.company', 'company-1')
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/auth/session')) {
        return jsonResponse({ user: USER, memberships: [membership(permissions)] })
      }
      if (url.includes('/results/explain')) {
        const body = JSON.parse(String(init?.body ?? '{}'))
        explainBodies.push(body)
        return jsonResponse(explanationFor(body))
      }
      if (url.includes('/recommendations')) {
        recommendationUrls.push(url)
        const runId = url.split('/detection-runs/')[1]?.split('/')[0] ?? 'unknown'
        return jsonResponse(recommendationSetFor(runId))
      }
      if (url.includes('/monitoring')) {
        requested.push(url)
        return jsonResponse(monitoring)
      }
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

/**
 * Every Copilot summary trigger on the screen, in document order.
 *
 * The fixture's order is the screen's order: the two biggest movements, then the two
 * headlines. `[0]` is therefore the largest movement, which is the revenue run.
 */
function summaryButtons(): HTMLElement[] {
  return Array.from(document.querySelectorAll('button')).filter((button) =>
    (button.textContent ?? '').includes('Copilot summary'),
  )
}

/** The summary trigger inside the row whose text contains `needle`. */
function summaryButtonIn(needle: string): HTMLElement {
  const row = Array.from(document.querySelectorAll('li')).find((item) =>
    (item.textContent ?? '').includes(needle),
  )
  if (!row) throw new Error(`No row contains ${needle}`)
  const button = Array.from(row.querySelectorAll('button')).find((element) =>
    (element.textContent ?? '').includes('Copilot summary'),
  )
  if (!button) throw new Error(`No summary trigger in the row containing ${needle}`)
  return button
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

    // The single recent abnormality is already under the biggest movements, and the
    // panel says so rather than printing the row again.
    expect(bodyText()).toContain('already listed under the biggest movements')
    // Three links to that result, each a different statement about it: the movement
    // row, the stored run that produced it, and the headline it earned. The
    // duplicate this test guards against would be a fourth, in the abnormalities
    // panel, which the assertion above rules out.
    expect(hrefs().filter((href) => href === '/results/run-revenue')).toHaveLength(3)
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

  it('prints the headline the server wrote, rather than composing one here', async () => {
    await renderOverview()

    expect(
      screen.getByText(
        'Net Revenue moved 37.2% below expectation on 28 Aug, with Metro West accounting for most of it (61.0% of the movement).',
      ),
    ).toBeTruthy()
    expect(screen.getByText('Orders moved 6.4% below expectation on 28 Aug.')).toBeTruthy()
    expect(bodyText()).toContain('2 abnormal movements in the last 7 days')
    // A share is a size. The panel prints the server's sentence, and the server
    // does not upgrade one to a cause.
    expect(bodyText().toLowerCase()).not.toContain('drove')
  })

  it('names no cause where no breakdown found one, and says why', async () => {
    await renderOverview()

    // The orders movement has no stored breakdown. The panel must say so rather
    // than leave a gap a reader would fill with the revenue movement's cause.
    expect(bodyText()).toContain('No breakdown has been run for this movement yet.')
  })

  it('asks the server for the findings period the reader selected', async () => {
    await renderOverview()

    expect(requested[0]).toContain('findings_window_days=7')
    // The four periods the specification names, and no others.
    for (const label of ['7 days', '14 days', '1 month', '3 months']) {
      expect(screen.getByText(label)).toBeTruthy()
    }

    fireEvent.click(screen.getByText('3 months'))
    await waitFor(() =>
      expect(requested.some((url) => url.includes('findings_window_days=90'))).toBe(true),
    )
    // The tally window is untouched: the two periods move independently. Anchored
    // so `findings_window_days=90` cannot satisfy this by containing the substring.
    expect(requested.every((url) => /[?&]window_days=90/.test(url))).toBe(true)
  })

  it('opens a headline in the existing investigation workflow', async () => {
    await renderOverview()

    // The same deep link the movement rows use — one workflow, not two.
    expect(hrefs()).toContain('/investigation?kpi=orders&date=2026-08-28')
  })
})

/**
 * The Copilot summary a monitoring row opens.
 *
 * The reason this is tested on the monitoring screen and not only on the Result page
 * is that monitoring is the screen with a figure on every row, so it is the screen
 * where "summarise this" is most tempting to answer locally. Four properties:
 *
 *  1. **Coordinates, not figures.** The request carries the KPI and the date. Every
 *     number on the row — actual, expected, deviation, contributor share — stays on
 *     the row, so the answer cannot have been assembled from what was rendered.
 *  2. **The server's sentences.** The section text is the server's, and it names its
 *     own subject, so a stale panel under a different row is detectable.
 *  3. **One at a time, per row.** A detection run appears twice on this screen — as a
 *     movement and as the headline it earned — and each occurrence has its own
 *     trigger. Opening one closes the other rather than toggling it shut and opening
 *     nothing.
 *  4. **Explanation, then advice.** The recommendation panel is aimed at that row's
 *     own run, and no other run's advice is fetched.
 */
describe('the Copilot summary on a monitoring row', () => {
  beforeEach(() => setUp(['analytics.read', 'kpi.read', 'investigation.read'], populated(true)))

  it('asks the server for the row that was clicked, and prints what it sent back', async () => {
    await renderOverview()
    // Nothing is summarised until it is asked for: eight rows would otherwise mean
    // eight retrievals for a reader who wanted one.
    expect(explainBodies).toHaveLength(0)

    fireEvent.click(summaryButtons()[0])

    await waitFor(() => expect(explainBodies).toHaveLength(1))
    expect(explainBodies[0]).toEqual({ kpi_id: 'net_revenue', target_date: '2026-08-28' })
    expect(
      await screen.findByText(
        'Stored evaluation of net_revenue on 2026-08-28, read from the run.',
      ),
    ).toBeTruthy()
    // The provenance of the prose travels with it.
    expect(bodyText()).toContain('Written by the platform from stored evidence')
  })

  it('sends no figure it is displaying', async () => {
    await renderOverview()
    fireEvent.click(summaryButtons()[0])
    await waitFor(() => expect(explainBodies).toHaveLength(1))

    // The endpoint accepts a KPI and a date. Anything else in this body would be a
    // figure the screen had a hand in, and the point of re-reading the stored run is
    // that it does not.
    expect(Object.keys(explainBodies[0]).sort()).toEqual(['kpi_id', 'target_date'])
    const sent = JSON.stringify(explainBodies[0])
    for (const figure of ['6000000', '10250000', '4250000', '37.2', '61']) {
      expect(sent).not.toContain(figure)
    }
  })

  it('follows the explanation with advice aimed at that row’s own run', async () => {
    await renderOverview()
    fireEvent.click(summaryButtons()[0])

    await waitFor(() =>
      expect(
        recommendationUrls.some((url) => url.includes('/detection-runs/run-revenue/recommendations')),
      ).toBe(true),
    )
    expect(await screen.findByText('Recommended actions derived from run-revenue.')).toBeTruthy()
    // The other abnormal movement's advice is not fetched for a summary nobody opened.
    expect(recommendationUrls.some((url) => url.includes('run-orders'))).toBe(false)
    // A share is a size, wherever the panel is rendered.
    expect(bodyText()).toContain('not a proven cause')
  })

  it('gives the headline of a run its own trigger, distinct from the movement’s', async () => {
    await renderOverview()

    // Both of these describe run-revenue. On the run id alone the second click would
    // read as "already open", close the first panel and open nothing.
    fireEvent.click(summaryButtons()[0])
    await waitFor(() => expect(explainBodies).toHaveLength(1))
    expect(summaryButtons()[0].textContent).toContain('Hide Copilot summary')

    fireEvent.click(summaryButtonIn('accounting for most of it'))
    await waitFor(() => expect(explainBodies).toHaveLength(2))

    // One open at a time: the headline's panel is open and the movement's is not.
    expect(summaryButtonIn('accounting for most of it').textContent).toContain(
      'Hide Copilot summary',
    )
    expect(summaryButtons()[0].textContent).toBe('✨ Copilot summary')
    expect(
      screen.getAllByText('Stored evaluation of net_revenue on 2026-08-28, read from the run.'),
    ).toHaveLength(1)
  })

  it('summarises the movement the reader chose, not the one above it', async () => {
    await renderOverview()

    fireEvent.click(summaryButtonIn('No breakdown has been run for this movement yet.'))

    await waitFor(() => expect(explainBodies).toHaveLength(1))
    expect(explainBodies[0]).toEqual({ kpi_id: 'orders', target_date: '2026-08-28' })
    expect(
      await screen.findByText('Stored evaluation of orders on 2026-08-28, read from the run.'),
    ).toBeTruthy()
    expect(
      screen.queryByText('Stored evaluation of net_revenue on 2026-08-28, read from the run.'),
    ).toBeNull()
  })

  it('closes on a second click without asking again', async () => {
    await renderOverview()
    fireEvent.click(summaryButtons()[0])
    await waitFor(() => expect(explainBodies).toHaveLength(1))

    fireEvent.click(summaryButtons()[0])

    await waitFor(() =>
      expect(
        screen.queryByText('Stored evaluation of net_revenue on 2026-08-28, read from the run.'),
      ).toBeNull(),
    )
    expect(explainBodies).toHaveLength(1)
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
    // Other people's notes are theirs. The derived headline panel stays, because a
    // stored abnormal verdict is `analytics.read` — what it loses is the cause.
    expect(rendered).not.toContain('Investigation notes')
    expect(rendered).not.toContain('Metro West')
  })

  it('shows the headlines but attributes no cause it may not disclose', async () => {
    await renderOverview()

    const rendered = bodyText()
    expect(screen.getByText('Net Revenue moved 37.2% below expectation on 28 Aug.')).toBeTruthy()
    // Entitlement, not absence: this reader is not told that nobody has looked.
    expect(rendered).toContain('Contributor analysis is not visible to your role.')
    expect(rendered).not.toContain('No breakdown has been run')
  })

  it('offers no route into the investigation surface', async () => {
    await renderOverview()

    expect(screen.queryByText('Investigate')).toBeNull()
    expect(screen.queryByText('Review investigation')).toBeNull()
    expect(hrefs().some((href) => href.startsWith('/investigation'))).toBe(false)
  })

  it('still offers the Copilot summary, which is analytics and not investigation', async () => {
    await renderOverview()

    // The summary reads the stored evaluation, which this reader may see; what the
    // server withholds from it is the apportionment, and it withholds that itself.
    // Losing the summary along with the "Investigate" link would take away an
    // entitlement nobody removed.
    fireEvent.click(summaryButtons()[0])

    await waitFor(() => expect(explainBodies).toHaveLength(1))
    expect(explainBodies[0]).toEqual({ kpi_id: 'net_revenue', target_date: '2026-08-28' })
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
    // The findings panel is honest about a quiet period rather than padded with
    // anything to fill it.
    expect(rendered).toContain('Nothing abnormal was recorded in the last 7 days')
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
