/** Small presentational primitives shared across the app. */

import type { ReactNode } from 'react'

export function Panel({
  title,
  actions,
  children,
  className = '',
  bodyClassName = 'p-4',
}: {
  title?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2 className="panel-title">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  )
}

const STATUS_TONES: Record<string, string> = {
  // Healthy / terminal-good
  ACTIVE: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  CONNECTED: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  GOOD: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  FRESH: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  PASS: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  SAFE: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  DIRECTLY_COMPATIBLE: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  APPROVED: 'border-emerald-800 bg-emerald-950/50 text-emerald-300',
  // Needs attention
  WARNING: 'border-amber-800 bg-amber-950/50 text-amber-300',
  WARN: 'border-amber-800 bg-amber-950/50 text-amber-300',
  STALE: 'border-amber-800 bg-amber-950/50 text-amber-300',
  SAFE_WITH_AGGREGATION: 'border-amber-800 bg-amber-950/50 text-amber-300',
  REQUIRES_AGGREGATION: 'border-amber-800 bg-amber-950/50 text-amber-300',
  REQUIRES_DIMENSION_MAPPING: 'border-amber-800 bg-amber-950/50 text-amber-300',
  UNDER_REVIEW: 'border-amber-800 bg-amber-950/50 text-amber-300',
  PROPOSED: 'border-sky-800 bg-sky-950/50 text-sky-300',
  // Problems
  POOR: 'border-rose-900 bg-rose-950/50 text-rose-300',
  FAIL: 'border-rose-900 bg-rose-950/50 text-rose-300',
  FAILED: 'border-rose-900 bg-rose-950/50 text-rose-300',
  RISKY: 'border-rose-900 bg-rose-950/50 text-rose-300',
  UNSAFE: 'border-rose-900 bg-rose-950/50 text-rose-300',
  REJECTED: 'border-rose-900 bg-rose-950/50 text-rose-300',
  // Neutral
  DRAFT: 'border-ink-600 bg-ink-850 text-slate-300',
  UNTESTED: 'border-ink-600 bg-ink-850 text-slate-400',
  UNKNOWN: 'border-ink-600 bg-ink-850 text-slate-400',
  DEPRECATED: 'border-ink-600 bg-ink-850 text-slate-500',
  SKIPPED: 'border-ink-600 bg-ink-850 text-slate-500',
}

export function StatusBadge({ status, label }: { status?: string | null; label?: string }) {
  if (!status) return <span className="text-slate-600">—</span>
  const tone = STATUS_TONES[status] ?? 'border-ink-600 bg-ink-850 text-slate-300'
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tone}`}
    >
      {label ?? status.replace(/_/g, ' ')}
    </span>
  )
}

export function Metric({
  label,
  value,
  hint,
  tone = 'default',
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'default' | 'muted' | 'good' | 'warn' | 'bad'
}) {
  const valueTone =
    tone === 'muted'
      ? 'text-slate-500'
      : tone === 'good'
        ? 'text-emerald-300'
        : tone === 'warn'
          ? 'text-amber-300'
          : tone === 'bad'
            ? 'text-rose-300'
            : 'text-slate-100'
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${valueTone}`}>{value}</div>
      {hint && <div className="mt-1 text-[11px] leading-snug text-slate-500">{hint}</div>}
    </div>
  )
}

export function Field({
  label,
  hint,
  children,
  required,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
  required?: boolean
}) {
  return (
    <label className="block">
      <span className="label">
        {label}
        {required && <span className="ml-0.5 text-rose-400">*</span>}
      </span>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </label>
  )
}

export function Alert({
  tone = 'error',
  children,
  onDismiss,
}: {
  tone?: 'error' | 'warn' | 'info' | 'success'
  children: ReactNode
  onDismiss?: () => void
}) {
  const tones = {
    error: 'border-rose-900/70 bg-rose-950/40 text-rose-200',
    warn: 'border-amber-900/70 bg-amber-950/40 text-amber-200',
    info: 'border-ink-600 bg-ink-850 text-slate-300',
    success: 'border-emerald-900/70 bg-emerald-950/40 text-emerald-200',
  }
  return (
    <div className={`flex items-start gap-3 rounded-md border px-3 py-2 text-sm ${tones[tone]}`}>
      <div className="flex-1">{children}</div>
      {onDismiss && (
        <button onClick={onDismiss} className="text-current opacity-60 hover:opacity-100">
          ✕
        </button>
      )}
    </div>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-500">
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-ink-600 border-t-accent" />
      {label ?? 'Loading…'}
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-10 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {description && (
        <p className="max-w-md text-xs leading-relaxed text-slate-500">{description}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  width = 'max-w-3xl',
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  children: ReactNode
  footer?: ReactNode
  width?: string
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex">
      <div className="flex-1 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <aside
        className={`flex w-full ${width} flex-col border-l border-ink-700 bg-ink-900 shadow-2xl`}
      >
        <header className="flex items-start justify-between gap-4 border-b border-ink-700 px-5 py-4">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold text-slate-100">{title}</h2>
            {subtitle && <div className="mt-1 text-xs text-slate-500">{subtitle}</div>}
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-500 hover:bg-ink-800 hover:text-slate-300"
            aria-label="Close"
          >
            ✕
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <footer className="flex items-center justify-end gap-2 border-t border-ink-700 px-5 py-3">
            {footer}
          </footer>
        )}
      </aside>
    </div>
  )
}

export function Modal({
  open,
  onClose,
  title,
  children,
  width = 'max-w-md',
}: {
  open: boolean
  onClose?: () => void
  title: ReactNode
  children: ReactNode
  width?: string
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      {/* Capped height with a scrolling body so tall content stays reachable on
          short viewports and small screens. */}
      <div className={`relative flex max-h-[90vh] w-full ${width} panel flex-col shadow-2xl`}>
        <header className="panel-head shrink-0">
          <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
          {onClose && (
            <button
              onClick={onClose}
              className="rounded p-1 text-slate-500 hover:bg-ink-800 hover:text-slate-300"
              aria-label="Close"
            >
              ✕
            </button>
          )}
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
      </div>
    </div>
  )
}

export function DefinitionRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(9rem,auto)_1fr] gap-3 border-b border-ink-800 py-2 last:border-0">
      <dt className="text-xs uppercase tracking-wider text-slate-500">{term}</dt>
      <dd className="min-w-0 text-sm text-slate-200">{children}</dd>
    </div>
  )
}

export function Placeholder({
  heading,
  arriving,
  bullets,
}: {
  heading: string
  arriving: string
  bullets: string[]
}) {
  return (
    <Panel>
      <div className="max-w-2xl space-y-4 py-6">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">{heading}</h1>
          <p className="mt-1 text-sm text-slate-400">{arriving}</p>
        </div>
        <ul className="space-y-1.5">
          {bullets.map((item) => (
            <li key={item} className="flex gap-2 text-sm text-slate-400">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-ink-600" />
              {item}
            </li>
          ))}
        </ul>
        <p className="rounded-md border border-ink-700 bg-ink-850 px-3 py-2 text-xs leading-relaxed text-slate-500">
          This surface is intentionally empty in Sprint 1. Sprint 1 establishes the governed
          foundation — company, data, security, documents and KPI contracts. Showing invented
          numbers here would misrepresent what the platform currently knows.
        </p>
      </div>
    </Panel>
  )
}
