"""Correctness tests for :func:`compute_aggregation`.

Validates that COUNT / SUM / AVG / MIN / MAX produce the right
numbers for a known fixture, with and without group_by, and that
COUNT_DISTINCT respects DISTINCT semantics.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    TimeGranularity,
    compute_aggregation,
)


@pytest.mark.django_db
def test_plain_count_sum_avg(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
            (AggregateOp.AVG, "total"),
            (AggregateOp.MIN, "total"),
            (AggregateOp.MAX, "total"),
        ],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["count"] == 6
    assert row["sum_total"] == Decimal("1125.00")
    assert row["min_total"] == Decimal("50.00")
    assert row["max_total"] == Decimal("400.00")
    # Decimal AVG: 1125 / 6 = 187.50
    assert row["avg_total"] == Decimal("187.50")


@pytest.mark.django_db
def test_count_distinct(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[
            (AggregateOp.COUNT_DISTINCT, "customer"),
            (AggregateOp.COUNT_DISTINCT, "status"),
        ],
    )
    row = rows[0]
    assert row["count_distinct_customer"] == 3
    assert row["count_distinct_status"] == 3


@pytest.mark.django_db
def test_group_by_customer(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        order_by=[("customer_id", "asc", None)],
    )
    customers, _ = sample_orders
    assert len(rows) == 3
    by_id = {r["customer_id"]: r for r in rows}
    assert by_id[customers[0].id]["count"] == 3
    assert by_id[customers[0].id]["sum_total"] == Decimal("700.00")
    assert by_id[customers[1].id]["count"] == 2
    assert by_id[customers[1].id]["sum_total"] == Decimal("350.00")
    assert by_id[customers[2].id]["count"] == 1
    assert by_id[customers[2].id]["sum_total"] == Decimal("75.00")


@pytest.mark.django_db
def test_group_by_month_truncate(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_month", "asc", None)],
    )
    assert len(rows) == 2  # April + May 2026
    counts = [r["count"] for r in rows]
    assert counts == [2, 4]


@pytest.mark.django_db
def test_sum_skips_null_does_not_treat_as_zero(sample_orders):
    """SQL NULL semantics: SUM/AVG/MIN/MAX skip NULL inputs.

    Adding an extra ``Order(total=None)`` row alongside the six
    non-null fixture rows must not change ``sum_total`` (a NULL must
    not be coerced to 0). ``count`` counts rows including the NULL
    one; ``count_distinct`` of a non-null column is unaffected.
    """
    import datetime

    from tests.models import Order

    customers, _ = sample_orders
    Order.objects.create(
        customer=customers[0],
        status="draft",
        total=None,  # NULL measure
        quantity=1,
        is_priority=False,
        created_at=datetime.datetime(
            2026,
            5,
            30,
            12,
            0,
            tzinfo=datetime.UTC,
        ),
    )

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
            (AggregateOp.AVG, "total"),
            (AggregateOp.MIN, "total"),
            (AggregateOp.MAX, "total"),
            (AggregateOp.COUNT_DISTINCT, "customer"),
        ],
    )
    row = rows[0]
    # count = COUNT(pk) counts ALL rows including the NULL-total one.
    assert row["count"] == 7
    # sum skips NULL: same as before (NULL is NOT 0).
    assert row["sum_total"] == Decimal("1125.00")
    # avg = sum / count(non-null) = 1125 / 6 = 187.50 (NOT 1125 / 7).
    assert row["avg_total"] == Decimal("187.50")
    # min/max skip NULL.
    assert row["min_total"] == Decimal("50.00")
    assert row["max_total"] == Decimal("400.00")
    # count_distinct unaffected (customer never NULL).
    assert row["count_distinct_customer"] == 3


@pytest.mark.django_db
def test_sum_of_all_null_group_returns_none(db):
    """All-NULL group: SUM/AVG/MIN/MAX return NULL, not 0."""
    import datetime

    from tests.models import Customer, Order

    c = Customer.objects.create(name="Solo")
    Order.objects.create(
        customer=c,
        status="draft",
        total=None,
        quantity=1,
        is_priority=False,
        created_at=datetime.datetime(
            2026,
            5,
            1,
            0,
            0,
            tzinfo=datetime.UTC,
        ),
    )

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
            (AggregateOp.AVG, "total"),
            (AggregateOp.MIN, "total"),
            (AggregateOp.MAX, "total"),
        ],
    )
    row = rows[0]
    assert row["count"] == 1  # row exists
    assert row["sum_total"] is None  # NOT Decimal("0")
    assert row["avg_total"] is None
    assert row["min_total"] is None
    assert row["max_total"] is None
