"""Models used by the strawberry-django-aggregates test suite.

Mirrors the SPEC examples (Customer / Order / OrderItem) so test code
reads like the spec. The o2m relation between Order and OrderItem is
deliberate — it lets us assert that the library refuses to aggregate
across `items__price` (would silently row-multiply).
"""

from __future__ import annotations

from django.db import models


class Customer(models.Model):
    name   = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        app_label = "tests"
        # Intrinsic ordering exercised by Stream 16's
        # ``respect_comodel_ordering`` flag. Other tests remain
        # robust to row order — they look rows up by FK id, not by
        # iteration order — so this addition is safe.
        ordering = ["name"]


class Order(models.Model):
    STATUS_CHOICES = [
        ("draft",     "Draft"),
        ("paid",      "Paid"),
        ("cancelled", "Cancelled"),
    ]

    customer    = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="orders",
    )
    status      = models.CharField(max_length=16, choices=STATUS_CHOICES)
    total       = models.DecimalField(max_digits=10, decimal_places=2)
    quantity    = models.IntegerField(default=1)
    is_priority = models.BooleanField(default=False)
    created_at  = models.DateTimeField()

    class Meta:
        app_label = "tests"


class OrderItem(models.Model):
    order  = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items",
    )
    sku    = models.CharField(max_length=32)
    price  = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        app_label = "tests"
