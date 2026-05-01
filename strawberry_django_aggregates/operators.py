"""Aggregate operator vocabulary.

Mirrors Odoo's ``READ_GROUP_AGGREGATE`` whitelist (``odoo/models.py:196``)
extended with ``stddev``, ``variance``, and ``string_agg`` — additions from
PostGraphile's ``pg-aggregates``. No operator outside this enum is
accepted; consumers cannot pass arbitrary SQL fragments.
"""

from __future__ import annotations

from enum import StrEnum


class AggregateOp(StrEnum):
    """Whitelisted aggregate operators.

    Database support flags are documented in ``docs/SPEC.md``. The
    Postgres-only operators (``ARRAY_AGG``, ``STRING_AGG``, ``STDDEV``,
    ``VARIANCE``, ``STDDEV_POP``, ``VAR_POP``) raise
    :class:`OperatorNotSupportedError` at resolver entry on SQLite
    connections.
    """

    COUNT          = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM            = "sum"
    AVG            = "avg"
    MIN            = "min"
    MAX            = "max"
    STDDEV         = "stddev"          # Postgres only — sample stddev
    VARIANCE       = "variance"        # Postgres only — sample variance
    STDDEV_POP     = "stddev_pop"      # Postgres only — population stddev
    VAR_POP        = "var_pop"         # Postgres only — population variance
    BOOL_AND       = "bool_and"
    BOOL_OR        = "bool_or"
    ARRAY_AGG      = "array_agg"       # Postgres only
    STRING_AGG     = "string_agg"      # Postgres only


# Operators each Django field type gets without explicit override.
# Consumers can narrow further via the AggregateBuilder allowlist arg.
_NUMERIC_OPS = (
    AggregateOp.SUM,
    AggregateOp.AVG,
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.STDDEV,
    AggregateOp.VARIANCE,
    AggregateOp.STDDEV_POP,
    AggregateOp.VAR_POP,
)

_DATE_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
)

_BOOL_OPS = (
    AggregateOp.BOOL_AND,
    AggregateOp.BOOL_OR,
)

_STRING_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
)

_ID_OPS = (
    AggregateOp.ARRAY_AGG,
)


def default_operators_for(field_type: str) -> tuple[AggregateOp, ...]:
    """Return the default operator allowlist for a Django field type name.

    ``field_type`` is the Django field class name as a string (e.g.
    ``"IntegerField"``). Used by :class:`AggregateBuilder` when the
    consumer doesn't specify per-field overrides.

    Unknown field types return an empty tuple — explicit allowlisting
    required.
    """
    if field_type in {
        "IntegerField", "BigIntegerField", "PositiveIntegerField",
        "PositiveSmallIntegerField", "SmallIntegerField",
        "FloatField", "DecimalField", "DurationField",
    }:
        return _NUMERIC_OPS
    if field_type in {"DateField", "DateTimeField", "TimeField"}:
        return _DATE_OPS
    if field_type == "BooleanField":
        return _BOOL_OPS
    if field_type in {
        "CharField", "TextField", "EmailField", "URLField", "SlugField",
    }:
        return _STRING_OPS
    if field_type in {"UUIDField", "AutoField", "BigAutoField"}:
        return _ID_OPS
    if field_type in {"ForeignKey", "OneToOneField"}:
        return _ID_OPS  # by ID
    return ()
