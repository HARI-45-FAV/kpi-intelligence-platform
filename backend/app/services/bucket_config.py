"""Company bucket configuration: which past dates are comparable to today.

This module owns exactly one idea, and deliberately nothing else:

    The *slots* are fixed by the algorithm. The *values* come from the company.

The five slots below are the only comparison shapes the detection engine knows
how to fill. Their contents -- which weekdays matter, which week of the month,
which months form a season, which dates an event falls on -- arrive as company
configuration and are never written here. That is what lets one engine serve two
companies whose busiest weekday is different without a branch.

Consequences worth stating out loud, because they are the point:

* There is no weekday, month, season, event or multiplier literal anywhere in
  this file. Weekdays and months are normalised to ISO integers and rendered
  back into words through :mod:`calendar`, so the vocabulary is the platform's
  calendar handling rather than any company's pattern.
* Nothing is enabled by default. An unconfigured company gets no buckets, and
  the engine falls back to its documented trailing-window floor and says so.
* Validation is strict and total. A configuration either normalises into
  :class:`BucketConfig` or raises, so the engine never has to interpret
  free-form input while computing a number.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.core.errors import ValidationFailure
from app.models.base import BucketType

# --- Bounds on the knobs a company may turn ---------------------------------
# Wide enough to express any real policy, narrow enough that a typo cannot make
# the engine issue an unbounded number of queries.
LOOKBACK_MIN_DAYS = 7
LOOKBACK_MAX_DAYS = 1826  # five years
LOOKBACK_DEFAULT_DAYS = 365

MIN_POINTS_FLOOR = 2
MIN_POINTS_CEILING = 60
MIN_POINTS_DEFAULT = 3

MAX_POINTS_FLOOR = 3
MAX_POINTS_CEILING = 200
MAX_POINTS_DEFAULT = 26

YOY_TOLERANCE_DEFAULT_DAYS = 7
YOY_TOLERANCE_MAX_DAYS = 45
DAYS_IN_YEAR = 365

WEEK_OF_MONTH_MAX = 5
ISO_WEEKDAY_MIN, ISO_WEEKDAY_MAX = 1, 7
MONTH_MIN, MONTH_MAX = 1, 12

# The engine tries slots in this order when deciding which one *applies* to a
# target date. Precedence is part of the algorithm, not of any company's setup:
# an event day is unlike any ordinary day, a weekday pattern is more specific
# than a week-of-month pattern, and the year-over-year slot is a last resort
# because it yields the fewest comparable dates.
BUCKET_PRECEDENCE: tuple[BucketType, ...] = (
    BucketType.BUSINESS_EVENT,
    BucketType.SAME_DAY_OF_WEEK,
    BucketType.SAME_WEEK_OF_MONTH,
    BucketType.SAME_MONTH_OR_SEASON,
    BucketType.YOY_PERIOD,
)

# The JSON key each slot is configured under.
SLOT_KEYS: dict[BucketType, str] = {
    BucketType.SAME_DAY_OF_WEEK: "same_day_of_week",
    BucketType.SAME_WEEK_OF_MONTH: "same_week_of_month",
    BucketType.SAME_MONTH_OR_SEASON: "same_month_or_season",
    BucketType.BUSINESS_EVENT: "business_event",
    BucketType.YOY_PERIOD: "yoy_period",
}
_KEY_TO_SLOT = {value: key for key, value in SLOT_KEYS.items()}

_CONFIG_KEYS = frozenset(SLOT_KEYS.values()) | {
    "lookback_days",
    "min_reference_points",
    "max_reference_points",
}


# ---------------------------------------------------------------------------
# Calendar helpers. Pure date arithmetic; no company knowledge.
# ---------------------------------------------------------------------------
def week_of_month(day: date) -> int:
    """Ordinal week of the month, counting from the 1st.

    ``1 + (day_of_month - 1) // 7`` -- days 1-7 are week 1, 8-14 week 2, and so
    on. Chosen over an ISO-aligned week because it is stable: "the third week"
    means the same span of dates in every month, which is what a business means
    when it says its third week runs hot. An ISO alignment would move the same
    date between week 2 and week 3 depending on which weekday the month started.
    """

    return (day.day - 1) // 7 + 1


def anniversary_of(day: date, *, years_back: int = 1) -> date:
    """The same calendar position N years earlier, clamped for 29 February."""

    year = day.year - years_back
    last_day = calendar.monthrange(year, day.month)[1]
    return date(year, day.month, min(day.day, last_day))


def weekday_label(day: date, *, plural: bool = False) -> str:
    """"Friday" / "Fridays" -- rendered from the date, never from a literal."""

    name = calendar.day_name[day.weekday()]
    return f"{name}s" if plural else name


def _weekday_aliases() -> dict[str, int]:
    """Accepted spellings of a weekday, mapped to its ISO number (Mon=1)."""

    aliases: dict[str, int] = {}
    for index in range(7):
        iso = index + 1
        for word in (calendar.day_name[index], calendar.day_abbr[index]):
            token = word.strip().upper()
            aliases[token] = iso
            aliases[token[:3]] = iso
    return aliases


def _month_aliases() -> dict[str, int]:
    aliases: dict[str, int] = {}
    for month in range(MONTH_MIN, MONTH_MAX + 1):
        for word in (calendar.month_name[month], calendar.month_abbr[month]):
            token = word.strip().upper()
            aliases[token] = month
            aliases[token[:3]] = month
    return aliases


WEEKDAY_ALIASES = _weekday_aliases()
MONTH_ALIASES = _month_aliases()


# ---------------------------------------------------------------------------
# Normalised configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BusinessEvent:
    """A named trading event and the dates it actually fell on.

    The dates are required to make the slot usable, and the platform refuses to
    guess them: no calendar in code can know when a given company observes a
    given event, and a wrong date silently corrupts every comparison built on
    it. A name supplied without dates is kept -- so the intent is visible and
    reviewable -- but reported as unusable rather than quietly approximated.
    """

    name: str
    dates: tuple[date, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.dates)

    def as_dict(self) -> dict:
        return {"name": self.name, "dates": [day.isoformat() for day in self.dates]}


@dataclass(frozen=True)
class DayOfWeekSlot:
    enabled: bool = False
    #: ISO weekday numbers the company considers distinctive. Empty with
    #: ``enabled`` means "always compare like weekday with like weekday".
    days: tuple[int, ...] = ()

    def applies_to(self, target: date) -> bool:
        if not self.enabled:
            return False
        return not self.days or target.isoweekday() in self.days

    def comparable(self, target: date, candidate: date) -> bool:
        return candidate.isoweekday() == target.isoweekday()

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "days": list(self.days)}


@dataclass(frozen=True)
class WeekOfMonthSlot:
    enabled: bool = False
    weeks: tuple[int, ...] = ()

    def applies_to(self, target: date) -> bool:
        if not self.enabled:
            return False
        return not self.weeks or week_of_month(target) in self.weeks

    def comparable(self, target: date, candidate: date) -> bool:
        return week_of_month(candidate) == week_of_month(target)

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "weeks": list(self.weeks)}


@dataclass(frozen=True)
class MonthSlot:
    """A season, expressed as the set of months that behave alike."""

    enabled: bool = False
    months: tuple[int, ...] = ()

    def applies_to(self, target: date) -> bool:
        if not self.enabled:
            return False
        return not self.months or target.month in self.months

    def comparable(self, target: date, candidate: date) -> bool:
        if self.months:
            # Every month in the configured set is comparable with every other:
            # that is what declaring them a season means.
            return candidate.month in self.months
        return candidate.month == target.month

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "months": list(self.months)}


@dataclass(frozen=True)
class EventSlot:
    enabled: bool = False
    events: tuple[BusinessEvent, ...] = ()

    def event_for(self, target: date) -> BusinessEvent | None:
        if not self.enabled:
            return None
        for event in self.events:
            if target in event.dates:
                return event
        return None

    def applies_to(self, target: date) -> bool:
        return self.event_for(target) is not None

    def comparable(self, target: date, candidate: date) -> bool:
        event = self.event_for(target)
        return bool(event and candidate in event.dates and candidate != target)

    @property
    def unusable_names(self) -> tuple[str, ...]:
        return tuple(event.name for event in self.events if not event.usable)

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "events": [event.as_dict() for event in self.events]}


@dataclass(frozen=True)
class YoySlot:
    enabled: bool = False
    tolerance_days: int = YOY_TOLERANCE_DEFAULT_DAYS

    def applies_to(self, target: date) -> bool:  # noqa: ARG002 - symmetry with siblings
        return self.enabled

    def comparable(self, target: date, candidate: date) -> bool:
        anniversary = anniversary_of(target)
        return abs((candidate - anniversary).days) <= self.tolerance_days

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "tolerance_days": self.tolerance_days}


@dataclass(frozen=True)
class BucketConfig:
    """A validated, normalised comparison policy for one company (or KPI)."""

    same_day_of_week: DayOfWeekSlot = field(default_factory=DayOfWeekSlot)
    same_week_of_month: WeekOfMonthSlot = field(default_factory=WeekOfMonthSlot)
    same_month_or_season: MonthSlot = field(default_factory=MonthSlot)
    business_event: EventSlot = field(default_factory=EventSlot)
    yoy_period: YoySlot = field(default_factory=YoySlot)

    lookback_days: int = LOOKBACK_DEFAULT_DAYS
    min_reference_points: int = MIN_POINTS_DEFAULT
    max_reference_points: int = MAX_POINTS_DEFAULT

    #: Non-fatal observations from validation, surfaced to reviewers.
    warnings: tuple[str, ...] = ()

    # -- slot access --------------------------------------------------------
    def slot(self, bucket: BucketType):
        return {
            BucketType.SAME_DAY_OF_WEEK: self.same_day_of_week,
            BucketType.SAME_WEEK_OF_MONTH: self.same_week_of_month,
            BucketType.SAME_MONTH_OR_SEASON: self.same_month_or_season,
            BucketType.BUSINESS_EVENT: self.business_event,
            BucketType.YOY_PERIOD: self.yoy_period,
        }[bucket]

    @property
    def enabled_buckets(self) -> tuple[BucketType, ...]:
        return tuple(b for b in BUCKET_PRECEDENCE if self.slot(b).enabled)

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled_buckets)

    @property
    def effective_lookback_days(self) -> int:
        """History the engine may search.

        A company that asked for a year-over-year view needs at least two years
        of window for an anniversary to exist at all, so enabling that slot
        raises the floor rather than silently producing no comparable dates.
        """

        if self.yoy_period.enabled:
            return max(self.lookback_days, 2 * DAYS_IN_YEAR)
        return self.lookback_days

    def applicable_buckets(self, target: date) -> tuple[BucketType, ...]:
        """Configured slots that actually describe ``target``, most specific first."""

        return tuple(b for b in BUCKET_PRECEDENCE if self.slot(b).applies_to(target))

    def comparable(self, bucket: BucketType, target: date, candidate: date) -> bool:
        return self.slot(bucket).comparable(target, candidate)

    def as_dict(self) -> dict:
        return {
            SLOT_KEYS[BucketType.SAME_DAY_OF_WEEK]: self.same_day_of_week.as_dict(),
            SLOT_KEYS[BucketType.SAME_WEEK_OF_MONTH]: self.same_week_of_month.as_dict(),
            SLOT_KEYS[BucketType.SAME_MONTH_OR_SEASON]: self.same_month_or_season.as_dict(),
            SLOT_KEYS[BucketType.BUSINESS_EVENT]: self.business_event.as_dict(),
            SLOT_KEYS[BucketType.YOY_PERIOD]: self.yoy_period.as_dict(),
            "lookback_days": self.lookback_days,
            "min_reference_points": self.min_reference_points,
            "max_reference_points": self.max_reference_points,
        }

    def signature(self, target: date) -> dict:
        """Compact record of the policy as it applied to one date.

        Persisted with every detection run so a result stays explainable even
        after the configuration moves on.
        """

        return {
            "enabled": [str(b) for b in self.enabled_buckets],
            "applicable": [str(b) for b in self.applicable_buckets(target)],
            "lookback_days": self.effective_lookback_days,
            "min_reference_points": self.min_reference_points,
            "max_reference_points": self.max_reference_points,
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _require_mapping(value: object, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValidationFailure(f"{label} must be an object.")
    return value


def _coerce_bool(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValidationFailure(f"{label} must be true or false.")


def _coerce_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValidationFailure(f"{label} must be a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValidationFailure(f"{label} must be a whole number.")


def _sequence(value: object, label: str) -> Sequence:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise ValidationFailure(f"{label} must be a list.")
    if isinstance(value, Sequence):
        return value
    raise ValidationFailure(f"{label} must be a list.")


def _bounded(value: int, *, low: int, high: int, label: str) -> int:
    if not low <= value <= high:
        raise ValidationFailure(f"{label} must be between {low} and {high}; got {value}.")
    return value


def _ordered_unique(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(set(values)))


def _parse_weekday(raw: object, label: str) -> int:
    if isinstance(raw, str):
        token = raw.strip().upper()
        if token in WEEKDAY_ALIASES:
            return WEEKDAY_ALIASES[token]
        if not token.lstrip("-").isdigit():
            raise ValidationFailure(
                f"{label}: '{raw}' is not a weekday. Use a weekday name, its "
                f"three-letter abbreviation, or an ISO number "
                f"({ISO_WEEKDAY_MIN}={calendar.day_name[0]} .. "
                f"{ISO_WEEKDAY_MAX}={calendar.day_name[6]})."
            )
    number = _coerce_int(raw, label)
    return _bounded(number, low=ISO_WEEKDAY_MIN, high=ISO_WEEKDAY_MAX, label=label)


def _parse_month(raw: object, label: str) -> int:
    if isinstance(raw, str):
        token = raw.strip().upper()
        if token in MONTH_ALIASES:
            return MONTH_ALIASES[token]
        if not token.lstrip("-").isdigit():
            raise ValidationFailure(
                f"{label}: '{raw}' is not a month. Use a month name, its "
                f"three-letter abbreviation, or a number "
                f"({MONTH_MIN}-{MONTH_MAX})."
            )
    number = _coerce_int(raw, label)
    return _bounded(number, low=MONTH_MIN, high=MONTH_MAX, label=label)


def _parse_date(raw: object, label: str) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError as exc:  # pragma: no cover - message is the point
            raise ValidationFailure(f"{label}: '{raw}' is not a YYYY-MM-DD date.") from exc
    raise ValidationFailure(f"{label} must be a YYYY-MM-DD date.")


def _parse_events(raw: object, label: str) -> tuple[BusinessEvent, ...]:
    """Accept the three shapes a reviewer or an extraction plausibly produces."""

    if raw is None:
        return ()

    entries: list[tuple[str, object]] = []
    if isinstance(raw, Mapping):
        # {"<event name>": ["2024-11-01", "2025-10-20"]}
        entries = [(str(name), dates) for name, dates in raw.items()]
    else:
        for index, item in enumerate(_sequence(raw, label)):
            if isinstance(item, str):
                entries.append((item, None))
            elif isinstance(item, Mapping):
                name = item.get("name") or item.get("event") or item.get("label")
                if not isinstance(name, str) or not name.strip():
                    raise ValidationFailure(f"{label}[{index}] needs a non-empty 'name'.")
                entries.append((name, item.get("dates")))
            else:
                raise ValidationFailure(
                    f"{label}[{index}] must be an event name or an object with "
                    f"'name' and 'dates'."
                )

    events: list[BusinessEvent] = []
    seen: set[str] = set()
    for name, raw_dates in entries:
        clean = name.strip()
        if not clean:
            raise ValidationFailure(f"{label} contains an empty event name.")
        if clean.upper() in seen:
            raise ValidationFailure(f"{label}: '{clean}' is configured twice.")
        seen.add(clean.upper())
        dates = tuple(
            sorted({_parse_date(item, f"{label}.{clean}.dates") for item in _sequence(raw_dates, f"{label}.{clean}.dates")})
        )
        events.append(BusinessEvent(name=clean, dates=dates))
    return tuple(events)


def validate_bucket_config(payload: object) -> BucketConfig:
    """Normalise raw configuration into a :class:`BucketConfig`, or raise.

    Strict by design: unknown keys are rejected rather than ignored, because a
    misspelled slot name would otherwise disable a company's pattern silently
    and the detection results would look plausible while comparing the wrong
    days.
    """

    data = _require_mapping(payload or {}, "Bucket configuration")

    unknown = sorted(set(map(str, data.keys())) - _CONFIG_KEYS)
    if unknown:
        known = ", ".join(sorted(_CONFIG_KEYS))
        raise ValidationFailure(
            f"Unknown bucket configuration key(s): {', '.join(unknown)}. Expected any of: {known}."
        )

    warnings: list[str] = []

    # --- SAME_DAY_OF_WEEK --------------------------------------------------
    key = SLOT_KEYS[BucketType.SAME_DAY_OF_WEEK]
    raw = _require_mapping(data.get(key) or {}, key)
    _reject_unknown(raw, {"enabled", "days"}, key)
    dow_enabled = _coerce_bool(raw.get("enabled", False), f"{key}.enabled")
    days = _ordered_unique(
        _parse_weekday(item, f"{key}.days") for item in _sequence(raw.get("days"), f"{key}.days")
    )
    if dow_enabled and not days:
        warnings.append(
            f"{key} is enabled with no specific days: every date will be compared "
            f"with the same weekday in history."
        )
    if days and not dow_enabled:
        warnings.append(f"{key} lists days but is disabled, so they are ignored.")
    day_slot = DayOfWeekSlot(enabled=dow_enabled, days=days)

    # --- SAME_WEEK_OF_MONTH ------------------------------------------------
    key = SLOT_KEYS[BucketType.SAME_WEEK_OF_MONTH]
    raw = _require_mapping(data.get(key) or {}, key)
    _reject_unknown(raw, {"enabled", "weeks"}, key)
    wom_enabled = _coerce_bool(raw.get("enabled", False), f"{key}.enabled")
    weeks = _ordered_unique(
        _bounded(_coerce_int(item, f"{key}.weeks"), low=1, high=WEEK_OF_MONTH_MAX, label=f"{key}.weeks")
        for item in _sequence(raw.get("weeks"), f"{key}.weeks")
    )
    if weeks and not wom_enabled:
        warnings.append(f"{key} lists weeks but is disabled, so they are ignored.")
    week_slot = WeekOfMonthSlot(enabled=wom_enabled, weeks=weeks)

    # --- SAME_MONTH_OR_SEASON ---------------------------------------------
    key = SLOT_KEYS[BucketType.SAME_MONTH_OR_SEASON]
    raw = _require_mapping(data.get(key) or {}, key)
    _reject_unknown(raw, {"enabled", "months"}, key)
    month_enabled = _coerce_bool(raw.get("enabled", False), f"{key}.enabled")
    months = _ordered_unique(
        _parse_month(item, f"{key}.months")
        for item in _sequence(raw.get("months"), f"{key}.months")
    )
    if months and not month_enabled:
        warnings.append(f"{key} lists months but is disabled, so they are ignored.")
    month_slot = MonthSlot(enabled=month_enabled, months=months)

    # --- BUSINESS_EVENT ----------------------------------------------------
    key = SLOT_KEYS[BucketType.BUSINESS_EVENT]
    raw = _require_mapping(data.get(key) or {}, key)
    _reject_unknown(raw, {"enabled", "events"}, key)
    event_enabled = _coerce_bool(raw.get("enabled", False), f"{key}.enabled")
    events = _parse_events(raw.get("events"), f"{key}.events")
    if event_enabled and not events:
        warnings.append(f"{key} is enabled but names no events, so it can never apply.")
    event_slot = EventSlot(enabled=event_enabled, events=events)
    for name in event_slot.unusable_names:
        warnings.append(
            f"{key}: '{name}' has no dates, so it cannot select comparable days. "
            f"Supply the dates the event fell on -- the platform will not infer them."
        )

    # --- YOY_PERIOD --------------------------------------------------------
    key = SLOT_KEYS[BucketType.YOY_PERIOD]
    raw = _require_mapping(data.get(key) or {}, key)
    _reject_unknown(raw, {"enabled", "tolerance_days"}, key)
    yoy_enabled = _coerce_bool(raw.get("enabled", False), f"{key}.enabled")
    tolerance = _bounded(
        _coerce_int(raw.get("tolerance_days", YOY_TOLERANCE_DEFAULT_DAYS), f"{key}.tolerance_days"),
        low=0,
        high=YOY_TOLERANCE_MAX_DAYS,
        label=f"{key}.tolerance_days",
    )
    yoy_slot = YoySlot(enabled=yoy_enabled, tolerance_days=tolerance)

    # --- search behaviour --------------------------------------------------
    lookback = _bounded(
        _coerce_int(data.get("lookback_days", LOOKBACK_DEFAULT_DAYS), "lookback_days"),
        low=LOOKBACK_MIN_DAYS,
        high=LOOKBACK_MAX_DAYS,
        label="lookback_days",
    )
    min_points = _bounded(
        _coerce_int(data.get("min_reference_points", MIN_POINTS_DEFAULT), "min_reference_points"),
        low=MIN_POINTS_FLOOR,
        high=MIN_POINTS_CEILING,
        label="min_reference_points",
    )
    max_points = _bounded(
        _coerce_int(data.get("max_reference_points", MAX_POINTS_DEFAULT), "max_reference_points"),
        low=MAX_POINTS_FLOOR,
        high=MAX_POINTS_CEILING,
        label="max_reference_points",
    )
    if max_points < min_points:
        raise ValidationFailure(
            f"max_reference_points ({max_points}) cannot be below "
            f"min_reference_points ({min_points})."
        )

    config = BucketConfig(
        same_day_of_week=day_slot,
        same_week_of_month=week_slot,
        same_month_or_season=month_slot,
        business_event=event_slot,
        yoy_period=yoy_slot,
        lookback_days=lookback,
        min_reference_points=min_points,
        max_reference_points=max_points,
        warnings=tuple(warnings),
    )
    if not config.is_configured:
        raise ValidationFailure(
            "A bucket configuration must enable at least one comparison slot: "
            + ", ".join(sorted(SLOT_KEYS.values()))
            + "."
        )
    return config


def _reject_unknown(raw: Mapping, allowed: set[str], label: str) -> None:
    unknown = sorted(set(map(str, raw.keys())) - allowed)
    if unknown:
        raise ValidationFailure(
            f"Unknown key(s) in {label}: {', '.join(unknown)}. Expected any of: "
            f"{', '.join(sorted(allowed))}."
        )


# ---------------------------------------------------------------------------
# Comparable-date selection
# ---------------------------------------------------------------------------
def candidate_dates(target: date, lookback_days: int) -> Iterator[date]:
    """Every date in the lookback window, most recent first, excluding ``target``.

    Walking the calendar day by day -- rather than stepping by seven, or by
    month -- is what makes the selection calendar-aware for free: month lengths,
    leap days and event dates all fall out of the predicates instead of needing
    arithmetic that has to know about them.
    """

    for offset in range(1, lookback_days + 1):
        yield target - timedelta(days=offset)


def select_comparable_dates(
    config: BucketConfig,
    target: date,
    buckets: Sequence[BucketType],
    *,
    limit: int | None = None,
) -> list[date]:
    """Dates comparable to ``target`` under *every* bucket in ``buckets``.

    Returned most recent first and capped at ``limit``, so the reference set
    reflects the current level of the business rather than its whole history.
    """

    if not buckets:
        return []
    found: list[date] = []
    for candidate in candidate_dates(target, config.effective_lookback_days):
        if all(config.comparable(bucket, target, candidate) for bucket in buckets):
            found.append(candidate)
            if limit is not None and len(found) >= limit:
                break
    return found


def trailing_dates(target: date, *, days: int, limit: int | None = None) -> list[date]:
    """The algorithm's floor: the most recent days, with no pattern claimed."""

    window = list(candidate_dates(target, days))
    return window[:limit] if limit is not None else window


