"""A small NovaMart-shaped source database for the automated tests.

This is a *test fixture*, not product data and not a demo dataset. The real
platform reads a live Supabase registered through the UI; this exists only so the
golden test can drive the real API end to end, deterministically and without
credentials.

Its shape mirrors the frozen NovaMart specification (orders / order_items /
marketing_daily plus reference tables) and is deliberately built to exercise the
behaviour the golden test asserts:

* ``orders.order_id`` is unique, ``orders.customer_id`` is not — so
  ``COUNT(DISTINCT customer_id)`` is a genuinely different KPI from
  ``COUNT(DISTINCT order_id)``.
* ``order_items.product_id`` is a **declared** foreign key;
  ``orders.customer_id`` is **not**, so relationship inference by name and
  containment must find it.
* ``customer_master`` holds ``email`` and ``phone``, which must be classified as
  personal data and withheld from a non-privileged profiling run.
* ``region_targets`` holds one row per region *per month*, so joining orders to
  it on ``region`` alone fans out — the join-safety trap.
* ``marketing_daily`` sits at a coarser grain and lags by three days, so
  reconciliation must report REQUIRES_AGGREGATION and freshness must report STALE.
* ``campaigns_archive`` is never selected, proving the analytical-scope boundary.
* ``kpi_contracts`` is the company's *own* KPI registry, the way a governed source
  really carries one. The platform must find it by column roles alone (no
  hardcoded table name), bind each formula to real columns, and flag the rows it
  cannot bind rather than quietly dropping or rewriting them.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HISTORY_DAYS = 75
MARKETING_LAG_DAYS = 3
SEED = 424242

REGIONS = ("North", "South", "West", "East")
REGION_SHARE = {"North": 0.30, "South": 0.30, "West": 0.25, "East": 0.15}
CHANNELS = ("Website", "Mobile App", "Marketplace")
CHANNEL_SHARE = {"Website": 0.45, "Mobile App": 0.35, "Marketplace": 0.20}
SECTORS = ("Electronics", "Fashion", "Home & Kitchen", "Grocery & Personal Care")
SECTOR_SHARE = {
    "Electronics": 0.35,
    "Fashion": 0.25,
    "Home & Kitchen": 0.20,
    "Grocery & Personal Care": 0.20,
}
CUSTOMER_TYPES = ("VIP", "Plus", "Regular")
CUSTOMER_TYPE_SHARE = {"VIP": 0.10, "Plus": 0.25, "Regular": 0.65}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE product_master (
    product_id   TEXT    PRIMARY KEY,
    product_name TEXT    NOT NULL,
    sector       TEXT    NOT NULL,
    list_price   NUMERIC NOT NULL,
    launch_date  DATE    NOT NULL
);

-- Holds personal data on purpose: exercises classification and access-aware
-- profiling. Not selected into scope by the golden test.
CREATE TABLE customer_master (
    customer_id   TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    customer_type TEXT NOT NULL,
    region        TEXT NOT NULL,
    signup_date   DATE NOT NULL
);

-- Grain: one row per order.
CREATE TABLE orders (
    order_id    TEXT    PRIMARY KEY,
    order_date  DATE    NOT NULL,
    customer_id TEXT    NOT NULL,   -- no declared FK: tests inferred relationships
    region      TEXT    NOT NULL,
    channel     TEXT    NOT NULL,
    order_value NUMERIC NOT NULL
);

-- Grain: one row per (order, product).
CREATE TABLE order_items (
    order_id   TEXT    NOT NULL,
    product_id TEXT    NOT NULL REFERENCES product_master(product_id),
    sector     TEXT    NOT NULL,
    quantity   INTEGER NOT NULL,
    item_value NUMERIC
);

-- Grain: one row per (date, region, sector, channel). Lags three days.
CREATE TABLE marketing_daily (
    spend_date     DATE    NOT NULL,
    region         TEXT    NOT NULL,
    sector         TEXT    NOT NULL,
    channel        TEXT    NOT NULL,
    campaign_spend NUMERIC NOT NULL
);

-- One row per region PER MONTH: joining orders on region alone fans out.
CREATE TABLE region_targets (
    region         TEXT    NOT NULL,
    target_month   DATE    NOT NULL,
    revenue_target NUMERIC NOT NULL
);

-- The company's own KPI definitions. Deliberately mixed: rows that bind cleanly,
-- a ratio, an inactive row, one naming a column that does not exist, and one whose
-- formula the governed grammar cannot express. Column names are ordinary registry
-- names, not names the platform looks for by string match.
CREATE TABLE kpi_contracts (
    kpi_id            INTEGER PRIMARY KEY,
    kpi_name          TEXT    NOT NULL,
    description       TEXT,
    formula           TEXT    NOT NULL,
    grain             TEXT,
    source_tables     TEXT,
    owner             TEXT,
    refresh_frequency TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE campaigns_archive (
    archive_id  TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    archived_on DATE NOT NULL,
    notes       TEXT
);

CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_marketing_date ON marketing_daily(spend_date);
"""

