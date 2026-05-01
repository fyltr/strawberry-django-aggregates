"""Stream 6 — locale-aware ``week_start``.

Three layers of coverage:

1. Unit tests for :func:`validate_week_start` — accepts ints in
   ``[1, 7]``; rejects everything else (including ``bool`` so a
   stray ``True`` doesn't silently pass as ``1``).
2. SQL-level tests through :func:`compute_aggregation` — the same
   fixture row bucketed with ``week_start=1`` (Mon-start) vs
   ``week_start=7`` (Sun-start) yields shifted boundaries; the
   ``DAY_OF_WEEK`` numeric extraction rotates as documented.
3. ``bucket_range`` integration — the helper accepts ``week_start``
   for symmetry with ``compute_aggregation``; the shifted boundary
   is the value already returned by SQL.

Plus a backward-compatibility check: omitting ``week_start`` (or
passing the ISO default ``1``) produces the same data as before
this stream.

No ``from __future__ import annotations`` — strawberry resolves
field-type annotations on the Query class against ``__globals__``
and PEP 563 turns ``built.aggregate_type`` into an unevaluable
string. Mirrors the pattern in ``test_bucket_range.py``.
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import strawberry

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    NumberGranularity,
    TimeGranularity,
    bucket_range,
    compute_aggregation,
    validate_week_start,
)

UTC = datetime.UTC


# ---------------------------------------------------------------------------
# 1 · validate_week_start unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 2, 3, 4, 5, 6, 7])
def test_validate_week_start_accepts_in_range(value: int) -> None:
    assert validate_week_start(value) == value


@pytest.mark.parametrize(
    "value",
    [0, -1, 8, 100, -100],
)
def test_validate_week_start_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError, match="week_start"):
        validate_week_start(value)


@pytest.mark.parametrize(
    "value",
    ["1", 1.0, None, [], {}, object()],
)
def test_validate_week_start_rejects_wrong_type(value) -> None:
    with pytest.raises(ValueError, match="week_start"):
        validate_week_start(value)


def test_validate_week_start_rejects_bool() -> None:
    """``bool`` is an ``int`` subclass — explicitly reject so a stray
    ``True`` / ``False`` doesn't silently slip through as 1 / 0."""
    with pytest.raises(ValueError, match="week_start"):
        validate_week_start(True)
    with pytest.raises(ValueError, match="week_start"):
        validate_week_start(False)


# ---------------------------------------------------------------------------
# 2 · SQL-level shift through compute_aggregation
# ---------------------------------------------------------------------------


@pytest.fixture
def week_orders(db):
    """Three orders spanning a week boundary on Sunday May 3 2026.

    - Sat May 2 → ISO week starts Apr 27 (Mon); Sun-start week
      starts Apr 26.
    - Sun May 3 → ISO week Apr 27; Sun-start week May 3.
    - Mon May 4 → ISO week May 4; Sun-start week May 3.
    """
    from tests.models import Customer, Order

    c = Customer.objects.create(name="WeekUser")
    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=1,
        created_at=datetime.datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
    )
    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=1,
        created_at=datetime.datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    )
    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=1,
        created_at=datetime.datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )
    return c


