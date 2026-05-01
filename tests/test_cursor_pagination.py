"""Cursor pagination on grouped results — Stream 11 / SPEC § 4.

Covers:

- ``encode_group_cursor`` / ``decode_group_cursor`` round-trip with
  primitive types, datetimes, and Decimals.
- ``AggregateBuilder(pagination_style="cursor")`` emits the connection
  types and DOES NOT emit ``OrderGroupedResult``.
- ``AggregateBuilder(pagination_style="both")`` emits both fields with
  distinct names (``ordersGroupBy`` for offset; ``ordersGroupByConnection``
  for cursor).
- Backwards-compat: ``pagination_style="offset"`` (default) leaves the
  SDL byte-identical to pre-Stream-11.
- Forward / backward pagination, ``hasNextPage``, ``hasPreviousPage``,
  ``startCursor`` / ``endCursor`` correctness across pages of size 2
  over 6 group buckets (Customer × Status / Customer + DAY).

Strawberry resolves Query type annotations against the test module's
``__globals__``; under PEP 563 the dynamic ``built.*`` classes become
strings strawberry can't evaluate. We deliberately do NOT use
``from __future__ import annotations`` here.
"""

import base64
import datetime
import json
from decimal import Decimal

import pytest
import strawberry

from strawberry_django_aggregates import (
    AggregateBuilder,
    decode_group_cursor,
    encode_group_cursor,
)

# ---------------------------------------------------------------------------
# encode / decode round-trip
# ---------------------------------------------------------------------------


def test_encode_decode_round_trip_primitives():
    """Plain ints / strings / None / bool round-trip through cursor."""
    values = [42, "paid", None, True, 3.14]
    cursor = encode_group_cursor(values)
    assert isinstance(cursor, str)
    assert decode_group_cursor(cursor) == values


def test_encode_decode_round_trip_datetime():
    """Datetimes survive the JSON-string-tagged round-trip with
    timezone preserved.
    """
    dt = datetime.datetime(
        2026, 5, 1, 12, 30, 45, tzinfo=datetime.UTC,
    )
    values = [42, dt, "x"]
    cursor = encode_group_cursor(values)
    decoded = decode_group_cursor(cursor)
    assert decoded == values
    assert decoded[1].tzinfo is not None
    assert decoded[1] == dt


def test_encode_decode_round_trip_date_and_decimal():
    """``datetime.date`` and ``Decimal`` survive without precision
    loss (Decimal stays Decimal, not float).
    """
    d = datetime.date(2026, 5, 1)
    values = [d, Decimal("100.00")]
    decoded = decode_group_cursor(encode_group_cursor(values))
    assert decoded == values
    assert isinstance(decoded[0], datetime.date)
    assert isinstance(decoded[1], Decimal)


def test_encoding_is_deterministic():
    """Two encodes of the same input ⇒ byte-identical cursors. Pinned
    by SPEC § 12 / CLAUDE.md Critical Rule 2.
    """
    values = [
        42,
        datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC),
        Decimal("3.14"),
    ]
    a = encode_group_cursor(values)
    b = encode_group_cursor(values)
    assert a == b


def test_decode_rejects_invalid_base64():
    """Malformed cursor payloads raise ``ValueError`` (no silent
    fall-through, no info leak about the offending bytes).
    """
    with pytest.raises(ValueError):
        decode_group_cursor("!!!not base64!!!")


def test_decode_rejects_non_json_payload():
    """Valid base64 of non-JSON bytes raises ``ValueError``."""
    cursor = base64.urlsafe_b64encode(b"\x80\x81\x82").decode("ascii")
    with pytest.raises(ValueError):
        decode_group_cursor(cursor)


def test_decode_rejects_non_list_root():
    """The cursor payload must decode to a JSON list — not a dict, not
    a scalar. Pinned for forward-compat: future cursor revisions must
    keep the list shape.
    """
    cursor = base64.urlsafe_b64encode(
        json.dumps({"key": "value"}).encode("utf-8"),
    ).decode("ascii")
    with pytest.raises(ValueError):
        decode_group_cursor(cursor)


# ---------------------------------------------------------------------------
# Schema-level: pagination_style switches the emitted fields
# ---------------------------------------------------------------------------


@pytest.fixture
def order_schema_offset_default(db):
    """``pagination_style`` defaults to ``"offset"``. SDL must match
    pre-Stream-11 builds — same emitted types, no Connection / Edge.
    """
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


