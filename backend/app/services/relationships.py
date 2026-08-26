"""Relationship detection across the company's selected tables.

Two discovery paths, and the catalog records which one produced each edge:

* **Declared** — a real foreign key in the source. Confidence 1.0.
* **Inferred** — matching column names plus a *containment check* run in the
  database: how many child values have no match in the parent. A name match with
  a high orphan rate is a coincidence, not a relationship, and is rejected.

Shared low-cardinality columns (``region``, ``channel``) are recorded too, but
labelled as shared dimensions rather than keys. They matter for cross-source
reconciliation, and they are exactly where fan-out joins come from.

Cost is controlled by only ever testing name-compatible identifier and
categorical pairs — never the full cross product of columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.models.base import RelationshipType, SemanticType
from app.models.profiling import ColumnProfile, TableRelationship
from app.models.source import SourceColumn, SourceTable

# A name match with more orphans than this is treated as coincidence.
MAX_ORPHAN_PCT = 5.0
# Below this cardinality a matching column is a shared dimension, not a key.
SHARED_DIMENSION_MAX_DISTINCT = 50

_ID_SUFFIXES = ("_id", "_key", "_code", "_ref", "_sk")


@dataclass(slots=True)
class RelationshipCandidate:
    child_table: SourceTable
    child_column: SourceColumn
    parent_table: SourceTable
    parent_column: SourceColumn
    method: str
    name_score: float


@dataclass(slots=True)
class RelationshipOutcome:
    created: int = 0
    updated: int = 0
    rejected: int = 0
    relationships: list[TableRelationship] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "relationships_found": len(self.relationships),
            "created": self.created,
            "updated": self.updated,
            "rejected": self.rejected,
            "rejections": self.rejections,
        }


def detect_relationships(
    session: Session,
    tables: list[SourceTable],
    connectors: dict[str, DataSourceConnector],
) -> RelationshipOutcome:
    """Detect relationships among ``tables`` (all already in analytical scope)."""
    outcome = RelationshipOutcome()
    profiles = _profiles_for(session, tables)

    declared = _declared_candidates(tables)
    inferred = _inferred_candidates(tables, profiles, exclude=set(declared))

    for candidate in list(declared.values()) + inferred:
        # Cross-source relationships cannot be verified with a single SQL join,
        # so containment is skipped and confidence reflects that.
        same_source = candidate.child_table.data_source_id == candidate.parent_table.data_source_id
        connector = connectors.get(candidate.child_table.data_source_id)

        orphan_count: int | None = None
        orphan_pct: float | None = None
        if same_source and connector is not None:
            orphan_count = connector.count_orphans(
                candidate.child_table.schema_name,
                candidate.child_table.table_name,
                candidate.child_column.column_name,
                candidate.parent_table.schema_name,
                candidate.parent_table.table_name,
                candidate.parent_column.column_name,
            )
            child_profile = profiles.get(candidate.child_column.id)
            non_null = _non_null_rows(child_profile)
            if orphan_count is not None and non_null:
                orphan_pct = round(orphan_count / non_null * 100, 4)

        is_declared = candidate.method == "declared_fk"
        if not is_declared and orphan_pct is not None and orphan_pct > MAX_ORPHAN_PCT:
            outcome.rejected += 1
            outcome.rejections.append(
                {
                    "from": f"{candidate.child_table.table_name}.{candidate.child_column.column_name}",
                    "to": f"{candidate.parent_table.table_name}.{candidate.parent_column.column_name}",
                    "reason": f"{orphan_pct:.2f}% of child values have no parent match",
                }
            )
            continue

        relationship, created = _upsert(session, candidate)
        child_profile = profiles.get(candidate.child_column.id)
        parent_profile = profiles.get(candidate.parent_column.id)
        containment_verified = same_source and orphan_count is not None

        relationship.relationship_type = _cardinality(child_profile, parent_profile)
        relationship.method = candidate.method
        relationship.is_declared = is_declared
        relationship.source_distinct_count = child_profile.distinct_count if child_profile else None
        relationship.target_distinct_count = (
            parent_profile.distinct_count if parent_profile else None
        )
        relationship.orphan_count = orphan_count
        relationship.orphan_pct = orphan_pct
        relationship.confidence = _confidence(
            is_declared=is_declared,
            name_score=candidate.name_score,
            orphan_pct=orphan_pct,
            verified=containment_verified,
        )
        relationship.evidence = {
            "name_score": candidate.name_score,
            "containment_verified": containment_verified,
            "cross_source": not same_source,
            "child_distinct": relationship.source_distinct_count,
            "parent_distinct": relationship.target_distinct_count,
            "child_unique": child_profile.is_unique if child_profile else None,
            "parent_unique": parent_profile.is_unique if parent_profile else None,
            "note": (
                None
                if containment_verified
                else "Cross-source pair: containment cannot be verified with one query."
            ),
        }

        outcome.relationships.append(relationship)
        if created:
            outcome.created += 1
        else:
            outcome.updated += 1

    return outcome


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------
def _declared_candidates(
    tables: list[SourceTable],
) -> dict[tuple[str, str, str, str], RelationshipCandidate]:
    """Foreign keys the source itself declares."""
    by_name: dict[tuple[str, str], SourceTable] = {
        (table.data_source_id, table.table_name): table for table in tables
    }
    candidates: dict[tuple[str, str, str, str], RelationshipCandidate] = {}

    for table in tables:
        for column in table.columns:
            if not column.is_foreign_key or not column.references_table:
                continue
            parent = by_name.get((table.data_source_id, column.references_table))
            if parent is None:
                # Referenced table exists in the source but is outside the
                # approved analytical scope.
                continue
            parent_column = next(
                (c for c in parent.columns if c.column_name == column.references_column), None
            )
            if parent_column is None:
                continue
            key = (table.id, column.column_name, parent.id, parent_column.column_name)
            candidates[key] = RelationshipCandidate(
                child_table=table,
                child_column=column,
                parent_table=parent,
                parent_column=parent_column,
                method="declared_fk",
                name_score=1.0,
            )
    return candidates


def _inferred_candidates(
    tables: list[SourceTable],
    profiles: dict[str, ColumnProfile],
    *,
    exclude: set[tuple[str, str, str, str]],
) -> list[RelationshipCandidate]:
    """Name-compatible pairs worth a containment check."""
    joinable: list[tuple[SourceTable, SourceColumn]] = [
        (table, column)
        for table in tables
        for column in table.columns
        if column.semantic_type in {SemanticType.IDENTIFIER, SemanticType.CATEGORICAL}
        and not (profiles.get(column.id) and profiles[column.id].access_withheld)
    ]

    candidates: list[RelationshipCandidate] = []
    seen: set[tuple[str, str, str, str]] = set(exclude)

    for child_table, child_column in joinable:
        for parent_table, parent_column in joinable:
            if child_table.id == parent_table.id:
                continue
            method, score = _name_match(
                child_table, child_column, parent_table, parent_column
            )
            if method is None:
                continue

            child_profile = profiles.get(child_column.id)
            parent_profile = profiles.get(parent_column.id)
            # Orient child -> parent: the parent side should be the more unique
            # one. Without that, every pair would be emitted twice.
            if not _is_plausible_parent(child_profile, parent_profile):
                continue

            distinct = parent_profile.distinct_count if parent_profile else None
            if (
                distinct is not None
                and distinct <= SHARED_DIMENSION_MAX_DISTINCT
                and child_column.semantic_type == SemanticType.CATEGORICAL
            ):
                method = "shared_dimension"

            key = (child_table.id, child_column.column_name, parent_table.id, parent_column.column_name)
            reverse = (parent_table.id, parent_column.column_name, child_table.id, child_column.column_name)
            if key in seen or reverse in seen:
                continue
            seen.add(key)
            candidates.append(
                RelationshipCandidate(
                    child_table=child_table,
                    child_column=child_column,
                    parent_table=parent_table,
                    parent_column=parent_column,
                    method=method,
                    name_score=score,
                )
            )
    return candidates


def _name_match(
    child_table: SourceTable,
    child_column: SourceColumn,
    parent_table: SourceTable,
    parent_column: SourceColumn,
) -> tuple[str | None, float]:
    child_name = child_column.column_name.lower()
    parent_name = parent_column.column_name.lower()

    if child_name == parent_name:
        return ("name_and_containment", 1.0)

    # sales.product_ref -> products.id  /  order_items.product_id -> product.id
    stem = _strip_id_suffix(child_name)
    parent_stem = _singular(parent_table.table_name.lower())
    if stem and parent_column.is_primary_key and stem in {parent_stem, _singular(stem)}:
        return ("name_and_containment", 0.85)

    return (None, 0.0)


def _strip_id_suffix(name: str) -> str | None:
    for suffix in _ID_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return None


def _singular(name: str) -> str:
    lowered = name.lower()
    for suffix in ("_master", "_dim", "_lookup", "_ref"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
    if lowered.endswith("ies"):
        return lowered[:-3] + "y"
    if lowered.endswith("ses"):
        return lowered[:-2]
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def _is_plausible_parent(
    child_profile: ColumnProfile | None, parent_profile: ColumnProfile | None
) -> bool:
    """Keep the orientation where the parent is at least as unique as the child."""
    if parent_profile is None:
        return False
    if parent_profile.is_unique:
        return True
    if child_profile is None:
        return False
    # Neither side is unique: keep one deterministic direction only.
    child_distinct = child_profile.distinct_count or 0
    parent_distinct = parent_profile.distinct_count or 0
    if parent_distinct != child_distinct:
        return parent_distinct > child_distinct
    return (child_profile.row_count or 0) >= (parent_profile.row_count or 0)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _cardinality(
    child_profile: ColumnProfile | None, parent_profile: ColumnProfile | None
) -> str:
    child_unique = child_profile.is_unique if child_profile else None
    parent_unique = parent_profile.is_unique if parent_profile else None
    if child_unique is None or parent_unique is None:
        return RelationshipType.UNKNOWN
    if child_unique and parent_unique:
        return RelationshipType.ONE_TO_ONE
    if parent_unique:
        return RelationshipType.MANY_TO_ONE
    if child_unique:
        return RelationshipType.ONE_TO_MANY
    return RelationshipType.MANY_TO_MANY


def _confidence(
    *, is_declared: bool, name_score: float, orphan_pct: float | None, verified: bool
) -> float:
    if is_declared:
        return 1.0
    if not verified:
        # Name evidence only. Deliberately capped low so the catalog does not
        # present an unverified guess as a fact.
        return round(min(0.5, name_score * 0.5), 4)
    penalty = (orphan_pct or 0.0) / MAX_ORPHAN_PCT * 0.25
    return round(max(0.3, min(0.95, name_score * 0.95 - penalty)), 4)


def _non_null_rows(profile: ColumnProfile | None) -> int | None:
    if profile is None or profile.row_count is None:
        return None
    return profile.row_count - (profile.null_count or 0)


def _profiles_for(session: Session, tables: list[SourceTable]) -> dict[str, ColumnProfile]:
    if not tables:
        return {}
    rows = session.scalars(
        select(ColumnProfile).where(
            ColumnProfile.source_table_id.in_([table.id for table in tables])
        )
    )
    return {row.source_column_id: row for row in rows}


def _upsert(
    session: Session, candidate: RelationshipCandidate
) -> tuple[TableRelationship, bool]:
    existing = session.scalar(
        select(TableRelationship).where(
            TableRelationship.source_table_id == candidate.child_table.id,
            TableRelationship.source_column == candidate.child_column.column_name,
            TableRelationship.target_table_id == candidate.parent_table.id,
            TableRelationship.target_column == candidate.parent_column.column_name,
        )
    )
    if existing is not None:
        return (existing, False)
    relationship = TableRelationship(
        company_id=candidate.child_table.company_id,
        source_table_id=candidate.child_table.id,
        source_column=candidate.child_column.column_name,
        target_table_id=candidate.parent_table.id,
        target_column=candidate.parent_column.column_name,
    )
    session.add(relationship)
    session.flush()
    return (relationship, True)
