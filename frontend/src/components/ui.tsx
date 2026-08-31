/** Small presentational primitives shared across the app. */

import { useState, type InputHTMLAttributes, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

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
  ACTIVE: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  CONNECTED: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  GOOD: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  FRESH: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  PASS: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  SAFE: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  DIRECTLY_COMPATIBLE: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  APPROVED: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  NORMAL: 'border-emerald-200 bg-emerald-50/80 text-emerald-700',
  // Needs attention
  WARNING: 'border-sky-200 bg-white/70 text-slate-600',
  WARN: 'border-sky-200 bg-white/70 text-slate-600',
  STALE: 'border-sky-200 bg-white/70 text-slate-600',
  SAFE_WITH_AGGREGATION: 'border-sky-200 bg-white/70 text-slate-600',
  REQUIRES_AGGREGATION: 'border-sky-200 bg-white/70 text-slate-600',
  REQUIRES_DIMENSION_MAPPING: 'border-sky-200 bg-white/70 text-slate-600',
  UNDER_REVIEW: 'border-sky-200 bg-white/70 text-slate-600',
  // Neutral glass treatment keeps the status readable without the yellow warning cue.
  NEEDS_REVIEW: 'border-sky-200 bg-white/70 text-slate-600',
  PROPOSED: 'border-sky-200 bg-sky-50/90 text-sky-700',
  // Problems
  POOR: 'border-rose-200 bg-rose-50/85 text-rose-700',
  FAIL: 'border-rose-200 bg-rose-50/85 text-rose-700',
  FAILED: 'border-rose-200 bg-rose-50/85 text-rose-700',
  RISKY: 'border-rose-200 bg-rose-50/85 text-rose-700',
  UNSAFE: 'border-rose-200 bg-rose-50/85 text-rose-700',
  REJECTED: 'border-rose-200 bg-rose-50/85 text-rose-700',
  ABNORMAL: 'border-rose-200 bg-rose-50/85 text-rose-700',
  // Neutral
  DRAFT: 'border-slate-200 bg-white/70 text-slate-500',
  UNTESTED: 'border-slate-200 bg-white/70 text-slate-500',
  UNKNOWN: 'border-slate-200 bg-white/70 text-slate-500',
  DEPRECATED: 'border-slate-200 bg-white/60 text-slate-500',
  SKIPPED: 'border-slate-200 bg-white/60 text-slate-500',
  // A verdict the platform declines to assert: neutral on purpose, so that
  // "not enough comparable history" never reads as a clean bill of health.
  LOW_CONFIDENCE: 'border-slate-200 bg-white/70 text-slate-500',
}

export function StatusBadge({ status, label }: { status?: string | null; label?: string }) {
  if (!status) return <span className="text-slate-600">—</span>
  const tone = STATUS_TONES[status] ?? 'border-slate-200 bg-white/70 text-slate-500'
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

/**
 * A password field with a reveal toggle.
 *
 * Masked on arrival, because a password left on screen is a password anyone
 * behind the reader can copy. The eye reveals it on demand, which is what stops
 * people mistyping a long passphrase into a field they cannot check. The icon
 * shows the action available rather than the state, so an open eye means
 * "show me" and a crossed-out eye means "hide it again".
 *
 * `type` belongs to this component, hence its absence from the accepted props.
 * Everything else an `<input>` takes — `value`, `onChange`, `required`,
 * `minLength`, `autoComplete`, `autoFocus` — passes straight through, so this
 * drops into a `Field` exactly where a plain masked `<input>` sat.
 */
export function PasswordInput({
  className = 'field',
  toggleLabel = 'password',
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> & { toggleLabel?: string }) {
  const [visible, setVisible] = useState(false)
  const action = visible ? `Hide ${toggleLabel}` : `Show ${toggleLabel}`
  return (
    <div className="relative">
      <input {...props} type={visible ? 'text' : 'password'} className={`${className} pr-10`} />
      <button
        type="button"
        // These fields live inside Field's <label>, where a click would hand
        // focus to the input and move the caret to its end. Suppressing mousedown
        // leaves the caret where the reader left it, so typing continues.
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => setVisible((current) => !current)}
        aria-label={action}
        title={action}
        className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-lg p-1.5 text-slate-500 transition hover:bg-white/70 hover:text-slate-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
      >
        {visible ? <EyeOffIcon /> : <EyeIcon />}
      </button>
    </div>
  )
}

/* No icon package is installed, so the two glyphs are inline and take their
   colour from the button around them. */

function EyeIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      aria-hidden="true"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M1.8 10S4.9 4.7 10 4.7 18.2 10 18.2 10 15.1 15.3 10 15.3 1.8 10 1.8 10Z" />
      <circle cx="10" cy="10" r="2.5" />
    </svg>
  )
}

function EyeOffIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      aria-hidden="true"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8.2 5A8 8 0 0 1 10 4.7c5.1 0 8.2 5.3 8.2 5.3a15.4 15.4 0 0 1-2.3 3" />
      <path d="M4.7 6.3A15.3 15.3 0 0 0 1.8 10s3.1 5.3 8.2 5.3a8 8 0 0 0 3-.55" />
      <path d="M8.3 8.4a2.5 2.5 0 0 0 3.4 3.4" />
      <path d="M3 3l14 14" />
    </svg>
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
    error: 'border-rose-200 bg-rose-50/85 text-rose-700',
    warn: 'border-sky-200 bg-white/75 text-slate-700 shadow-[0_8px_18px_rgba(55,92,128,0.08)]',
    info: 'border-sky-200 bg-sky-50/70 text-sky-800 shadow-[0_8px_18px_rgba(55,92,128,0.08)]',
    success: 'border-emerald-200 bg-emerald-50/85 text-emerald-700',
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

/**
 * Every full-screen overlay renders into `document.body`, never in place.
 *
 * `position: fixed` is only relative to the viewport while no ancestor has
 * created a containing block for it — and `backdrop-filter`, `transform`, `filter`
 * and `perspective` all do. The app shell frosts its content area
 * (`.app-content-shell` carries `backdrop-filter: blur(22px)`), so an overlay left
 * inside the page tree resolves `inset: 0` against that element instead of the
 * screen. The shell is taller than the viewport on every populated page, so
 * `items-center` then parks the dialog several hundred pixels below the fold: it
 * mounts, it is in the DOM, it holds the right data, and the user sees nothing.
 *
 * A portal makes the overlay a child of `<body>`, which restores the viewport as
 * its containing block. React keeps the event and context tree intact, so
 * handlers and providers behave exactly as they did in place.
 *
 * This cannot be caught by the test suite: jsdom performs no layout, so a
 * mispositioned dialog and a correctly positioned one are the same document.
 * `dashboard-kpi-popup.test.tsx` therefore asserts the portal itself.
 */
export function Overlay({ children }: { children: ReactNode }) {  if (typeof document === 'undefined') return <>{children}</>
  return createPortal(children, document.body)
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
    <Overlay>
      <div className="fixed inset-0 z-40 flex">
        <div className="anim-overlay flex-1 bg-sky-950/20 backdrop-blur-sm" onClick={onClose} />
        <aside
          className={`anim-drawer flex w-full ${width} flex-col border-l border-white/80 bg-white/85 shadow-[var(--shadow-floating)] backdrop-blur-2xl`}
        >
          <header className="flex items-start justify-between gap-4 border-b border-ink-700/70 px-5 py-4">
            <div className="min-w-0">
              <h2 className="truncate text-base font-semibold text-slate-100">{title}</h2>
              {subtitle && <div className="mt-1 text-xs text-slate-500">{subtitle}</div>}
            </div>
            <button
              onClick={onClose}
              data-bare
              className="rounded-lg p-1 text-slate-500 transition-colors hover:bg-white hover:text-slate-300"
              aria-label="Close"
            >
              ✕
            </button>
          </header>
          <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && (
            <footer className="flex items-center justify-end gap-2 border-t border-ink-700/70 px-5 py-3">
              {footer}
            </footer>
          )}
        </aside>
      </div>
    </Overlay>
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
    <Overlay>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div className="anim-overlay absolute inset-0 bg-sky-950/20 backdrop-blur-sm" onClick={onClose} />
        {/* Capped height with a scrolling body so tall content stays reachable on
            short viewports and small screens. */}
        <div className={`anim-dialog relative flex max-h-[90vh] w-full ${width} panel flex-col shadow-2xl`}>
          <header className="panel-head shrink-0">
            <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
            {onClose && (
              <button
                onClick={onClose}
                data-bare
                className="rounded-lg p-1 text-slate-500 transition-colors hover:bg-white hover:text-slate-300"
                aria-label="Close"
              >
                ✕
              </button>
            )}
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
        </div>
      </div>
    </Overlay>
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

