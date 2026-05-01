"""SQL-standard wire aliases — `every` ≡ `bool_and`, `some` ≡ `bool_or`.

Stream 4 adds wire-level aliases on ``<Model>Aggregate`` /
``<Model>Grouped`` SDL types. No new :class:`AggregateOp` member; both
aliases route to the same compiler annotation as their canonical
sibling and share the same nested-type instance in the response.

Note: we deliberately do NOT use ``from __future__ import annotations``
here — strawberry resolves field-type annotations on the Query class
against ``__globals__``; under PEP 563 the dynamic ``built.aggregate_type``
becomes a string strawberry cannot evaluate. Same pattern as
``test_builder_integration.py``.
"""

import dataclasses

import pytest
import strawberry

from strawberry_django_aggregates import AggregateBuilder, AggregateOp
from strawberry_django_aggregates.builder import _OP_FROM_WIRE

# ---------------------------------------------------------------------------
# Schema fixture — uses Customer.active (single-word BooleanField) so the
# tests don't double up with a known camelCase / snake_case wire-walker
# limitation on field names like `is_priority`. Stream 4's surface is
# the alias itself; verifying it on a non-controversial field name
# isolates the alias logic.
# ---------------------------------------------------------------------------

@pytest.fixture
def customer_schema(sample_orders):
    from tests.models import Customer

    built = AggregateBuilder(
        model=Customer,
        aggregate_fields=["active"],
        group_by_fields=["name"],
    ).build()

    @strawberry.type
    class Query:
        customer_aggregate:  built.aggregate_type      = built.aggregate_field
        customers_group_by:  built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query), built


# ---------------------------------------------------------------------------
# Wire-walker mapping — ``every`` / ``some`` route to BOOL_AND / BOOL_OR.
# ---------------------------------------------------------------------------

def test_op_from_wire_includes_every_and_some():
    """The selection walker's name → AggregateOp dict gains the two
    SQL-standard aliases. No new :class:`AggregateOp` member is
    introduced; both aliases share entries in the same dict.
    """
    assert _OP_FROM_WIRE["every"] is AggregateOp.BOOL_AND
    assert _OP_FROM_WIRE["some"]  is AggregateOp.BOOL_OR
    # Canonical names still map to the canonical ops (regression guard
    # against the alias accidentally shadowing them).
    assert _OP_FROM_WIRE["boolAnd"] is AggregateOp.BOOL_AND
    assert _OP_FROM_WIRE["boolOr"]  is AggregateOp.BOOL_OR


# ---------------------------------------------------------------------------
# Equivalence — `every` matches `bool_and`, `some` matches `bool_or`.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_every_matches_bool_and(customer_schema):
    """Querying `every` returns the same Boolean as `bool_and`."""
    schema, _ = customer_schema
    result = schema.execute_sync("""
        query {
            customerAggregate {
                boolAnd { active }
                every { active }
            }
        }
    """)
    assert result.errors is None, result.errors
    data = result.data["customerAggregate"]
    # 3 customers; one (Gamma) is inactive ⇒ BOOL_AND is False.
    assert data["boolAnd"]["active"] is False
    assert data["every"]["active"] is False
    assert data["every"] == data["boolAnd"]


@pytest.mark.django_db
def test_some_matches_bool_or(customer_schema):
    schema, _ = customer_schema
    result = schema.execute_sync("""
        query {
            customerAggregate {
                boolOr { active }
                some { active }
            }
        }
    """)
    assert result.errors is None, result.errors
    data = result.data["customerAggregate"]
    # At least one active customer ⇒ BOOL_OR is True.
    assert data["boolOr"]["active"] is True
    assert data["some"]["active"] is True
    assert data["some"] == data["boolOr"]


