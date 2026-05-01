"""End-to-end builder test — runs an actual GraphQL query against a
schema produced by :class:`AggregateBuilder`. This exercises the full
stack: type emission → resolver → ``compute_aggregation`` → result
shaping back into the strawberry types.

Note: we deliberately do NOT use ``from __future__ import annotations``
here. Strawberry resolves field-type annotations on the Query class
against ``__globals__``; under PEP 563 the dynamic ``built.aggregate_type``
becomes a string strawberry cannot evaluate. This file documents the
canonical user-facing pattern: annotate with the live class object.
"""

from decimal import Decimal

import pytest
import strawberry

from strawberry_django_aggregates import AggregateBuilder


@pytest.fixture
def order_schema(sample_orders):
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status"],
    ).build()

    @strawberry.type
    class Query:
        order_aggregate:  built.aggregate_type     = built.aggregate_field
        orders_group_by:  built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query), built


@pytest.mark.django_db
def test_aggregate_query(order_schema):
    schema, _ = order_schema
    result = schema.execute_sync("""
        query {
            orderAggregate {
                count
                sum { total quantity }
                avg { total }
            }
        }
    """)
    assert result.errors is None, result.errors
    data = result.data["orderAggregate"]
    assert data["count"] == 6
    assert Decimal(data["sum"]["total"]) == Decimal("1125.00")
    # ``quantity`` is an IntegerField; SUM emits the ``BigInt`` scalar
    # which serializes as a JSON string per Stream 2 (v1.0). Clients
    # parse with ``int()`` / ``BigInt()`` to recover the value.
    assert data["sum"]["quantity"] == "13"
    assert int(data["sum"]["quantity"]) == 13
    assert Decimal(data["avg"]["total"]) == Decimal("187.50")


@pytest.mark.django_db
def test_group_by_query(order_schema):
    schema, _ = order_schema
    result = schema.execute_sync("""
        query {
            ordersGroupBy(
                groupBy: [{ field: CUSTOMER }]
            ) {
                totalCount
                pageInfo { offset limit }
                results {
                    key { customerId }
                    count
                    sum { total }
                }
            }
        }
    """)
    assert result.errors is None, result.errors
    data = result.data["ordersGroupBy"]
    assert data["totalCount"] == 3
    assert len(data["results"]) == 3
    counts = sorted(r["count"] for r in data["results"])
    assert counts == [1, 2, 3]


@pytest.mark.django_db
def test_group_by_with_having(order_schema):
    schema, _ = order_schema
    result = schema.execute_sync("""
        query {
            ordersGroupBy(
                groupBy: [{ field: CUSTOMER }]
                having: { sumTotalGt: "300.00" }
            ) {
                totalCount
                results { count sum { total } }
            }
        }
    """)
    assert result.errors is None, result.errors
    data = result.data["ordersGroupBy"]
    # Alpha (700) and Beta (350); Gamma (75) excluded by HAVING.
    assert data["totalCount"] == 2


@pytest.mark.django_db
def test_having_without_projecting_measure(order_schema):
    """Regression for C2: HAVING on a measure NOT in the projection
    must still filter. The previous implementation silently dropped
    the HAVING clause if the measure wasn't selected.
    """
    schema, _ = order_schema
    result = schema.execute_sync("""
        query {
            ordersGroupBy(
                groupBy: [{ field: CUSTOMER }]
                having: { sumTotalGt: "300.00" }
            ) {
                totalCount
                results { count }
            }
        }
    """)
    assert result.errors is None, result.errors
    data = result.data["ordersGroupBy"]
    # `sum` is not requested, but HAVING must still apply.
    assert data["totalCount"] == 2
    assert len(data["results"]) == 2


@pytest.mark.django_db
def test_aggregate_query_with_fragment(order_schema):
    """Regression for C3: the selection walker must descend into
    fragments. Without fragment-flattening, a query like the one
    below silently returns ``sum: null`` because the operator
    selections inside ``...AggBits`` get skipped by the walker.
    """
    schema, _ = order_schema
    result = schema.execute_sync("""
        fragment AggBits on OrderAggregate {
            count
            sum { total }
        }
        query {
            orderAggregate { ...AggBits }
        }
    """)
    assert result.errors is None, result.errors
    data = result.data["orderAggregate"]
    assert data["count"] == 6
    assert data["sum"] is not None
    assert Decimal(data["sum"]["total"]) == Decimal("1125.00")


