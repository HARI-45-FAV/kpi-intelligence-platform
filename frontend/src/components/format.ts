/** Formatting helpers. */

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (Math.abs(value) < 1000) return formatNumber(value)
  return value.toLocaleString(undefined, { notation: 'compact', maximumFractionDigits: 1 })
}

export function formatCurrency(
  value: number | null | undefined,
  currency = 'USD',
  compact = false,
): string {
  if (value === null || value === undefined) return '—'
  try {
    return value.toLocaleString(undefined, {
      style: 'currency',
      currency,
      notation: compact ? 'compact' : 'standard',
      maximumFractionDigits: compact ? 1 : 2,
    })
  } catch {
    return `${currency} ${formatNumber(value, 2)}`
  }
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—'
  return `${value.toFixed(digits)}%`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' })
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return String(value)
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 0) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return formatDate(value)
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = seconds / 3600
  if (hours < 48) return `${hours.toFixed(1)} h`
  return `${Math.round(hours / 24)} d`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

export function titleCase(value: string | null | undefined): string {
  if (!value) return '—'
  return value
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/**
 * A KPI or metric name as a person should read it.
 *
 * Names reach the browser in whatever shape the KPI was registered in —
 * `average_order_value`, `total-revenue`, `orders.count` — and a technical key
 * is not something to put in front of a business reader. Separators become
 * spaces and every word gets a capital: `average_order_value` reads as
 * `Average Order Value`.
 *
 * Two kinds of word are left exactly as written, because "Title Case" applied
 * blindly makes them worse:
 *
 *   - a short all-caps token, which is an acronym (`MRR`, `AOV`, `GMV`) and
 *     must not become `Mrr`;
 *   - a word that already mixes cases (`eCommerce`), which someone typed
 *     deliberately.
 *
 * So a display name an administrator authored by hand survives untouched, and
 * only a raw key is rewritten. This is presentation only — `kpi_key` is still
 * what gets sent back to the API, filtered on and matched against a contract.
 */
export function formatKpiName(value: string | null | undefined, fallback = '—'): string {
  if (value === null || value === undefined) return fallback
  const words = value.trim().replace(/[_\-.]+/g, ' ').split(/\s+/).filter(Boolean)
  if (words.length === 0) return fallback
  return words.map(formatKpiWord).join(' ')
}

function formatKpiWord(word: string): string {
  const hasLower = /[a-z]/.test(word)
  const hasUpper = /[A-Z]/.test(word)
  // An acronym, or a deliberately mixed-case word: keep the author's capitals.
  if ((hasUpper && !hasLower && word.length <= 4) || (hasUpper && hasLower)) return word
  return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

export function monthName(month: number | null | undefined): string {
  if (!month || month < 1 || month > 12) return '—'
  return MONTHS[month - 1]
}

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

export function weekdayName(day: number | null | undefined): string {
  if (!day || day < 1 || day > 7) return '—'
  return WEEKDAYS[day - 1]
}