@pytest.mark.django_db
def test_every_and_some_in_grouped(customer_schema):
    """Aliases also surface on ``<Model>Grouped`` and match canonical."""
    schema, _ = customer_schema
    result = schema.execute_sync("""
        query {
            customersGroupBy(groupBy: [{ field: NAME }]) {
                results {
                    key { name }
                    boolAnd { active }
                    every   { active }
                    boolOr  { active }
                    some    { active }
                }
            }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["customersGroupBy"]["results"]
    assert len(rows) == 3
    for r in rows:
        assert r["every"] == r["boolAnd"]
        assert r["some"] == r["boolOr"]


@pytest.mark.django_db
def test_alias_alone_works_without_canonical(customer_schema):
    """Selecting ONLY ``every`` (without ``boolAnd``) must still
    produce a value. Verifies the wire walker translates ``every`` →
    BOOL_AND and the shaping path populates ``every`` even when the
    canonical name is not in the projection.
    """
    schema, _ = customer_schema
    result = schema.execute_sync("""
        query {
            customerAggregate { every { active } }
        }
    """)
    assert result.errors is None, result.errors
    assert result.data["customerAggregate"]["every"]["active"] is False


@pytest.mark.django_db
def test_alias_alone_works_without_canonical_some(customer_schema):
    schema, _ = customer_schema
    result = schema.execute_sync("""
        query {
            customerAggregate { some { active } }
        }
    """)
    assert result.errors is None, result.errors
    assert result.data["customerAggregate"]["some"]["active"] is True


# ---------------------------------------------------------------------------
# Dataclass surface — aliases share the SAME nested type as canonical.
# ---------------------------------------------------------------------------

def test_aggregate_type_aliases_share_canonical_nested_type(db):
    """``every`` and ``boolAnd`` (resp. ``some`` / ``boolOr``) must
    declare the same dataclass-field type — i.e. the SAME generated
    ``<Model>BoolAndFields`` / ``<Model>BoolOrFields`` class. No
    separate ``<Model>EveryFields`` is generated.
    """
    from tests.models import Customer

    built = AggregateBuilder(
        model=Customer,
        aggregate_fields=["active"],
        group_by_fields=["name"],
    ).build()

    fields_by_name = {
        f.name: f for f in dataclasses.fields(built.aggregate_type)
    }
    assert fields_by_name["bool_and"].type == fields_by_name["every"].type
    assert fields_by_name["bool_or"].type  == fields_by_name["some"].type

    # Same identity check on the grouped type.
    grouped_fields = {
        f.name: f for f in dataclasses.fields(built.grouped_type)
    }
    assert (
        grouped_fields["bool_and"].type == grouped_fields["every"].type
    )
    assert (
        grouped_fields["bool_or"].type  == grouped_fields["some"].type
    )


# ---------------------------------------------------------------------------
# SDL surface — aliases share the canonical nested type.
# ---------------------------------------------------------------------------

def test_sdl_emits_every_and_some_aliases(db):
    """SDL must contain ``every: <Model>BoolAndFields`` and
    ``some: <Model>BoolOrFields`` referencing the SAME named nested
    types as ``boolAnd`` / ``boolOr``. This also confirms no separate
    ``<Model>EveryFields`` / ``<Model>SomeFields`` types are emitted.
    """
    from tests.models import Customer

    built = AggregateBuilder(
        model=Customer,
        aggregate_fields=["active"],
        group_by_fields=["name"],
    ).build()

    @strawberry.type
    class Query:
        customer_aggregate:  built.aggregate_type      = built.aggregate_field
        customers_group_by:  built.grouped_result_type = built.group_by_field

    sdl = strawberry.Schema(query=Query).as_str()

    # Aliases reference the canonical nested types.
    assert "every: CustomerBoolAndFields" in sdl
    assert "some: CustomerBoolOrFields"  in sdl

    # No separate <Model>EveryFields / <Model>SomeFields type emitted.
    assert "CustomerEveryFields" not in sdl
    assert "CustomerSomeFields"  not in sdl


def test_sdl_skips_aliases_when_no_boolean_fields(db):
    """If the allowlist excludes BOOL_AND/BOOL_OR (e.g. no boolean
    fields in scope), neither ``every`` nor ``some`` is emitted. Guards
    against the degenerate empty-nested-type case strawberry would
    refuse at schema-build time.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],  # decimal only — no booleans
        group_by_fields=["customer"],
    ).build()

    @strawberry.type
    class Query:
        order_aggregate: built.aggregate_type = built.aggregate_field

    sdl = strawberry.Schema(query=Query).as_str()
    assert "every:" not in sdl
    assert "some:"  not in sdl
    assert "boolAnd" not in sdl
    assert "boolOr"  not in sdl


# ---------------------------------------------------------------------------
# Determinism — adding aliases must not break byte-identical SDL.
# ---------------------------------------------------------------------------

def _build_sdl():
    from tests.models import Customer

    built = AggregateBuilder(
        model=Customer,
        aggregate_fields=["active"],
        group_by_fields=["name"],
    ).build()

    @strawberry.type
    class Query:
        customer_aggregate:  built.aggregate_type      = built.aggregate_field
        customers_group_by:  built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query).as_str()


def test_alias_emission_deterministic(db):
    sdl_1 = _build_sdl()
    sdl_2 = _build_sdl()
    assert sdl_1 == sdl_2, "Alias emission introduced non-determinism."
