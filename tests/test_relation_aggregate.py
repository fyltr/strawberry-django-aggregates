"""Stream 9 — cross-relation aggregate field.

Exercises :func:`register_relation_aggregate` end-to-end: a
``Customer`` strawberry-django type gets an ``ordersAggregate``
field that aggregates each row's child orders, optionally filtered.

Determinism: registering twice yields byte-identical SDL.

Note: we deliberately do NOT use ``from __future__ import annotations``
here — Strawberry resolves field-type annotations on the Query class
against ``__globals__``; under PEP 563 the dynamic
``built.aggregate_type`` becomes a string Strawberry cannot evaluate.
Same pattern as ``test_builder_integration.py``.
"""

from decimal import Decimal

import pytest
import strawberry
import strawberry_django

from strawberry_django_aggregates import AggregateBuilder
from strawberry_django_aggregates.relations import (
    register_relation_aggregate,
)


@pytest.fixture
def order_built():
    """Built aggregates for the Order child model."""
    from tests.models import Order

    return AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status"],
    ).build()


@pytest.fixture
def customer_schema(order_built, sample_orders):
    """Schema with ``Customer`` parent + ``ordersAggregate`` relation field."""
    from tests.models import Customer

    @strawberry_django.type(Customer)
    class CustomerType:
        id: int
        name: str

    register_relation_aggregate(CustomerType, "orders", order_built)

    @strawberry.type
    class Query:
        customers: list[CustomerType] = strawberry_django.field()

    return strawberry.Schema(query=Query)


