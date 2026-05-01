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
    ``VARIANCE``, ``STDDEV_POP``, ``VAR_POP``, ``PERCENTILE_CONT``,
    ``PERCENTILE_DISC``, ``MODE``) raise
    :class:`OperatorNotSupportedError` at resolver entry on SQLite
    connections.
    """

    COUNT                = "count"
    COUNT_DISTINCT       = "count_distinct"
    COUNT_DISTINCT_TUPLE = "count_distinct_tuple"
    SUM                  = "sum"
    AVG                  = "avg"
    MIN                  = "min"
    MAX                  = "max"
    STDDEV               = "stddev"           # PG only — sample stddev
    VARIANCE             = "variance"         # PG only — sample variance
    STDDEV_POP           = "stddev_pop"       # PG only — population stddev
    VAR_POP              = "var_pop"          # PG only — population var
    PERCENTILE_CONT      = "percentile_cont"  # PG only — interpolated pct
    PERCENTILE_DISC      = "percentile_disc"  # PG only — discrete pct
    MODE                 = "mode"             # PG only — most-frequent value
    BOOL_AND             = "bool_and"
    BOOL_OR              = "bool_or"
    ARRAY_AGG            = "array_agg"        # Postgres only
    STRING_AGG           = "string_agg"       # Postgres only


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
    AggregateOp.PERCENTILE_CONT,
    AggregateOp.PERCENTILE_DISC,
    AggregateOp.MODE,
)

_DATE_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.PERCENTILE_DISC,
    AggregateOp.MODE,
)

_BOOL_OPS = (
    AggregateOp.BOOL_AND,
    AggregateOp.BOOL_OR,
)

_STRING_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.MODE,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
)

_ID_OPS = (
    AggregateOp.ARRAY_AGG,
)


# Default operator allowlists for declared JSON-path types. Mirrors the
# field-type defaults above, but keyed on the wire-level type tokens the
# caller passes in :class:`AggregateBuilder.json_paths`. Per SPEC § 6.1.
#
# Numeric JSON paths get the same operator surface as native numeric
# Field types (sum/avg/min/max/stddev/variance/stddev_pop/var_pop) —
# ``Cast(KeyTextTransform(...))`` widens cleanly through the SQL
# variance/stddev aggregates on Postgres. ``MODE`` is excluded across
# all JSON-path types in v1.0 — ordered-set aggregates over the JSON
# cast wrap (``MODE() WITHIN GROUP (ORDER BY ...)``) are not validated
# against the casted expression yet; revisit in v1.x.
_JSON_NUMERIC_OPS = (
    AggregateOp.SUM,
    AggregateOp.AVG,
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.STDDEV,
    AggregateOp.VARIANCE,
    AggregateOp.STDDEV_POP,
    AggregateOp.VAR_POP,
)

_JSON_STRING_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
)

_JSON_BOOL_OPS = (
    AggregateOp.BOOL_AND,
    AggregateOp.BOOL_OR,
)

_JSON_DATE_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
)


def default_operators_for_json_type(
    declared_type: str,
) -> tuple[AggregateOp, ...]:
    """Return default operator allowlist for a declared JSON-path type.

    ``declared_type`` is one of the SPEC § 6.1 wire tokens
    (``"str"``, ``"int"``, ``"float"``, ``"Decimal"``, ``"bool"``,
    ``"date"``, ``"datetime"``). Unknown tokens return an empty tuple.
    """
    if declared_type in {"int", "float", "Decimal"}:
        return _JSON_NUMERIC_OPS
    if declared_type == "str":
        return _JSON_STRING_OPS
    if declared_type == "bool":
        return _JSON_BOOL_OPS
    if declared_type in {"date", "datetime"}:
        return _JSON_DATE_OPS
    return ()


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
