/**
 * Regression test for the KPI-setup routing/state bug.
 *
 * The reported symptom was that adding a suggested KPI made the KPI Setup page
 * "disappear or navigate away". There were two independent causes, and this file
 * pins both so neither can return:
 *
 *  1. `/auth/admin-unlock` returned a token with no identity on it, and the
 *     client adopted that response as its session. `user` went null for a tick,
 *     `App` renders the sign-in screen when `user` is null, and the entire
 *     authenticated tree — the half-configured KPI Setup with it — unmounted and
 *     remounted. Fixed by merging identity instead of overwriting it, and by
 *     keeping the elevated token separate from the session token.
 *
 *  2. The accepted KPI's id was held in component state, so anything that
 *     remounted the panel lost the selection. It now lives in `?kpi=`.
 *
 * Deliberately driven through the real components and the real router: the bug
 * was a lifecycle bug, and a unit test on a reducer would not have caught it.
 */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider, useAuth } from './auth/AuthContext'
import KpiSetup from './pages/kpi-setup/KpiSetup'

const USER = {
  id: 'user-1',
  email: 'admin@novamart.test',
  full_name: 'Ada Admin',
  is_active: true,
  is_platform_admin: false,
  created_at: '2026-01-01T00:00:00Z',
}

const MEMBERSHIP = {
  company_id: 'company-1',
  company_name: 'NovaMart',
  company_slug: 'novamart',
  role_key: 'ADMIN',
  role_name: 'Administrator',
  status: 'ACTIVE',
  is_admin_role: true,
  permissions: ['kpi.read', 'kpi.create', 'kpi.approve', 'company.manage'],
}

const COMPANY_DEFINITION = {
  kpi_key: 'revenue',
  name: 'Revenue',
  business_definition: 'Total recognised sales revenue across all orders.',
  source_formula: 'SUM(orders.order_value)',
  resolution_status: 'RESOLVED',
  formula_expression: 'SUM(orders.order_value)',
  source_table_id: 'table-orders',
  source_table: 'orders',
  time_field: 'order_date',
  time_grain: 'DAY',
  kind: 'SIMPLE',
  unit: 'currency',
  direction: 'HIGHER_IS_BETTER',
  owner: 'Finance',
  is_active: true,
  declared_grain: 'daily',
  declared_source: 'orders',
  dimensions: [],
  materiality_threshold_pct: 5,
  issues: [],
  already_registered: false,
  registered_kpi_id: null,
  importable: true,
}

const PROPOSAL = {
  kpi_key: 'units_sold',
  name: 'Units Sold',
  kind: 'SIMPLE',
  business_definition: 'Total units sold.',
  formula_expression: 'SUM(order_items.quantity)',
  source_table_id: 'table-items',
  source_table: 'order_items',
  time_field: null,
  time_grain: 'DAY',
  unit: 'count',
  confidence: 0.45,
  rationale: '',
  already_registered: false,
  dimensions: [],
  drivers: [],
  evidence: { reason: 'quantity is an additive numeric measure.' },
  warnings: [],
}

function kpiDefinition(id: string, name: string, origin = 'COMPANY') {
  return {
    id,
    kpi_key: name.toLowerCase(),
    name,
    status: 'PROPOSED',
    current_version: 1,
    versions: [
      {
        id: `${id}-v1`,
        version: 1,
        status: 'PROPOSED',
        formula_expression: 'SUM(orders.order_value)',
        time_grain: 'DAY',
        proposal_origin: origin,
        created_at: '2026-01-01T00:00:00Z',
      },
    ],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}

/** Mutable server state, so a POST can change what the next GET returns. */
let registry: ReturnType<typeof kpiDefinition>[]
let acceptedProposal: boolean

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    text: async () => JSON.stringify(body),
  } as Response
}

/**
 * `/auth/admin-unlock` responds the way the fixed backend does: identity travels
 * with the elevated token. A test that stubbed it without `user` would pass while
 * the real app broke, which is precisely what happened.
 */
function stubFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'

    if (url.endsWith('/auth/session')) {
      return jsonResponse({ user: USER, memberships: [MEMBERSHIP] })
    }
    if (url.endsWith('/auth/admin-unlock')) {
      return jsonResponse({
        access_token: 'admin-token',
        token_type: 'bearer',
        expires_at: '2099-01-01T00:00:00Z',
        company_id: 'company-1',
        company_name: 'NovaMart',
        role_key: 'ADMIN',
        permissions: MEMBERSHIP.permissions,
        user: USER,
        memberships: [MEMBERSHIP],
      })
    }
    if (url.includes('/kpi-source-definitions/import')) {
      registry = [...registry, kpiDefinition('kpi-revenue', 'Revenue')]
      return jsonResponse(
        {
          imported: [registry[registry.length - 1]],
          skipped: [],
          counts: { imported: 1, skipped: 0 },
        },
        201,
      )
    }
    if (url.includes('/kpi-source-definitions')) {
      const registered = registry.some((k) => k.kpi_key === 'revenue')
      return jsonResponse({
        definition_table: {
          source_table_id: 'table-contracts',
          data_source_name: 'NovaMart Supabase',
          schema: 'public',
          table: 'kpi_contracts',
          role_columns: { name: 'kpi_name', formula: 'formula' },
          matched_roles: 8,
          row_count: 1,
          detection_method: 'deterministic column-role scan',
        },
        other_candidate_tables: [],
        definitions: [
          {
            ...COMPANY_DEFINITION,
            already_registered: registered,
            registered_kpi_id: registered ? 'kpi-revenue' : null,
            importable: !registered,
          },
        ],
        counts: {
          total: 1,
          active: 1,
          resolved: 1,
          needs_mapping: 0,
          registered: registered ? 1 : 0,
          importable: registered ? 0 : 1,
        },
        note: 'Read verbatim from the company KPI registry.',
      })
    }
    if (url.includes('/kpi-proposals/accept')) {
      acceptedProposal = true
      const created = kpiDefinition('kpi-units', 'Units Sold', 'DISCOVERY')
      registry = [...registry, created]
      return jsonResponse(created, 201)
    }
    if (url.includes('/kpi-proposals')) {
      return jsonResponse({
        proposals: [{ ...PROPOSAL, already_registered: acceptedProposal }],
        note: 'Optional suggestions.',
      })
    }
    if (url.match(/\/kpis\/[^/]+$/) && method === 'GET') {
      return jsonResponse({ definition: registry[0], version: null, validation: null })
    }
    if (url.endsWith('/kpis')) {
      return jsonResponse(registry)
    }
    if (url.includes('/tables')) {
      return jsonResponse([
        {
          id: 'table-orders',
          table_name: 'orders',
          data_source_name: 'NovaMart Supabase',
          selected: true,
        },
      ])
    }
    return jsonResponse([])
  })
}