@pytest.mark.django_db
def test_week_bucket_default_iso(week_orders) -> None:
    """``week_start`` omitted → ISO Monday-start (Django default)."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.filter(customer=week_orders),
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_week", "asc", None)],
    )
    # Sat + Sun both in week of Apr 27; Mon May 4 in week of May 4.
    assert len(rows) == 2
    by_week = {r["created_at_week"].date(): r["count"] for r in rows}
    assert by_week[datetime.date(2026, 4, 27)] == 2
    assert by_week[datetime.date(2026, 5, 4)] == 1


@pytest.mark.django_db
def test_week_bucket_explicit_iso_matches_default(week_orders) -> None:
    """``week_start=1`` is identical to omitting it."""
    from tests.models import Order

    qs = Order.objects.filter(customer=week_orders)
    rows_default = compute_aggregation(
        qs,
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_week", "asc", None)],
    )
    rows_iso = compute_aggregation(
        qs,
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_week", "asc", None)],
        week_start=1,
    )
    assert rows_default == rows_iso


@pytest.mark.django_db
def test_week_bucket_sunday_start_shifts_boundary(week_orders) -> None:
    """``week_start=7`` (Sunday-first) shifts boundaries one day
    earlier — Sun May 3 starts a new week, Sat May 2 is the previous
    one."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.filter(customer=week_orders),
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_week", "asc", None)],
        week_start=7,
    )
    # Sat May 2 alone in week of Apr 26 (Sun); Sun May 3 + Mon May 4
    # in week of May 3 (Sun).
    assert len(rows) == 2
    by_week = {r["created_at_week"].date(): r["count"] for r in rows}
    assert by_week[datetime.date(2026, 4, 26)] == 1
    assert by_week[datetime.date(2026, 5, 3)] == 2


