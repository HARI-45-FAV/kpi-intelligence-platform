"""Two source databases that have nothing in common but their purpose.

These exist to make one claim falsifiable: the detection engine holds no
knowledge of any company, table, column, formula or weekday. Proving that needs
two tenants whose schemas disagree about everything the engine touches.

======================  ==============================  ==============================
                        Company A -- Aurora Retail      Company B -- Borealis Foods
======================  ==============================  ==============================
revenue table           ``orders``                      ``sales_transactions``
revenue measure         ``net_revenue``                 ``amount``
time field              ``order_date`` (DATE)           ``transaction_date`` (TIMESTAMP)
second KPI's table      ``orders`` (same table)         ``refunds`` (different table)
second KPI's time       ``order_date``                  ``refund_date`` (DATE)
busiest weekday         Friday                          Tuesday
======================  ==============================  ==============================

The time-field types differ on purpose. A DATE column is bounded by the date; a
TIMESTAMP column has to be bounded by the first and last instant of the day or
an afternoon transaction is silently dropped. The engine reads which it is from
the profiled column rather than from the column's name, and Company B is the
tenant that would expose it if that ever regressed.

**Every daily total here is exact.** The two ``*_total_for`` functions below are
the seeded truth, and the tests recompute the expected median and MAD from them
with the standard library -- never by calling the engine's own statistics. Rows
within a day are laid out so the day's aggregate lands on the intended figure to
the rupee: one row carries the remainder and the rest carry 1.0 each.

The alternating weekday pattern is not decoration either. Twenty-six comparable
weekdays alternating between two values give thirteen of each, so the median is
their midpoint and every absolute deviation from it is identical -- which makes
the MAD, and therefore the modified z-score, exactly predictable rather than
approximately so.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Target dates. Fixed, not relative to "today", so the arithmetic in the tests
# is stable whenever the suite runs.
# ---------------------------------------------------------------------------
COMPANY_A_TARGET = date(2026, 8, 28)  # a Friday
COMPANY_B_TARGET = date(2026, 8, 25)  # a Tuesday

#: Far enough back to fill a prior-year comparable era, which Company A's
#: configuration needs in order to exercise the year-over-year path (the 78th
#: comparable weekday is 546 days back).
HISTORY_DAYS = 620

#: How many comparable dates the engine's default budget will use. Thirteen of
#: each alternating value.
BUDGET = 26


# ---------------------------------------------------------------------------
# Company A -- Aurora Retail. Friday is the trading peak.
# ---------------------------------------------------------------------------
A_SCHEMA = """
CREATE TABLE orders (
    order_id    TEXT PRIMARY KEY,
    order_date  DATE    NOT NULL,
    region      TEXT    NOT NULL,
    channel     TEXT    NOT NULL,
    net_revenue REAL    NOT NULL
);
CREATE INDEX idx_orders_order_date ON orders(order_date);
"""

A_REGIONS = ("North", "South", "East", "West")
A_CHANNELS = ("STORE", "ONLINE")

#: Ordinary weekdays, by ISO weekday number. Friday is absent: it alternates.
A_WEEKDAY_REVENUE = {1: 4_000_000, 2: 4_200_000, 3: 4_400_000, 4: 5_000_000, 6: 8_000_000, 7: 6_000_000}
A_FRIDAY_HIGH = 10_500_000
A_FRIDAY_LOW = 10_000_000

#: The anomaly under test: a Friday that came in far below comparable Fridays.
A_TARGET_REVENUE = 6_000_000

A_FRIDAY_ORDERS_HIGH = 7
A_FRIDAY_ORDERS_LOW = 5
A_ORDINARY_ORDERS = 4
#: Deliberately the median of the comparable Fridays, so the order-count KPI is
#: NORMAL on the very date revenue is ABNORMAL. One engine, one date, two KPIs,
#: two different verdicts -- because the KPIs are different, not the code.
A_TARGET_ORDERS = 6


def _friday_index(day: date) -> int:
    """Whole weeks between ``day`` and Company A's target date."""
    return (COMPANY_A_TARGET - day).days // 7


def a_revenue_total_for(day: date) -> int:
    """The exact ``SUM(orders.net_revenue)`` seeded for ``day``."""
    if day == COMPANY_A_TARGET:
        return A_TARGET_REVENUE
    if day.isoweekday() == 5:
        return A_FRIDAY_HIGH if _friday_index(day) % 2 == 0 else A_FRIDAY_LOW
    return A_WEEKDAY_REVENUE[day.isoweekday()]


def a_order_count_for(day: date) -> int:
    """The exact ``COUNT(DISTINCT orders.order_id)`` seeded for ``day``."""
    if day == COMPANY_A_TARGET:
        return A_TARGET_ORDERS
    if day.isoweekday() == 5:
        return A_FRIDAY_ORDERS_HIGH if _friday_index(day) % 2 == 0 else A_FRIDAY_ORDERS_LOW
    return A_ORDINARY_ORDERS


# ---------------------------------------------------------------------------
# Company B -- Borealis Foods. Tuesday is the trading peak, and the revenue
# time field is a timestamp rather than a date.
# ---------------------------------------------------------------------------
B_SCHEMA = """
CREATE TABLE sales_transactions (
    txn_id           TEXT      PRIMARY KEY,
    transaction_date TIMESTAMP NOT NULL,
    territory        TEXT      NOT NULL,
    amount           REAL      NOT NULL
);
CREATE TABLE refunds (
    refund_id     TEXT PRIMARY KEY,
    refund_date   DATE NOT NULL,
    territory     TEXT NOT NULL,
    refund_amount REAL NOT NULL
);
CREATE INDEX idx_txn_date ON sales_transactions(transaction_date);
CREATE INDEX idx_refund_date ON refunds(refund_date);
"""

