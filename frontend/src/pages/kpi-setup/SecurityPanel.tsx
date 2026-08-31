/**
 * Who can access what.
 *
 * The backend model is unchanged: named permissions, checked individually, across
 * every role. What this screen changes is what it asks the reader to hold in their
 * head. Each role reads as name / access level / status / Manage, and the full
 * breakdown — what it reaches, what it does not, and the exact permission list —
 * opens from Manage. Every role stays selectable in the pickers and is enforced
 * exactly as before.
 */

import { useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { Member, RoleInfo } from '../../api/types'
import { useAuth } from '../../auth/AuthContext'
import { formatDateTime } from '../../components/format'
import {
  Alert,
  EmptyState,
  Field,
  HelpList,
  HelpSection,
  Modal,
  Panel,
  PasswordInput,
  SectionHeader,
  SectionHelp,
  Spinner,
  StatusBadge,
} from '../../components/ui'
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
      <SectionHeader
        title="Security"
        help={<SecurityHelp roles={roles.data ?? []} />}
        actions={
          <button className="btn-primary btn-xs" onClick={() => setInviteOpen(true)}>
            + Add member
          </button>
        }
      />

      <RolesOverview roles={roles.data ?? []} loading={roles.loading && !roles.data} />

      <Panel title="Members" bodyClassName="">
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

/**
 * Roles as cards: name, how much access it carries, whether it is in use.
 *
 * The permission model is untouched — 25 named permissions, still checked
 * individually, still derived by the backend into `access_areas`. What moved is
 * the prose: the per-role description and the full permission matrix are in the
 * Help dialog and the Manage dialog, so this surface answers "who can do roughly
 * what" at a glance and the exact answer stays one click away.
 */
function RolesOverview({ roles, loading }: { roles: RoleInfo[]; loading: boolean }) {
  const [manageRole, setManageRole] = useState<RoleInfo | null>(null)

  const core = useMemo(() => roles.filter((role) => role.is_core), [roles])
  const shown = core.length ? core : roles
  const additional = useMemo(() => roles.filter((role) => !role.is_core), [roles])

  if (loading) {
    return (
      <Panel title="Roles">
        <Spinner />
      </Panel>
    )
  }

  return (
    <>
      <Panel
        title="Roles"
        actions={
          additional.length > 0 ? (
            <span className="text-[11px] text-slate-500">
              {roles.length} roles · {additional.length} additional
            </span>
          ) : undefined
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {shown.map((role) => (
            <RoleCard key={role.role_key} role={role} onManage={() => setManageRole(role)} />
          ))}
          {additional.map((role) => (
            <RoleCard key={role.role_key} role={role} onManage={() => setManageRole(role)} />
          ))}
        </div>
      </Panel>

      {manageRole && (
        <RoleDetailModal role={manageRole} onClose={() => setManageRole(null)} />
      )}
    </>
  )
}

/** How much of the platform a role reaches, as one business-readable phrase. */
function accessLevel(role: RoleInfo): { label: string; tone: 'good' | 'warn' | 'muted' } {
  const areas = role.access_areas ?? {}
  const granted = ACCESS_AREAS.filter((area) => areas[area.key]).length
  if (role.is_admin_role || granted === ACCESS_AREAS.length) {
    return { label: 'Full access', tone: 'good' }
  }
  if (granted === 0) return { label: 'View only', tone: 'muted' }
  return { label: 'Partial access', tone: 'warn' }
}

function RoleCard({ role, onManage }: { role: RoleInfo; onManage: () => void }) {
  const level = accessLevel(role)
  return (
    <div className="surface-card surface-card-lift flex flex-col p-4">
      <div className="flex items-start justify-between gap-2">
        <span className="truncate text-[15px] font-semibold text-slate-100">{role.name}</span>
        {role.is_admin_role && <span className="chip">Admin</span>}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <StatusBadge
          status={level.tone === 'good' ? 'GOOD' : level.tone === 'warn' ? 'WARNING' : 'UNKNOWN'}
          label={level.label}
        />
        <StatusBadge status="ACTIVE" label="Active" />
      </div>
      <div className="mt-3.5 flex items-center justify-between gap-2 border-t border-ink-800 pt-3">
        <span className="text-[11px] text-slate-500">{role.permissions.length} permissions</span>
        <button className="btn-ghost btn-xs" onClick={onManage}>
          Manage
        </button>
      </div>
    </div>
  )
}

/** Everything about one role: what it reaches, what it does not, and the exact
 *  permissions behind that. Read-only, exactly as this screen always was. */
function RoleDetailModal({ role, onClose }: { role: RoleInfo; onClose: () => void }) {
  const areas = role.access_areas ?? {}
  const allowed = ACCESS_AREAS.filter((area) => areas[area.key])
  const denied = ACCESS_AREAS.filter((area) => !areas[area.key])

  return (
    <Modal open onClose={onClose} title={role.name} width="max-w-2xl">
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status="ACTIVE" label={accessLevel(role).label} />
          {role.is_admin_role && <span className="chip">Admin role</span>}
          {!role.is_core && <span className="chip text-slate-500">Additional</span>}
        </div>

        {role.access_summary && (
          <p className="text-[13px] leading-relaxed text-slate-300">{role.access_summary}</p>
        )}

        <HelpSection heading="Can access">
          {allowed.length ? (
            <ul className="space-y-1.5">
              {allowed.map((area) => (
                <li key={area.key} className="flex gap-2 text-[13px]">
                  <span className="text-emerald-700">✓</span>
                  <span>
                    <span className="font-medium text-slate-100">{area.label}</span>
                    <span className="text-slate-500"> — {area.note}</span>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[13px] text-slate-500">
              No configuration areas. This role reads reported KPI results only.
            </p>
          )}
        </HelpSection>

        <HelpSection heading="Cannot access">
          {denied.length ? (
            <ul className="space-y-1.5">
              {denied.map((area) => (
                <li key={area.key} className="flex gap-2 text-[13px]">
                  <span className="text-slate-500">✕</span>
                  <span>
                    <span className="font-medium text-slate-300">{area.label}</span>
                    <span className="text-slate-500"> — {area.note}</span>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[13px] text-slate-500">Nothing is withheld from this role.</p>
          )}
        </HelpSection>

        <HelpSection heading="KPI and data visibility">
          <p>
            This role sees every KPI approved for the company. Data visibility can be narrowed
            further per member: a member may be restricted to certain rows — one region, say — and
            denied specific columns. Those limits are set on the member, not the role, under
            Members.
          </p>
          <p>
            {areas.sensitive_data
              ? 'This role may read columns classified as personal or confidential.'
              : 'Columns classified as personal or confidential are never read for this role — they are skipped during profiling rather than read and hidden afterwards.'}
          </p>
        </HelpSection>

        <HelpSection heading="Investigation and actions">
          <p>
            {areas.kpi_definitions
              ? 'This role can create, edit and approve what a KPI means, and can investigate why a figure moved.'
              : 'This role can review KPI results and investigate why a figure moved, but cannot change what a KPI means.'}
          </p>
        </HelpSection>

        <details className="rounded-xl border border-white/85 bg-white/60 px-3 py-2">
          <summary className="cursor-pointer text-[12px] font-medium text-slate-400">
            Exact permissions ({role.permissions.length})
          </summary>
          <div className="mt-2 flex flex-wrap gap-1">
            {role.permissions.map((permission) => (
              <span
                key={permission}
                className={`chip mono ${
                  permission.startsWith('data.read') ? 'border-amber-300 text-amber-700' : ''
                }`}
              >
                {permission}
              </span>
            ))}
          </div>
        </details>
      </div>
    </Modal>
  )
}

function SecurityHelp({ roles }: { roles: RoleInfo[] }) {
  return (
    <SectionHelp title="About access and permissions">
      <HelpSection heading="What this section is">
        <p>
          Who belongs to this workspace, what each role may reach, and how an individual member's
          view can be narrowed further.
        </p>
      </HelpSection>
      <HelpSection heading="What you see on a role card">
        <HelpList
          items={[
            ['Role name', 'The named role members are assigned to.'],
            [
              'Access level',
              'Full access reaches every configuration area; partial reaches some; view only reads reported results.',
            ],
            ['Status', 'Whether the role is available for assignment.'],
            ['Manage', 'The complete breakdown: what it can and cannot reach, and every permission.'],
          ]}
        />
      </HelpSection>
      <HelpSection heading="The four access areas">
        <HelpList items={ACCESS_AREAS.map((area) => [area.label, area.note] as [string, string])} />
      </HelpSection>
      <HelpSection heading="Per-member limits">
        <HelpList
          items={[
            [
              'Row scope',
              'Restricts a member to particular rows — for example a regional manager who sees only their own region.',
            ],
            [
              'Denied columns',
              'Hides named columns from one member, on top of whatever their role allows.',
            ],
          ]}
        />
      </HelpSection>
      <HelpSection heading="Why it matters">
        <p>
          Permissions are checked one by one and never inferred from a role's name, so adding a role
          cannot quietly widen access. Each company is also isolated from every other: the company
          named in a request is treated as a claim and re-checked against membership on every
          request. There {roles.length === 1 ? 'is' : 'are'} {roles.length} role
          {roles.length === 1 ? '' : 's'} configured for this workspace.
        </p>
      </HelpSection>
    </SectionHelp>
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
          hint="Only needed if this person does not already have an account. At least 6 characters, using two of: lowercase, uppercase, digits, symbols."
        >
          <PasswordInput
            value={form.password}
            onChange={(e) => set('password', e.target.value)}
            autoComplete="new-password"
            minLength={6}
            toggleLabel="initial password"
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
