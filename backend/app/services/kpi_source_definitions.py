"""Company-provided KPI definitions read from the connected source.

The company is the authority on what its KPIs mean. Most sources that have been
governed at all already carry that authority in a table — a KPI contract /
semantic-contract / metric-registry table listing each metric's name, formula and
grain. This module finds that table and reads it, so the platform *starts* from
the company's own definitions instead of proposing its own.

Nothing here is hardcoded to a particular schema and nothing here calls a model:

* **Locating the table** is a deterministic column-role scan. A table qualifies as
  a KPI-definition table when it has both a metric-name column and a
  formula/expression column; the candidate matching the most known roles wins.
* **Reading the rows** goes through the connector's bounded row read, so PostgREST
  and SQL sources behave identically.
* **Resolving a formula** re-uses the governed parser in ``kpi_formula``. A
  company formula that the grammar accepts *and* whose columns exist in the
  discovered catalog is marked RESOLVED and can be imported as a contract
  directly. Anything else is still listed — it is the company's definition — but
  flagged NEEDS_MAPPING with the precise reason, for an administrator to bind by
  hand. The platform never rewrites a business definition to make it parse.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.core.errors import ValidationFailure
from app.models.base import SemanticType, TimeGrain
from app.models.source import DataSource, SelectedTable, SourceTable
from app.services.kpi_formula import parse_formula

# How many definition rows are read. A KPI registry is a small governance table;
# a source with more rows than this is not a KPI registry.
MAX_DEFINITION_ROWS = 500

# Column roles, in resolution priority order. Each tuple is matched by exact name
# first across all candidates, then by substring, so `kpi_name` claims NAME
# rather than being swallowed by a broader pattern.
_ROLE_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("key", ("kpi_id", "kpi_key", "metric_id", "metric_key", "id", "code", "slug")),
    ("name", ("kpi_name", "display_name", "metric_name", "name", "label", "title")),
    ("formula", ("formula", "expression", "calculation", "calc", "formula_sql")),
    (
        "description",
        ("description", "business_definition", "definition", "meaning", "purpose", "notes"),
    ),
    ("grain", ("time_grain", "grain", "granularity", "period", "frequency")),
    ("source", ("source_tables", "source_table", "source", "dataset", "base_table", "table")),
    ("dimensions", ("dimensions", "breakdowns", "dims", "slice_by")),
    ("owner", ("owner", "steward", "owned_by", "business_owner")),
    ("active", ("is_active", "active", "enabled", "status")),
    ("unit", ("unit", "uom", "currency", "measure_unit")),
    ("direction", ("direction", "polarity", "goal", "higher_is_better")),
    ("threshold", ("material_change_threshold_pct", "threshold", "tolerance", "materiality")),
)

_REQUIRED_ROLES = ("name", "formula")

_GRAIN_WORDS = {
    "hour": TimeGrain.HOUR,
    "hourly": TimeGrain.HOUR,
    "day": TimeGrain.DAY,
    "daily": TimeGrain.DAY,
    "date": TimeGrain.DAY,
    "week": TimeGrain.WEEK,
    "weekly": TimeGrain.WEEK,
    "month": TimeGrain.MONTH,
    "monthly": TimeGrain.MONTH,
    "quarter": TimeGrain.QUARTER,
    "quarterly": TimeGrain.QUARTER,
    "year": TimeGrain.YEAR,
    "yearly": TimeGrain.YEAR,
    "annual": TimeGrain.YEAR,
}

_FALSE_WORDS = {"false", "0", "no", "n", "inactive", "disabled", "off", "archived", "deprecated"}

_SPLIT_RE = re.compile(r"[,;|+/\s]+")


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DefinitionTable:
    """A discovered table that carries company KPI definitions."""

    source_table_id: str
    data_source_id: str
    data_source_name: str | None
    schema_name: str
    table_name: str
    role_columns: dict[str, str]
    matched_roles: int
    row_count: int | None

    def as_dict(self) -> dict:
        return {
            "source_table_id": self.source_table_id,
            "data_source_id": self.data_source_id,
            "data_source_name": self.data_source_name,
            "schema": self.schema_name,
            "table": self.table_name,
            "role_columns": self.role_columns,
            "matched_roles": self.matched_roles,
            "row_count": self.row_count,
            "detection_method": (
                "deterministic column-role scan: a metric-name column and a "
                "formula column are required; the table matching the most known "
                "definition roles is treated as authoritative"
            ),
        }


@dataclass(slots=True)
class CompanyKpiDefinition:
    """One row of the company's own KPI registry, resolved against the catalog."""

    kpi_key: str
    name: str
    business_definition: str
    source_formula: str
    resolution_status: str  # RESOLVED | NEEDS_MAPPING
    formula_expression: str | None = None
    source_table_id: str | None = None
    source_table: str | None = None
    data_source_id: str | None = None
    time_field: str | None = None
    time_grain: str = TimeGrain.DAY
    kind: str | None = None
    unit: str | None = None
    direction: str = "HIGHER_IS_BETTER"
    owner: str | None = None
    is_active: bool = True
    declared_grain: str | None = None
    declared_source: str | None = None
    dimensions: list[dict] = field(default_factory=list)
    materiality_threshold_pct: float | None = None
    issues: list[str] = field(default_factory=list)
    already_registered: bool = False
    registered_kpi_id: str | None = None
    raw_row: dict = field(default_factory=dict)

    @property
    def importable(self) -> bool:
        return (
            self.resolution_status == "RESOLVED"
            and not self.already_registered
            and self.source_table_id is not None
        )

    def as_dict(self) -> dict:
        return {
            "kpi_key": self.kpi_key,
            "name": self.name,
            "business_definition": self.business_definition,
            "source_formula": self.source_formula,
            "resolution_status": self.resolution_status,
            "formula_expression": self.formula_expression,
            "source_table_id": self.source_table_id,
            "source_table": self.source_table,
            "data_source_id": self.data_source_id,
            "time_field": self.time_field,
            "time_grain": self.time_grain,
            "kind": self.kind,
            "unit": self.unit,
            "direction": self.direction,
            "owner": self.owner,
            "is_active": self.is_active,
            "declared_grain": self.declared_grain,
            "declared_source": self.declared_source,
            "dimensions": self.dimensions,
            "materiality_threshold_pct": self.materiality_threshold_pct,
            "issues": self.issues,
            "already_registered": self.already_registered,
            "registered_kpi_id": self.registered_kpi_id,
            "importable": self.importable,
        }


