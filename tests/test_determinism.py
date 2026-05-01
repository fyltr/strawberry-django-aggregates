"""Deterministic SDL emission — SPEC § 12 / CLAUDE.md Rule 2.

For ``(model, aggregate_fields, group_by_fields, operators)`` ⇒
byte-identical schema. We build twice and ``schema.as_str()``-diff.

No ``from __future__ import annotations`` — strawberry resolves
Query field types eagerly from the live class objects.
"""

import strawberry

from strawberry_django_aggregates import AggregateBuilder, AggregateOp


def _build_schema(operators=None):
    """Construct a schema using AggregateBuilder. Returns
    ``schema.as_str()`` so the caller can byte-compare.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity", "is_priority", "created_at"],
        group_by_fields=["customer", "status", "created_at"],
        operators=operators if operators is not None else {
            "is_priority": (AggregateOp.BOOL_AND, AggregateOp.BOOL_OR),
        },
    ).build()

    @strawberry.type
    class Query:
        order_aggregate:  built.aggregate_type     = built.aggregate_field
        orders_group_by:  built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    return schema.as_str()


def _diff(sdl_1: str, sdl_2: str) -> str:
    pairs = zip(sdl_1.splitlines(), sdl_2.splitlines(), strict=False)
    return "\n".join(f"--- {a}\n+++ {b}" for a, b in pairs if a != b)


def test_schema_is_byte_identical_across_runs(db):
    sdl_1 = _build_schema()
    sdl_2 = _build_schema()
    assert sdl_1 == sdl_2, (
        f"SDL non-deterministic across runs:\n{_diff(sdl_1, sdl_2)}"
    )


def test_schema_independent_of_operators_dict_order(db):
    """Iteration order of the ``operators`` argument must not leak
    into the SDL. CLAUDE.md Rule 2 mandates ``sorted()`` iteration of
    overrides; this test pins the property.
    """
    sdl_a = _build_schema(operators={
        "is_priority": (AggregateOp.BOOL_AND,),
        "total":       (AggregateOp.SUM, AggregateOp.AVG),
    })
    sdl_b = _build_schema(operators={
        "total":       (AggregateOp.SUM, AggregateOp.AVG),
        "is_priority": (AggregateOp.BOOL_AND,),
    })
    assert sdl_a == sdl_b, (
        f"SDL depends on operators dict order:\n{_diff(sdl_a, sdl_b)}"
    )
