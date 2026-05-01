"""Stream 5 — half-open ``[from, to)`` ranges for TIME-granularity buckets.

Two layers of coverage:

1. Direct unit tests of :func:`bucket_range` for every TimeGranularity
   member, exercising the manual stdlib month / quarter / year
   arithmetic.
2. Integration through ``AggregateBuilder`` — bucket a fixture by
   month, query through GraphQL, assert each result row's
   ``createdAtMonthRange`` matches the expected boundaries.
3. Cross-tz: with ``tz="Asia/Tokyo"`` the bucket boundaries land on
   the local-tz midnight (May 1 in Tokyo, not UTC).
4. NUMBER-granularity SDL audit — ``createdAtDayOfWeek`` does NOT get
   a ``Range`` sibling (no contiguous interval).

Note: we deliberately do NOT use ``from __future__ import annotations``
here. Strawberry resolves field-type annotations on the Query class
against ``__globals__``; under PEP 563 the dynamic ``built.aggregate_type``
becomes a string strawberry cannot evaluate. Mirrors the pattern in
``test_builder_integration.py``.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest
import strawberry

from strawberry_django_aggregates import (
    AggregateBuilder,
    BucketRange,
    TimeGranularity,
    bucket_range,
)

# ---------------------------------------------------------------------------
# Direct unit tests — bucket_range over every TimeGranularity.
# ---------------------------------------------------------------------------

UTC = datetime.UTC


def test_bucket_range_year() -> None:
    v = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.YEAR) == (
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        datetime.datetime(2027, 1, 1, tzinfo=UTC),
    )


def test_bucket_range_quarter_q1() -> None:
    # Q1 — Jan/Feb/Mar bucket starts Jan 1, ends Apr 1.
    v = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.QUARTER) == (
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 1, tzinfo=UTC),
    )


def test_bucket_range_quarter_q4_crosses_year() -> None:
    # Q4 — Oct/Nov/Dec bucket starts Oct 1, ends Jan 1 next year.
    v = datetime.datetime(2026, 10, 1, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.QUARTER) == (
        datetime.datetime(2026, 10, 1, tzinfo=UTC),
        datetime.datetime(2027, 1, 1, tzinfo=UTC),
    )


def test_bucket_range_month() -> None:
    v = datetime.datetime(2026, 5, 1, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.MONTH) == (
        datetime.datetime(2026, 5, 1, tzinfo=UTC),
        datetime.datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_bucket_range_month_crosses_year() -> None:
    # December bucket → next is January next year.
    v = datetime.datetime(2026, 12, 1, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.MONTH) == (
        datetime.datetime(2026, 12, 1, tzinfo=UTC),
        datetime.datetime(2027, 1, 1, tzinfo=UTC),
    )


def test_bucket_range_week_monday_start() -> None:
    # Monday May 4, 2026 — week bucket runs Mon→following Mon.
    v = datetime.datetime(2026, 5, 4, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.WEEK) == (
        datetime.datetime(2026, 5, 4, tzinfo=UTC),
        datetime.datetime(2026, 5, 11, tzinfo=UTC),
    )


def test_bucket_range_day() -> None:
    v = datetime.datetime(2026, 5, 1, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.DAY) == (
        datetime.datetime(2026, 5, 1, tzinfo=UTC),
        datetime.datetime(2026, 5, 2, tzinfo=UTC),
    )


def test_bucket_range_hour() -> None:
    v = datetime.datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.HOUR) == (
        datetime.datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
        datetime.datetime(2026, 5, 1, 15, 0, tzinfo=UTC),
    )


def test_bucket_range_minute() -> None:
    v = datetime.datetime(2026, 5, 1, 14, 30, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.MINUTE) == (
        datetime.datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        datetime.datetime(2026, 5, 1, 14, 31, tzinfo=UTC),
    )


def test_bucket_range_second() -> None:
    v = datetime.datetime(2026, 5, 1, 14, 30, 15, tzinfo=UTC)
    assert bucket_range(v, TimeGranularity.SECOND) == (
        datetime.datetime(2026, 5, 1, 14, 30, 15, tzinfo=UTC),
        datetime.datetime(2026, 5, 1, 14, 30, 16, tzinfo=UTC),
    )


def test_bucket_range_preserves_tzinfo() -> None:
    """The returned interval's tzinfo matches the input value's tz —
    never silently converted to UTC."""
    tokyo = ZoneInfo("Asia/Tokyo")
    v = datetime.datetime(2026, 5, 1, tzinfo=tokyo)
    from_, to = bucket_range(v, TimeGranularity.MONTH)
    assert from_.tzinfo is tokyo
    assert to.tzinfo is tokyo


# ---------------------------------------------------------------------------
# Integration — AggregateBuilder wires range siblings into the GraphQL
# response.
# ---------------------------------------------------------------------------


@pytest.fixture
def order_schema_with_dates(sample_orders):
    """Schema variant including ``created_at`` in groupable fields so
    the wire surface accepts ``CREATED_AT`` group_by."""
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
def test_grouped_emits_month_range_sibling(order_schema_with_dates):
    """Group by ``created_at`` MONTH — each result row carries
    ``createdAtMonthRange { from to }`` with the half-open interval.
    """
    schema, _ = order_schema_with_dates
    result = schema.execute_sync("""
        query {
          ordersGroupBy(groupBy: [
            { field: CREATED_AT, granularity: MONTH }
          ]) {
            results {
              key {
                createdAtMonth
                createdAtMonthRange { from to }
              }
              count
            }
          }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    assert len(rows) == 2
    by_month = {r["key"]["createdAtMonth"]: r for r in rows}

    # April 2026 bucket — covers the two April orders.
    apr_key = next(k for k in by_month if k.startswith("2026-04"))
    apr = by_month[apr_key]["key"]["createdAtMonthRange"]
    assert apr["from"].startswith("2026-04-01")
    assert apr["to"].startswith("2026-05-01")

    # May 2026 bucket — covers the four May orders.
    may_key = next(k for k in by_month if k.startswith("2026-05"))
    may = by_month[may_key]["key"]["createdAtMonthRange"]
    assert may["from"].startswith("2026-05-01")
    assert may["to"].startswith("2026-06-01")


@pytest.mark.django_db
def test_grouped_range_uses_user_tz_boundaries(order_schema_with_dates):
    """``tz="Asia/Tokyo"`` shifts the bucket boundary to local-tz
    midnight. The resolver doesn't accept a ``tz`` arg in v1.0, so
    drive this via the backend primitive — the helper used in the
    resolver path must produce identical local-tz boundaries."""
    from strawberry_django_aggregates import (
        AggregateOp,
        compute_aggregation,
    )
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_month", "asc", None)],
        tz="Asia/Tokyo",
    )
    # Each bucketed value is tz-aware in Tokyo. Apply bucket_range to
    # confirm the half-open interval lands on Tokyo-local midnight.
    for row in rows:
        value = row["created_at_month"]
        assert value.tzinfo is not None
        from_, to = bucket_range(value, TimeGranularity.MONTH)
        # ``from`` aligns with the bucketed value (already
        # tz-truncated); ``to`` is the start of the next month in the
        # SAME tzinfo. UTC offsets must match.
        assert from_ == value
        assert from_.utcoffset() == to.utcoffset()
        # First-of-month / midnight in the user tz.
        assert from_.day == 1
        assert from_.hour == 0
        assert from_.minute == 0


