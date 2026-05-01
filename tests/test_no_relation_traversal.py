"""Refuse o2m / m2m measure traversal — SPEC § 11."""

from __future__ import annotations

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import (
    AggregationAcrossRelationError,
)


@pytest.mark.django_db
def test_aggregate_across_o2m_raises(sample_orders):
    """``SUM(order.items__price)`` would silently row-multiply.
    Refuse with a typed error and a pointer to the explicit
    alternative (query the child model with the parent FK).
    """
    from tests.models import Order

    with pytest.raises(AggregationAcrossRelationError) as exc_info:
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.SUM, "items__price")],
        )
    assert "row multiplication" in str(exc_info.value).lower()


@pytest.mark.django_db
def test_group_by_across_o2m_raises(sample_orders):
    from tests.models import Customer

    # Customer has reverse FK 'orders'; group_by on 'orders__total'
    # would also row-multiply.
    with pytest.raises(AggregationAcrossRelationError):
        compute_aggregation(
            Customer.objects.all(),
            group_by=[("orders__total", None)],
            aggregates=[(AggregateOp.COUNT, None)],
        )
