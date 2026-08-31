/**
 * KPI names are what a business reader sees, so the rewriting rule is pinned here.
 *
 * The point of the helper is that no `snake_case` identifier reaches a screen,
 * while a name someone already wrote properly — including an acronym — survives
 * being passed through it.
 */

import { describe, expect, it } from 'vitest'
import { formatKpiName } from './components/format'

describe('formatKpiName', () => {
  it('reads a technical key as English', () => {
    expect(formatKpiName('average_order_value')).toBe('Average Order Value')
    expect(formatKpiName('total_revenue')).toBe('Total Revenue')
    expect(formatKpiName('customer_count')).toBe('Customer Count')
  })

  it('handles the other separators a key can arrive with', () => {
    expect(formatKpiName('gross-margin')).toBe('Gross Margin')
    expect(formatKpiName('orders.count')).toBe('Orders Count')
    expect(formatKpiName('net__revenue')).toBe('Net Revenue')
    expect(formatKpiName('  units_sold  ')).toBe('Units Sold')
  })

  it('leaves a display name that is already correct alone', () => {
    expect(formatKpiName('Gross Margin')).toBe('Gross Margin')
    expect(formatKpiName('Revenue')).toBe('Revenue')
  })

  it('is idempotent, so passing a formatted name through again is safe', () => {
    const once = formatKpiName('average_order_value')
    expect(formatKpiName(once)).toBe(once)
  })

  it('keeps acronyms as acronyms', () => {
    expect(formatKpiName('MRR')).toBe('MRR')
    expect(formatKpiName('net_MRR')).toBe('Net MRR')
    expect(formatKpiName('aov_by_channel')).toBe('Aov By Channel')
    expect(formatKpiName('AOV_by_channel')).toBe('AOV By Channel')
  })

  it('does not shout a key that was written in caps', () => {
    expect(formatKpiName('TOTAL_REVENUE')).toBe('Total Revenue')
  })

  it('falls back rather than rendering nothing', () => {
    expect(formatKpiName(null)).toBe('—')
    expect(formatKpiName(undefined)).toBe('—')
    expect(formatKpiName('')).toBe('—')
    expect(formatKpiName('___')).toBe('—')
    expect(formatKpiName(null, 'Unnamed KPI')).toBe('Unnamed KPI')
  })

  it('never leaves an underscore behind', () => {
    for (const key of ['average_order_value', 'net_revenue', 'units_sold', 'refund_value']) {
      expect(formatKpiName(key)).not.toContain('_')
    }
  })
})