@pytest.mark.django_db
def test_number_granularity_no_range_sibling_in_sdl(
    order_schema_with_dates,
):
    """``createdAtDayOfWeek`` (NUMBER granularity) has NO ``Range``
    sibling in the emitted SDL — there is no contiguous interval for
    "all Tuesdays."
    """
    schema, _ = order_schema_with_dates
    sdl = schema.as_str()
    # Spot-check: TIME-granularity range sibling is present.
    assert "createdAtMonthRange" in sdl
    # NUMBER-granularity range sibling is absent. Each NUMBER
    # granularity name (snake → camel) must NOT have a paired
    # ``Range`` field.
    for camel in (
        "createdAtDayOfWeek",
        "createdAtDayOfMonth",
        "createdAtDayOfYear",
        "createdAtHourNumber",
        "createdAtMonthNumber",
        "createdAtIsoWeekNumber",
    ):
        # The base field is present; the range sibling must not be.
        assert camel in sdl, f"Expected {camel} in SDL"
        assert f"{camel}Range" not in sdl, (
            f"Unexpected NUMBER-granularity range sibling "
            f"{camel}Range emitted in SDL."
        )


@pytest.mark.django_db
def test_grouped_range_is_none_when_no_time_bucket_requested(
    order_schema_with_dates,
):
    """Group by a non-date field — the GroupKey carries the date-bucket
    fields as Optional but they're all None, including the range
    siblings.
    """
    schema, _ = order_schema_with_dates
    result = schema.execute_sync("""
        query {
          ordersGroupBy(groupBy: [{ field: CUSTOMER }]) {
            results {
              key {
                customerId
                createdAtMonth
                createdAtMonthRange { from to }
              }
              count
            }
          }
        }
    """)
    assert result.errors is None, result.errors
    for r in result.data["ordersGroupBy"]["results"]:
        assert r["key"]["createdAtMonth"] is None
        assert r["key"]["createdAtMonthRange"] is None


# ---------------------------------------------------------------------------
# Public-API smoke — ``BucketRange`` and ``bucket_range`` are exported.
# ---------------------------------------------------------------------------


def test_bucket_range_public_export() -> None:
    """Both the type and the helper appear in ``__all__``."""
    import strawberry_django_aggregates as sda

    assert sda.BucketRange is BucketRange
    assert sda.bucket_range is bucket_range
    assert "BucketRange" in sda.__all__
    assert "bucket_range" in sda.__all__
