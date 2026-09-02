import { useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Alert, Field, PasswordInput } from '../components/ui'
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
    <div className="flex min-h-screen items-center justify-center px-4 py-8">
      <div className="w-full max-w-md">
        <div className="mb-7 text-center">
          <img
            src="/logo.svg"
            alt="BusinessIntelligence.ai"
            className="mx-auto mb-3 h-14 w-14 rounded-2xl object-cover shadow-[0_12px_24px_rgba(28,111,195,0.32)]"
          />
          <h1 className="text-[2rem] font-semibold tracking-tight text-slate-100">
            BusinessIntelligence<span className="text-[#2d8fe0]">.ai</span>
          </h1>
          <p className="mt-1.5 text-xs text-slate-500">
            Governed KPI intelligence · Sprint 1 foundation
          </p>
        </div>

        <form onSubmit={submit} className="panel space-y-5 p-5 shadow-[0_24px_60px_rgba(41,89,127,0.18)] sm:p-6">
          {/*
              A block-level flex row, so `mx-auto` centres it — an inline-flex box
              ignores auto margins and sat off to one side. `data-bare` keeps each
              half inside the switch: without it the global pebble rule squares the
              corners and lifts the option out of the pill it belongs to.
            */}
          <div className="segmented-switch mx-auto flex w-full max-w-[320px] p-1.5">
            {(['login', 'register'] as const).map((option) => (
              <button
                key={option}
                type="button"
                data-bare
                onClick={() => setMode(option)}
                className={`segmented-option flex-1 whitespace-nowrap px-3 py-2 text-xs ${mode === option ? 'segmented-option-active' : ''}`}
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
            hint={
              mode === 'register'
                ? 'At least 6 characters, using two of: lowercase, uppercase, digits, symbols.'
                : undefined
            }
          >
            <PasswordInput
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              required
              minLength={mode === 'register' ? 6 : undefined}
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
