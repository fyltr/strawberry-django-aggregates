"""Fail-loud ordering on aggregate aliases — SPEC § 9."""

from __future__ import annotations

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
    parse_aggregate_order,
)
from strawberry_django_aggregates.errors import OrderFieldNotAllowed


@pytest.mark.django_db
def test_order_by_aggregate_alias(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        order_by=[("sum_total", "desc", None)],
    )
    sums = [r["sum_total"] for r in rows]
    # Decimal totals 700, 350, 75 in descending order.
    assert sums == sorted(sums, reverse=True)
    assert sums[0] == max(sums)


@pytest.mark.django_db
def test_order_unknown_alias_raises(sample_orders):
    from tests.models import Order

    with pytest.raises(OrderFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            order_by=[("xyz_unknown", "desc", None)],
        )


def test_parse_django_flavor_descending():
    canonical, direction = parse_aggregate_order(
        "-sum_total",
        group_by_fields=["customer_id"],
        aggregate_aliases=["count", "sum_total"],
    )
    assert (canonical, direction) == ("sum_total", "desc")


def test_parse_explicit_suffix():
    canonical, direction = parse_aggregate_order(
        "count desc",
        group_by_fields=["customer_id"],
        aggregate_aliases=["count"],
    )
    assert (canonical, direction) == ("count", "desc")


def test_parse_odoo_flavor_field_colon_op():
    canonical, direction = parse_aggregate_order(
        "total:sum",
        group_by_fields=["customer_id"],
        aggregate_aliases=["sum_total"],
    )
    assert (canonical, direction) == ("sum_total", "asc")


def test_parse_bucketed_groupby_reference():
    canonical, direction = parse_aggregate_order(
        "created_at:month",
        group_by_fields=["created_at_month"],
        aggregate_aliases=[],
    )
    assert (canonical, direction) == ("created_at_month", "asc")


def test_parse_unknown_term_raises():
    with pytest.raises(OrderFieldNotAllowed):
        parse_aggregate_order(
            "nonexistent",
            group_by_fields=["customer_id"],
            aggregate_aliases=["count"],
        )


def test_parse_field_allowlist():
    canonical, direction = parse_aggregate_order(
        "name",
        group_by_fields=[],
        aggregate_aliases=[],
        field_allowlist=["name"],
    )
    assert (canonical, direction) == ("name", "asc")