@pytest.mark.django_db
def test_week_bucket_saturday_start(week_orders) -> None:
    """``week_start=6`` (Saturday-first) — exercise a non-default,
    non-Sunday choice. Sat May 2 starts the new week, Sun May 3 +
    Mon May 4 stay in the same week."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.filter(customer=week_orders),
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_week", "asc", None)],
        week_start=6,
    )
    assert len(rows) == 1
    by_week = {r["created_at_week"].date(): r["count"] for r in rows}
    assert by_week[datetime.date(2026, 5, 2)] == 3


@pytest.mark.django_db
def test_day_of_week_default_iso(week_orders) -> None:
    """Default DAY_OF_WEEK is ISO: Mon=1..Sun=7."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.filter(customer=week_orders),
        group_by=[("created_at", NumberGranularity.DAY_OF_WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_day_of_week", "asc", None)],
    )
    by_dow = {r["created_at_day_of_week"]: r["count"] for r in rows}
    assert by_dow == {1: 1, 6: 1, 7: 1}  # Mon=1, Sat=6, Sun=7


@pytest.mark.django_db
def test_day_of_week_sunday_start_rotation(week_orders) -> None:
    """``week_start=7`` rotates so Sun=1, Mon=2 .. Sat=7."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.filter(customer=week_orders),
        group_by=[("created_at", NumberGranularity.DAY_OF_WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_day_of_week", "asc", None)],
        week_start=7,
    )
    by_dow = {r["created_at_day_of_week"]: r["count"] for r in rows}
    # Sun May 3 → 1; Mon May 4 → 2; Sat May 2 → 7.
    assert by_dow == {1: 1, 2: 1, 7: 1}


@pytest.mark.django_db
def test_day_of_week_saturday_start_rotation(week_orders) -> None:
    """``week_start=6`` (Saturday-first) — Sat=1, Sun=2 .. Fri=7."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.filter(customer=week_orders),
        group_by=[("created_at", NumberGranularity.DAY_OF_WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_day_of_week", "asc", None)],
        week_start=6,
    )
    by_dow = {r["created_at_day_of_week"]: r["count"] for r in rows}
    # Sat May 2 → 1; Sun May 3 → 2; Mon May 4 → 3.
    assert by_dow == {1: 1, 2: 1, 3: 1}


@pytest.mark.django_db
def test_compute_aggregation_rejects_out_of_range_week_start(
    week_orders,
) -> None:
    from tests.models import Order

    with pytest.raises(ValueError, match="week_start"):
        compute_aggregation(
            Order.objects.filter(customer=week_orders),
            group_by=[("created_at", TimeGranularity.WEEK)],
            aggregates=[(AggregateOp.COUNT, None)],
            week_start=0,
        )
    with pytest.raises(ValueError, match="week_start"):
        compute_aggregation(
            Order.objects.filter(customer=week_orders),
            group_by=[("created_at", TimeGranularity.WEEK)],
            aggregates=[(AggregateOp.COUNT, None)],
            week_start=8,
        )


# ---------------------------------------------------------------------------
# 3 · bucket_range integration
# ---------------------------------------------------------------------------


def test_bucket_range_week_default_iso() -> None:
    """``week_start=1`` (default) — Mon-start week, +7 days."""
    v = datetime.datetime(2026, 5, 4, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.WEEK) == (
        datetime.datetime(2026, 5, 4, tzinfo=UTC),
        datetime.datetime(2026, 5, 11, tzinfo=UTC),
    )


def test_bucket_range_week_sunday_start() -> None:
    """``week_start=7`` (Sun-first) — May 3 (Sun) is a bucket start;
    +7 days = May 10 (next Sun)."""
    v = datetime.datetime(2026, 5, 3, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.WEEK, week_start=7) == (
        datetime.datetime(2026, 5, 3, tzinfo=UTC),
        datetime.datetime(2026, 5, 10, tzinfo=UTC),
    )


def test_bucket_range_week_validates_week_start() -> None:
    v = datetime.datetime(2026, 5, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="week_start"):
        bucket_range(v, TimeGranularity.WEEK, week_start=0)
    with pytest.raises(ValueError, match="week_start"):
        bucket_range(v, TimeGranularity.WEEK, week_start=8)


def test_bucket_range_non_week_ignores_week_start() -> None:
    """``week_start`` is validated for fail-loud feedback but does not
    affect non-WEEK granularities."""
    v = datetime.datetime(2026, 5, 1, tzinfo=UTC)
    assert (
        bucket_range(v, TimeGranularity.MONTH, week_start=7)
        == bucket_range(v, TimeGranularity.MONTH, week_start=1)
    )


# ---------------------------------------------------------------------------
# 4 · Cross-tz: tz="Asia/Tokyo" + week_start=7
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_week_start_with_tokyo_tz(db) -> None:
    """Combine ``tz="Asia/Tokyo"`` and ``week_start=7`` (Sun-first).

    Sat May 2 23:30 UTC → Sun May 3 08:30 Tokyo. With Sun-start week,
    that timestamp lands in the week of May 3 (Sun) in Tokyo — but in
    the week of Apr 26 (Sat) when bucketed in UTC.
    """
    from tests.models import Customer, Order

    c = Customer.objects.create(name="TokyoWeek")
    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=1,
        created_at=datetime.datetime(2026, 5, 2, 23, 30, tzinfo=UTC),
    )
    rows = compute_aggregation(
        Order.objects.filter(customer=c),
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        tz="Asia/Tokyo",
        week_start=7,
    )
    assert len(rows) == 1
    bucketed = rows[0]["created_at_week"]
    # Tokyo-local bucket boundary, Sunday-start week.
    assert bucketed.tzinfo is not None
    # The bucket value lands on Sunday May 3 in Tokyo.
    assert bucketed.date() == datetime.date(2026, 5, 3)


# ---------------------------------------------------------------------------
# 5 · Backward compat — existing fixture data unchanged
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_default_week_start_is_iso_for_sample_orders(sample_orders) -> None:
    """The shared ``sample_orders`` fixture produces the same rows
    with and without ``week_start=1`` — the default is ISO."""
    from tests.models import Order

    rows_default = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_week", "asc", None)],
    )
    rows_iso = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_week", "asc", None)],
        week_start=1,
    )
    assert rows_default == rows_iso


@pytest.mark.django_db
def test_default_day_of_week_is_iso_for_sample_orders(sample_orders) -> None:
    from tests.models import Order

    rows_default = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", NumberGranularity.DAY_OF_WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_day_of_week", "asc", None)],
    )
    rows_iso = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", NumberGranularity.DAY_OF_WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_day_of_week", "asc", None)],
        week_start=1,
    )
    assert rows_default == rows_iso


