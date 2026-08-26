/**
 * Who can access what.
 *
 * The backend model is unchanged: 23 named permissions, checked individually,
 * across every role. What this screen changes is what it asks the reader to hold
 * in their head. It leads with the four boundaries a business actually decides
 * about — the workspace, KPI definitions, sensitive data and documents — for the
 * three roles the access model is explained with. Every other role stays
 * available in the role picker and is enforced exactly as before; the full
 * permission matrix is one click away rather than the default view.
 */

import { useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { Member, RoleInfo } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime } from '../../components/format'
import { Alert, EmptyState, Field, Modal, Panel, Spinner, StatusBadge } from '../../components/ui'
import { useAction, useResource } from '../../components/useResource'

/**
 * The access boundaries the overview answers, in the order they matter. Keys
 * match `access_areas` on the roles endpoint, which the backend derives from the
 * permissions each role actually holds — so this table cannot drift away from
 * what is really enforced.
 */
const ACCESS_AREAS: Array<{ key: string; label: string; note: string }> = [
  {
    key: 'workspace_configuration',
    label: 'Workspace settings',
    note: 'Company profile, calendar, data sources and analytical scope.',
  },
  {
    key: 'kpi_definitions',
    label: 'KPI definitions',
    note: 'Create, edit or approve what a KPI means.',
  },
  {
    key: 'sensitive_data',
    label: 'Sensitive data',
    note: 'Columns classified as personal, confidential or restricted.',
  },
  { key: 'documents', label: 'Documents', note: 'Company documents and their versions.' },
]