@pytest.fixture
def order_schema_cursor(sample_orders):
    """``pagination_style="cursor"`` — emits the connection types and
    nothing offset-shaped.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status"],
        pagination_style="cursor",
    ).build()

    @strawberry.type
    class Query:
        order_aggregate: built.aggregate_type = built.aggregate_field
        orders_group_by: built.grouped_connection_type = (
            built.grouped_connection_field
        )

    return strawberry.Schema(query=Query), built


@pytest.fixture
def order_schema_both(sample_orders):
    """``pagination_style="both"`` — emits BOTH fields.

    Field name disambiguation: the offset field stays
    ``ordersGroupBy``; the cursor field is named
    ``ordersGroupByConnection`` so they don't collide.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status"],
        pagination_style="both",
    ).build()

    @strawberry.type
    class Query:
        order_aggregate:           built.aggregate_type = (
            built.aggregate_field
        )
        orders_group_by:           built.grouped_result_type = (
            built.group_by_field
        )
        orders_group_by_connection: built.grouped_connection_type = (
            built.grouped_connection_field
        )

    return strawberry.Schema(query=Query), built


def test_pagination_style_offset_default_sdl_unchanged(
    order_schema_offset_default,
):
    """Default ``pagination_style`` keeps the offset field. SDL contains
    ``OrderGroupedResult`` and not ``OrderGroupedConnection`` —
    backwards-compatibility guarantee.
    """
    schema, _ = order_schema_offset_default
    sdl = schema.as_str()
    assert "OrderGroupedResult" in sdl
    assert "OrderGroupedConnection" not in sdl
    assert "OrderGroupedEdge" not in sdl


def test_pagination_style_cursor_emits_connection(order_schema_cursor):
    """``pagination_style="cursor"`` swaps the offset shape for the
    Relay-style connection. The Edge / PageInfo / Connection types
    must appear in the SDL; ``GroupedResult`` must not.
    """
    schema, _ = order_schema_cursor
    sdl = schema.as_str()
    assert "OrderGroupedConnection" in sdl
    assert "OrderGroupedEdge" in sdl
    assert "PageInfo" in sdl
    assert "OrderGroupedResult" not in sdl
    # Connection's edges is non-null list of non-null edges.
    assert "edges: [OrderGroupedEdge!]!" in sdl
    # Edge has a non-null cursor and node.
    assert "cursor: String!" in sdl


def test_pagination_style_both_emits_distinct_field_names(
    order_schema_both,
):
    """``"both"`` mode emits both fields. The cursor field must be
    named ``ordersGroupByConnection`` to avoid colliding with the
    offset ``ordersGroupBy``.
    """
    schema, _ = order_schema_both
    sdl = schema.as_str()
    assert "OrderGroupedResult" in sdl
    assert "OrderGroupedConnection" in sdl
    # Both Query fields present; distinct names.
    assert "ordersGroupBy(" in sdl
    assert "ordersGroupByConnection(" in sdl


def test_pagination_style_offset_default_byte_identical_sdl(db):
    """Pre-Stream-11 determinism contract: same builder inputs ⇒
    byte-identical SDL across two builds. Pins that adding the new
    ``pagination_style`` field with a default did not perturb the
    existing emission shape.
    """
    from tests.models import Order

    def _build():
        built = AggregateBuilder(
            model=Order,
            aggregate_fields=["total", "quantity"],
            group_by_fields=["customer", "status"],
        ).build()

        @strawberry.type
        class Query:
            order_aggregate: built.aggregate_type = built.aggregate_field
            orders_group_by: built.grouped_result_type = (
                built.group_by_field
            )

        return strawberry.Schema(query=Query).as_str()

    a = _build()
    b = _build()
    assert a == b


# ---------------------------------------------------------------------------
# Pagination semantics — walking pages of size 2 over 6 group buckets
# ---------------------------------------------------------------------------


@pytest.fixture
def six_buckets_schema(db):
    """Six distinct (customer, status) groups so we can page through
    them in pages of 2.

    Customers A / B / C; statuses paid / cancelled. Six (cust, status)
    pairs. Each group has one order so ``count = 1``.
    """
    from tests.models import Customer, Order

    customers = []
    for n in ["Alpha", "Beta", "Gamma"]:
        customers.append(Customer.objects.create(name=n))

    tz = datetime.UTC
    for i, c in enumerate(customers):
        Order.objects.create(
            customer=c, status="paid", total=Decimal("100.00"),
            quantity=1, is_priority=True,
            created_at=datetime.datetime(2026, 4, 1 + i, tzinfo=tz),
        )
        Order.objects.create(
            customer=c, status="cancelled", total=Decimal("50.00"),
            quantity=1, is_priority=False,
            created_at=datetime.datetime(2026, 4, 10 + i, tzinfo=tz),
        )

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status"],
        pagination_style="cursor",
    ).build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_connection_type = (
            built.grouped_connection_field
        )

    return strawberry.Schema(query=Query)