PRODUCTS = (
    ("P001", "Aurora Smartphone X", "Electronics", 28999),
    ("P002", "Aurora Earbuds Z", "Electronics", 2499),
    ("P003", "Aurora LED TV 43", "Electronics", 31499),
    ("P004", "Vertex Denim Jacket", "Fashion", 3499),
    ("P005", "Vertex Running Shoes", "Fashion", 4299),
    ("P006", "Nova Air Fryer 5L", "Home & Kitchen", 5499),
    ("P007", "Nova Cookware Set", "Home & Kitchen", 2899),
    ("P008", "Daily Grocery Basket", "Grocery & Personal Care", 1299),
    ("P009", "Personal Care Bundle", "Grocery & Personal Care", 899),
)

SECTOR_OF = {product[0]: product[2] for product in PRODUCTS}
PRICE_OF = {product[0]: product[3] for product in PRODUCTS}
PRODUCTS_BY_SECTOR: dict[str, list[str]] = {}
for _pid, _name, _sector, _price in PRODUCTS:
    PRODUCTS_BY_SECTOR.setdefault(_sector, []).append(_pid)


def _weighted(rng: random.Random, shares: dict[str, float]) -> str:
    keys = list(shares)
    return rng.choices(keys, weights=[shares[k] for k in keys])[0]


def _day_multiplier(day: date) -> float:
    """The hidden rules the platform is never told: Fri +20%, Sat +25%, week 3 +15%."""
    factor = 1.0
    weekday = day.weekday()
    if weekday == 4:
        factor *= 1.20
    elif weekday == 5:
        factor *= 1.25
    if 15 <= day.day <= 21:
        factor *= 1.15
    if day.month == 1:
        factor *= 1.10
    return factor


