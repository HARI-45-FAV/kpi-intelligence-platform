/**
 * Typed API client.
 *
 * Two tokens are held deliberately, matching the Sprint 1 navigation model:
 * the ordinary session token, and a separate short-lived *admin* token issued by
 * /auth/admin-unlock that gates the KPI Setup workspace. The governance area is
 * re-authenticated rather than merely hidden, so a left-open browser tab is not
 * an open door to the KPI contracts.
 */

const API = '/api/v1'

export class ApiError extends Error {
  status: number
  code: string
  details?: unknown
  requestId?: string

  constructor(status: number, body: any) {
    super(body?.message ?? `Request failed with ${status}`)
    this.status = status
    this.code = body?.code ?? 'unknown'
    this.details = body?.details
    this.requestId = body?.request_id
  }
}

type TokenSource = () => string | null

let sessionToken: TokenSource = () => null
let adminToken: TokenSource = () => null

export function configureTokens(session: TokenSource, admin: TokenSource) {
  sessionToken = session
  adminToken = admin
}

interface RequestOptions {
  method?: string
  body?: unknown
  /** Use the elevated admin token from /auth/admin-unlock. */
  admin?: boolean
  /** Send as multipart instead of JSON. */
  form?: FormData
  query?: Record<string, string | number | boolean | undefined>
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, admin = false, form, query } = options

  let url = `${API}${path}`
  if (query) {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null) params.set(key, String(value))
    }
    const qs = params.toString()
    if (qs) url += `?${qs}`
  }

  const headers: Record<string, string> = {}
  const token = admin ? adminToken() ?? sessionToken() : sessionToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let payload: BodyInit | undefined
  if (form) {
    payload = form
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  const response = await fetch(url, { method, headers, body: payload })

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const parsed = text ? safeJson(text) : null

  if (!response.ok) throw new ApiError(response.status, parsed)
  return parsed as T
}

function safeJson(text: string) {
  try {
    return JSON.parse(text)
  } catch {
    return { message: text }
  }
}

export const api = {
  get: <T,>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'GET' }),
  post: <T,>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'POST', body }),
  patch: <T,>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'PATCH', body }),
  put: <T,>(path: string, body?: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'PUT', body }),
  del: <T,>(path: string, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'DELETE' }),
  upload: <T,>(path: string, form: FormData, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: 'POST', form }),
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (Array.isArray(error.details)) {
      const parts = error.details
        .map((d: any) => (d?.field ? `${d.field}: ${d.problem}` : d?.problem))
        .filter(Boolean)
      if (parts.length) return `${error.message} (${parts.join('; ')})`
    }
    if (error.details && typeof error.details === 'object') {
      const missing = (error.details as any).missing_permissions
      if (Array.isArray(missing) && missing.length) {
        return `${error.message} Missing: ${missing.join(', ')}.`
      }
    }
    return error.message
  }
  if (error instanceof Error) return error.message
  return 'Something went wrong.'
}