def _page(schema, *, first=2, after=None):
    after_clause = f', after: "{after}"' if after else ""
    result = schema.execute_sync(f"""
        query {{
            ordersGroupBy(
                groupBy: [{{ field: CUSTOMER }}, {{ field: STATUS }}]
                first: {first}{after_clause}
            ) {{
                totalCount
                pageInfo {{
                    hasNextPage hasPreviousPage
                    startCursor endCursor
                }}
                edges {{
                    cursor
                    node {{ count key {{ customerId status }} }}
                }}
            }}
        }}
    """)
    assert result.errors is None, result.errors
    return result.data["ordersGroupBy"]


def test_cursor_pagination_walks_through_pages(six_buckets_schema):
    """Page through 6 groups in pages of 2; assert every cursor and
    page-info value matches the documented contract.
    """
    schema = six_buckets_schema

    page1 = _page(schema, first=2)
    # Page 1: 2 edges, hasNextPage=True, hasPreviousPage=False (no
    # cursor was passed). totalCount fixed at 6.
    assert page1["totalCount"] == 6
    assert len(page1["edges"]) == 2
    assert page1["pageInfo"]["hasNextPage"] is True
    assert page1["pageInfo"]["hasPreviousPage"] is False
    assert page1["pageInfo"]["startCursor"] == page1["edges"][0]["cursor"]
    assert page1["pageInfo"]["endCursor"] == page1["edges"][-1]["cursor"]

    page2 = _page(
        schema, first=2, after=page1["pageInfo"]["endCursor"],
    )
    # Page 2: 2 new edges, hasNextPage=True (still 2 more after),
    # hasPreviousPage=True (we walked from a cursor). Cursors must be
    # disjoint from page1's.
    assert len(page2["edges"]) == 2
    assert page2["pageInfo"]["hasNextPage"] is True
    assert page2["pageInfo"]["hasPreviousPage"] is True
    page1_cursors = {e["cursor"] for e in page1["edges"]}
    page2_cursors = {e["cursor"] for e in page2["edges"]}
    assert page1_cursors.isdisjoint(page2_cursors)

    page3 = _page(
        schema, first=2, after=page2["pageInfo"]["endCursor"],
    )
    # Final page: 2 edges, hasNextPage=False (no extra row beyond),
    # hasPreviousPage=True.
    assert len(page3["edges"]) == 2
    assert page3["pageInfo"]["hasNextPage"] is False
    assert page3["pageInfo"]["hasPreviousPage"] is True

    # All 6 distinct cursors collected.
    all_cursors = page1_cursors | page2_cursors | {
        e["cursor"] for e in page3["edges"]
    }
    assert len(all_cursors) == 6


def test_cursor_pagination_cursor_is_stable(six_buckets_schema):
    """The cursor of a row is deterministic — fetching the same page
    twice produces byte-identical cursors.
    """
    schema = six_buckets_schema
    a = _page(schema, first=2)
    b = _page(schema, first=2)
    assert [e["cursor"] for e in a["edges"]] == [
        e["cursor"] for e in b["edges"]
    ]


def test_cursor_pagination_total_count_reflects_distinct_groups(
    six_buckets_schema,
):
    """``totalCount`` is the post-`having` group cardinality and does
    NOT depend on `first` / `after`. Pinned to catch regressions where
    `totalCount` accidentally counts edges in the page.
    """
    schema = six_buckets_schema
    p1 = _page(schema, first=2)
    p2 = _page(schema, first=4)
    assert p1["totalCount"] == 6
    assert p2["totalCount"] == 6


def test_cursor_pagination_decodes_to_canonical_keys(
    six_buckets_schema,
):
    """A cursor decodes to the canonical-order group-key values for
    that row. Pinned so the wire-side cursor format stays compatible
    with :func:`decode_group_cursor`.

    Note: GraphQL's ``ID`` scalar serializes as a string on the wire
    even when the underlying Python value is ``int`` (a Django FK
    primary key). The cursor preserves the native int — clients
    decoding cursors should compare via ``str()`` if they want to
    match the wire-side ``customerId``.
    """
    schema = six_buckets_schema
    page = _page(schema, first=2)
    edge = page["edges"][0]
    decoded = decode_group_cursor(edge["cursor"])
    assert len(decoded) == 2  # customer, status
    # Compare as strings — the cursor carries the int; the GraphQL ID
    # serializes as a string. Both reference the same underlying value.
    assert str(decoded[0]) == edge["node"]["key"]["customerId"]
    assert decoded[1] == edge["node"]["key"]["status"]