# ---------------------------------------------------------------------------
# 6 · GraphQL surface — weekStart resolver arg
# ---------------------------------------------------------------------------


@pytest.fixture
def schema_with_weeks(week_orders):
    """Schema variant including ``created_at`` in groupable fields."""
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status", "created_at"],
    ).build()

    @strawberry.type
    class Query:
        order_aggregate:  built.aggregate_type     = built.aggregate_field
        orders_group_by:  built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query), built


@pytest.mark.django_db
def test_graphql_week_start_default_iso(schema_with_weeks) -> None:
    schema, _ = schema_with_weeks
    result = schema.execute_sync("""
        query {
          ordersGroupBy(groupBy: [
            { field: CREATED_AT, granularity: WEEK }
          ]) {
            results {
              key { createdAtWeek createdAtWeekRange { from to } }
              count
            }
          }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    # Two ISO weeks: Apr 27 (covers Sat+Sun) and May 4 (covers Mon).
    weeks = sorted(r["key"]["createdAtWeek"][:10] for r in rows)
    assert weeks == ["2026-04-27", "2026-05-04"]


@pytest.mark.django_db
def test_graphql_week_start_sunday(schema_with_weeks) -> None:
    schema, _ = schema_with_weeks
    result = schema.execute_sync("""
        query {
          ordersGroupBy(
            groupBy: [{ field: CREATED_AT, granularity: WEEK }],
            weekStart: 7,
          ) {
            results {
              key { createdAtWeek createdAtWeekRange { from to } }
              count
            }
          }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    # Sun-start: Apr 26 (Sat alone) and May 3 (Sun + Mon).
    by_week = {
        r["key"]["createdAtWeek"][:10]: r["count"] for r in rows
    }
    assert by_week["2026-04-26"] == 1
    assert by_week["2026-05-03"] == 2
    # Range siblings honour the same shifted boundary: bucket of
    # May 3 spans [May 3, May 10).
    may_row = next(
        r for r in rows
        if r["key"]["createdAtWeek"].startswith("2026-05-03")
    )
    assert may_row["key"]["createdAtWeekRange"]["from"].startswith(
        "2026-05-03",
    )
    assert may_row["key"]["createdAtWeekRange"]["to"].startswith(
        "2026-05-10",
    )


@pytest.mark.django_db
def test_graphql_invalid_week_start_raises(schema_with_weeks) -> None:
    schema, _ = schema_with_weeks
    result = schema.execute_sync("""
        query {
          ordersGroupBy(
            groupBy: [{ field: CREATED_AT, granularity: WEEK }],
            weekStart: 8,
          ) {
            results { key { createdAtWeek } count }
          }
        }
    """)
    assert result.errors is not None
    assert any("week_start" in str(e) for e in result.errors)


# ---------------------------------------------------------------------------
# 7 · Determinism — week_start arg appears unconditionally
# ---------------------------------------------------------------------------


def test_sdl_contains_week_start_arg(db) -> None:
    """The grouped field accepts ``weekStart: Int`` regardless of any
    flag; the value passed at query time is what changes behaviour
    (CLAUDE.md Critical Rule 2 — emission stays stable)."""
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["customer", "created_at"],
    ).build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    sdl = strawberry.Schema(query=Query).as_str()
    assert "weekStart" in sdl


# Tokyo-time helper for cross-tz assertions.
def test_bucket_range_tokyo_sun_start_aligns() -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    v = datetime.datetime(2026, 5, 3, tzinfo=tokyo)  # Sun in Tokyo
    from_, to = bucket_range(v, TimeGranularity.WEEK, week_start=7)
    assert from_.tzinfo is tokyo
    assert to.tzinfo is tokyo
    assert from_.date() == datetime.date(2026, 5, 3)
    assert to.date() == datetime.date(2026, 5, 10)
