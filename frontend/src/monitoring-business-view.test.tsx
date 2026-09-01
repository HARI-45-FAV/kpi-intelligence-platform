/**
 * The business detection surface shows the answer and withholds the method.
 *
 * Requirement 11 of the detection specification is a *negative* requirement: the
 * monitoring screen must show KPI, Actual, Expected, Deviation and Status, and
 * must not expose the bucket calculations, the SQL, the joins or the statistical
 * implementation. A negative requirement cannot be checked by looking at the
 * screen once; it has to be pinned, because the natural direction of drift is for
 * a developer debugging an odd verdict to render the evidence "just for now".
 *
 * So the stubbed server here deliberately returns the *full* response the real
 * backend gives a caller holding `kpi.read` — evidence block included, with the
 * median, the MAD, the modified z-score, the bucket slot that was applied, the
 * reference dates and the generated SQL. Everything the screen must not print is
 * therefore present in the data it receives, and the assertions check the rendered
 * document for it. If someone widens the surface, this fails.
 *
 * The second thing pinned here is that the browser does no arithmetic. The stub
 * returns a `deviation_pct` that does not agree with its own actual and expected
 * (−37.2%, where the two figures would imply −41.5%). That is not realistic — the
 * engine derives all three consistently — but it is the only way to tell "printed
 * the server's field" apart from "recomputed it locally", and the whole point of
 * the design is that detection happens in exactly one deterministic place.
 */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from './auth/AuthContext'
import { formatCurrency } from './components/format'
import Monitoring from './pages/Monitoring'

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

/** Everything the business screen is forbidden to print, as the server sends it. */
const EVIDENCE = {
  kpi_version: 1,
  kpi_version_id: 'kpiver-1',
  source: {
    table: 'orders',
    time_field: 'order_date',
    formula: 'SUM(orders.net_revenue)',
    sql: 'SELECT SUM(orders.net_revenue) FROM orders WHERE order_date = ? GROUP BY order_date',
  },
  bucket: {
    applied: 'SAME_DAY_OF_WEEK',
    all_applied: ['SAME_DAY_OF_WEEK', 'YOY_PERIOD'],
    decisions: [{ bucket: 'SAME_DAY_OF_WEEK', outcome: 'APPLIED' }],
    config_key: 'aurora-weekly',
    config_version: 1,
    signature: 'same_day_of_week:FRI',
  },
  reference: {
    count: 26,
    points: [
      { date: '2026-08-21', value: 10500000 },
      { date: '2026-08-14', value: 10000000 },
    ],
  },
  statistics: {
    median: 10250000,
    mad: 250000,
    dispersion: 250000,
    dispersion_basis: 'MAD',
    modified_z_score: -11.4665,
    z_threshold: 3.5,
    statistically_significant: true,
  },
  tolerance: { relative_pct: 8, absolute: null, breached: true },
  year_over_year: { applied: true, factor: 1.0 },
  method: 'robust median with modified z-score',
}

const REVENUE_RESULT = {
  kpi: 'Revenue',
  kpi_key: 'revenue',
  target_date: '2026-08-28',
  actual: 6000000,
  expected: 10250000,
  // Deliberately not derivable from the two figures above. See the file docstring.
  deviation_pct: -37.2,
  deviation_absolute: -4250000,
  status: 'ABNORMAL',
  comparison: 'Comparable Fridays',
  headline: 'Revenue came in well below comparable Fridays.',
  unit: 'currency',
  currency: 'INR',
}

const REFUND_RESULT = {
  kpi: 'Refund Value',
  kpi_key: 'refund_value',
  target_date: '2026-08-28',
  actual: 90000,
  expected: null,
  deviation_pct: null,
  deviation_absolute: null,
  status: 'LOW_CONFIDENCE',
  comparison: 'Recent days',
  headline: 'Not enough comparable history to judge Refund Value yet.',
  unit: 'currency',
  currency: 'INR',
}

