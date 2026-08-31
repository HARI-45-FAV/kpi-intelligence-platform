/**
 * The reveal toggle on a password field.
 *
 * Masking is the default and has to stay the default — a field that arrives
 * revealed leaks the password to whoever is behind the reader. What is worth
 * pinning is that the toggle actually changes the input's `type` (a toggle that
 * only swaps the icon looks right and does nothing), that its accessible name
 * says which way it will go, and that the props a form passes — `value`,
 * `onChange`, `required`, `minLength`, `autoComplete` — still reach the input.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Field, PasswordInput } from './components/ui'

afterEach(cleanup)

function theInput(): HTMLInputElement {
  // A masked input exposes no ARIA role, so it is found by tag.
  const input = document.querySelector('input')
  if (!input) throw new Error('no input rendered')
  return input
}

describe('PasswordInput', () => {
  it('masks the value until the reader asks for it', () => {
    render(<PasswordInput value="hunter2" onChange={() => {}} />)
    const input = theInput()
    expect(input.type).toBe('password')

    fireEvent.click(screen.getByRole('button', { name: 'Show password' }))
    expect(input.type).toBe('text')

    fireEvent.click(screen.getByRole('button', { name: 'Hide password' }))
    expect(input.type).toBe('password')
  })

  it('passes the attributes a form sets through to the input', () => {
    render(
      <PasswordInput
        value=""
        onChange={() => {}}
        required
        minLength={6}
        autoComplete="new-password"
      />,
    )

    const input = theInput()
    expect(input.required).toBe(true)
    expect(input.minLength).toBe(6)
    expect(input.autocomplete).toBe('new-password')
    expect(input.className).toContain('field')
  })

  it('keeps typing working: the toggle does not steal the caret', () => {
    render(<PasswordInput value="" onChange={vi.fn()} />)

    // Suppressing mousedown is what leaves focus in the input, so the event has
    // to come back defaultPrevented — fireEvent returns false in that case.
    const notPrevented = fireEvent.mouseDown(screen.getByRole('button', { name: 'Show password' }))
    expect(notPrevented).toBe(false)
  })

  it('names itself after the field it belongs to', () => {
    render(<PasswordInput value="" onChange={() => {}} toggleLabel="initial password" />)
    expect(screen.getByRole('button', { name: 'Show initial password' })).toBeTruthy()
  })

  it('drops into a Field without breaking the label or hint', () => {
    render(
      <Field label="Password" required hint="At least 6 characters.">
        <PasswordInput value="" onChange={() => {}} />
      </Field>,
    )

    expect(screen.getByText('Password')).toBeTruthy()
    expect(screen.getByText('At least 6 characters.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Show password' })).toBeTruthy()
  })
})