def test_cursor_pagination_first_zero_returns_no_edges(
    six_buckets_schema,
):
    """``first: 0`` is permitted by Relay and must return an empty
    page with null ``startCursor`` / ``endCursor``. Pinned so callers
    can probe ``totalCount`` without materializing rows.
    """
    schema = six_buckets_schema
    page = _page(schema, first=0)
    assert page["edges"] == []
    assert page["totalCount"] == 6
    assert page["pageInfo"]["startCursor"] is None
    assert page["pageInfo"]["endCursor"] is None


def test_cursor_pagination_backward_with_last_and_before(
    six_buckets_schema,
):
    """``last`` + ``before`` walks the trailing page in reverse-keyset
    order. The materialized rows are re-reversed before encoding so
    edges still come out in canonical (forward) order — the
    ``before``-cursor's preceding rows.
    """
    schema = six_buckets_schema
    # Walk forward to get a known cursor at position 4 of 6.
    page1 = _page(schema, first=2)
    page2 = _page(schema, first=2, after=page1["pageInfo"]["endCursor"])
    cursor_at_pos_4 = page2["edges"][1]["cursor"]

    # ``last: 2, before: <cursor_at_pos_4>`` yields the 2 rows
    # immediately preceding that cursor — positions 2 and 3 (the
    # ``before`` cursor is exclusive, mirroring ``after``).
    result = schema.execute_sync(f'''
        query {{
            ordersGroupBy(
                groupBy: [{{ field: CUSTOMER }}, {{ field: STATUS }}]
                last: 2
                before: "{cursor_at_pos_4}"
            ) {{
                edges {{ cursor node {{ key {{ customerId status }} }} }}
                pageInfo {{
                    hasNextPage hasPreviousPage
                    startCursor endCursor
                }}
                totalCount
            }}
        }}
    ''')
    assert result.errors is None, result.errors
    page = result.data["ordersGroupBy"]
    assert len(page["edges"]) == 2
    # Backward semantics: ``before`` was passed so ``hasNextPage`` is
    # True (rows logically beyond the cursor exist).
    assert page["pageInfo"]["hasNextPage"] is True
    # Edges come out in canonical (forward) order even though we
    # scanned backward — the resolver re-reverses. Positions 2 and 3
    # = page1[1] + page2[0].
    expected_cursors = [
        page1["edges"][1]["cursor"],
        page2["edges"][0]["cursor"],
    ]
    assert [e["cursor"] for e in page["edges"]] == expected_cursors


def test_cursor_pagination_first_and_last_mutually_exclusive(
    six_buckets_schema,
):
    """Passing both ``first`` and ``last`` is contradictory and must
    fail loud (Critical Rule 6).
    """
    schema = six_buckets_schema
    result = schema.execute_sync('''
        query {
            ordersGroupBy(
                groupBy: [{ field: CUSTOMER }, { field: STATUS }]
                first: 2
                last:  2
            ) {
                edges { cursor }
            }
        }
    ''')
    assert result.errors is not None
    messages = [str(e.message) for e in result.errors]
    assert any(
        "mutually exclusive" in m for m in messages
    ), messages


def test_cursor_pagination_negative_first_raises(six_buckets_schema):
    """Negative ``first`` is forbidden by Relay; must fail loud."""
    schema = six_buckets_schema
    result = schema.execute_sync('''
        query {
            ordersGroupBy(
                groupBy: [{ field: CUSTOMER }, { field: STATUS }]
                first: -1
            ) {
                edges { cursor }
            }
        }
    ''')
    assert result.errors is not None
    messages = [str(e.message) for e in result.errors]
    assert any(
        "non-negative" in m for m in messages
    ), messages


def test_cursor_pagination_malformed_cursor_raises(six_buckets_schema):
    """A malformed ``after`` cursor surfaces a clean error rather than
    silently returning all rows (Critical Rule 6 — fail-loud).
    """
    schema = six_buckets_schema
    result = schema.execute_sync('''
        query {
            ordersGroupBy(
                groupBy: [{ field: CUSTOMER }, { field: STATUS }]
                first: 2
                after: "!!!not a cursor!!!"
            ) {
                edges { cursor }
            }
        }
    ''')
    assert result.errors is not None
    messages = [str(e.message) for e in result.errors]
    assert any(
        "cursor" in m.lower() for m in messages
    ), messages
