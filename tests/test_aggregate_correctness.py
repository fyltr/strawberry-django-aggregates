"""Correctness tests for :func:`compute_aggregation`.

Validates that COUNT / SUM / AVG / MIN / MAX produce the right
numbers for a known fixture, with and without group_by, and that
COUNT_DISTINCT respects DISTINCT semantics.
"""

import dataclasses
import datetime
import typing
from decimal import Decimal

import pytest
import strawberry

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    BigInt,
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


# ---------------------------------------------------------------------------
# BigInt scalar — Stream 2.
#
# Postgres widens ``SUM(int_col)`` to ``bigint``; the 32-bit GraphQL
# ``Int`` would silently overflow at 2**31 - 1. We emit the custom
# ``BigInt`` scalar (string-encoded on the wire) for SUM over integer
# Django field types. Per SPEC § 5.
# ---------------------------------------------------------------------------


def test_sum_fields_quantity_emits_bigint_scalar(db):
    """OrderSumFields.quantity uses the BigInt scalar (not Int).

    The dataclass field type for an ``IntegerField`` SUM should be
    ``BigInt | None``, where ``BigInt`` is the strawberry ScalarWrapper
    re-exported from :mod:`strawberry_django_aggregates`.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer"],
    ).build()

    sum_fields_field = next(
        f for f in dataclasses.fields(built.aggregate_type)
        if f.name == "sum"
    )
    # f.type is ``OrderSumFields | None`` — unwrap.
    sum_fields_type = next(
        a for a in typing.get_args(sum_fields_field.type)
        if a is not type(None)
    )

    quantity_field = next(
        f for f in dataclasses.fields(sum_fields_type)
        if f.name == "quantity"
    )
    args = typing.get_args(quantity_field.type)
    non_none = [a for a in args if a is not type(None)]
    assert non_none == [BigInt], (
        f"Expected SUM(quantity) to emit BigInt; got {non_none!r}"
    )

    total_field = next(
        f for f in dataclasses.fields(sum_fields_type)
        if f.name == "total"
    )
    total_args = typing.get_args(total_field.type)
    total_non_none = [a for a in total_args if a is not type(None)]
    assert total_non_none == [Decimal], (
        f"Expected SUM(total) to emit Decimal; got {total_non_none!r}"
    )


def test_sdl_declares_bigint_scalar_for_int_sum(db):
    """The emitted SDL must declare ``BigInt`` and use it for
    ``OrderSumFields.quantity``.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer"],
    ).build()

    @strawberry.type
    class Query:
        order_aggregate: built.aggregate_type = built.aggregate_field

    sdl = strawberry.Schema(query=Query).as_str()
    assert "scalar BigInt" in sdl, sdl
    # Find the SumFields block and assert quantity: BigInt.
    assert "type OrderSumFields {" in sdl
    block_start = sdl.index("type OrderSumFields {")
    block_end = sdl.index("}", block_start)
    block = sdl[block_start:block_end]
    assert "quantity: BigInt" in block, block
    assert "total: Decimal" in block, block


def test_compute_aggregation_returns_python_int_above_2_31(db):
    """``compute_aggregation`` returns native Python ``int`` for
    integer-field SUM. Three rows of ``quantity = 2_000_000_000`` sum
    to 6_000_000_000, comfortably above 2**31 - 1 = 2_147_483_647 but
    well within ``bigint`` range. The string-encoding behavior is at
    the Strawberry scalar layer, NOT at the compiler primitive.
    """
    from tests.models import Customer, Order

    customer = Customer.objects.create(name="LargeQty")
    tz = datetime.UTC
    for i in range(3):
        Order.objects.create(
            customer=customer,
            status="paid",
            total=Decimal("100.00"),
            quantity=2_000_000_000,
            is_priority=False,
            created_at=datetime.datetime(2026, 6, i + 1, tzinfo=tz),
        )

    rows = compute_aggregation(
        Order.objects.filter(customer=customer),
        aggregates=[(AggregateOp.SUM, "quantity")],
    )
    assert len(rows) == 1
    sum_quantity = rows[0]["sum_quantity"]
    assert sum_quantity == 6_000_000_000
    # Above 32-bit signed int max — would have overflowed `Int`.
    assert sum_quantity > 2**31 - 1
    # Still a Python int at the compiler layer; serialization to
    # string is the Strawberry scalar's job (verified separately).
    assert isinstance(sum_quantity, int)


def test_bigint_serializes_as_string_over_graphql(db):
    """End-to-end: SUM(quantity) above 2**31 returns as a JSON string
    when queried via the GraphQL schema. The custom scalar's
    ``serialize=str`` callable converts the Python int.
    """
    from tests.models import Customer, Order

    customer = Customer.objects.create(name="HugeQty")
    tz = datetime.UTC
    for i in range(3):
        Order.objects.create(
            customer=customer,
            status="paid",
            total=Decimal("10.00"),
            quantity=2_000_000_000,
            is_priority=False,
            created_at=datetime.datetime(2026, 7, i + 1, tzinfo=tz),
        )

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer"],
    ).build()

    @strawberry.type
    class Query:
        order_aggregate: built.aggregate_type = built.aggregate_field

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync(
        """
        query {
            orderAggregate {
                count
                sum { quantity }
            }
        }
        """,
    )
    assert result.errors is None, result.errors
    payload = result.data["orderAggregate"]
    assert payload["count"] == 3
    # BigInt serializes as string on the wire — the custom scalar's
    # raison d'être (JS Number safe range stops at 2**53).
    assert payload["sum"]["quantity"] == "6000000000"
    assert isinstance(payload["sum"]["quantity"], str)
    # Round-trips back to the original Python int.
    assert int(payload["sum"]["quantity"]) == 6_000_000_000


def test_bigint_exported_from_package_root(db):
    """``BigInt`` is part of the public SemVer surface. Per SPEC § 5
    Stream 2 — clients import the scalar to type-annotate their own
    custom resolvers when they need BigInt-shaped fields outside the
    aggregate pipeline.
    """
    import strawberry_django_aggregates as pkg

    assert "BigInt" in pkg.__all__
    assert pkg.BigInt is BigInt
    # Sanity: the wrapper carries a ScalarDefinition we can inspect.
    definition = pkg.BigInt._scalar_definition
    assert definition.name == "BigInt"
    assert definition.serialize is str
    assert definition.parse_value is int