# ---------------------------------------------------------------------------
# Locating the definition table
# ---------------------------------------------------------------------------
def _assign_roles(column_names: list[str]) -> dict[str, str]:
    """Map role -> column name. Exact matches win over substring matches."""
    remaining = list(column_names)
    roles: dict[str, str] = {}

    for pass_exact in (True, False):
        for role, tokens in _ROLE_TOKENS:
            if role in roles:
                continue
            for token in tokens:
                hit = next(
                    (
                        column
                        for column in remaining
                        if (column.lower() == token if pass_exact else token in column.lower())
                    ),
                    None,
                )
                if hit is not None:
                    roles[role] = hit
                    remaining.remove(hit)
                    break
    return roles


def find_definition_tables(session: Session, company_id: str) -> list[DefinitionTable]:
    """Every discovered table that looks like a company KPI registry, best first.

    Discovery, not analytical scope, is the right input here: a KPI registry is
    governance metadata about the business, not a table anyone wants profiled.
    """
    tables = list(
        session.scalars(
            select(SourceTable)
            .where(SourceTable.company_id == company_id)
            .order_by(SourceTable.table_name)
        )
    )
    if not tables:
        return []

    source_names = {
        source.id: source.name
        for source in session.scalars(
            select(DataSource).where(DataSource.company_id == company_id)
        )
    }

    found: list[DefinitionTable] = []
    for table in tables:
        columns = [column.column_name for column in table.columns]
        if not columns:
            continue
        roles = _assign_roles(columns)
        if any(role not in roles for role in _REQUIRED_ROLES):
            continue
        found.append(
            DefinitionTable(
                source_table_id=table.id,
                data_source_id=table.data_source_id,
                data_source_name=source_names.get(table.data_source_id),
                schema_name=table.schema_name,
                table_name=table.table_name,
                role_columns=roles,
                matched_roles=len(roles),
                row_count=table.approx_row_count,
            )
        )

    # Most complete registry first; stable for ties.
    found.sort(key=lambda t: (-t.matched_roles, t.table_name))
    return found


