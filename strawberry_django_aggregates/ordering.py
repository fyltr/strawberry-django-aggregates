"""Aggregate-aware order parser.

Resolves order terms against three namespaces in this priority:

1. Aggregate aliases — ``count``, ``count_distinct_<field>``,
   ``<op>_<field>``.
2. Group-by field paths — including bucketed forms like
   ``created_at_month``.
3. Plain model field paths declared in the order-by allowlist.

Unknown terms raise :class:`OrderFieldNotAllowed`. Mirrors Odoo's
post-17 fail-loud behaviour (``odoo/models.py:2249`` —
``ValueError("Order term ... is not a valid aggregate nor valid
groupby")``); pre-17 ``read_group`` silently dropped unknown terms
and was a recurring source of "why isn't this query ordered?" bugs.

This module is framework-agnostic — it has no Django, Strawberry, or
GraphQL imports. Pure string parsing + namespace lookup.
"""

from __future__ import annotations

from typing import Literal

from strawberry_django_aggregates.errors import OrderFieldNotAllowed
from strawberry_django_aggregates.operators import AggregateOp

Direction = Literal["asc", "desc"]


def parse_aggregate_order(
    term: str,
    *,
    group_by_fields: list[str],
    aggregate_aliases: list[str],
    field_allowlist: list[str] | None = None,
) -> tuple[str, Direction]:
    """Parse a single order term into ``(canonical_alias, direction)``.

    Accepted forms (resolved in this priority):

    - ``"-field"``               — Django flavor descending
    - ``"field desc"``           — explicit direction suffix
    - ``"field asc"``            — explicit direction suffix
    - ``"<op>_<field>"``         — snake-case aggregate alias
                                   (e.g. ``sum_total``)
    - ``"field:<op>"``           — Odoo-flavor aggregate alias
                                   (e.g. ``total:sum``)
    - ``"field"``                — plain field / group-by alias

    The canonical alias returned matches the alias produced by
    :mod:`strawberry_django_aggregates.compiler` (``count``,
    ``sum_<field>``, ``<field>_<granularity>``, etc.) so the caller can
    pass it straight into ``order_by``.

    Raises
    ------
    OrderFieldNotAllowed
        ``term`` does not resolve in any namespace.
    """
    raw, direction = _split_direction(term)
    canonical = _resolve(
        raw,
        aggregate_aliases=aggregate_aliases,
        group_by_fields=group_by_fields,
        field_allowlist=field_allowlist or [],
    )
    return canonical, direction


def _split_direction(term: str) -> tuple[str, Direction]:
    """Strip leading ``-`` and trailing ``ASC|DESC`` (case-insensitive)
    from a term, returning the bare body and the direction.
    """
    s = term.strip()
    if not s:
        raise OrderFieldNotAllowed("Empty order term.")
    if s.startswith("-"):
        return s[1:].strip(), "desc"
    parts = s.split()
    if len(parts) == 2:
        body, suffix = parts
        suffix_lower = suffix.lower()
        if suffix_lower == "desc":
            return body, "desc"
        if suffix_lower == "asc":
            return body, "asc"
    return s, "asc"


def _resolve(
    raw: str,
    *,
    aggregate_aliases: list[str],
    group_by_fields: list[str],
    field_allowlist: list[str],
) -> str:
    """Resolve a bare order-term body against the three namespaces."""
    agg_set       = set(aggregate_aliases)
    group_set     = set(group_by_fields)
    allow_set     = set(field_allowlist)

    # Odoo flavor: "field:op" -> "<op>_<field>".
    if ":" in raw:
        field, op = raw.rsplit(":", 1)
        canonical = f"{op}_{field}"
        if canonical in agg_set:
            return canonical
        # Bucketed group-by reference: "created_at:month"
        bucketed = f"{field}_{op}"
        if bucketed in group_set:
            return bucketed
        raise OrderFieldNotAllowed(
            f"Order term `{raw}` does not match any aggregate alias "
            f"({sorted(agg_set)}) or group_by ({sorted(group_set)})."
        )

    if raw in agg_set:
        return raw
    if raw in group_set:
        return raw
    if raw in allow_set:
        return raw

    raise OrderFieldNotAllowed(
        f"Order term `{raw}` is not a valid aggregate alias "
        f"({sorted(agg_set)}), group_by alias ({sorted(group_set)}), "
        f"nor allowlisted field ({sorted(allow_set)})."
    )


def aggregate_aliases_from_spec(
    aggregates: list[tuple[AggregateOp | str, str | None]],
) -> list[str]:
    """Compute the alias names :func:`compute_aggregation` will emit
    for a given aggregate spec.

    Single source of truth: delegates to
    :func:`compiler.aggregate_alias`. Used by the order parser and the
    HAVING input validator. Per Critical Rule 9, importing from
    ``compiler`` here is fine — both modules are framework-agnostic.
    """
    # Local import avoids a circular-import risk if compiler ever
    # adds an ordering helper.
    from strawberry_django_aggregates.compiler import aggregate_alias

    aliases: list[str] = []
    for op, field_path in aggregates:
        op_enum = op if isinstance(op, AggregateOp) else AggregateOp(op)
        aliases.append(aggregate_alias(op_enum, field_path))
    return aliases
