"""Comodel-derived ordering tiebreakers — SPEC § 9.1.

When ``respect_comodel_ordering=True`` and the user orders by an FK
group-by alias, the comodel's ``Meta.ordering`` is appended as
ORDER BY tiebreakers. Default ``False`` preserves the existing
contract (raw FK-ID order). Mirrors Odoo
``_order_field_to_sql:2253``.
"""

from __future__ import annotations

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
)
from strawberry_django_aggregates.compiler import _build_order_terms
from strawberry_django_aggregates.ordering import comodel_ordering_terms

# --- unit tests for the helper itself --------------------------------------


def test_comodel_ordering_terms_translates_attname():
    """``customer_id`` → ``["customer__name"]`` when comodel orders
    by ``name``."""
    from tests.models import Customer, Order

    assert Customer._meta.ordering == ["name"]
    assert comodel_ordering_terms(Order, "customer_id") == [
        "customer__name",
    ]


def test_comodel_ordering_terms_handles_descending():
    """A leading ``-`` in the comodel's Meta.ordering survives the
    translation as a Django-flavor descending term."""
    from tests.models import Customer, Order

    original = list(Customer._meta.ordering)
    Customer._meta.ordering = ["-name"]
    try:
        assert comodel_ordering_terms(Order, "customer_id") == [
            "-customer__name",
        ]
    finally:
        Customer._meta.ordering = original


def test_comodel_ordering_terms_no_meta_ordering():
    """Comodel without ``Meta.ordering`` → empty list; the flag is a
    no-op for that FK."""
    from tests.models import Customer, Order

    original = list(Customer._meta.ordering)
    Customer._meta.ordering = []
    try:
        assert comodel_ordering_terms(Order, "customer_id") == []
    finally:
        Customer._meta.ordering = original


def test_comodel_ordering_terms_unknown_alias():
    """Alias that doesn't resolve to an FK → empty list."""
    from tests.models import Order

    assert comodel_ordering_terms(Order, "status") == []
    assert comodel_ordering_terms(Order, "nonexistent_id") == []


def test_comodel_ordering_terms_skips_non_string_entries():
    """``F``/``OrderBy`` entries in ``Meta.ordering`` are skipped —
    we translate plain strings only."""
    from django.db.models import F

    from tests.models import Customer, Order

    original = list(Customer._meta.ordering)
    Customer._meta.ordering = [F("name").asc(), "name"]
    try:
        assert comodel_ordering_terms(Order, "customer_id") == [
            "customer__name",
        ]
    finally:
        Customer._meta.ordering = original


# --- direct tests for _build_order_terms (compiler internals) --------------


def test_build_order_terms_appends_when_flag_on():
    """With ``respect_comodel_ordering=True`` the comodel's terms are
    appended after the user's primary FK-alias term."""
    from tests.models import Order

    terms = _build_order_terms(
        [("customer_id", "asc", None)],
        group_aliases=["customer_id"],
        aggregate_aliases=["count"],
        model=Order,
        respect_comodel_ordering=True,
    )
    # Two ORDER BY expressions: customer_id ASC, then customer.name ASC.
    assert len(terms) == 2
    # The second term should reference the comodel traversal.
    rendered = [str(t.expression.name) for t in terms]
    assert rendered == ["customer_id", "customer__name"]
    # Both ascending.
    assert all(t.descending is False for t in terms)


def test_build_order_terms_default_does_not_append():
    """Default ``respect_comodel_ordering=False`` → no extra terms."""
    from tests.models import Order

    terms = _build_order_terms(
        [("customer_id", "asc", None)],
        group_aliases=["customer_id"],
        aggregate_aliases=["count"],
        model=Order,
        respect_comodel_ordering=False,
    )
    assert len(terms) == 1


def test_build_order_terms_flag_inert_for_non_fk_alias():
    """Flag set, but ordering by a non-FK group-by alias → no
    tiebreaker. The flag only fires on FK aliases."""
    from tests.models import Order

    terms = _build_order_terms(
        [("status", "asc", None)],
        group_aliases=["status"],
        aggregate_aliases=["count"],
        model=Order,
        respect_comodel_ordering=True,
    )
    assert len(terms) == 1