const OVERVIEW = {
  kpis: [
    {
      kpi_id: 'kpi-revenue',
      kpi_key: 'revenue',
      name: 'Revenue',
      detectable: true,
      blocked_reason: null,
      unit: 'currency',
      currency: 'INR',
      kpi_version: 1,
      latest_run: {
        id: 'run-1',
        kpi_key: 'revenue',
        kpi_name: 'Revenue',
        kpi_version: 1,
        target_date: '2026-08-21',
        actual_value: 10400000,
        expected_value: 10250000,
        deviation_absolute: 150000,
        deviation_pct: 1.5,
        status: 'NORMAL',
        comparison_label: 'Comparable Fridays',
        headline: 'Revenue behaved normally.',
        unit: 'currency',
        currency: 'INR',
        executed_at: '2026-08-22T04:00:00Z',
      },
    },
    {
      kpi_id: 'kpi-refunds',
      kpi_key: 'refund_value',
      name: 'Refund Value',
      detectable: true,
      blocked_reason: null,
      unit: 'currency',
      currency: 'INR',
      kpi_version: 1,
      latest_run: null,
    },
    {
      kpi_id: 'kpi-orders',
      kpi_key: 'order_count',
      name: 'Order Count',
      detectable: true,
      blocked_reason: null,
      unit: 'count',
      currency: null,
      kpi_version: 1,
      latest_run: null,
    },
    {
      kpi_id: 'kpi-margin',
      kpi_key: 'gross_margin',
      name: 'Gross Margin',
      detectable: false,
      blocked_reason: 'The KPI has no approved version bound to a source table.',
      unit: null,
      currency: null,
      kpi_version: null,
      latest_run: null,
    },
  ],
  counts: { total: 4, detectable: 3 },
  configuration: {
    company_default: { config_key: 'aurora-weekly', name: 'Aurora weekly rhythm', version: 1 },
    kpi_overrides: [],
    note: null,
  },
}

/**
 * The overview that sits above the detection panel, for a window with nothing in it.
 *
 * The Monitoring screen mounts `MonitoringOverview`, so this file has to serve
 * `/monitoring` — but it serves an *empty* window on purpose. This file's subject
 * is the detection panel's five figures and the method it must not print, and a
 * populated overview would put a second set of KPI names and status badges into
 * the same document, so every assertion here would start matching two elements and
 * stop meaning what it says. The populated overview — including what a reader
 * without `investigation.read` is not told — is pinned in
 * `monitoring-overview.test.tsx` instead.
 *
 * Empty is still a real response: this is exactly what the server returns for a
 * company that has never run detection, and it keeps the negative assertions below
 * honest by covering the overview's empty states too.
 */
const MONITORING = {
  window_days: 90,
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
  monitoring_note:
    'Detection runs when it is triggered — this platform has no scheduler in this version.',
}

const BATCH = {
  target_date: '2026-08-28',
  results: [
    { result: REVENUE_RESULT, run_id: 'run-2', persisted: true, evidence: EVIDENCE },
    { result: REFUND_RESULT, run_id: 'run-3', persisted: true, evidence: EVIDENCE },
  ],
  skipped: [
    { kpi_id: 'order_count', reason: 'No rows for 2026-08-28 in the registered source.' },
  ],
  counts: { evaluated: 2, skipped: 1 },
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, text: async () => JSON.stringify(body) } as Response
}

let batchCalls: unknown[]

function stubFetch(permissions: string[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/auth/session')) {
      return jsonResponse({ user: USER, memberships: [membership(permissions)] })
    }
    if (url.includes('/detection/overview')) return jsonResponse(OVERVIEW)
    if (url.includes('/monitoring')) return jsonResponse(MONITORING)
    if (url.includes('/run-detection/batch')) {
      batchCalls.push(JSON.parse(String(init?.body ?? '{}')))
      return jsonResponse(BATCH)
    }
    return jsonResponse({})
  })
}

async function renderMonitoring() {
  const view = render(
    <MemoryRouter initialEntries={['/monitoring']}>
      <AuthProvider>
        <Monitoring />
      </AuthProvider>
    </MemoryRouter>,
  )
  await screen.findByText('Aurora Retail · Monitoring')
  return view
}