def build_fixture_database(path: str | Path, *, seed: int = SEED, today: date | None = None) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    rng = random.Random(seed)
    reference_day = today or datetime.now(UTC).date()
    conn = sqlite3.connect(path)
    counts: dict[str, int] = {}

    try:
        conn.executescript(SCHEMA)

        conn.executemany(
            "INSERT INTO product_master VALUES (?,?,?,?,?)",
            [
                (pid, name, sector, price, (reference_day - timedelta(days=400)).isoformat())
                for pid, name, sector, price in PRODUCTS
            ],
        )
        counts["product_master"] = len(PRODUCTS)

        customers = []
        for i in range(1, 121):
            name = f"Customer {i:03d}"
            customers.append(
                (
                    f"C{i:05d}",
                    name,
                    f"customer{i:03d}@example.com",
                    f"+91-90000{i:05d}",
                    _weighted(rng, CUSTOMER_TYPE_SHARE),
                    _weighted(rng, REGION_SHARE),
                    (reference_day - timedelta(days=rng.randint(30, 800))).isoformat(),
                )
            )
        conn.executemany("INSERT INTO customer_master VALUES (?,?,?,?,?,?,?)", customers)
        counts["customer_master"] = len(customers)
        customer_ids = [row[0] for row in customers]

        orders: list[tuple] = []
        items: list[tuple] = []
        order_seq = 0

        for days_ago in range(HISTORY_DAYS, -1, -1):
            day = reference_day - timedelta(days=days_ago)
            daily_orders = max(4, int(round(9 * _day_multiplier(day) * rng.uniform(0.9, 1.1))))
            for _ in range(daily_orders):
                order_seq += 1
                order_id = f"ORD-{day.strftime('%Y%m%d')}-{order_seq:05d}"
                region = _weighted(rng, REGION_SHARE)
                channel = _weighted(rng, CHANNEL_SHARE)
                # A repeat customer base, so unique customers < orders.
                customer_id = rng.choice(customer_ids)

                line_count = rng.choices((1, 2, 3), weights=(0.6, 0.3, 0.1))[0]
                chosen: list[str] = []
                order_value = 0.0
                for _ in range(line_count):
                    sector = _weighted(rng, SECTOR_SHARE)
                    product_id = rng.choice(PRODUCTS_BY_SECTOR[sector])
                    if product_id in chosen:
                        continue
                    chosen.append(product_id)
                    quantity = rng.choices((1, 2, 3), weights=(0.75, 0.2, 0.05))[0]
                    unit = PRICE_OF[product_id] * rng.uniform(0.95, 1.05)
                    item_value = round(unit * quantity, 2)
                    order_value += item_value
                    # ~2% of item values are missing in the feed. Left as NULL.
                    stored = None if rng.random() < 0.02 else item_value
                    items.append((order_id, product_id, SECTOR_OF[product_id], quantity, stored))

                if not chosen:
                    continue
                orders.append(
                    (
                        order_id,
                        day.isoformat(),
                        customer_id,
                        region,
                        channel,
                        round(order_value, 2),
                    )
                )

        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", orders)
        conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
        counts["orders"] = len(orders)
        counts["order_items"] = len(items)

        marketing: list[tuple] = []
        for days_ago in range(HISTORY_DAYS, MARKETING_LAG_DAYS - 1, -1):
            day = reference_day - timedelta(days=days_ago)
            for region in REGIONS:
                for sector in SECTORS:
                    for channel in CHANNELS:
                        spend = (
                            40_000
                            * REGION_SHARE[region]
                            * SECTOR_SHARE[sector]
                            * CHANNEL_SHARE[channel]
                            * _day_multiplier(day)
                            * rng.uniform(0.9, 1.1)
                        )
                        marketing.append(
                            (day.isoformat(), region, sector, channel, round(spend, 2))
                        )
        conn.executemany("INSERT INTO marketing_daily VALUES (?,?,?,?,?)", marketing)
        counts["marketing_daily"] = len(marketing)

        targets: list[tuple] = []
        anchor = date(reference_day.year, reference_day.month, 1)
        for offset in range(4):
            index = anchor.year * 12 + (anchor.month - 1) - offset
            month = date(index // 12, index % 12 + 1, 1)
            for region in REGIONS:
                targets.append((region, month.isoformat(), round(2_400_000 * REGION_SHARE[region], 2)))
        conn.executemany("INSERT INTO region_targets VALUES (?,?,?)", targets)
        counts["region_targets"] = len(targets)

        # The company KPI registry. Order is not alphabetical on purpose: the API
        # must sort deterministically rather than inheriting insertion order.
        contracts = [
            (
                1,
                "Revenue",
                "Total recognised sales revenue across all orders.",
                "SUM(orders.order_value)",
                "daily",
                "orders",
                "Finance",
                "DAILY",
                1,
            ),
            (
                2,
                "Orders",
                "Number of distinct orders placed.",
                "COUNT(DISTINCT order_id)",
                "daily",
                "orders",
                "Commercial",
                "DAILY",
                1,
            ),
            (
                3,
                "Average Order Value",
                "Revenue divided by order count.",
                "SUM(orders.order_value) / COUNT(DISTINCT orders.order_id)",
                "daily",
                "orders",
                "Finance",
                "DAILY",
                1,
            ),
            (
                4,
                "Marketing Spend",
                "Total campaign investment.",
                "SUM(campaign_spend)",
                "daily",
                "marketing_daily",
                "Marketing",
                "DAILY",
                1,
            ),
            (
                5,
                "Legacy Basket Size",
                "Retired metric kept for historical reporting.",
                "SUM(order_items.quantity)",
                "daily",
                "order_items",
                "Commercial",
                "DAILY",
                0,
            ),
            (
                6,
                "Gross Margin Percent",
                "Margin after cost of goods, as a percentage.",
                "SUM(orders.gross_margin) / SUM(orders.order_value)",
                "monthly",
                "orders",
                "Finance",
                "MONTHLY",
                1,
            ),
            (
                7,
                "Repeat Purchase Rate",
                "Share of customers who ordered more than once in the period.",
                "SUM(CASE WHEN order_count > 1 THEN 1 ELSE 0 END) / COUNT(DISTINCT customer_id)",
                "monthly",
                "orders",
                "Commercial",
                "MONTHLY",
                1,
            ),
        ]
        conn.executemany(
            "INSERT INTO kpi_contracts VALUES (?,?,?,?,?,?,?,?,?)", contracts
        )
        counts["kpi_contracts"] = len(contracts)

        archive = [
            (
                f"ARC-{i:04d}",
                f"CMP-{rng.randint(100, 499)}",
                (reference_day - timedelta(days=rng.randint(400, 900))).isoformat(),
                "Archived for finance reconciliation.",
            )
            for i in range(1, 21)
        ]
        conn.executemany("INSERT INTO campaigns_archive VALUES (?,?,?,?)", archive)
        counts["campaigns_archive"] = len(archive)

        conn.commit()
    finally:
        conn.close()

    return {
        "path": str(path),
        "seed": seed,
        "reference_date": reference_day.isoformat(),
        "history_days": HISTORY_DAYS,
        "marketing_lag_days": MARKETING_LAG_DAYS,
        "row_counts": counts,
    }
