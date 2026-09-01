"""Declared investigation metadata, for KPIs whose dimensions are not yet governed.

The investigation engine never chooses a breakdown column. A dimension is a
governed decision -- an approved :class:`~app.models.kpi.KpiDimension` row on the
KPI version, with its own column, its own hierarchy and its own ``allowed``
switch -- and when a KPI has none, the honest answer is that it has no breakdown.

This module is the one exception, and it is deliberately shaped so that it can be
deleted rather than unwound. It holds *metadata only*, for one demo schema,
written down in one place:

    KPI's own table -> allowed dimensions -> hierarchy -> where each one lives

That is the same four-part shape the KPI contract will carry once the contract,
the company's confirmed table metadata and its reference documents drive it. When
approved dimensions exist, :func:`app.services.contribution.available_dimensions`
finds them, this file is never read, and nothing downstream can tell the
difference -- :class:`MappedDimension` answers to exactly the attributes the
engine reads off a governed row.

Three things this file must never contain, and does not:

* **A measured value.** Every figure on the investigation surface comes from a
  query against the company's own data.
* **An entity name.** The list of values a dimension takes is read from the
  source, per KPI, per date. Nothing here enumerates them.
* **A judgement.** No status, no threshold, no expectation.

It also records the one structural fact a breakdown cannot work without: that
these dimensions do *not* all live on the KPI's own table, and how the finer
table is matched to it. A KPI measured once per record cannot simply be summed
against a table that holds several rows per record -- that multiplies the KPI --
so the relationship carries the weight column that
:mod:`app.services.kpi_breakdown` apportions by. See that module for the
arithmetic and for why it leaves the KPI's own total unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import registry
from app.models.kpi import KpiVersion
from app.models.source import DataSource, SourceTable


@dataclass(frozen=True)
class MappedRelationship:
    """How a finer-grained table is matched to the table a KPI is measured on.

    ``allocation_weight`` is the numeric column on the finer table that decides
    how much of a record's measured value belongs to each of its rows. Without
    one, a KPI measured at the coarser grain cannot be split along the finer
    table at all, and the breakdown is refused rather than approximated.
    """

    table: str
    foreign_key: str
    parent_key: str
    allocation_weight: str | None = None
    grain_note: str | None = None


@dataclass(frozen=True)
class MappedDimension:
    """A declared breakdown, in the shape of an approved one.

    Attribute-for-attribute compatible with the parts of
    :class:`~app.models.kpi.KpiDimension` the investigation engine reads, so the
    engine receives one or the other and branches on neither.
    """

    dimension_name: str
    source_table: str
    source_column: str
    hierarchy: list[str] = field(default_factory=list)
    allowed: bool = True
    is_default_breakdown: bool = False
    approx_cardinality: int | None = None
    notes: str | None = None

    # -- optional display name, for a dimension whose values are identifiers ----
    # A code is a correct answer and a poor label. When the source carries a
    # human name for it, these three say where: one small lookup, cosmetic only,
    # and skipped entirely when the table is not present.
    label_table: str | None = None
    label_key: str | None = None
    label_column: str | None = None


# ---------------------------------------------------------------------------
# The demo schema
# ---------------------------------------------------------------------------
#: The table the demo company's KPIs are measured on. A KPI bound to anything
#: else gets nothing from this module.
_ANCHOR_TABLE = "orders"

#: The line-level table beneath it: several rows per record, so a KPI measured
#: per record is apportioned across it rather than summed against it.
_LINE_RELATIONSHIP = MappedRelationship(
    table="order_items",
    foreign_key="order_id",
    parent_key="order_id",
    allocation_weight="item_value",
    grain_note="one row per item within a record",
)

_ANCHOR_DIMENSIONS: tuple[MappedDimension, ...] = (
    MappedDimension(
        dimension_name="region",
        source_table="orders",
        source_column="region",
        # Two candidate next levels, finest first. ``sector`` is a level of detail
        # *within* a region and is the better descent when the source can be read
        # with a join; ``channel`` is the fallback for a source that cannot, so a
        # guided drill-down still has somewhere to go instead of stopping at the
        # first level. Whichever of the two is unavailable is dropped by
        # ``contribution.next_dimensions`` rather than offered.
        hierarchy=["sector", "channel"],
        is_default_breakdown=True,
        approx_cardinality=4,
        notes="Where the activity was recorded.",
    ),
    MappedDimension(
        dimension_name="channel",
        source_table="orders",
        source_column="channel",
        # Declares nothing below it: a channel is how a record was placed, not a
        # level of the region hierarchy, so the guided descent never arrives here
        # and a person reaching it has chosen it by name.
        hierarchy=[],
        approx_cardinality=3,
        notes="How the activity was placed.",
    ),
    MappedDimension(
        dimension_name="sector",
        source_table="order_items",
        source_column="sector",
        hierarchy=["product"],
        approx_cardinality=6,
        notes="The category the items belong to, within the chosen region.",
    ),
    MappedDimension(
        dimension_name="product",
        source_table="order_items",
        source_column="product_id",
        hierarchy=[],
        approx_cardinality=40,
        notes="The individual item, within the chosen category.",
        label_table="product_master",
        label_key="product_id",
        label_column="product_name",
    ),
)


def relationship_for(anchor_table: str, related_table: str) -> MappedRelationship | None:
    """How ``related_table`` is reached from ``anchor_table``, if this map knows."""

    if (anchor_table or "").lower() != _ANCHOR_TABLE:
        return None
    if (related_table or "").lower() != _LINE_RELATIONSHIP.table:
        return None
    return _LINE_RELATIONSHIP


# ---------------------------------------------------------------------------
# What a KPI may be broken down by, when nothing was registered
# ---------------------------------------------------------------------------
def _can_apportion(version: KpiVersion) -> bool:
    """Whether this KPI's measure can be split along a finer-grained table.

    Only a plain total can. A count of records, a distinct count and a ratio
    cannot: one record spans several line rows, so any split of a count would
    either count the record once per row or hand back fractions of a record, and
    a ratio of apportioned parts is not the ratio of the whole. Those KPIs keep
    the dimensions that live on their own table and are offered nothing else --
    which is a smaller answer, not a wrong one.
    """

    spec = version.formula_spec or {}
    if spec.get("denominator"):
        return False
    numerator = spec.get("numerator") or {}
    if bool(numerator.get("distinct")):
        return False
    if str(numerator.get("column") or "") == "*":
        return False
    return str(numerator.get("aggregation") or "").upper() == "SUM"


def _sibling_tables(session: Session, anchor: SourceTable) -> set[str]:
    """Every table registered on the same source as ``anchor``, lower-cased."""

    names = session.scalars(
        select(SourceTable.table_name).where(
            SourceTable.company_id == anchor.company_id,
            SourceTable.data_source_id == anchor.data_source_id,
        )
    ).all()
    return {str(name).lower() for name in names}


def _can_read_two_tables_at_once(session: Session, anchor: SourceTable) -> bool:
    """Whether this source can match the finer table to the KPI's own table.

    A source reached over REST returns one table per request. Both tables are
    readable; neither can be joined to the other in a single pass, which is
    precisely what apportioning a KPI across a finer grain requires --
    :mod:`app.services.kpi_breakdown` refuses the breakdown outright for such a
    source. Asking here means the dimension is never *offered*, so a reader is not
    handed a drill-down that fails the moment they click it.
    """

    source = session.get(DataSource, anchor.data_source_id)
    if source is None:
        return False
    return registry.supports_multi_table_reads(source.source_type)


def dimensions_for(session: Session, version: KpiVersion) -> list[MappedDimension]:
    """The declared breakdowns for this KPI version, most useful first.

    Empty unless every condition holds: the KPI is measured on the table this map
    describes, and -- for a dimension on a finer-grained table -- that table is
    actually registered on the same source, the source can read the two tables
    together, and the KPI's measure can be apportioned. A dimension is never
    offered that a drill-down would then fail on, which is the whole reason for the
    checks.
    """

    table_id = version.primary_source_table_id
    if not table_id:
        return []
    anchor = session.get(SourceTable, table_id)
    if anchor is None or (anchor.table_name or "").lower() != _ANCHOR_TABLE:
        return []

    registered = _sibling_tables(session, anchor)
    apportionable = _can_apportion(version)
    joinable = _can_read_two_tables_at_once(session, anchor)

    out: list[MappedDimension] = []
    for dimension in _ANCHOR_DIMENSIONS:
        on_anchor = dimension.source_table.lower() == _ANCHOR_TABLE
        if on_anchor:
            out.append(dimension)
            continue
        if not apportionable or not joinable:
            continue
        if dimension.source_table.lower() not in registered:
            continue
        out.append(dimension)

    # A hierarchy entry that did not survive the checks above is dropped by
    # ``contribution.next_dimensions``, which only offers a next level that is
    # itself available -- so a trimmed list never produces a dead-end click.
    return out


def label_source_registered(session: Session, version: KpiVersion, table: str) -> bool:
    """Whether the display-name table for a dimension exists on the KPI's source."""

    table_id = version.primary_source_table_id
    if not table_id or not table:
        return False
    anchor = session.get(SourceTable, table_id)
    if anchor is None:
        return False
    return table.lower() in _sibling_tables(session, anchor)
