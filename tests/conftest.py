"""Pytest configuration for strawberry-django-aggregates tests.

Provides a minimal Django setup so tests can use ORM querysets and
strawberry schemas without booting a full project.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import django
import pytest
from django.conf import settings


def pytest_configure() -> None:
    if settings.configured:
        return
    settings.configure(
        DEBUG=True,
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME":   ":memory:",
            },
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "tests.apps.TestsConfig",
        ],
        USE_TZ=True,
        TIME_ZONE="UTC",
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    django.setup()


@pytest.fixture
def sample_orders(db):
    """Three customers, six orders spread across two months in UTC.

    Returns ``(customers, orders)``. Tests can scope further or just
    use the orders queryset.
    """
    from tests.models import Customer, Order

    a = Customer.objects.create(name="Alpha")
    b = Customer.objects.create(name="Beta")
    g = Customer.objects.create(name="Gamma", active=False)

    tz = datetime.UTC

    o1 = Order.objects.create(
        customer=a, status="paid", total=Decimal("100.00"),
        quantity=2, is_priority=True,
        created_at=datetime.datetime(2026, 4, 1, 12, 0, tzinfo=tz),
    )
    o2 = Order.objects.create(
        customer=a, status="paid", total=Decimal("200.00"),
        quantity=4, is_priority=False,
        created_at=datetime.datetime(2026, 4, 15, 9, 0, tzinfo=tz),
    )
    o3 = Order.objects.create(
        customer=b, status="paid", total=Decimal("300.00"),
        quantity=1, is_priority=True,
        created_at=datetime.datetime(2026, 5, 5, 14, 0, tzinfo=tz),
    )
    o4 = Order.objects.create(
        customer=b, status="cancelled", total=Decimal("50.00"),
        quantity=1, is_priority=False,
        created_at=datetime.datetime(2026, 5, 10, 23, 30, tzinfo=tz),
    )
    o5 = Order.objects.create(
        customer=g, status="draft", total=Decimal("75.00"),
        quantity=3, is_priority=False,
        created_at=datetime.datetime(2026, 5, 20, 1, 0, tzinfo=tz),
    )
    o6 = Order.objects.create(
        customer=a, status="paid", total=Decimal("400.00"),
        quantity=2, is_priority=True,
        created_at=datetime.datetime(2026, 5, 25, 16, 0, tzinfo=tz),
    )

    return [a, b, g], [o1, o2, o3, o4, o5, o6]


@pytest.fixture
def sample_order_items(sample_orders):
    """Build an OrderItem fan-out on top of ``sample_orders``.

    Lays down a known per-order ``items.price`` distribution so tests
    of relation-traversing measures can assert exact subquery sums.
    Returns ``(customers, orders, items_by_order)``.
    """
    from tests.models import OrderItem

    customers, orders = sample_orders
    o1, o2, o3, o4, o5, o6 = orders

    # Spread items so each order has a different sum:
    #   o1 → 10 + 20 = 30
    #   o2 → 50
    #   o3 → 25 + 75 = 100
    #   o4 → (no items — exercises NULL-safe SUM in subquery)
    #   o5 → 5 + 5 + 5 = 15
    #   o6 → 200
    items_by_order: dict[int, list[OrderItem]] = {o.pk: [] for o in orders}
    pairs = [
        (o1, "A1", Decimal("10.00")),
        (o1, "A2", Decimal("20.00")),
        (o2, "B1", Decimal("50.00")),
        (o3, "C1", Decimal("25.00")),
        (o3, "C2", Decimal("75.00")),
        (o5, "D1", Decimal("5.00")),
        (o5, "D2", Decimal("5.00")),
        (o5, "D3", Decimal("5.00")),
        (o6, "E1", Decimal("200.00")),
    ]
    for order, sku, price in pairs:
        item = OrderItem.objects.create(order=order, sku=sku, price=price)
        items_by_order[order.pk].append(item)

    return customers, orders, items_by_order
