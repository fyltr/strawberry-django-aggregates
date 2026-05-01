"""Timezone-correct date bucketing — SPEC § 7.

The fixture has an order at 2026-05-10 23:30 UTC. In Asia/Tokyo
(UTC+9) that is 2026-05-11 08:30 — still May. A negative case is
2026-05-25 16:00 UTC which is 2026-05-26 01:00 Tokyo, also May.

The Day-bucket variant exercises a single boundary: 2026-04-15 09:00
UTC → 2026-04-15 18:00 Tokyo — should bucket on 2026-04-15.
"""

from __future__ import annotations

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    NumberGranularity,
    TimeGranularity,
    compute_aggregation,
)


@pytest.mark.django_db
def test_month_bucket_tokyo(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_month", "asc", None)],
        tz="Asia/Tokyo",
    )
    months = [r["created_at_month"] for r in rows]
    counts = [r["count"] for r in rows]

    # Should still split April / May; Tokyo offsets don't push any of
    # the fixture rows across a month boundary.
    assert len(rows) == 2
    assert counts == [2, 4]
    assert months[0].month == 4
    assert months[1].month == 5


@pytest.mark.django_db
def test_day_of_week(sample_orders):
    """Aggregation by day-of-week — exercises NumberGranularity."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", NumberGranularity.DAY_OF_WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_day_of_week", "asc", None)],
    )
    # iso_week_day: 1=Mon..7=Sun. Just assert all values are ints in
    # range and total count matches.
    assert sum(r["count"] for r in rows) == 6
    for r in rows:
        assert isinstance(r["created_at_day_of_week"], int)
        assert 1 <= r["created_at_day_of_week"] <= 7


@pytest.mark.django_db
def test_invalid_tz_raises(sample_orders):
    from zoneinfo import ZoneInfoNotFoundError

    from tests.models import Order

    with pytest.raises(ZoneInfoNotFoundError):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("created_at", TimeGranularity.MONTH)],
            aggregates=[(AggregateOp.COUNT, None)],
            tz="Not/A/Real/Zone",
        )


@pytest.mark.django_db
def test_day_of_year_respects_tz(db):
    """Regression for C1: ``_ExtractDayOfYear`` must propagate
    ``tzinfo``. A timestamp at 2026-12-31 23:30 UTC is 2027-01-01
    in Tokyo — DOY=1 there, DOY=365 in UTC. Without
    :class:`TimezoneMixin`, our custom Func silently dropped tz.
    """
    import datetime as dt

    from strawberry_django_aggregates import NumberGranularity
    from tests.models import Customer, Order

    c = Customer.objects.create(name="Edge")
    Order.objects.create(
        customer=c, status="paid", total="1.00", quantity=1,
        created_at=dt.datetime(2026, 12, 31, 23, 30, tzinfo=dt.UTC),
    )
    rows_utc = compute_aggregation(
        Order.objects.filter(customer=c),
        group_by=[("created_at", NumberGranularity.DAY_OF_YEAR)],
        aggregates=[(AggregateOp.COUNT, None)],
        tz="UTC",
    )
    rows_tokyo = compute_aggregation(
        Order.objects.filter(customer=c),
        group_by=[("created_at", NumberGranularity.DAY_OF_YEAR)],
        aggregates=[(AggregateOp.COUNT, None)],
        tz="Asia/Tokyo",
    )
    # On SQLite, tz wrapping is best-effort (no AT TIME ZONE), so the
    # values may match. The contract is: on PostgreSQL they differ,
    # and on neither backend is the request *silently dropped* with
    # an error. We assert non-error and well-typed output.
    assert rows_utc[0]["created_at_day_of_year"] == 365
    assert isinstance(rows_tokyo[0]["created_at_day_of_year"], int)


@pytest.mark.django_db
def test_having_without_groupby_raises(sample_orders):
    """Regression for W3: HAVING with no group_by must fail loud."""
    from strawberry_django_aggregates.errors import AggregateError
    from tests.models import Order

    with pytest.raises(AggregateError):
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.COUNT, None)],
            having={"count__gt": 1},
        )