@pytest.mark.django_db
def test_relation_aggregate_count_per_parent(
    customer_schema, sample_orders,
):
    """Each customer's ``ordersAggregate.count`` matches the
    number of orders attached to that customer.
    """
    customers, orders = sample_orders
    expected_counts = {c.name: 0 for c in customers}
    for o in orders:
        expected_counts[o.customer.name] += 1

    result = customer_schema.execute_sync("""
        query {
            customers {
                id
                name
                ordersAggregate { count }
            }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["customers"]
    assert len(rows) == 3
    for row in rows:
        assert row["ordersAggregate"]["count"] == \
            expected_counts[row["name"]]


@pytest.mark.django_db
def test_relation_aggregate_sum_per_parent(
    customer_schema, sample_orders,
):
    """Each customer's ``ordersAggregate.sum.total`` matches the
    Decimal sum of ``Order.total`` for that customer.
    """
    customers, orders = sample_orders
    expected_totals = {c.name: Decimal("0.00") for c in customers}
    for o in orders:
        expected_totals[o.customer.name] += o.total

    result = customer_schema.execute_sync("""
        query {
            customers {
                id
                name
                ordersAggregate {
                    count
                    sum { total }
                }
            }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["customers"]
    by_name = {r["name"]: r for r in rows}
    for name, expected in expected_totals.items():
        actual = Decimal(by_name[name]["ordersAggregate"]["sum"]["total"])
        assert actual == expected, (
            f"Customer {name}: expected {expected}, got {actual}"
        )


@pytest.mark.django_db
def test_relation_aggregate_quantity_sum_bigint(
    customer_schema, sample_orders,
):
    """SUM over an IntegerField surfaces as a ``BigInt`` scalar
    (string-encoded). Sanity check the wire shape — same Stream 2
    semantics as the top-level aggregate field.
    """
    customers, orders = sample_orders
    expected_qty = {c.name: 0 for c in customers}
    for o in orders:
        expected_qty[o.customer.name] += o.quantity

    result = customer_schema.execute_sync("""
        query {
            customers {
                name
                ordersAggregate {
                    sum { quantity }
                }
            }
        }
    """)
    assert result.errors is None, result.errors
    by_name = {r["name"]: r for r in result.data["customers"]}
    for name, expected in expected_qty.items():
        wire_value = by_name[name]["ordersAggregate"]["sum"]["quantity"]
        assert int(wire_value) == expected


# ---------------------------------------------------------------------------
# Filter argument
# ---------------------------------------------------------------------------


@pytest.fixture
def customer_schema_with_filter(order_built, sample_orders):
    """Variant exposing a ``filter`` argument on ``ordersAggregate``.

    Filters child orders by ``status`` so we can pin "paid only"
    aggregations. Uses strawberry-django's auto-generated filter
    type wrapping a status field.
    """
    from tests.models import Customer, Order

    @strawberry_django.filter_type(Order, lookups=True)
    class OrderFilter:
        status: strawberry.auto

    @strawberry_django.type(Customer)
    class CustomerType:
        id: int
        name: str

    register_relation_aggregate(
        CustomerType, "orders", order_built, filter_type=OrderFilter,
    )

    @strawberry.type
    class Query:
        customers: list[CustomerType] = strawberry_django.field()

    return strawberry.Schema(query=Query)


@pytest.mark.django_db
def test_relation_aggregate_with_filter(
    customer_schema_with_filter, sample_orders,
):
    """``ordersAggregate(filter: { status: { exact: "paid" } })``
    returns only paid orders' aggregates per customer.
    """
    customers, orders = sample_orders
    paid_counts = {c.name: 0 for c in customers}
    paid_totals = {c.name: Decimal("0.00") for c in customers}
    for o in orders:
        if o.status == "paid":
            paid_counts[o.customer.name] += 1
            paid_totals[o.customer.name] += o.total

    result = customer_schema_with_filter.execute_sync("""
        query {
            customers {
                name
                ordersAggregate(
                    filter: { status: { exact: "paid" } }
                ) {
                    count
                    sum { total }
                }
            }
        }
    """)
    assert result.errors is None, result.errors
    by_name = {r["name"]: r for r in result.data["customers"]}
    for name in paid_counts:
        agg = by_name[name]["ordersAggregate"]
        assert agg["count"] == paid_counts[name]
        if paid_totals[name] > 0:
            assert Decimal(agg["sum"]["total"]) == paid_totals[name]
        else:
            # No paid orders → SUM over empty set is NULL.
            assert agg["sum"]["total"] is None


# ---------------------------------------------------------------------------
# SDL determinism — registering twice yields identical SDL
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_register_twice_byte_identical_sdl():
    """CLAUDE.md Critical Rule 2: same registration twice ⇒
    byte-identical SDL output. Re-registering the same field name
    must not introduce duplicates or change ordering.
    """
    from tests.models import Customer, Order

    def build_schema():
        order_built = AggregateBuilder(
            model=Order,
            aggregate_fields=["total", "quantity"],
        ).build()

        @strawberry_django.type(Customer)
        class CustomerType:
            id: int
            name: str

        register_relation_aggregate(
            CustomerType, "orders", order_built,
        )
        # Register a second time on the same class — should be
        # idempotent: the cached definition's fields list deduplicates
        # by Python name, so SDL output is unchanged.
        register_relation_aggregate(
            CustomerType, "orders", order_built,
        )

        @strawberry.type
        class Query:
            customers: list[CustomerType] = strawberry_django.field()

        return str(strawberry.Schema(query=Query))

    a = build_schema()
    b = build_schema()
    assert a == b, "SDL output diverged across two builds"
    # Sanity-check the field is present exactly once on CustomerType.
    customer_block_starts = [
        i for i, line in enumerate(a.splitlines())
        if line.startswith("type CustomerType {")
    ]
    assert len(customer_block_starts) == 1
    customer_block = []
    for line in a.splitlines()[customer_block_starts[0]:]:
        customer_block.append(line)
        if line == "}":
            break
    aggregate_lines = [
        ln for ln in customer_block if "ordersAggregate" in ln
    ]
    assert len(aggregate_lines) == 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_register_on_non_strawberry_django_type_raises(order_built):
    """Plain :func:`strawberry.type` parents lack the django
    definition descriptor — registration should fail loud, not
    silently produce a broken field.
    """
    @strawberry.type
    class NotADjangoType:
        id: int

    with pytest.raises(TypeError, match="strawberry-django type"):
        register_relation_aggregate(
            NotADjangoType, "orders", order_built,
        )


@pytest.mark.django_db
def test_register_unknown_relation_raises(order_built):
    """Unknown relation name → clear ``ValueError`` referencing the
    parent model.
    """
    from tests.models import Customer

    @strawberry_django.type(Customer)
    class CustomerType:
        id: int

    with pytest.raises(ValueError, match="no field"):
        register_relation_aggregate(
            CustomerType, "nonexistent_relation", order_built,
        )


@pytest.mark.django_db
def test_register_on_forward_field_raises(order_built):
    """Asking for an aggregate over a scalar / forward FK is a
    user error — we want one-to-many or many-to-many reverse only.
    """
    from tests.models import Customer

    @strawberry_django.type(Customer)
    class CustomerType:
        id: int

    # ``name`` is a CharField — not a reverse relation.
    with pytest.raises(ValueError, match="reverse"):
        register_relation_aggregate(
            CustomerType, "name", order_built,
        )
