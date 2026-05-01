"""Refuse o2m / m2m measure traversal — SPEC § 11.

Default: refuse with :class:`AggregationAcrossRelationError`.

Opt-in: ``compute_aggregation(..., allow_relation_traversal=True)``
emits a correlated ``Subquery`` per measure (one ``Subquery`` per
measure, not a JOIN), dodging the row-multiplication trap.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import (
    AggregateError,
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
    msg = str(exc_info.value).lower()
    assert "row multiplication" in msg
    # Default-error message must point at the opt-in flag so callers
    # can find the escape hatch without grepping the source.
    assert "allow_relation_traversal" in str(exc_info.value)


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


@pytest.mark.django_db
def test_relation_traversal_flag_sums_correctly(sample_order_items):
    """``SUM(items__price)`` with the flag returns the per-row sum
    via a correlated subquery — no row multiplication.

    Per the fixture, each order's items.price sum is:
      o1 → 30, o2 → 50, o3 → 100, o4 → None (no items),
      o5 → 15, o6 → 200.
    Total = 30 + 50 + 100 + 0 + 15 + 200 = 395.
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("id", None)],
        aggregates=[(AggregateOp.SUM, "items__price")],
        allow_relation_traversal=True,
    )
    by_id = {r["id"]: r["sum_items__price"] for r in rows}
    # Pull the actual order PKs from the fixture.
    _, orders, _ = sample_order_items
    o1, o2, o3, o4, o5, o6 = orders
    assert by_id[o1.pk] == Decimal("30.00")
    assert by_id[o2.pk] == Decimal("50.00")
    assert by_id[o3.pk] == Decimal("100.00")
    # o4 has zero items → SUM is NULL in SQL, surfaces as Python None.
    assert by_id[o4.pk] is None
    assert by_id[o5.pk] == Decimal("15.00")
    assert by_id[o6.pk] == Decimal("200.00")


@pytest.mark.django_db
def test_relation_traversal_with_groupby(sample_order_items):
    """Group by ``customer`` and sum ``items__price`` per customer.

    Per the fixture:
      Alpha owns o1 (30), o2 (50), o6 (200) → 280
      Beta  owns o3 (100), o4 (None)        → 100
      Gamma owns o5 (15)                    → 15
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.SUM, "items__price")],
        allow_relation_traversal=True,
    )
    customers, _, _ = sample_order_items
    alpha, beta, gamma = customers
    by_customer = {r["customer_id"]: r["sum_items__price"] for r in rows}
    assert by_customer[alpha.pk] == Decimal("280.00")
    assert by_customer[beta.pk] == Decimal("100.00")
    assert by_customer[gamma.pk] == Decimal("15.00")


@pytest.mark.django_db
def test_relation_traversal_no_row_multiplication(sample_order_items):
    """Combining a relation-traversing measure with a scalar measure
    on the OUTER model must not row-multiply the scalar.

    ``SUM(total)`` on Order is a per-row sum of the outer table — it
    must be computed against the outer rows once, not once per child
    item. If the relation traversal were JOIN-based, ``Order.total``
    would be summed N times per order (where N = #items), grossly
    inflating the value. The Subquery emission keeps the two measures
    independent.
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[
            (AggregateOp.SUM, "total"),         # outer, no traversal
            (AggregateOp.SUM, "items__price"),  # inner, traversal
        ],
        allow_relation_traversal=True,
    )
    assert len(rows) == 1
    # Outer SUM(total) is the un-multiplied sum of the order totals.
    # From conftest sample_orders: 100 + 200 + 300 + 50 + 75 + 400 = 1125.
    assert rows[0]["sum_total"] == Decimal("1125.00")
    # Inner SUM(items__price): 30 + 50 + 100 + 15 + 200 = 395.
    assert rows[0]["sum_items__price"] == Decimal("395.00")


@pytest.mark.django_db
def test_relation_traversal_groupby_still_refused(sample_order_items):
    """Even with the flag set, ``group_by`` paths cannot traverse
    relations — that would row-multiply the OUTER query and corrupt
    every measure regardless of subquery isolation.
    """
    from tests.models import Customer

    with pytest.raises(AggregationAcrossRelationError):
        compute_aggregation(
            Customer.objects.all(),
            group_by=[("orders__total", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            allow_relation_traversal=True,
        )


@pytest.mark.django_db
def test_relation_traversal_unsupported_op_raises(sample_order_items):
    """STDDEV with the relation-traversal flag raises a v1.0 error.

    The flag-supported set is intentionally minimal (SUM/AVG/MIN/MAX/
    COUNT/COUNT_DISTINCT). Other operators raise an
    :class:`AggregateError` with a clear v1.0-limitation message
    rather than emitting incorrect SQL.
    """
    from tests.models import Order

    with pytest.raises(AggregateError) as exc_info:
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.STDDEV, "items__price")],
            allow_relation_traversal=True,
        )
    msg = str(exc_info.value)
    assert "stddev" in msg
    assert "allow_relation_traversal" in msg
    assert "v1.0" in msg


@pytest.mark.django_db
def test_relation_traversal_count_works(sample_order_items):
    """``COUNT_DISTINCT(items__price)`` with the flag counts distinct
    leaves per outer row via a subquery. Used here to verify a
    non-SUM operator path still emits correct SQL.

    Parents with no matching child rows surface as ``None`` in the
    subquery (the inner ``GROUP BY`` produces no rows), and the
    outer ``SUM`` of a single-row group passes that ``None``
    through. This matches SQL semantics — callers who need the
    "0 for empty" surface should ``COALESCE`` post-fetch.
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("id", None)],
        aggregates=[(AggregateOp.COUNT_DISTINCT, "items__price")],
        allow_relation_traversal=True,
    )
    _, orders, _ = sample_order_items
    o1, o2, o3, o4, o5, o6 = orders
    by_id = {r["id"]: r["count_distinct_items__price"] for r in rows}
    # o5 has three items at the same price (5.00) → distinct count 1.
    assert by_id[o1.pk] == 2  # 10, 20
    assert by_id[o2.pk] == 1  # 50
    assert by_id[o3.pk] == 2  # 25, 75
    # o4 has no items — Subquery emits NULL, surfaces as None.
    assert by_id[o4.pk] is None
    assert by_id[o5.pk] == 1  # 5
    assert by_id[o6.pk] == 1  # 200