# ---------------------------------------------------------------------------
# Multi-column countDistinct (Hasura-style) — Stream 8.
#
# Single GraphQL surface ``countDistinct(field?, fields?)`` accepts EITHER
# a single ``field`` argument (backward-compatible single-column distinct)
# OR a list of ``fields`` (multi-column tuple distinct). Mutually
# exclusive at the resolver — both-set / neither-set raises.
# ---------------------------------------------------------------------------


@pytest.fixture
def order_schema_with_fk_countable(sample_orders):
    """Schema variant whose aggregate_fields include the FK + status
    columns so they appear in ``OrderCountableField``. Without this, the
    multi-column count_distinct tests can't reference CUSTOMER / STATUS
    on the wire (they would parse-error against the enum).
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity", "customer", "status"],
        group_by_fields=["customer", "status"],
    ).build()

    @strawberry.type
    class Query:
        order_aggregate:  built.aggregate_type     = built.aggregate_field
        orders_group_by:  built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query), built


@pytest.mark.django_db
def test_count_distinct_single_field_backwards_compatible(
    order_schema_with_fk_countable,
):
    """``countDistinct(field: ...)`` continues to work unchanged."""
    schema, _ = order_schema_with_fk_countable
    result = schema.execute_sync("""
        query {
            orderAggregate {
                countDistinct(field: CUSTOMER)
            }
        }
    """)
    assert result.errors is None, result.errors
    assert result.data["orderAggregate"]["countDistinct"] == 3


@pytest.mark.django_db
def test_count_distinct_multi_field_tuple(order_schema_with_fk_countable):
    """``countDistinct(fields: [CUSTOMER, STATUS])`` returns the count
    of distinct (customer, status) tuples — 4 for the fixture (see the
    correctness test for the breakdown).
    """
    schema, _ = order_schema_with_fk_countable
    result = schema.execute_sync("""
        query {
            orderAggregate {
                countDistinct(fields: [CUSTOMER, STATUS])
            }
        }
    """)
    assert result.errors is None, result.errors
    assert result.data["orderAggregate"]["countDistinct"] == 4


@pytest.mark.django_db
def test_count_distinct_multi_field_order_insensitive(
    order_schema_with_fk_countable,
):
    """Wire-input order of ``fields`` doesn't change the result —
    canonicalized via sorted-tuple key. Pinning the sort behavior so
    clients don't accidentally rely on input ordering.
    """
    schema, _ = order_schema_with_fk_countable
    a = schema.execute_sync("""
        query { orderAggregate {
            countDistinct(fields: [CUSTOMER, STATUS]) } }
    """)
    b = schema.execute_sync("""
        query { orderAggregate {
            countDistinct(fields: [STATUS, CUSTOMER]) } }
    """)
    assert a.errors is None and b.errors is None
    assert a.data["orderAggregate"]["countDistinct"] == \
        b.data["orderAggregate"]["countDistinct"]


@pytest.mark.django_db
def test_count_distinct_neither_field_nor_fields_raises(
    order_schema_with_fk_countable,
):
    """Calling ``countDistinct`` with neither argument raises a clear
    error rather than silently returning 0 or all-rows.
    """
    schema, _ = order_schema_with_fk_countable
    result = schema.execute_sync("""
        query {
            orderAggregate {
                countDistinct
            }
        }
    """)
    # Mutual-exclusion validation runs in the resolver — Strawberry
    # surfaces a single error referencing the field path.
    assert result.errors is not None
    messages = [str(e.message) for e in result.errors]
    assert any(
        "countDistinct" in m or "exactly one" in m for m in messages
    ), messages


@pytest.mark.django_db
def test_count_distinct_both_field_and_fields_raises(
    order_schema_with_fk_countable,
):
    """Calling ``countDistinct(field: X, fields: [Y])`` is contradictory
    and must fail loud.
    """
    schema, _ = order_schema_with_fk_countable
    result = schema.execute_sync("""
        query {
            orderAggregate {
                countDistinct(field: CUSTOMER, fields: [STATUS])
            }
        }
    """)
    assert result.errors is not None
    messages = [str(e.message) for e in result.errors]
    assert any(
        "exactly one" in m or "countDistinct" in m for m in messages
    ), messages
