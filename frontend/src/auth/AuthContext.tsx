/**
 * Session and elevated-admin state.
 *
 * Two distinct credentials, matching the navigation model in the Sprint 1 spec:
 *
 *  - the session token, held for the whole app;
 *  - an admin token from /auth/admin-unlock, required by KPI Setup.
 *
 * The admin token is kept in memory only. Reloading the page closes the
 * governance workspace again, which is the intent: KPI Setup is re-authenticated
 * rather than merely hidden behind a route.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { api, configureTokens } from '../api/client'
import type { AuthResult, Membership, SessionInfo } from '../api/types'

const SESSION_KEY = 'bi.ai.session'
const COMPANY_KEY = 'bi.ai.company'

interface StoredSession {
  token: string
  expiresAt: string
}

interface AuthState {
  ready: boolean
  user: SessionInfo['user'] | null
  memberships: Membership[]
  companyId: string | null
  membership: Membership | null
  adminUnlocked: boolean
  adminPermissions: string[]
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string, fullName: string) => Promise<void>
  logout: () => void
  selectCompany: (companyId: string) => void
  refresh: () => Promise<void>
  unlockAdmin: (email: string, password: string) => Promise<void>
  lockAdmin: () => void
  can: (permission: string) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

function readStored(): StoredSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as StoredSession
    if (new Date(parsed.expiresAt).getTime() <= Date.now()) {
      localStorage.removeItem(SESSION_KEY)
      return null
    }
    return parsed
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<StoredSession | null>(() => readStored())
  const [user, setUser] = useState<SessionInfo['user'] | null>(null)
  const [memberships, setMemberships] = useState<Membership[]>([])
  const [companyId, setCompanyId] = useState<string | null>(
    () => localStorage.getItem(COMPANY_KEY),
  )
  const [adminToken, setAdminToken] = useState<string | null>(null)
  const [adminPermissions, setAdminPermissions] = useState<string[]>([])
  const [ready, setReady] = useState(false)

  // Who is signed in, readable without closing over a render's stale `user`.
  // `unlockAdmin` needs this: it fires from a click that can land before the
  // initial /auth/session resolves, and a stale null there would look like "a
  // different account is unlocking" and swap the session token needlessly.
  const userIdRef = useRef<string | null>(null)
  useEffect(() => {
    userIdRef.current = user?.id ?? null
  }, [user?.id])

  // The client reads tokens through getters so it always sees current values.
  useEffect(() => {
    configureTokens(
      () => (session ? session.token : null),
      () => adminToken,
    )
  }, [session, adminToken])

  const applyAuth = useCallback((result: AuthResult) => {
    const stored = { token: result.access_token, expiresAt: result.expires_at }
    localStorage.setItem(SESSION_KEY, JSON.stringify(stored))
    setSession(stored)
    // Merged, never overwritten with a blank. A response that omits identity —
    // /auth/admin-unlock historically did — must not blank the signed-in user,
    // because App renders the sign-in screen when `user` is null. That unmounts
    // the entire authenticated tree, and any half-finished KPI Setup work with
    // it, for the tick before /auth/session repopulates it.
    if (result.user) setUser(result.user)
    if (result.memberships?.length) setMemberships(result.memberships)
    const next = result.company_id ?? result.memberships?.[0]?.company_id ?? null
    if (next) {
      localStorage.setItem(COMPANY_KEY, next)
      setCompanyId(next)
    }
  }, [])

  const refresh = useCallback(async () => {
    if (!session) {
      setReady(true)
      return
    }
    try {
      const info = await api.get<SessionInfo>('/auth/session')
      const list = info.memberships ?? []
      setUser(info.user ?? null)
      setMemberships(list)
      setCompanyId((current) => {
        if (current && list.some((m) => m.company_id === current)) return current
        const fallback = list[0]?.company_id ?? null
        if (fallback) localStorage.setItem(COMPANY_KEY, fallback)
        return fallback
      })
    } catch {
      // An expired or revoked token: drop it rather than looping on 401s.
      localStorage.removeItem(SESSION_KEY)
      setSession(null)
      setUser(null)
      setMemberships([])
    } finally {
      setReady(true)
    }
  }, [session])

  useEffect(() => {
    void refresh()
    // Intentionally keyed on the token: re-resolve identity when it changes.
  }, [session?.token]) // eslint-disable-line react-hooks/exhaustive-deps

  const login = useCallback(
    async (email: string, password: string) => {
      const result = await api.post<AuthResult>('/auth/login', { email, password })
      applyAuth(result)
    },
    [applyAuth],
  )

  const register = useCallback(
    async (email: string, password: string, fullName: string) => {
      const result = await api.post<AuthResult>('/auth/register', {
        email,
        password,
        full_name: fullName,
      })
      applyAuth(result)
    },
    [applyAuth],
  )

  const logout = useCallback(() => {
    localStorage.removeItem(SESSION_KEY)
    localStorage.removeItem(COMPANY_KEY)
    setSession(null)
    setUser(null)
    setMemberships([])
    setCompanyId(null)
    setAdminToken(null)
    setAdminPermissions([])
  }, [])

  const selectCompany = useCallback((next: string) => {
    localStorage.setItem(COMPANY_KEY, next)
    setCompanyId(next)
    // Elevation is per-company: switching workspaces must re-lock governance.
    setAdminToken(null)
    setAdminPermissions([])
  }, [])

  const unlockAdmin = useCallback(
    async (email: string, password: string) => {
      if (!companyId) throw new Error('Select a company workspace first.')
      const result = await api.post<AuthResult>('/auth/admin-unlock', {
        email,
        password,
        company_id: companyId,
      })
      setAdminToken(result.access_token)
      setAdminPermissions(result.permissions ?? [])
      // Identity is refreshed in place. The elevated token stays a *separate*
      // credential held in memory — adopting it as the session token would both
      // shorten the session to the elevation TTL and change `session.token`,
      // which re-runs the identity effect and remounts the workspace under the
      // administrator's feet.
      if (result.user) setUser(result.user)
      if (result.memberships?.length) setMemberships(result.memberships)
      // Unlocking as a *different* account does become the active session: the
      // signed-in identity genuinely changed, so a remount is correct there.
      // Guarded on actually knowing who was signed in — while that is still
      // loading, updating identity in place is the safe move.
      if (result.user && userIdRef.current && result.user.id !== userIdRef.current) {
        applyAuth(result)
      }
    },
    [companyId, applyAuth],
  )

  const lockAdmin = useCallback(() => {
    setAdminToken(null)
    setAdminPermissions([])
  }, [])

  const membership = useMemo(
    () => (memberships ?? []).find((m) => m.company_id === companyId) ?? null,
    [memberships, companyId],
  )

  const can = useCallback(
    (permission: string) => {
      if (adminPermissions.length) return adminPermissions.includes(permission)
      return membership?.permissions.includes(permission) ?? false
    },
    [adminPermissions, membership],
  )

  const value = useMemo<AuthState>(
    () => ({
      ready,
      user,
      memberships,
      companyId,
      membership,
      adminUnlocked: Boolean(adminToken),
      adminPermissions,
      login,
      register,
      logout,
      selectCompany,
      refresh,
      unlockAdmin,
      lockAdmin,
      can,
    }),
    [
      ready,
      user,
      memberships,
      companyId,
      membership,
      adminToken,
      adminPermissions,
      login,
      register,
      logout,
      selectCompany,
      refresh,
      unlockAdmin,
      lockAdmin,
      can,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