/* ===================================================================
   Enterprise governance primitives.

   The KPI Setup workspace configures things a business person decides
   about, but every fact behind those decisions is technical. These
   primitives enforce one split: the page states the decision, and the
   explanation lives one click away in a Help dialog. Nothing here reads
   or writes data — they are presentation only.
   =================================================================== */

/**
 * A section's explanation, behind a `?`.
 *
 * The panel keeps a single short line at most; everything a reader might
 * need to *understand* the section — what it is, what each field changes,
 * why it matters to the business — goes in `children` and stays out of the
 * way until asked for.
 */
export function SectionHelp({
  title,
  children,
  width = 'max-w-2xl',
  label = 'What is this?',
}: {
  title: string
  children: ReactNode
  width?: string
  label?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className="help-btn"
        onClick={() => setOpen(true)}
        aria-label={label}
        title={label}
      >
        ?
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title={title} width={width}>
        <div className="space-y-5 text-sm leading-relaxed text-slate-300">{children}</div>
      </Modal>
    </>
  )
}

/** One titled block inside a Help dialog. */
export function HelpSection({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
        {heading}
      </h3>
      <div className="space-y-2 text-[13px] leading-relaxed text-slate-300">{children}</div>
    </section>
  )
}

/** A term-and-explanation list for Help dialogs. */
export function HelpList({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="space-y-2">
      {items.map(([term, description]) => (
        <div key={term} className="grid gap-0.5 sm:grid-cols-[11rem_1fr] sm:gap-3">
          <dt className="text-[13px] font-medium text-slate-100">{term}</dt>
          <dd className="text-[13px] leading-relaxed text-slate-400">{description}</dd>
        </div>
      ))}
    </dl>
  )
}

/**
 * A number the reader is meant to notice.
 *
 * `Metric` renders a figure inside prose; this renders the figure *as* the
 * content, which is what a governance summary needs — four of these in a row
 * answer "how much is configured" before any table is read.
 */
export function StatCard({
  label,
  value,
  caption,
  tone = 'default',
}: {
  label: string
  value: ReactNode
  caption?: ReactNode
  tone?: 'default' | 'good' | 'warn' | 'bad' | 'muted'
}) {
  const valueTone =
    tone === 'good'
      ? 'text-emerald-700'
      : tone === 'warn'
        ? 'text-amber-700'
        : tone === 'bad'
          ? 'text-rose-700'
          : tone === 'muted'
            ? 'text-slate-500'
            : 'text-slate-100'
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value mt-2 ${valueTone}`}>{value}</div>
      {caption && <div className="mt-1.5 text-[11px] leading-snug text-slate-500">{caption}</div>}
    </div>
  )
}

/** A read-only business fact: label above, value below. */
export function InfoTile({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="surface-card px-3.5 py-3">
      <div className="text-[10.5px] font-semibold uppercase tracking-[0.13em] text-slate-500">
        {label}
      </div>
      <div className="mt-1.5 truncate text-[15px] font-medium text-slate-100">{value}</div>
    </div>
  )
}

/**
 * Setting → current value → action.
 *
 * The shape every configuration surface in this workspace uses, so a reader
 * never has to work out where the current state is on a page they have not
 * seen before.
 */
export function SettingRow({
  name,
  value,
  action,
  status,
}: {
  name: string
  value: ReactNode
  action?: ReactNode
  status?: ReactNode
}) {
  return (
    <div className="setting-row">
      <div className="setting-name">{name}</div>
      <div className="setting-value">{value}</div>
      {status && <div className="shrink-0">{status}</div>}
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

/** A tab's heading: what this screen is, plus its actions and its Help. */
export function SectionHeader({
  title,
  summary,
  help,
  actions,
}: {
  title: string
  summary?: ReactNode
  help?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2.5">
        <h2 className="truncate text-[17px] font-semibold tracking-tight text-slate-100">{title}</h2>
        {help}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {summary && <span className="text-xs text-slate-500">{summary}</span>}
        {actions}
      </div>
    </div>
  )
}