async function pressRun() {
  const button = await screen.findByRole('button', { name: /run detection/i })
  await act(async () => {
    button.click()
  })
  await waitFor(() => expect(batchCalls).toHaveLength(1))
  await screen.findByText('ABNORMAL')
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

function setUp(permissions: string[]) {
  batchCalls = []
  localStorage.clear()
  localStorage.setItem(
    'bi.ai.session',
    JSON.stringify({ token: 'session-token', expiresAt: '2099-01-01T00:00:00Z' }),
  )
  localStorage.setItem('bi.ai.company', 'company-1')
  vi.stubGlobal('fetch', stubFetch(permissions))
}

describe('the business detection surface', () => {
  beforeEach(() => setUp(['analytics.read', 'detection.run', 'kpi.read']))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the five business figures and the comparison in plain language', async () => {
    await renderMonitoring()
    await pressRun()

    expect(screen.getByText('Revenue')).toBeTruthy()
    // Actual and Expected are the server's numbers, formatted for the reader.
    expect(screen.getByText(formatCurrency(6000000, 'INR', true))).toBeTruthy()
    expect(screen.getByText(formatCurrency(10250000, 'INR', true))).toBeTruthy()
    expect(screen.getByText('ABNORMAL')).toBeTruthy()
    // The comparison is offered, and only as the prose the server wrote.
    expect(screen.getAllByText(/Comparison: Comparable Fridays/).length).toBeGreaterThan(0)
  })

  it('prints the deviation the server calculated rather than recomputing it', async () => {
    await renderMonitoring()
    await pressRun()

    expect(screen.getByText('-37.2%')).toBeTruthy()
    // What a browser-side (actual - expected) / expected would have produced.
    expect(screen.queryByText('-41.5%')).toBeNull()
  })

  /**
   * The load-bearing test. Every forbidden term below is present in the response
   * the screen received, so absence from the document is a real result and not an
   * artefact of the stub being thin.
   */
  it('never renders the statistics, the bucket internals or the SQL', async () => {
    await renderMonitoring()
    await pressRun()

    const rendered = bodyText()
    for (const term of [
      'median',
      'absolute deviation',
      'z-score',
      'z_score',
      'dispersion',
      'same_day_of_week',
      'yoy',
      'group by',
      'net_revenue',
      'order_date',
      'aurora-weekly',
      'signature',
      'reference point',
    ]) {
      expect(rendered.toLowerCase()).not.toContain(term)
    }
    // Case-sensitive so ordinary words like "selected" cannot mask a query.
    expect(rendered).not.toContain('SELECT')
    expect(rendered).not.toContain('SUM(')
    // And the statistics are not merely hidden by CSS: the numbers are absent.
    expect(rendered).not.toContain('250,000')
    expect(rendered).not.toContain('11.4')
  })

  it('shows the last stored verdict before anybody presses anything', async () => {
    await renderMonitoring()

    // A blank screen would be less honest than the verdict the platform last
    // reached, and inventing a number for today would be worse than either.
    expect(screen.getByText('NORMAL')).toBeTruthy()
    expect(screen.getAllByText(/stored result/).length).toBeGreaterThan(0)
    expect(batchCalls).toHaveLength(0)
  })

  it('says why a KPI cannot be evaluated instead of dropping it', async () => {
    await renderMonitoring()

    expect(screen.getByText('Not ready to evaluate')).toBeTruthy()
    expect(screen.getByText('Gross Margin')).toBeTruthy()
    expect(
      screen.getByText('The KPI has no approved version bound to a source table.'),
    ).toBeTruthy()
  })

  it('reports a KPI the engine skipped beside the ones it evaluated', async () => {
    await renderMonitoring()
    await pressRun()

    expect(screen.getByText('Order Count')).toBeTruthy()
    expect(
      screen.getByText('No rows for 2026-08-28 in the registered source.'),
    ).toBeTruthy()
  })

  it('declines to assert a verdict it does not have, without hiding the measurement', async () => {
    await renderMonitoring()
    await pressRun()

    expect(screen.getByText('LOW CONFIDENCE')).toBeTruthy()
    expect(screen.getByText(formatCurrency(90000, 'INR', true))).toBeTruthy()
    // No expected value and no deviation were returned, and none is invented.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('asks the server for exactly the KPIs it can evaluate', async () => {
    await renderMonitoring()
    await pressRun()

    expect(batchCalls[0]).toEqual({
      target_date: expect.any(String),
      kpi_ids: ['revenue', 'refund_value', 'order_count'],
    })
  })
})

describe('a reader without permission to run detection', () => {
  beforeEach(() => setUp(['analytics.read']))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('can read the stored verdicts but cannot trigger a run', async () => {
    await renderMonitoring()

    expect(screen.getByText('NORMAL')).toBeTruthy()
    const button = screen.getByRole('button', { name: /run detection/i }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    await act(async () => {
      button.click()
    })
    expect(batchCalls).toHaveLength(0)
  })
})