/** Renders the workspace at /kpi-setup/kpis and exposes the live location. */
function LocationProbe() {
  const location = useLocation()
  return (
    <div data-testid="location">{`${location.pathname}${location.search}`}</div>
  )
}

function Unlocker() {
  const { adminUnlocked, unlockAdmin } = useAuth()
  return (
    <button
      data-testid="unlock"
      onClick={() => void unlockAdmin(USER.email, 'correct-horse-battery')}
    >
      {adminUnlocked ? 'unlocked' : 'locked'}
    </button>
  )
}

async function renderWorkspace() {
  const view = render(
    <MemoryRouter initialEntries={['/kpi-setup/kpis']}>
      <AuthProvider>
        <Unlocker />
        <LocationProbe />
        <Routes>
          <Route path="/kpi-setup/*" element={<KpiSetup />} />
          <Route path="/" element={<div data-testid="dashboard">dashboard</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
  // Elevate exactly the way the app does, then wait for the panels to load.
  await act(async () => {
    screen.getByTestId('unlock').click()
  })
  await waitFor(() => expect(screen.getByTestId('unlock').textContent).toBe('unlocked'))
  return view
}

describe('KPI Setup state and routing lifecycle', () => {
  beforeEach(() => {
    registry = []
    acceptedProposal = false
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

  it('elevating to admin keeps the signed-in identity and the current route', async () => {
    await renderWorkspace()

    // The bug: adopting the unlock response as the session blanked `user`, which
    // renders SignIn and tears the workspace down.
    expect(screen.queryByTestId('dashboard')).toBeNull()
    expect(screen.getByTestId('location').textContent).toBe('/kpi-setup/kpis')
    expect(await screen.findByText('Company-defined KPIs')).toBeTruthy()

    // The elevated token must not become the session token: doing so both
    // shortens the session and re-runs the identity effect.
    const stored = JSON.parse(localStorage.getItem('bi.ai.session') ?? '{}')
    expect(stored.token).toBe('session-token')
  })

  it('accepting a suggested KPI stays on the KPI page and updates the list', async () => {
    await renderWorkspace()

    // Suggestions sit below the company's own definitions and start collapsed
    // once those exist. Expand it if it is not already open.
    const disclosure = await screen.findByRole('button', {
      name: /Optional additional KPI suggestions/i,
    })
    if (disclosure.getAttribute('aria-expanded') !== 'true') {
      await act(async () => {
        disclosure.click()
      })
    }
    expect(disclosure.getAttribute('aria-expanded')).toBe('true')

    const accept = await screen.findByRole('button', { name: /accept suggestion/i })
    await act(async () => {
      accept.click()
    })

    await waitFor(() => expect(acceptedProposal).toBe(true))

    // Still on the KPI page, still in the same company context, no navigation.
    expect(screen.getByTestId('location').textContent).toBe('/kpi-setup/kpis')
    expect(screen.queryByTestId('dashboard')).toBeNull()
    // The setup flow is intact: the primary company-definitions panel is still
    // mounted rather than having been reset or replaced.
    expect(screen.getByText('Company-defined KPIs')).toBeTruthy()
    // And the new KPI is in the governed list immediately.
    await waitFor(() => expect(screen.getByText(/KPI registry — 0 of 1 live/)).toBeTruthy())
  })

  it('importing company definitions stays on the page and preserves selection in the URL', async () => {
    await renderWorkspace()

    const importButton = await screen.findByRole('button', { name: /import 1 into governance/i })
    await act(async () => {
      importButton.click()
    })

    await waitFor(() => expect(registry).toHaveLength(1))
    expect(screen.getByTestId('location').textContent).toBe('/kpi-setup/kpis')

    // Selecting the imported KPI records it in the URL, so a remount reopens it
    // rather than losing the administrator's place.
    const open = await screen.findByRole('button', { name: /open contract/i })
    await act(async () => {
      open.click()
    })
    await waitFor(() =>
      expect(screen.getByTestId('location').textContent).toBe('/kpi-setup/kpis?kpi=kpi-revenue'),
    )
  })
})
