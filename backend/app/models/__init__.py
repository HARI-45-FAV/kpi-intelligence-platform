"""Model registry.

Importing this package registers every table on ``Base.metadata`` — Alembic
autogenerate and ``create_all`` both depend on that happening in one place.
"""

from app.models.base import *  # noqa: F401,F403  (enums and mixins)
from app.models.catalog import CatalogVersion
from app.models.detection import (
    AgentRun,
    AgentRunExplanation,
    CompanyBucketConfig,
    ContributionRun,
    DetectionRun,
)
from app.models.document import CompanyDocument, CompanyDocumentVersion
from app.models.kpi import (
    KpiAccessPolicy,
    KpiDefinition,
    KpiDimension,
    KpiDriver,
    KpiLineage,
    KpiMaterialityRule,
    KpiValidationCheck,
    KpiValidationRun,
    KpiVersion,
)
from app.models.observability import AuditLog, ExecutionLog, SystemEvent
from app.models.profiling import (
    ColumnProfile,
    JoinSafety,
    SourceReconciliation,
    TableGrain,
    TableProfile,
    TableRelationship,
)
from app.models.source import (
    DataSource,
    SelectedTable,
    SourceColumn,
    SourceHealth,
    SourceTable,
)
from app.models.tenant import (
    Company,
    CompanyCalendar,
    CompanyUser,
    Permission,
    Role,
    RolePermission,
    User,
)

__all__ = [
    "AuditLog",
    "CatalogVersion",
    "ColumnProfile",
    "Company",
    "CompanyBucketConfig",
    "CompanyCalendar",
    "CompanyDocument",
    "CompanyDocumentVersion",
    "CompanyUser",
    "ContributionRun",
    "DataSource",
    "AgentRun",
    "AgentRunExplanation",
    "DetectionRun",
    "ExecutionLog",
    "JoinSafety",
    "KpiAccessPolicy",
    "KpiDefinition",
    "KpiDimension",
    "KpiDriver",
    "KpiLineage",
    "KpiMaterialityRule",
    "KpiValidationCheck",
    "KpiValidationRun",
    "KpiVersion",
    "Permission",
    "Role",
    "RolePermission",
    "SelectedTable",
    "SourceColumn",
    "SourceHealth",
    "SourceReconciliation",
    "SourceTable",
    "SystemEvent",
    "TableGrain",
    "TableProfile",
    "TableRelationship",
    "User",
]
