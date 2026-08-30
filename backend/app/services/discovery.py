"""Table and column discovery.

Discovery answers "what is in this source?" and nothing more. It does not grant
analytical access: a discovered table only enters profiling, the catalog and KPI
registration once an administrator selects it under Data Scope. Keeping those
two ideas separate is what stops a BI platform from quietly analysing every
table it can see.

Re-running discovery is idempotent — existing rows are updated in place so
selections, profiles and KPI bindings survive a refresh.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.core.clock import utcnow
from app.models.source import DataSource, SourceColumn, SourceTable
from app.services.classification import classify_sensitivity, classify_structural
from app.services.source_governance import apply_column_role, apply_table_candidates


@dataclass(slots=True)
class DiscoveryResult:
    tables_found: int
    tables_created: int
    tables_updated: int
    tables_removed: int
    columns_created: int
    columns_updated: int
    schema_name: str
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "tables_found": self.tables_found,
            "tables_created": self.tables_created,
            "tables_updated": self.tables_updated,
            "tables_removed": self.tables_removed,
            "columns_created": self.columns_created,
            "columns_updated": self.columns_updated,
            "schema": self.schema_name,
            "duration_ms": self.duration_ms,
        }


def discover_source(
    session: Session,
    source: DataSource,
    connector: DataSourceConnector,
    *,
    schema: str | None = None,
) -> DiscoveryResult:
    target_schema = schema or source.schema_name or "public"
    discovered = connector.list_tables(target_schema)
    result = DiscoveryResult(
        tables_found=len(discovered),
        tables_created=0,
        tables_updated=0,
        tables_removed=0,
        columns_created=0,
        columns_updated=0,
        schema_name=target_schema,
    )

    existing_tables = {
        (table.schema_name, table.table_name): table
        for table in session.scalars(
            select(SourceTable).where(SourceTable.data_source_id == source.id)
        )
    }
    seen: set[tuple[str, str]] = set()
    now = utcnow()

    for meta in discovered:
        key = (meta.schema_name, meta.table_name)
        seen.add(key)
        table = existing_tables.get(key)
        if table is None:
            table = SourceTable(
                company_id=source.company_id,
                data_source_id=source.id,
                database_name=meta.database_name,
                schema_name=meta.schema_name,
                table_name=meta.table_name,
            )
            session.add(table)
            session.flush()
            result.tables_created += 1
        else:
            result.tables_updated += 1

        table.table_type = meta.table_type
        table.approx_row_count = meta.approx_row_count
        table.column_count = meta.column_count
        table.comment = meta.comment
        table.database_name = meta.database_name
        table.discovered_at = now

        created, updated = _sync_columns(session, source, table, connector)
        result.columns_created += created
        result.columns_updated += updated

    # Tables that vanished from the source are removed from the registry, but a
    # table the admin had selected is kept and flagged rather than deleted:
    # silently dropping it would silently invalidate KPI lineage.
    for key, table in existing_tables.items():
        if key in seen:
            continue
        if table.selection is not None and table.selection.enabled:
            table.comment = "NOT FOUND during last discovery; selection retained for review."
            continue
        session.delete(table)
        result.tables_removed += 1

    source.last_discovered_at = now
    return result


def _sync_columns(
    session: Session,
    source: DataSource,
    table: SourceTable,
    connector: DataSourceConnector,
) -> tuple[int, int]:
    try:
        column_metas = connector.get_column_metadata(table.schema_name, table.table_name)
    except Exception:
        # A table we cannot reflect (permission denied, exotic type) should not
        # abort discovery of the rest of the schema.
        table.comment = "Column metadata could not be read during discovery."
        return (0, 0)

    existing = {column.column_name: column for column in table.columns}
    seen: set[str] = set()
    created = updated = 0
    live: list[SourceColumn] = []

    for meta in column_metas:
        seen.add(meta.column_name)
        column = existing.get(meta.column_name)
        if column is None:
            column = SourceColumn(
                company_id=source.company_id,
                source_table_id=table.id,
                column_name=meta.column_name,
                ordinal_position=meta.ordinal_position,
                data_type=meta.data_type,
            )
            session.add(column)
            created += 1
            fresh = True
        else:
            updated += 1
            fresh = False

        live.append(column)
        column.ordinal_position = meta.ordinal_position
        column.data_type = meta.data_type
        column.is_nullable = meta.is_nullable
        column.default_value = meta.default_value
        column.is_primary_key = meta.is_primary_key
        column.is_foreign_key = meta.is_foreign_key
        column.references_table = meta.references_table
        column.references_column = meta.references_column
        column.comment = meta.comment
        column.semantic_type = classify_structural(meta)
        # A structure-only role proposal, good enough for a review screen before
        # anything has been profiled. Rewritten with cardinality evidence on the
        # next profile; a confirmed role is never touched.
        apply_column_role(column)

        # Sensitivity is only auto-assigned on first sight. Re-discovery must
        # not overwrite an administrator's deliberate reclassification.
        if fresh:
            verdict = classify_sensitivity(meta.column_name, table.table_name)
            column.classification = verdict.classification
            column.is_pii = verdict.is_pii
            column.is_sensitive = verdict.is_sensitive
            column.is_restricted = verdict.is_restricted

    for name, column in existing.items():
        if name not in seen:
            session.delete(column)

    # Candidate identifier / time / company columns, from structure alone. Passed
    # explicitly because columns created above are not yet on the relationship.
    apply_table_candidates(table, columns=live)
    table.column_count = len(column_metas)
    return (created, updated)
