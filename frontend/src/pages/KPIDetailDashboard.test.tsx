import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { KpiContract, DetectionRunSummary } from '../api/types'
import KPIDetailDashboard from './KPIDetailDashboard'

const contract = {
  kpi_id: 'revenue',
  name: 'Revenue',
  unit: 'currency',
  currency: 'INR',
} as KpiContract

const runs: DetectionRunSummary[] = [
  {
    id: 'run-28',
    kpi_key: 'revenue',
    kpi_name: 'Revenue',
    kpi_version: 1,
    target_date: '2026-08-28',
    actual_value: 6000000,
    expected_value: 10250000,
    deviation_absolute: -4250000,
    deviation_pct: -41.5,
    status: 'ABNORMAL',
    comparison_label: 'Comparable Fridays',
    headline: 'Revenue was below comparable history.',
    unit: 'currency',
    currency: 'INR',
    executed_at: '2026-08-29T04:00:00Z',
  },
  {
    id: 'run-27',
    kpi_key: 'revenue',
    kpi_name: 'Revenue',
    kpi_version: 1,
    target_date: '2026-08-27',
    actual_value: 10000000,
    expected_value: 9900000,
    deviation_absolute: 100000,
    deviation_pct: 1.0,
    status: 'NORMAL',
    comparison_label: 'Comparable Thursdays',
    headline: 'Revenue was in line with history.',
    unit: 'currency',
    currency: 'INR',
    executed_at: '2026-08-28T04:00:00Z',
  },
  ...Array.from({ length: 9 }, (_, index) => ({
    id: `run-${index + 1}`,
    kpi_key: 'revenue',
    kpi_name: 'Revenue',
    kpi_version: 1,
    target_date: `2026-08-${String(index + 1).padStart(2, '0')}`,
    actual_value: 9500000 + index * 200000,
    expected_value: 9500000,
    deviation_absolute: index % 2 === 0 ? 150000 : -120000,
    deviation_pct: index % 2 === 0 ? 1.6 : -1.3,
    status: index % 3 === 0 ? 'NORMAL' : index % 3 === 1 ? 'ABNORMAL' : 'LOW_CONFIDENCE',
    comparison_label: 'Comparable day',
    headline: 'Historical baseline point.',
    unit: 'currency',
    currency: 'INR',
    executed_at: `2026-08-${String(index + 1).padStart(2, '0')}T04:00:00Z`,
  })),
]

afterEach(cleanup)

describe('KPIDetailDashboard', () => {
  it('uses one reusable workspace for persisted KPI history', () => {
    render(
      <KPIDetailDashboard
        contract={contract}
        runs={runs}
        window={{ start: '2026-08-01', end: '2026-08-31' }}
        onClose={() => undefined}
      />,
    )

    expect(screen.getByText('Revenue')).toBeTruthy()
    expect(screen.getByText('Historical Runs')).toBeTruthy()
    expect(screen.getByText('Historical Performance')).toBeTruthy()
    expect(screen.getByText('KPI Load Calendar')).toBeTruthy()
    expect(screen.getByText('Persisted detection result · no recalculation')).toBeTruthy()
  })

  it('selects a stored run without making a request', () => {
    render(
      <KPIDetailDashboard
        contract={contract}
        runs={runs}
        window={{ start: '2026-08-01', end: '2026-08-31' }}
        onClose={() => undefined}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Aug 27, 2026, NORMAL' }))
    expect(screen.getAllByText(/Revenue · Aug 27, 2026/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('₹10M').length).toBeGreaterThan(0)
  })

  it('reports an empty calendar date instead of fabricating a result', () => {
    render(
      <KPIDetailDashboard
        contract={contract}
        runs={runs}
        window={{ start: '2026-08-01', end: '2026-08-31' }}
        onClose={() => undefined}
      />,
    )

    fireEvent.click(screen.getAllByRole('button', { name: /no run available/ })[0])
    expect(screen.getByText('No run available for this date')).toBeTruthy()
  })

  it('colors the calendar tile and opens a popup with the last seven runs only', () => {
    render(
      <KPIDetailDashboard
        contract={contract}
        runs={runs}
        window={{ start: '2026-08-01', end: '2026-08-31' }}
        onClose={() => undefined}
      />,
    )

    const abnormalTile = screen.getByRole('button', { name: /Aug 28, 2026, ABNORMAL/ })
    expect(abnormalTile.className).toContain('bg-rose')

    fireEvent.click(abnormalTile)

    expect(screen.getByText('Run detail')).toBeTruthy()
    expect(screen.getByText('Last 7 historical runs')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /Run detail historical bar/ }).length).toBe(7)
  })
})
