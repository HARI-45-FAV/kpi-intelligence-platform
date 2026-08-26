/** Data-fetching hook with manual reload, used by every page. */

import { useCallback, useEffect, useState } from 'react'
import { describeError } from '../api/client'

export function useResource<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  options: { enabled?: boolean } = {},
) {
  const { enabled = true } = options
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState<string | null>(null)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(loader, deps)

  const reload = useCallback(async () => {
    if (!enabled) {
      setLoading(false)
      return
    }
    setLoading(true)
    setError(null)
    try {
      setData(await run())
    } catch (err) {
      setError(describeError(err))
    } finally {
      setLoading(false)
    }
  }, [run, enabled])

  useEffect(() => {
    void reload()
  }, [reload])

  return { data, loading, error, reload, setData }
}

/** Tracks a one-shot action: pending flag, error and success message. */
export function useAction() {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const run = useCallback(
    async <T,>(task: () => Promise<T>, successMessage?: string): Promise<T | undefined> => {
      setPending(true)
      setError(null)
      setMessage(null)
      try {
        const result = await task()
        if (successMessage) setMessage(successMessage)
        return result
      } catch (err) {
        setError(describeError(err))
        return undefined
      } finally {
        setPending(false)
      }
    },
    [],
  )

  const reset = useCallback(() => {
    setError(null)
    setMessage(null)
  }, [])

  return { pending, error, message, run, reset, setError }
}