# ---------------------------------------------------------------------------
# Reading and resolving
# ---------------------------------------------------------------------------
def read_company_definitions(
    session: Session,
    company_id: str,
    definition_table: DefinitionTable,
    connector: DataSourceConnector,
) -> list[CompanyKpiDefinition]:
    """Read the company's KPI registry rows and resolve each against the catalog."""
    rows = connector.fetch_rows(
        definition_table.schema_name,
        definition_table.table_name,
        limit=MAX_DEFINITION_ROWS,
    )
    catalog = _catalog_index(session, company_id)
    roles = definition_table.role_columns

    definitions: list[CompanyKpiDefinition] = []
    seen_keys: set[str] = set()
    for row in rows:
        definition = _definition_from_row(row, roles, catalog)
        if definition is None:
            continue
        # A duplicate key in the source registry is the source's problem, not
        # something to silently merge: keep the first and note it on the second.
        if definition.kpi_key in seen_keys:
            definition.kpi_key = f"{definition.kpi_key}_2"[:80]
            definition.issues.append(
                "Another row in the source registry already used this KPI key."
            )
        seen_keys.add(definition.kpi_key)
        definitions.append(definition)

    # Active definitions first, then unresolved ones needing attention, then name.
    definitions.sort(
        key=lambda d: (not d.is_active, d.resolution_status == "RESOLVED", d.name.lower())
    )
    return definitions


def _definition_from_row(
    row: dict[str, Any], roles: dict[str, str], catalog: _Catalog
) -> CompanyKpiDefinition | None:
    name = _text(row.get(roles["name"]))
    source_formula = _text(row.get(roles["formula"]))
    if not name or not source_formula:
        # A registry row without a name or a formula is not a definition.
        return None

    key_value = _text(row.get(roles.get("key", ""))) if "key" in roles else None
    description = _text(row.get(roles.get("description", ""))) if "description" in roles else None
    declared_grain = _text(row.get(roles.get("grain", ""))) if "grain" in roles else None
    declared_source = _text(row.get(roles.get("source", ""))) if "source" in roles else None

    definition = CompanyKpiDefinition(
        kpi_key=_slug(_stable_key(key_value, name)),
        name=name,
        business_definition=description or f"Company-defined KPI: {name}.",
        source_formula=source_formula,
        resolution_status="NEEDS_MAPPING",
        owner=_text(row.get(roles.get("owner", ""))) if "owner" in roles else None,
        is_active=_truthy(row.get(roles.get("active", ""))) if "active" in roles else True,
        declared_grain=declared_grain,
        declared_source=declared_source,
        unit=_text(row.get(roles.get("unit", ""))) if "unit" in roles else None,
        materiality_threshold_pct=(
            _number(row.get(roles.get("threshold", ""))) if "threshold" in roles else None
        ),
        raw_row={key: _jsonable(value) for key, value in row.items()},
    )
    if "direction" in roles:
        direction = _text(row.get(roles["direction"]))
        if direction and _looks_lower_is_better(direction):
            definition.direction = "LOWER_IS_BETTER"
    if declared_grain:
        definition.time_grain = _grain_of(declared_grain) or TimeGrain.DAY

    _resolve_formula(definition, catalog)
    _resolve_dimensions(definition, row, roles, catalog)
    return definition