def describe_buckets(
    config: BucketConfig, target: date, buckets: Sequence[BucketType]
) -> str:
    """A business-readable comparison label, built from the date and the config.

    Deliberately plain language: this is the one piece of the engine's internal
    reasoning the business surface is allowed to show.
    """

    if not buckets:
        return "Recent days"

    parts: list[str] = []
    for bucket in buckets:
        if bucket == BucketType.BUSINESS_EVENT:
            event = config.business_event.event_for(target)
            parts.append(f"{event.name} dates" if event else "event dates")
        elif bucket == BucketType.SAME_DAY_OF_WEEK:
            parts.append(weekday_label(target, plural=True))
        elif bucket == BucketType.SAME_WEEK_OF_MONTH:
            parts.append(f"week {week_of_month(target)} of the month")
        elif bucket == BucketType.SAME_MONTH_OR_SEASON:
            months = config.same_month_or_season.months
            if len(months) > 1:
                parts.append("the same season")
            else:
                parts.append(f"{calendar.month_name[target.month]}")
        elif bucket == BucketType.YOY_PERIOD:
            parts.append("the same period last year")
        elif bucket == BucketType.TRAILING_PERIOD:
            parts.append("recent days")

    if not parts:
        return "Recent days"
    if len(parts) == 1:
        return f"Comparable {parts[0]}"
    return "Comparable " + " in ".join(parts)
