"""What period a KPI's source actually holds data for.

A per-day read cannot distinguish two very different facts:

* *the source holds this day, and nothing matched* -- a real observation, and for a
  KPI whose contract says ``TREAT_AS_ZERO`` a genuine zero;
* *the source holds nothing for this day at all* -- no observation, and counting it
  as zero would invent history.

Both arrive as "no rows". Left unresolved, the second one is corrosive: a date
before the source's first row becomes a legitimate-looking ``0.0``, enough of them
drag the median to zero, and detection then reports an expected value of zero with
every appearance of having measured it.

So coverage is established once per source table, from the registered time field,
with one pushed-down MIN/MAX. Nothing here knows any company's calendar, any table
name or any KPI: the bounds come from the data, and a connector that cannot answer
yields ``known=False``, which callers must treat as *unknown* rather than *empty*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.connectors.base import DataSourceConnector
from app.core.errors import ConnectorError

#: Cache key: the source table and time field a coverage window was measured for.
CoverageKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Coverage:
    """The inclusive date range a source table holds, as far as it can be told."""

    start: date | None
    end: date | None
    known: bool
    note: str

    def contains(self, day: date) -> bool:
        """True unless ``day`` is provably outside the data the source holds.

        Unknown coverage admits every date: refusing to compare on the strength of
        a fact the source declined to state would be the same overreach in the
        other direction.
        """

        if not self.known:
            return True
        if self.start is None and self.end is None:
            # Measured, and the source holds no dated row at all: nothing is
            # comparable, and nothing should be read as though it were.
            return False
        if self.start is not None and day < self.start:
            return False
        if self.end is not None and day > self.end:
            return False
        return True

    def describe(self) -> str:
        """The window as a phrase, readable in the middle of a sentence."""

        if not self.known:
            return "unknown"
        if self.start is None and self.end is None:
            return "no dated rows"
        if self.start is None or self.end is None:
            bound = self.end or self.start
            assert bound is not None  # noqa: S101 - one of the two is set here
            return f"a single edge at {bound.isoformat()}"
        return f"{self.start.isoformat()} to {self.end.isoformat()}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "note": self.note,
        }


UNKNOWN = Coverage(
    start=None,
    end=None,
    known=False,
    note=(
        "This source did not report the extent of its time field, so no date was "
        "excluded on coverage grounds."
    ),
)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # A REST source returns the column as text; a driver may return either.
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate.rstrip("Z")).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def source_coverage(
    connector: DataSourceConnector,
    *,
    schema: str,
    table: str,
    time_column: str | None,
    cache: dict[CoverageKey, Coverage] | None = None,
) -> Coverage:
    """The date range ``table`` holds on ``time_column``, measured once.

    ``cache`` is supplied by a batch run so that a dozen KPIs over one table cost
    one extra query rather than a dozen.
    """

    if not time_column:
        return Coverage(
            start=None,
            end=None,
            known=False,
            note=(
                "The KPI has no registered time field, so its source's coverage "
                "cannot be established."
            ),
        )

    key: CoverageKey = (schema, table, time_column)
    if cache is not None and key in cache:
        return cache[key]

    try:
        extent = connector.time_extent(schema, table, time_column)
    except (ConnectorError, NotImplementedError):
        extent = None

    if extent is None:
        coverage = UNKNOWN
    else:
        start, end = (_as_date(extent[0]), _as_date(extent[1]))
        if start is None and end is None:
            coverage = Coverage(
                start=None,
                end=None,
                known=True,
                note=f"{schema}.{table} holds no rows with a value in {time_column}.",
            )
        else:
            coverage = Coverage(
                start=start,
                end=end,
                known=True,
                note=(
                    f"{schema}.{table} holds data on {time_column} from "
                    f"{start.isoformat() if start else 'an unknown start'} to "
                    f"{end.isoformat() if end else 'an unknown end'}."
                ),
            )

    if cache is not None:
        cache[key] = coverage
    return coverage
