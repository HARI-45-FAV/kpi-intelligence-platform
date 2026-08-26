"""Permission catalogue and default role definitions.

Permissions are checked, not inferred from role names, so adding a role later
cannot accidentally widen access. The mapping below is the single place that
decides what each role can do.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    key: str
    category: str
    description: str


PERMISSIONS: tuple[PermissionSpec, ...] = (
    # Company
    PermissionSpec("company.read", "company", "View company profile and settings"),
    PermissionSpec("company.manage", "company", "Edit company profile, calendar and status"),
    # Users
    PermissionSpec("user.read", "user", "View members and their roles"),
    PermissionSpec("user.manage", "user", "Invite members, assign roles and scopes"),
    # Sources
    PermissionSpec("source.read", "source", "View registered data sources and discovered tables"),
    PermissionSpec("source.manage", "source", "Register sources, test connections, set data scope"),
    PermissionSpec("profiling.run", "source", "Execute profiling, grain and relationship analysis"),
    # Sensitive data. Separated from source.read so profiling can be
    # access-aware instead of profiling everything and redacting afterwards.
    PermissionSpec("data.read_confidential", "data", "Read CONFIDENTIAL-classified columns"),
    PermissionSpec("data.read_restricted", "data", "Read RESTRICTED-classified columns"),
    PermissionSpec("data.read_pii", "data", "Read columns classified as personal data"),
    # Documents
    PermissionSpec("document.read", "document", "View company documents and metadata"),
    PermissionSpec("document.manage", "document", "Upload, version and archive documents"),
    # Catalog
    PermissionSpec("catalog.read", "catalog", "View the semantic catalog"),
    PermissionSpec("catalog.publish", "catalog", "Publish an immutable catalog version"),
    # KPI governance
    PermissionSpec("kpi.read", "kpi", "View KPI definitions and versions"),
    PermissionSpec("kpi.create", "kpi", "Create KPI drafts and discovery proposals"),
    PermissionSpec("kpi.edit", "kpi", "Edit KPI drafts and create new versions"),
    PermissionSpec("kpi.validate", "kpi", "Run KPI validation suites"),
    PermissionSpec("kpi.approve", "kpi", "Approve, activate, reject and deprecate KPIs"),
    # Downstream surfaces (present so later sprints need no permission migration)
    PermissionSpec("analytics.read", "analytics", "View dashboards and KPI values"),
    PermissionSpec("investigation.read", "analytics", "View investigations and evidence"),
    # Observability
    PermissionSpec("audit.read", "observability", "View the audit trail"),
    PermissionSpec("telemetry.read", "observability", "View runtime telemetry"),
)

PERMISSION_KEYS = tuple(p.key for p in PERMISSIONS)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    key: str
    name: str
    description: str
    permissions: tuple[str, ...]
    is_admin_role: bool = False
    rank: int = 100
    # Presentation only. The three core roles are the ones the access model is
    # explained with; the rest stay fully supported and fully enforced, just not
    # front-and-centre on the security screen. Nothing here affects a permission
    # check -- authorisation reads `permissions`, never this flag.
    is_core: bool = False
    # A one-line business answer to "what can this role reach?", used by the
    # security overview instead of a wall of permission keys.
    access_summary: str = ""


_READ_ONLY = (
    "company.read",
    "user.read",
    "source.read",
    "document.read",
    "catalog.read",
    "kpi.read",
    "analytics.read",
)

ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(
        key="ADMIN",
        name="Administrator",
        description="Full governance control over the company workspace.",
        permissions=PERMISSION_KEYS,
        is_admin_role=True,
        rank=10,
        is_core=True,
        access_summary=(
            "Everything in this workspace: KPI definitions and approval, data "
            "sources and scope, sensitive columns, documents and the audit trail."
        ),
    ),
    RoleSpec(
        key="ANALYST",
        name="Analyst",
        description=(
            "Builds and validates KPI definitions and runs profiling. "
            "Cannot approve its own work."
        ),
        permissions=(
            *_READ_ONLY,
            "profiling.run",
            "data.read_confidential",
            "kpi.create",
            "kpi.edit",
            "kpi.validate",
            "investigation.read",
            "telemetry.read",
        ),
        rank=30,
        is_core=True,
        access_summary=(
            "Builds and validates KPI definitions and runs profiling, and may read "
            "confidential columns. Cannot approve a KPI or change who has access."
        ),
    ),
    RoleSpec(
        key="EXECUTIVE",
        name="Executive",
        description="Consumes headline KPI outcomes across the whole company.",
        permissions=(*_READ_ONLY, "investigation.read"),
        rank=20,
        access_summary="Reads KPI outcomes and investigations for the whole company.",
    ),
    RoleSpec(
        key="MANAGER",
        name="Manager",
        description="Consumes KPI outcomes and investigations for their area.",
        permissions=(*_READ_ONLY, "investigation.read"),
        rank=40,
        access_summary="Reads KPI outcomes and investigations for their area.",
    ),
    RoleSpec(
        key="REGIONAL_MANAGER",
        name="Regional Manager",
        description=(
            "Same surfaces as Manager, restricted to the regions on their "
            "membership scope."
        ),
        permissions=(*_READ_ONLY, "investigation.read"),
        rank=50,
        access_summary=(
            "Same surfaces as Manager, limited to the regions on their membership "
            "row scope."
        ),
    ),
    RoleSpec(
        key="VIEWER",
        name="Viewer",
        description="Read-only access to published KPI values.",
        permissions=("company.read", "kpi.read", "catalog.read", "analytics.read"),
        rank=60,
        is_core=True,
        access_summary=(
            "Reads published KPI values and definitions only. No sensitive columns, "
            "no documents, no configuration."
        ),
    ),
)

ROLES_BY_KEY = {role.key: role for role in ROLES}
ADMIN_ROLE_KEY = "ADMIN"

# The roles the security screen leads with. Everything else stays available in the
# role picker and is enforced identically -- this only decides prominence.
CORE_ROLE_KEYS = tuple(role.key for role in ROLES if role.is_core)