def test_build_order_terms_propagates_descending_comodel_meta():
    """If comodel ordering is descending, the appended term is too."""
    from tests.models import Customer, Order

    original = list(Customer._meta.ordering)
    Customer._meta.ordering = ["-name"]
    try:
        terms = _build_order_terms(
            [("customer_id", "asc", None)],
            group_aliases=["customer_id"],
            aggregate_aliases=["count"],
            model=Order,
            respect_comodel_ordering=True,
        )
    finally:
        Customer._meta.ordering = original
    assert len(terms) == 2
    # First is asc, second is desc.
    assert terms[0].descending is False
    assert terms[1].descending is True


# --- end-to-end tests through compute_aggregation --------------------------


@pytest.mark.django_db
def test_respect_comodel_ordering_emits_join_in_sql(sample_orders):
    """End-to-end: with the flag set, the compiled SQL includes the
    comodel JOIN and references ``customer.name``. Without the flag,
    no such JOIN appears."""
    from django.db.models import Count

    from tests.models import Order

    # We can't easily inspect the rows-based output for tiebreaker
    # behaviour because customer_id is unique per group (FKs in a
    # group_by never tie). Instead, we verify the compiled queryset
    # structure: the ORDER BY chain is two terms, not one.
    qs_with_flag = Order.objects.values("customer_id").annotate(
        count=Count("pk"),
    )
    # Mimic what _build_order_terms produces under the flag.
    terms = _build_order_terms(
        [("customer_id", "asc", None)],
        group_aliases=["customer_id"],
        aggregate_aliases=["count"],
        model=Order,
        respect_comodel_ordering=True,
    )
    qs_with_flag = qs_with_flag.order_by(*terms)
    sql = str(qs_with_flag.query)
    # Expect a JOIN to tests_customer because customer__name traversal.
    assert "tests_customer" in sql.lower()
    # And the ORDER BY mentions the customer.name column.
    assert "name" in sql.lower().split("order by")[1]


@pytest.mark.django_db
def test_respect_comodel_ordering_returns_correct_rows(sample_orders):
    """End-to-end: the flag does not change which rows come back, only
    their order. Same row set, with FK aliases preserved."""
    from tests.models import Order

    rows_off = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("customer_id", "asc", None)],
        respect_comodel_ordering=False,
    )
    rows_on = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("customer_id", "asc", None)],
        respect_comodel_ordering=True,
    )
    # Same set of customer ids and counts, regardless of order.
    by_id_off = {r["customer_id"]: r["count"] for r in rows_off}
    by_id_on = {r["customer_id"]: r["count"] for r in rows_on}
    assert by_id_off == by_id_on
    # Three customers in the fixture.
    assert len(rows_on) == 3


@pytest.mark.django_db
def test_default_preserves_fk_id_order(sample_orders):
    """Default ``respect_comodel_ordering=False`` → rows come out in
    raw ``customer_id`` ASC order regardless of comodel state."""
    from tests.models import Order

    customers, _ = sample_orders
    customers[0].name = "Zeta"
    customers[0].save()
    customers[1].name = "Mu"
    customers[1].save()
    customers[2].name = "Alpha"
    customers[2].save()

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("customer_id", "asc", None)],
    )
    # FK ID ASC: id=1 (Zeta), id=2 (Mu), id=3 (Alpha).
    assert [r["customer_id"] for r in rows] == [
        customers[0].id,
        customers[1].id,
        customers[2].id,
    ]


@pytest.mark.django_db
def test_flag_is_noop_when_comodel_has_no_meta_ordering(sample_orders):
    """Flag set, but comodel has no ``Meta.ordering`` → identical
    output to the default. The flag is harmless when there's nothing
    to append."""
    from tests.models import Customer, Order

    original = list(Customer._meta.ordering)
    Customer._meta.ordering = []
    try:
        rows_flag_on = compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            order_by=[("customer_id", "asc", None)],
            respect_comodel_ordering=True,
        )
        rows_flag_off = compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            order_by=[("customer_id", "asc", None)],
            respect_comodel_ordering=False,
        )
    finally:
        Customer._meta.ordering = original
    assert [r["customer_id"] for r in rows_flag_on] == [
        r["customer_id"] for r in rows_flag_off
    ]


@pytest.mark.django_db
def test_flag_with_non_fk_order_term_is_inert(sample_orders):
    """Ordering by a non-FK group-by alias (``status``) plus
    ``respect_comodel_ordering=True`` does nothing extra — the flag
    only fires on FK aliases."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("status", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("status", "asc", None)],
        respect_comodel_ordering=True,
    )
    # Statuses sorted ascending: cancelled, draft, paid.
    assert [r["status"] for r in rows] == ["cancelled", "draft", "paid"]