B_TERRITORIES = ("Metro", "Coastal", "Inland")

B_WEEKDAY_AMOUNT = {1: 500_000, 3: 520_000, 4: 540_000, 5: 600_000, 6: 700_000, 7: 450_000}
B_TUESDAY_HIGH = 850_000
B_TUESDAY_LOW = 800_000
#: Just above the comparable median -- an ordinary Tuesday.
B_TARGET_AMOUNT = 830_000

B_WEEKDAY_REFUND = {1: 20_000, 3: 22_000, 4: 24_000, 5: 26_000, 6: 30_000, 7: 18_000}
B_TUESDAY_REFUND_HIGH = 50_000
B_TUESDAY_REFUND_LOW = 40_000
#: Double the comparable median: refunds spiked on a day revenue looked fine.
B_TARGET_REFUND = 90_000

#: Times of day spread across the working day. Any of these falling outside the
#: day's bounds would drop the transaction from the aggregate, which is exactly
#: the regression the TIMESTAMP path guards against.
B_TIMES = (time(0, 0, 1), time(9, 30), time(14, 15), time(19, 45), time(23, 59, 59))


def _tuesday_index(day: date) -> int:
    return (COMPANY_B_TARGET - day).days // 7


def b_amount_total_for(day: date) -> int:
    """The exact ``SUM(sales_transactions.amount)`` seeded for ``day``."""
    if day == COMPANY_B_TARGET:
        return B_TARGET_AMOUNT
    if day.isoweekday() == 2:
        return B_TUESDAY_HIGH if _tuesday_index(day) % 2 == 0 else B_TUESDAY_LOW
    return B_WEEKDAY_AMOUNT[day.isoweekday()]


def b_refund_total_for(day: date) -> int:
    """The exact ``SUM(refunds.refund_amount)`` seeded for ``day``."""
    if day == COMPANY_B_TARGET:
        return B_TARGET_REFUND
    if day.isoweekday() == 2:
        return B_TUESDAY_REFUND_HIGH if _tuesday_index(day) % 2 == 0 else B_TUESDAY_REFUND_LOW
    return B_WEEKDAY_REFUND[day.isoweekday()]


# ---------------------------------------------------------------------------
# Row layout
# ---------------------------------------------------------------------------
def _split(total: int, rows: int) -> list[float]:
    """``rows`` amounts summing to exactly ``total``.

    One row carries the remainder and the others carry 1.0 each, so the day's
    aggregate is the seeded figure to the rupee. Distributing evenly would
    introduce a floating-point residue and make every assertion approximate.
    """
    if rows < 1:
        raise ValueError("a seeded day needs at least one row")
    return [float(total - (rows - 1))] + [1.0] * (rows - 1)


def _days(target: date) -> list[date]:
    """The target date and every day of history behind it."""
    return [target - timedelta(days=offset) for offset in range(0, HISTORY_DAYS + 1)]


def build_company_a_source(path: str | Path) -> dict:
    """Aurora Retail: ``orders`` keyed on ``order_date`` (a DATE)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    rows: list[tuple] = []
    for day in _days(COMPANY_A_TARGET):
        count = a_order_count_for(day)
        for index, amount in enumerate(_split(a_revenue_total_for(day), count)):
            rows.append(
                (
                    f"AO-{day.isoformat()}-{index:02d}",
                    day.isoformat(),
                    A_REGIONS[index % len(A_REGIONS)],
                    A_CHANNELS[index % len(A_CHANNELS)],
                    amount,
                )
            )

    conn = sqlite3.connect(path)
    try:
        conn.executescript(A_SCHEMA)
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()

    return {
        "path": str(path),
        "target_date": COMPANY_A_TARGET,
        "orders": len(rows),
    }


def build_company_b_source(path: str | Path) -> dict:
    """Borealis Foods: ``sales_transactions`` on a TIMESTAMP, plus ``refunds``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    txns: list[tuple] = []
    refunds: list[tuple] = []
    for day in _days(COMPANY_B_TARGET):
        amounts = _split(b_amount_total_for(day), len(B_TIMES))
        for index, (moment, amount) in enumerate(zip(B_TIMES, amounts, strict=True)):
            txns.append(
                (
                    f"BT-{day.isoformat()}-{index:02d}",
                    datetime.combine(day, moment).isoformat(sep=" "),
                    B_TERRITORIES[index % len(B_TERRITORIES)],
                    amount,
                )
            )
        # Two refund rows a day: enough for a real aggregate, few enough that the
        # table's grain is unambiguous.
        for index, amount in enumerate(_split(b_refund_total_for(day), 2)):
            refunds.append(
                (
                    f"BR-{day.isoformat()}-{index:02d}",
                    day.isoformat(),
                    B_TERRITORIES[index % len(B_TERRITORIES)],
                    amount,
                )
            )

    conn = sqlite3.connect(path)
    try:
        conn.executescript(B_SCHEMA)
        conn.executemany("INSERT INTO sales_transactions VALUES (?,?,?,?)", txns)
        conn.executemany("INSERT INTO refunds VALUES (?,?,?,?)", refunds)
        conn.commit()
    finally:
        conn.close()

    return {
        "path": str(path),
        "target_date": COMPANY_B_TARGET,
        "sales_transactions": len(txns),
        "refunds": len(refunds),
    }
