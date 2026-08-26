"""The governed formula grammar is the platform's main injection boundary and the
basis of every validation check, so its accept/reject behaviour is tested
directly rather than only through the API."""

from __future__ import annotations

import pytest

from app.core.errors import PlatformError
from app.models.base import Aggregation, KpiKind
from app.services.kpi_formula import FormulaSpec, parse_formula


@pytest.mark.parametrize(
    ("expression", "expected_kind", "expected_aggregation"),
    [
        ("SUM(orders.order_value)", KpiKind.SIMPLE, Aggregation.SUM),
        ("COUNT(DISTINCT orders.order_id)", KpiKind.SIMPLE, Aggregation.COUNT_DISTINCT),
        ("COUNT(DISTINCT customer_id)", KpiKind.SIMPLE, Aggregation.COUNT_DISTINCT),
        ("SUM(order_items.quantity)", KpiKind.SIMPLE, Aggregation.SUM),
        ("COUNT(*)", KpiKind.SIMPLE, Aggregation.COUNT),
        ("AVG(order_value)", KpiKind.SIMPLE, Aggregation.AVG),
        (
            "SUM(orders.order_value) / COUNT(DISTINCT orders.order_id)",
            KpiKind.RATIO,
            Aggregation.SUM,
        ),
    ],
)
def test_accepts_governed_formulas(expression, expected_kind, expected_aggregation):
    spec = parse_formula(expression, default_table="orders")
    assert spec.kind == expected_kind
    assert spec.numerator.effective_aggregation == expected_aggregation


def test_the_four_sprint_one_kpis_parse():
    """Revenue, Orders, Unique Customers and AOV are the frozen KPI set."""
    formulas = {
        "revenue": "SUM(orders.order_value)",
        "orders": "COUNT(DISTINCT orders.order_id)",
        "unique_customers": "COUNT(DISTINCT orders.customer_id)",
        "aov": "SUM(orders.order_value) / COUNT(DISTINCT orders.order_id)",
    }
    parsed = {key: parse_formula(value, default_table="orders") for key, value in formulas.items()}
    assert parsed["aov"].kind == KpiKind.RATIO
    assert parsed["aov"].denominator is not None
    assert all(spec.kind == KpiKind.SIMPLE for key, spec in parsed.items() if key != "aov")


def test_default_table_is_applied_when_column_is_unqualified():
    spec = parse_formula("SUM(order_value)", default_table="orders")
    assert spec.numerator.table == "orders"
    assert spec.render() == "SUM(orders.order_value)"


def test_round_trip_through_stored_dict_is_lossless():
    original = parse_formula(
        "SUM(orders.order_value) / COUNT(DISTINCT orders.order_id)", default_table="orders"
    )
    restored = FormulaSpec.from_dict(original.as_dict())
    assert restored.render() == original.render()
    assert restored.kind == original.kind
    assert restored.denominator is not None


def test_lineage_is_derived_from_the_contract():
    spec = parse_formula(
        "SUM(orders.order_value) / COUNT(DISTINCT orders.order_id)", default_table="orders"
    )
    refs = {(role, column) for role, _table, column in spec.referenced_columns}
    assert ("NUMERATOR", "order_value") in refs
    assert ("DENOMINATOR", "order_id") in refs


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("SUM(order_value); DROP TABLE orders", "statement separator"),
        ("SUM(order_value) -- comment", "sql comment"),
        ("SUM(order_value) /* comment */", "block comment"),
        ("SELECT SUM(order_value) FROM orders", "subquery"),
        ("SUM(a) FROM orders JOIN x ON 1=1", "join"),
        ("SUM(a) UNION SELECT 1", "set operation"),
        ("SUM(CASE WHEN x THEN 1 END)", "conditional"),
        ("SUM(a) / COUNT(b) / COUNT(c)", "more than one division"),
        ("SUM(DISTINCT order_value)", "non-count distinct"),
        ("TOTAL(order_value)", "unknown aggregation"),
        ("SUM(order_value", "unbalanced parentheses"),
        ("", "empty"),
        ("SUM(orders.order_value + 1)", "arithmetic inside the aggregate"),
        ("COUNT(DISTINCT *)", "distinct star"),
        ("SUM(*)", "sum over star"),
    ],
)
def test_rejects_anything_outside_the_grammar(expression, reason):
    with pytest.raises(PlatformError):
        parse_formula(expression, default_table="orders")


def test_filters_reject_unlisted_operators():
    with pytest.raises(PlatformError):
        parse_formula(
            "SUM(order_value)",
            default_table="orders",
            filters=[{"column": "region", "operator": "; DROP TABLE", "value": "North"}],
        )


def test_filters_require_a_value_unless_null_check():
    with pytest.raises(PlatformError):
        parse_formula(
            "SUM(order_value)",
            default_table="orders",
            filters=[{"column": "region", "operator": "="}],
        )
    spec = parse_formula(
        "SUM(order_value)",
        default_table="orders",
        filters=[{"column": "region", "operator": "IS NOT NULL"}],
    )
    assert spec.filters[0].operator == "IS NOT NULL"