export default function SecurityPanel() {
  const { companyId, user } = useAuth()
  const base = `/companies/${companyId}`

  const members = useResource<Member[]>(() => api.get(`${base}/members`, { admin: true }), [companyId])
  const roles = useResource<RoleInfo[]>(() => api.get(`${base}/roles`, { admin: true }), [companyId])
  const [inviteOpen, setInviteOpen] = useState(false)

  return (
    <div className="space-y-5">
      <Panel
        title="Members & roles"
        actions={
          <button className="btn-primary btn-xs" onClick={() => setInviteOpen(true)}>
            + Add member
          </button>
        }
        bodyClassName=""
      >
        {members.loading && !members.data ? (
          <div className="p-4">
            <Spinner />
          </div>
        ) : members.error ? (
          <div className="p-4">
            <Alert>{members.error}</Alert>
          </div>
        ) : !members.data?.length ? (
          <EmptyState title="No members" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-ink-700">
                  <th className="table-head">Member</th>
                  <th className="table-head">Role</th>
                  <th className="table-head">Status</th>
                  <th className="table-head">Row scope</th>
                  <th className="table-head">Denied columns</th>
                  <th className="table-head">Added</th>
                  <th className="table-head" />
                </tr>
              </thead>
              <tbody>
                {members.data.map((member) => (
                  <MemberRow
                    key={member.membership_id}
                    base={base}
                    member={member}
                    roles={roles.data ?? []}
                    isSelf={member.email === user?.email}
                    onChanged={members.reload}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <AccessOverview roles={roles.data ?? []} loading={roles.loading && !roles.data} />

      {inviteOpen && (
        <InviteModal
          base={base}
          roles={roles.data ?? []}
          onClose={() => setInviteOpen(false)}
          onCreated={async () => {
            setInviteOpen(false)
            await members.reload()
          }}
        />
      )}
    </div>
  )
}

/** Core roles first, additional ones grouped below. Every role stays selectable —
 *  this only stops the picker leading with the ones the demo does not need. */
function RoleOptions({ roles }: { roles: RoleInfo[] }) {
  const core = roles.filter((role) => role.is_core)
  const additional = roles.filter((role) => !role.is_core)
  if (!core.length) {
    return (
      <>
        {roles.map((role) => (
          <option key={role.role_key} value={role.role_key}>
            {role.name}
          </option>
        ))}
      </>
    )
  }
  return (
    <>
      {core.map((role) => (
        <option key={role.role_key} value={role.role_key}>
          {role.name}
        </option>
      ))}
      {additional.length > 0 && (
        <optgroup label="Additional roles">
          {additional.map((role) => (
            <option key={role.role_key} value={role.role_key}>
              {role.name}
            </option>
          ))}
        </optgroup>
      )}
    </>
  )
}

function AccessOverview({ roles, loading }: { roles: RoleInfo[]; loading: boolean }) {
  const [showAdvanced, setShowAdvanced] = useState(false)

  // Core roles lead. `is_core` comes from the backend permission catalogue, so
  // this screen follows that single source rather than a duplicate list here.
  const core = useMemo(() => roles.filter((role) => role.is_core), [roles])
  const additional = useMemo(() => roles.filter((role) => !role.is_core), [roles])
  const shown = core.length ? core : roles

  if (loading) {
    return (
      <Panel title="Who can access what?">
        <Spinner />
      </Panel>
    )
  }

  return (
    <Panel title="Who can access what?" bodyClassName="">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-ink-700">
              <th className="table-head">Role</th>
              {ACCESS_AREAS.map((area) => (
                <th key={area.key} className="table-head" title={area.note}>
                  {area.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((role) => (
              <tr key={role.role_key} className="border-b border-ink-800 last:border-0">
                <td className="table-cell align-top">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-slate-100">{role.name}</span>
                    {role.is_admin_role && <span className="chip">admin</span>}
                  </div>
                  {role.access_summary && (
                    <p className="mt-1 max-w-md text-[11px] leading-snug text-slate-500">
                      {role.access_summary}
                    </p>
                  )}
                </td>
                {ACCESS_AREAS.map((area) => (
                  <td key={area.key} className="table-cell align-top">
                    {role.access_areas?.[area.key] ? (
                      <span className="text-emerald-400">✓ Yes</span>
                    ) : (
                      <span className="text-slate-600">— No</span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="border-t border-ink-800 px-4 py-3 text-[11px] leading-relaxed text-slate-500">
        Every company is also isolated from every other: the company in a request URL is treated as
        a claim by the caller, and membership is re-checked against the database on each request. A
        member can additionally be limited to specific rows — one region, say — and denied specific
        columns; set those per member above.
      </p>

      <div className="border-t border-ink-800">
        <button
          className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left text-sm text-slate-300 hover:bg-ink-850"
          onClick={() => setShowAdvanced((v) => !v)}
          aria-expanded={showAdvanced}
        >
          <span className="text-slate-500">{showAdvanced ? '▾' : '▸'}</span>
          Advanced permissions
          <span className="chip">{roles.length} roles</span>
          {additional.length > 0 && !showAdvanced && (
            <span className="text-[11px] text-slate-600">
              including {additional.map((r) => r.name).join(', ')}
            </span>
          )}
        </button>

        {showAdvanced && (
          <div className="border-t border-ink-800">
            <p className="px-4 py-3 text-xs leading-relaxed text-slate-500">
              Permissions are checked individually, never inferred from a role name, so adding a
              role cannot accidentally widen access. The three{' '}
              <code className="mono">data.read_*</code> permissions are what make profiling
              access-aware: a role without them never reads the column at all, rather than reading
              it and having the result stripped out afterwards.
            </p>
            {roles.map((role) => (
              <div key={role.role_key} className="border-t border-ink-800 px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-slate-100">{role.name}</span>
                  <span className="chip mono">{role.role_key}</span>
                  {role.is_admin_role && <StatusBadge status="ACTIVE" label="admin" />}
                  {!role.is_core && (
                    <span
                      className="chip text-slate-500"
                      title="Fully supported and enforced; not part of the core access model"
                    >
                      additional
                    </span>
                  )}
                  <span className="text-[11px] text-slate-600">
                    {role.permissions.length} permissions
                  </span>
                </div>
                {role.description && (
                  <p className="mt-1 text-xs text-slate-500">{role.description}</p>
                )}
                <div className="mt-2 flex flex-wrap gap-1">
                  {role.permissions.map((permission) => (
                    <span
                      key={permission}
                      className={`chip mono ${
                        permission.startsWith('data.read')
                          ? 'border-amber-900/60 text-amber-300'
                          : ''
                      }`}
                    >
                      {permission}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Panel>
  )
}

function MemberRow({
  base,
  member,
  roles,
  isSelf,
  onChanged,
}: {
  base: string
  member: Member
  roles: RoleInfo[]
  isSelf: boolean
  onChanged: () => Promise<void>
}) {
  const update = useAction()
  const remove = useAction()
  const [editing, setEditing] = useState(false)
  const [roleKey, setRoleKey] = useState(member.role_key)
  const [scopeText, setScopeText] = useState(
    Object.entries(member.row_scope ?? {})
      .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join('|') : value}`)
      .join(', '),
  )
  const [deniedText, setDeniedText] = useState((member.denied_columns ?? []).join(', '))

  const parseScope = () => {
    const scope: Record<string, string[]> = {}
    for (const part of scopeText.split(',')) {
      const [key, value] = part.split('=').map((s) => s.trim())
      if (key && value) scope[key] = value.split('|').map((v) => v.trim())
    }
    return scope
  }

  const save = async () => {
    const ok = await update.run(() =>
      api.patch(
        `${base}/members/${member.membership_id}`,
        {
          role_key: roleKey,
          row_scope: parseScope(),
          denied_columns: deniedText
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean),
        },
        { admin: true },
      ),
    )
    if (ok) {
      setEditing(false)
      await onChanged()
    }
  }

  if (editing) {
    return (
      <tr className="border-b border-ink-800 bg-ink-850">
        <td className="table-cell text-slate-200">{member.email}</td>
        <td className="table-cell" colSpan={4}>
          <div className="grid gap-2 py-1 sm:grid-cols-3">
            <select
              className="field text-xs"
              value={roleKey}
              onChange={(e) => setRoleKey(e.target.value)}
            >
              <RoleOptions roles={roles} />
            </select>
            <input
              className="field text-xs"
              value={scopeText}
              onChange={(e) => setScopeText(e.target.value)}
              placeholder="region=South|West"
              title="Row scope, e.g. region=South|West"
            />
            <input
              className="field text-xs"
              value={deniedText}
              onChange={(e) => setDeniedText(e.target.value)}
              placeholder="customer_master.email"
              title="Denied columns, comma-separated"
            />
          </div>
          {update.error && (
            <div className="pb-1">
              <Alert>{update.error}</Alert>
            </div>
          )}
        </td>
        <td className="table-cell">
          <div className="flex gap-1">
            <button className="btn-primary btn-xs" disabled={update.pending} onClick={save}>
              Save
            </button>
            <button className="btn-ghost btn-xs" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </td>
        <td />
      </tr>
    )
  }

  return (
    <tr className="border-b border-ink-800 last:border-0">
      <td className="table-cell">
        <span className="text-slate-200">{member.full_name}</span>
        <span className="ml-2 text-[11px] text-slate-600">{member.email}</span>
        {isSelf && <span className="ml-1.5 chip">you</span>}
      </td>
      <td className="table-cell">
        <span className="text-slate-300">{member.role_name}</span>
        {member.is_admin_role && <span className="ml-1.5 chip">admin</span>}
      </td>
      <td className="table-cell">
        <StatusBadge status={member.status} />
      </td>
      <td className="table-cell">
        {Object.keys(member.row_scope ?? {}).length ? (
          <div className="flex flex-wrap gap-1">
            {Object.entries(member.row_scope).map(([key, value]) => (
              <span key={key} className="chip mono">
                {key}={Array.isArray(value) ? value.join('|') : String(value)}
              </span>
            ))}
          </div>
        ) : (
          <span className="text-[11px] text-slate-600">unrestricted</span>
        )}
      </td>
      <td className="table-cell">
        {member.denied_columns?.length ? (
          <div className="flex flex-wrap gap-1">
            {member.denied_columns.map((column) => (
              <span key={column} className="chip mono text-rose-300">
                {column}
              </span>
            ))}
          </div>
        ) : (
          <span className="text-[11px] text-slate-600">—</span>
        )}
      </td>
      <td className="table-cell text-[11px] text-slate-600">
        {formatDateTime(member.created_at)}
      </td>
      <td className="table-cell">
        <div className="flex gap-1">
          <button className="btn-ghost btn-xs" onClick={() => setEditing(true)}>
            Edit
          </button>
          {!isSelf && (
            <button
              className="btn-danger btn-xs"
              disabled={remove.pending}
              onClick={async () => {
                const ok = await remove.run(() =>
                  api.del(`${base}/members/${member.membership_id}`, { admin: true }),
                )
                if (ok !== undefined) await onChanged()
              }}
            >
              Remove
            </button>
          )}
        </div>
        {remove.error && <div className="mt-1 text-[11px] text-rose-400">{remove.error}</div>}
      </td>
    </tr>
  )
}

function InviteModal({
  base,
  roles,
  onClose,
  onCreated,
}: {
  base: string
  roles: RoleInfo[]
  onClose: () => void
  onCreated: () => Promise<void>
}) {
  const create = useAction()
  const [form, setForm] = useState({
    email: '',
    full_name: '',
    password: '',
    role_key: 'ANALYST',
    scope: '',
  })

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const scope: Record<string, string[]> = {}
    for (const part of form.scope.split(',')) {
      const [key, value] = part.split('=').map((s) => s.trim())
      if (key && value) scope[key] = value.split('|').map((v) => v.trim())
    }
    const ok = await create.run(() =>
      api.post(
        `${base}/members`,
        {
          email: form.email,
          full_name: form.full_name,
          password: form.password || undefined,
          role_key: form.role_key,
          row_scope: scope,
        },
        { admin: true },
      ),
    )
    if (ok) await onCreated()
  }

  return (
    <Modal open onClose={onClose} title="Add member" width="max-w-md">
      <form onSubmit={submit} className="space-y-4">
        {create.error && <Alert>{create.error}</Alert>}

        <Field label="Email" required>
          <input
            type="email"
            className="field"
            value={form.email}
            onChange={(e) => set('email', e.target.value)}
            required
          />
        </Field>

        <Field label="Full name" hint="Used when creating a new user account.">
          <input
            className="field"
            value={form.full_name}
            onChange={(e) => set('full_name', e.target.value)}
          />
        </Field>

        <Field
          label="Initial password"
          hint="Only needed if this person does not already have an account."
        >
          <input
            type="password"
            className="field"
            value={form.password}
            onChange={(e) => set('password', e.target.value)}
            autoComplete="new-password"
          />
        </Field>

        <Field label="Role" required>
          <select
            className="field"
            value={form.role_key}
            onChange={(e) => set('role_key', e.target.value)}
          >
            <RoleOptions roles={roles} />
          </select>
        </Field>

        <Field
          label="Row scope"
          hint="Restricts which rows this member may see, e.g. region=South. Leave blank for unrestricted."
        >
          <input
            className="field mono"
            value={form.scope}
            onChange={(e) => set('scope', e.target.value)}
            placeholder="region=South"
          />
        </Field>

        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className="btn-ghost btn-xs" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary btn-xs" disabled={create.pending}>
            {create.pending ? 'Adding…' : 'Add member'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
