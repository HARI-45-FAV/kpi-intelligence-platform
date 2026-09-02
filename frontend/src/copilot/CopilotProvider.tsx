/**
 * Where the Copilot gets its context from.
 *
 * The rule this file exists to enforce is that the user never types context. A
 * question asked from a KPI tile is already about that KPI, on that date, in that
 * company — so the panel reads all of it from where the user is standing rather
 * than from a form.
 *
 * Three sources feed it, in order of how automatic they are:
 *
 *  - **Company** comes from the session (`useAuth().companyId`) and is never a
 *    field. It is also not authorisation: the server re-derives the company from
 *    the JWT and the membership row, and ignores anything the client claims.
 *  - **Page** comes from the router, so every screen contributes it for free.
 *  - **Panel, KPI, version, selected date, dimension, selected entity and agent
 *    run** are published by whichever screen knows them, through
 *    `useCopilotScreen`. A screen that knows nothing publishes nothing and the
 *    panel simply has less context.
 *
 * Everything published here is a *hint*. The server re-resolves each field inside
 * the caller's own company before it reaches the model — a KPI id from another
 * company resolves to nothing, exactly as a deleted one would, an unapproved
 * dimension is dropped, and an entity outside the user's row scope is refused.
 *
 * What no screen publishes is a number. Every panel below has an actual, an
 * expected value and a deviation rendered on it, and none of them is sent: the
 * server re-reads the measurement from the run it stored. That is why one context
 * shape serves all five panels — it carries where the user is, not what they see.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useLocation } from 'react-router-dom'
import type { CopilotRequestContext } from '../api/types'

/**
 * Which panel is asking. One Copilot serves them all; the panel decides which
 * verified result the answer is anchored to and which mistake it must not make.
 *
 * This mirrors the server's `PANELS` set, which is the authority: a value the
 * server does not recognise is dropped there rather than passed to the model, so a
 * panel missing from this union is a screen whose context silently arrives blank.
 */
export type CopilotPanel =
  | 'stage_performance'
  | 'detection_detail'
  | 'historical_run'
  | 'investigation'
  | 'future_action'
  | 'kpi_setup'
  | 'monitoring'
  | 'dashboard'
  // The two explainability surfaces, both anchored on a stored result.
  | 'kpi_result'
  | 'investigation_node'

/** What a screen can tell the Copilot about itself. All optional. */
export interface CopilotScreenContext {
  panel?: CopilotPanel | null
  kpiId?: string | null
  kpiVersion?: number | null
  selectedDate?: string | null
  /** The approved dimension whose breakdown is on screen, if any. */
  dimension?: string | null
  /** The contributor selected within that dimension, if any. */
  selectedEntity?: string | null
  /** The agent run whose stored results are being displayed. */
  agentRunId?: string | null
  /** Shown in the panel so the user can see what the answer will be about. */
  label?: string | null
}

interface CopilotState {
  open: boolean
  /** Open the panel, optionally with a question already typed in. */
  openPanel: (question?: string) => void
  closePanel: () => void
  /** The seed question consumed once by the panel on open. */
  seed: string | null
  clearSeed: () => void
  screen: CopilotScreenContext
  page: string
  /** The request context, assembled. Company is deliberately not in here. */
  requestContext: CopilotRequestContext
  publish: (context: CopilotScreenContext | null) => void
}

/**
 * The state a page sees when no provider is above it.
 *
 * The Copilot is an optional layer, and a screen must not break because it is not
 * mounted — a page rendered outside the app shell (a test harness, a future
 * embed) should simply have no assistant. So the fallback is inert rather than a
 * thrown error: publishing context does nothing and the launcher does nothing.
 *
 * Defined once at module scope because `publish` is an effect dependency in
 * `useCopilotScreen`; a fresh closure per render would re-fire that effect
 * forever.
 */
const INERT: CopilotState = {
  open: false,
  openPanel: () => {},
  closePanel: () => {},
  seed: null,
  clearSeed: () => {},
  screen: {},
  page: '',
  requestContext: {},
  publish: () => {},
}

const CopilotStateContext = createContext<CopilotState>(INERT)

/** Router path → the page label recorded in the audit trail. */
function pageLabel(pathname: string): string {
  if (pathname === '/' || pathname === '') return 'dashboard'
  const trimmed = pathname.replace(/^\/+|\/+$/g, '')
  // KPI Setup has sub-routes worth distinguishing: "kpi-setup/documents" is a
  // more useful audit record than "kpi-setup".
  return trimmed.slice(0, 80)
}

export function CopilotProvider({ children }: { children: ReactNode }) {
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [seed, setSeed] = useState<string | null>(null)
  const [screen, setScreen] = useState<CopilotScreenContext>({})

  const page = pageLabel(location.pathname)

  const openPanel = useCallback((question?: string) => {
    if (question) setSeed(question)
    setOpen(true)
  }, [])

  const closePanel = useCallback(() => setOpen(false), [])
  const clearSeed = useCallback(() => setSeed(null), [])

  const publish = useCallback((context: CopilotScreenContext | null) => {
    setScreen(context ?? {})
  }, [])

  const requestContext = useMemo<CopilotRequestContext>(
    () => ({
      panel: screen.panel ?? null,
      kpi_id: screen.kpiId ?? null,
      kpi_version: screen.kpiVersion ?? null,
      selected_date: screen.selectedDate ?? null,
      dimension: screen.dimension ?? null,
      selected_entity: screen.selectedEntity ?? null,
      agent_run_id: screen.agentRunId ?? null,
      page,
    }),
    [
      screen.panel,
      screen.kpiId,
      screen.kpiVersion,
      screen.selectedDate,
      screen.dimension,
      screen.selectedEntity,
      screen.agentRunId,
      page,
    ],
  )

  const value = useMemo<CopilotState>(
    () => ({
      open,
      openPanel,
      closePanel,
      seed,
      clearSeed,
      screen,
      page,
      requestContext,
      publish,
    }),
    [open, openPanel, closePanel, seed, clearSeed, screen, page, requestContext, publish],
  )

  return <CopilotStateContext.Provider value={value}>{children}</CopilotStateContext.Provider>
}

export function useCopilot(): CopilotState {
  return useContext(CopilotStateContext)
}

/**
 * Publish this screen's context to the Copilot for as long as the screen is
 * mounted, and withdraw it on unmount.
 *
 * Keyed on the serialised value rather than on the object, because callers build
 * the object inline on every render and an identity-keyed effect would loop.
 * Withdrawing on unmount is the important half: a stale KPI left behind by a
 * closed detail modal would make the next answer quietly about the wrong thing.
 */
export function useCopilotScreen(context: CopilotScreenContext): void {
  const { publish } = useCopilot()
  const key = JSON.stringify({
    panel: context.panel ?? null,
    kpiId: context.kpiId ?? null,
    kpiVersion: context.kpiVersion ?? null,
    selectedDate: context.selectedDate ?? null,
    dimension: context.dimension ?? null,
    selectedEntity: context.selectedEntity ?? null,
    agentRunId: context.agentRunId ?? null,
    label: context.label ?? null,
  })

  useEffect(() => {
    publish(JSON.parse(key) as CopilotScreenContext)
    return () => publish(null)
  }, [key, publish])
}