def _resolve_formula(definition: CompanyKpiDefinition, catalog: _Catalog) -> None:
    """Bind a company formula to real catalog columns, or say why it cannot bind.

    The formula text is the company's; only the *binding* is inferred. Candidate
    tables come from the registry's own source column first, then from the tables
    the formula names, then from analytical scope — so an explicit declaration
    always beats a guess.
    """
    raw = definition.source_formula
    candidates = _candidate_tables(definition, catalog)
    if not candidates:
        definition.issues.append(
            "No discovered table matches this definition. Register and discover "
            "the source that holds it, then re-read the registry."
        )
        return

    last_error: str | None = None
    for table_name in candidates:
        table = catalog.tables.get(table_name)
        if table is None:
            continue
        try:
            spec = parse_formula(raw, default_table=table_name)
        except ValidationFailure as exc:
            last_error = str(exc)
            continue

        missing: list[str] = []
        for _role, ref_table, column in spec.referenced_columns:
            owner = ref_table or table_name
            if not catalog.has_column(owner, column):
                missing.append(f"{owner}.{column}")
        if missing:
            last_error = f"Column(s) not found in the discovered catalog: {', '.join(missing)}."
            continue

        # A qualified formula may legitimately point somewhere other than the
        # candidate; the numerator's table is the KPI's primary source.
        primary_name = spec.numerator.table or table_name
        primary = catalog.tables.get(primary_name, table)
        definition.resolution_status = "RESOLVED"
        definition.formula_expression = spec.render()
        definition.kind = spec.kind
        definition.source_table_id = primary.id
        definition.source_table = primary.table_name
        definition.data_source_id = primary.data_source_id
        definition.time_field = catalog.time_column(primary.table_name)
        if definition.time_field is None:
            definition.issues.append(
                "No date or timestamp column was discovered in "
                f"{primary.table_name}, so this KPI cannot be tracked over time yet."
            )
        if not primary.selected:
            definition.issues.append(
                f"{primary.table_name} is not in the approved analytical scope. "
                "Add it to the scope before validation can execute this KPI."
            )
        if spec.kind == "RATIO":
            definition.issues.append(
                "Ratio KPI: not additive. It must be recomputed at each level of "
                "aggregation rather than summed from parts."
            )
        return

    definition.issues.append(
        last_error
        or "The formula does not fit the governed grammar "
        "(AGG([DISTINCT] [table.]column), optionally divided by one more)."
    )


def _candidate_tables(definition: CompanyKpiDefinition, catalog: _Catalog) -> list[str]:
    ordered: list[str] = []

    def add(name: str | None) -> None:
        if not name:
            return
        actual = catalog.resolve_name(name)
        if actual and actual not in ordered:
            ordered.append(actual)

    for token in _SPLIT_RE.split(definition.declared_source or ""):
        add(token.strip().strip("\"'[]{}()").split(".")[-1] or None)
    for token in re.findall(r"([A-Za-z_][A-Za-z0-9_$]*)\s*\.", definition.source_formula):
        add(token)
    # Fall back to any scoped table that actually holds the referenced columns.
    for column in re.findall(r"([A-Za-z_][A-Za-z0-9_$]*)", definition.source_formula):
        for name in catalog.tables_with_column(column):
            add(name)
    return ordered


def _resolve_dimensions(
    definition: CompanyKpiDefinition,
    row: dict[str, Any],
    roles: dict[str, str],
    catalog: _Catalog,
) -> None:
    if "dimensions" not in roles or definition.source_table_id is None:
        return
    declared = _string_list(row.get(roles["dimensions"]))
    table_name = definition.source_table or ""
    for name in declared:
        column = catalog.column_named(table_name, name)
        if column is None:
            definition.issues.append(
                f"Declared dimension '{name}' has no matching column in {table_name}."
            )
            continue
        definition.dimensions.append(
            {
                "dimension_name": column.lower(),
                "source_table_id": definition.source_table_id,
                "source_column": column,
                "notes": "Declared by the company KPI registry.",
            }
        )


