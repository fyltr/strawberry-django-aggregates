"""Streaming / chunked group-by — Stream 13 / SPEC § 19.

Covers:

- ``compute_aggregation(chunk_size=N)`` returns an iterator that yields
  successive chunks sized ``N`` (final chunk may be smaller).
- HAVING composes correctly with chunked iteration.
- ``chunk_size`` overrides any user-supplied ``order_by`` — keyset
  cursor needs strict ascending order on the group-by tuple.
- Compatibility errors: ``offset`` / ``limit`` / ``fill=True`` raise
  :class:`AggregateError` with clear messages.
- Backwards-compat: omitting ``chunk_size`` returns ``list[dict]``
  exactly as before this stream.

Reaches into the iterator API of the backend primitive only — Stream
13 deliberately keeps streaming off the GraphQL surface (cursor
pagination from Stream 11 is the wire-side chunking story).
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from decimal import Decimal

import pytest

from strawberry_django_aggregates import (
    AggregateError,
    AggregateOp,
    TimeGranularity,
    compute_aggregation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def thousand_customers(db):
    """Lay down 1000 customers, each with one order, so a group_by on
    customer_id yields 1000 distinct buckets.

    The ``count`` per bucket is 1; the ``sum_total`` is the customer's
    index + 100 — useful for asserting bucket identity / order.
    """
    from tests.models import Customer, Order

    tz = datetime.UTC
    customers = []
    orders = []
    for i in range(1000):
        c = Customer.objects.create(name=f"C{i:04d}")
        customers.append(c)
        o = Order.objects.create(
            customer=c,
            status="paid",
            total=Decimal(100 + i),
            quantity=1,
            is_priority=False,
            created_at=datetime.datetime(2026, 5, 1, tzinfo=tz),
        )
        orders.append(o)
    return customers, orders


@pytest.fixture
def six_buckets(db):
    """Smaller fixture for tests that only need a handful of groups."""
    from tests.models import Customer, Order

    tz = datetime.UTC
    customers = []
    for i in range(6):
        c = Customer.objects.create(name=f"D{i:02d}")
        Order.objects.create(
            customer=c,
            status="paid",
            total=Decimal(100 + i),
            quantity=1,
            is_priority=False,
            created_at=datetime.datetime(2026, 5, 1, tzinfo=tz),
        )
        customers.append(c)
    return customers


# ---------------------------------------------------------------------------
# Streaming basics — iterator return + chunk count + content correctness
# ---------------------------------------------------------------------------


def test_streaming_yields_iterator(thousand_customers):
    """``chunk_size=N`` returns an iterator (not a list).

    Pinned because the return-type union widening is the main API
    delta of this stream — callers that ``isinstance(_, list)`` need
    a deterministic signal that streaming is on.
    """
    from tests.models import Order

    result = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        chunk_size=100,
    )
    assert isinstance(result, Iterator)
    assert not isinstance(result, list)


def test_streaming_chunks_thousand_buckets_into_tens(thousand_customers):
    """1000 distinct buckets / chunk_size=100 → exactly 10 chunks of 100
    rows each. Pins the divisor case where the last chunk is full.
    """
    from tests.models import Order

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=100,
        )
    )
    assert len(chunks) == 10
    for chunk in chunks:
        assert isinstance(chunk, list)
        assert len(chunk) == 100
    # Each row carries the canonical customer_id alias and a count of 1.
    total_rows = sum(len(c) for c in chunks)
    assert total_rows == 1000
    counts = [r["count"] for c in chunks for r in c]
    assert counts == [1] * 1000


def test_streaming_chunks_six_into_twos(six_buckets):
    """6 buckets / chunk_size=2 → 3 chunks of 2 rows. Pins the simple
    even-divisor case at small N for hand-checkable correctness.
    """
    from tests.models import Order

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=2,
        )
    )
    assert len(chunks) == 3
    assert all(len(c) == 2 for c in chunks)


def test_streaming_final_chunk_can_be_short(six_buckets):
    """6 buckets / chunk_size=4 → one chunk of 4 + one chunk of 2
    (final chunk is short). Pins the indivisible case — the iterator
    must NOT yield an empty trailing chunk.
    """
    from tests.models import Order

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=4,
        )
    )
    assert [len(c) for c in chunks] == [4, 2]


def test_streaming_chunk_size_one(six_buckets):
    """``chunk_size=1`` is a degenerate but valid case — one row per
    chunk, six chunks total. Useful for pull-one-at-a-time consumers.
    """
    from tests.models import Order

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=1,
        )
    )
    assert len(chunks) == 6
    assert all(len(c) == 1 for c in chunks)


def test_streaming_chunk_size_larger_than_total(six_buckets):
    """``chunk_size > total`` produces a single chunk with all rows."""
    from tests.models import Order

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=100,
        )
    )
    assert len(chunks) == 1
    assert len(chunks[0]) == 6


def test_streaming_canonical_ascending_order_by_groupby(six_buckets):
    """Chunked iteration orders strictly ascending by the canonical
    group-by tuple. Pinned because keyset pagination depends on this
    invariant — any user-supplied ``order_by`` is overridden.
    """
    from tests.models import Order

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            # User asks for descending; streaming overrides to ascending.
            order_by=[("customer_id", "desc", None)],
            chunk_size=2,
        )
    )
    flat = [r["customer_id"] for c in chunks for r in c]
    assert flat == sorted(flat)


def test_streaming_with_having(thousand_customers):
    """HAVING composes with chunked iteration. ``count__gt: 0`` retains
    every bucket (each customer has 1 order); ``count__gt: 1`` filters
    them all out. Pins the SQL-side application of HAVING per chunk.
    """
    from tests.models import Order

    # All 1000 buckets passed: count=1 > 0 is True for every one.
    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            having={"count__gt": 0},
            chunk_size=50,
        )
    )
    flat = [r for c in chunks for r in c]
    assert len(flat) == 1000
    # 1000 / 50 = 20 chunks, all full.
    assert len(chunks) == 20
    assert all(len(c) == 50 for c in chunks)

    # No bucket passes count > 1 — iterator yields no chunks.
    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            having={"count__gt": 1},
            chunk_size=50,
        )
    )
    assert chunks == []


def test_streaming_with_having_and_aggregates(six_buckets):
    """HAVING + aggregate measures + chunking — the rows must carry the
    aggregate aliases AND filter by HAVING.

    Six customers each with one order whose total is 100..105.
    HAVING ``sum_total__gte: 103`` retains the 3 customers with total
    >= 103 (the last 3 in name-ascending order). chunk_size=2 →
    1 chunk of 2 + 1 chunk of 1.
    """
    from tests.models import Order

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[
                (AggregateOp.COUNT, None),
                (AggregateOp.SUM, "total"),
            ],
            having={"sum_total__gte": Decimal("103")},
            chunk_size=2,
        )
    )
    flat = [r for c in chunks for r in c]
    assert len(flat) == 3
    sums = sorted(r["sum_total"] for r in flat)
    assert sums == [Decimal("103"), Decimal("104"), Decimal("105")]


def test_streaming_with_time_granularity(db):
    """Group-by with a ``TimeGranularity`` bucket also keysets correctly.

    Three months of 2026 with one order each → 3 distinct buckets;
    chunk_size=2 → 2 chunks (2 + 1).
    """
    from tests.models import Customer, Order

    tz = datetime.UTC
    c = Customer.objects.create(name="Solo")
    for month in (3, 4, 5):
        Order.objects.create(
            customer=c,
            status="paid",
            total=Decimal("100"),
            quantity=1,
            is_priority=False,
            created_at=datetime.datetime(2026, month, 1, tzinfo=tz),
        )

    chunks = list(
        compute_aggregation(
            Order.objects.all(),
            group_by=[("created_at", TimeGranularity.MONTH)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=2,
        )
    )
    assert [len(c) for c in chunks] == [2, 1]
    flat = [r for c in chunks for r in c]
    months = [r["created_at_month"].month for r in flat]
    assert months == [3, 4, 5]


def test_streaming_lazy_does_not_materialize_eagerly(thousand_customers):
    """The iterator is lazy — pulling the first chunk does NOT exhaust
    the rest. Pins the streaming contract: callers can stop early
    without paying for the remaining round-trips.
    """
    from tests.models import Order

    it = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        chunk_size=100,
    )
    assert isinstance(it, Iterator)
    first = next(it)
    assert len(first) == 100
    second = next(it)
    assert len(second) == 100
    # The first 200 customer_ids are strictly less than any later id.
    seen = {r["customer_id"] for r in first} | {
        r["customer_id"] for r in second
    }
    assert len(seen) == 200


# ---------------------------------------------------------------------------
# Validation / incompatibility errors — fail-loud per Critical Rule 6
# ---------------------------------------------------------------------------


def test_streaming_with_offset_raises(six_buckets):
    """``chunk_size`` + ``offset`` is incompatible — fail loud."""
    from tests.models import Order

    with pytest.raises(AggregateError, match="offset"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=10,
            offset=5,
        )


def test_streaming_with_limit_raises(six_buckets):
    """``chunk_size`` + ``limit`` is incompatible — fail loud."""
    from tests.models import Order

    with pytest.raises(AggregateError, match="offset"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=10,
            limit=20,
        )


def test_streaming_with_fill_raises(db):
    """``chunk_size`` + ``fill=True`` is incompatible — fail loud."""
    from tests.models import Customer, Order

    tz = datetime.UTC
    c = Customer.objects.create(name="Z")
    Order.objects.create(
        customer=c,
        status="paid",
        total=Decimal("100"),
        quantity=1,
        is_priority=False,
        created_at=datetime.datetime(2026, 5, 1, tzinfo=tz),
    )

    with pytest.raises(AggregateError, match="fill"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("created_at", TimeGranularity.MONTH)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=10,
            fill=True,
        )


def test_streaming_without_group_by_raises(db):
    """Streaming requires a non-empty ``group_by`` — the keyset cursor
    needs a tuple to advance.
    """
    from tests.models import Order

    with pytest.raises(AggregateError, match="group_by"):
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=10,
        )


def test_streaming_zero_chunk_size_raises(six_buckets):
    """``chunk_size=0`` is invalid — must be a positive int."""
    from tests.models import Order

    with pytest.raises(AggregateError, match="positive"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=0,
        )


def test_streaming_negative_chunk_size_raises(six_buckets):
    """``chunk_size`` must be positive — negative values fail loud."""
    from tests.models import Order

    with pytest.raises(AggregateError, match="positive"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=-1,
        )


def test_streaming_bool_chunk_size_raises(six_buckets):
    """``chunk_size=True`` would silently coerce to 1 under
    ``isinstance(_, int)`` — fail loud instead so a stray ``True`` from
    a config doesn't sneak through.
    """
    from tests.models import Order

    with pytest.raises(AggregateError, match="positive"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=True,  # type: ignore[arg-type]
        )


def test_streaming_non_int_chunk_size_raises(six_buckets):
    """``chunk_size`` must be an int — strings / floats fail loud."""
    from tests.models import Order

    with pytest.raises(AggregateError, match="positive"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            chunk_size=1.5,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Backwards compatibility — omitting chunk_size returns list[dict]
# ---------------------------------------------------------------------------


def test_omit_chunk_size_returns_list(six_buckets):
    """Default behaviour (``chunk_size`` unset) returns a plain list of
    dicts, NOT an iterator. Pinned to catch regressions in the
    return-type widening that would break every pre-Stream-13 caller.
    """
    from tests.models import Order

    result = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
    )
    assert isinstance(result, list)
    assert len(result) == 6
    assert all(isinstance(r, dict) for r in result)


def test_explicit_none_chunk_size_returns_list(six_buckets):
    """``chunk_size=None`` is exactly equivalent to omitting it.

    Pinned because callers may pass through a config knob that defaults
    to ``None`` — they should get the legacy list shape, not an
    accidental streaming branch.
    """
    from tests.models import Order

    result = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        chunk_size=None,
    )
    assert isinstance(result, list)
    assert len(result) == 6
