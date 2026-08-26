"""Time helpers.

SQLite does not preserve timezone offsets, so datetimes read back from the
platform DB are naive. Everything is written as UTC and normalised on read via
``as_utc`` so comparisons never mix aware and naive values.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime; convert an aware one to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    aware = as_utc(value)
    return aware.isoformat() if aware else None