# ---------------------------------------------------------------------------
# Catalog index
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _CatalogTable:
    id: str
    table_name: str
    data_source_id: str
    selected: bool
    columns: dict[str, str]  # lowercase -> actual
    time_columns: list[str]


@dataclass(slots=True)
class _Catalog:
    tables: dict[str, _CatalogTable]  # keyed by actual table name
    _lower: dict[str, str] = field(default_factory=dict)

    def resolve_name(self, name: str) -> str | None:
        if name in self.tables:
            return name
        return self._lower.get(name.lower())

    def has_column(self, table_name: str, column: str) -> bool:
        if column == "*":
            return True
        actual = self.resolve_name(table_name)
        table = self.tables.get(actual or "")
        return bool(table and column.lower() in table.columns)

    def column_named(self, table_name: str, column: str) -> str | None:
        actual = self.resolve_name(table_name)
        table = self.tables.get(actual or "")
        return table.columns.get(column.lower()) if table else None

    def tables_with_column(self, column: str) -> list[str]:
        lowered = column.lower()
        return [
            name
            for name, table in self.tables.items()
            if lowered in table.columns and table.selected
        ]

    def time_column(self, table_name: str) -> str | None:
        actual = self.resolve_name(table_name)
        table = self.tables.get(actual or "")
        return table.time_columns[0] if table and table.time_columns else None


def _catalog_index(session: Session, company_id: str) -> _Catalog:
    tables = list(
        session.scalars(
            select(SourceTable).where(SourceTable.company_id == company_id)
        )
    )
    selected_ids = set(
        session.scalars(
            select(SelectedTable.source_table_id).where(
                SelectedTable.company_id == company_id, SelectedTable.enabled.is_(True)
            )
        )
    )

    index: dict[str, _CatalogTable] = {}
    for table in tables:
        columns = {column.column_name.lower(): column.column_name for column in table.columns}
        time_columns = [
            column.column_name
            for column in sorted(table.columns, key=lambda c: c.ordinal_position)
            if column.semantic_type in {SemanticType.DATE, SemanticType.TIMESTAMP}
        ]
        index[table.table_name] = _CatalogTable(
            id=table.id,
            table_name=table.table_name,
            data_source_id=table.data_source_id,
            selected=table.id in selected_ids,
            columns=columns,
            time_columns=time_columns,
        )
    catalog = _Catalog(tables=index)
    catalog._lower = {name.lower(): name for name in index}
    return catalog


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSE_WORDS


def _looks_lower_is_better(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in ("lower", "down", "decrease", "minimi"))


def _grain_of(value: str) -> str | None:
    lowered = value.lower()
    for word, grain in _GRAIN_WORDS.items():
        if word in lowered:
            return grain
    return None


def _string_list(value: Any) -> list[str]:
    """Accept a JSON array, a JSON string holding one, or a delimited string."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        if isinstance(parsed, dict):
            return [str(key).strip() for key in parsed]
    return [part for part in (p.strip() for p in _SPLIT_RE.split(text)) if part]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _stable_key(key_value: str | None, name: str) -> str:
    """Prefer the registry's own key, unless it is a meaningless surrogate.

    Plenty of registries key on an auto-increment integer. Adopting that as the
    governed KPI key would make every contract read ``kpi_key: "4"``, which tells
    a reader nothing and silently reshuffles if rows are ever renumbered. A
    non-numeric key from the source is meaningful and is kept as-is.
    """
    if key_value and not key_value.replace(".", "", 1).isdigit():
        return key_value
    return name


def _slug(label: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in label.lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:80] or "kpi"
