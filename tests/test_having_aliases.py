"""HAVING with aggregate aliases — SPEC § 8."""

from __future__ import annotations

from decimal import Decimal

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import HavingFieldNotAllowed


@pytest.mark.django_db
def test_having_sum_total_gt(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        having={"sum_total__gt": Decimal("300.00")},
    )
    # Only customer Alpha (700) and Beta (350) have sum_total > 300.
    sums = sorted(r["sum_total"] for r in rows)
    assert sums == [Decimal("350.00"), Decimal("700.00")]


@pytest.mark.django_db
def test_having_count_eq(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        having={"count__eq": 3},
    )
    # Only customer Alpha has 3 orders.
    assert len(rows) == 1
    assert rows[0]["count"] == 3


@pytest.mark.django_db
def test_having_neq(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        having={"count__neq": 3},
    )
    counts = sorted(r["count"] for r in rows)
    assert counts == [1, 2]


@pytest.mark.django_db
def test_having_unknown_alias_raises(sample_orders):
    from tests.models import Order

    with pytest.raises(HavingFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            having={"sum_unknown_field__gt": Decimal("0")},
        )


@pytest.mark.django_db
def test_having_unknown_comparison_raises(sample_orders):
    from tests.models import Order

    with pytest.raises(HavingFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            having={"count__between": 1},
        )
