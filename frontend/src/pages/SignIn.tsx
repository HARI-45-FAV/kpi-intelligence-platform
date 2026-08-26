import { useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Alert, Field } from '../components/ui'
import { useAction } from '../components/useResource'

export default function SignIn() {
  const { login, register } = useAuth()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const { pending, error, run } = useAction()

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (mode === 'login') {
      await run(() => login(email, password))
    } else {
      await run(() => register(email, password, fullName))
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-7 text-center">
          <span className="mx-auto mb-3 grid h-10 w-10 place-items-center rounded-lg bg-accent text-sm font-bold text-white">
            BI
          </span>
          <h1 className="text-lg font-semibold text-slate-100">
            BusinessIntelligence<span className="text-accent">.ai</span>
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            Governed KPI intelligence · Sprint 1 foundation
          </p>
        </div>

        <form onSubmit={submit} className="panel space-y-4 p-5">
          <div className="flex gap-1 rounded-md border border-ink-700 bg-ink-850 p-1">
            {(['login', 'register'] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setMode(option)}
                className={`flex-1 rounded px-3 py-1.5 text-sm transition-colors ${
                  mode === option
                    ? 'bg-ink-700 font-medium text-slate-100'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {option === 'login' ? 'Sign in' : 'Create account'}
              </button>
            ))}
          </div>

          {error && <Alert>{error}</Alert>}

          {mode === 'register' && (
            <Field label="Full name" required>
              <input
                className="field"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                autoComplete="name"
                required
              />
            </Field>
          )}

          <Field label="Work email" required>
            <input
              type="email"
              className="field"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
            />
          </Field>

          <Field
            label="Password"
            required
            hint={mode === 'register' ? 'At least 12 characters.' : undefined}
          >
            <input
              type="password"
              className="field"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              required
              minLength={mode === 'register' ? 12 : undefined}
            />
          </Field>

          <button type="submit" className="btn-primary w-full" disabled={pending}>
            {pending ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
          </button>
        </form>
      </div>
    </div>
  )
}
