"""Type generators for strawberry-django-aggregates.

Each function takes a Django model + field allowlists and returns a
Strawberry type / input. Generation is deterministic for a given input
(see ``docs/SPEC.md`` § 12).

Public surface:

- :func:`make_aggregate_type` — emits ``<Model>Aggregate`` and the
  per-operator ``<Model>SumFields``/``<Model>AvgFields``/etc. nested
  types.
- :func:`make_grouped_type` — emits ``<Model>GroupKey``,
  ``<Model>Grouped``, and ``<Model>GroupedResult``.
- :func:`make_having_input` — emits ``<Model>Having``.
- :func:`make_group_by_spec` — emits ``<Model>GroupBySpec`` plus the
  groupable-field enum.
- :func:`make_group_order_input` — emits ``<Model>GroupOrder``.

Strawberry imports are confined to this module and ``builder.py`` —
``compiler.py`` / ``ordering.py`` / ``operators.py`` / ``granularity.py``
remain framework-agnostic per CLAUDE.md Critical Rule 9.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import uuid
from typing import TYPE_CHECKING, Any

import strawberry
import strawberry.federation
from strawberry_django.pagination import OffsetPaginationInfo

from strawberry_django_aggregates.granularity import (
    NumberGranularity,
    TimeGranularity,
)
from strawberry_django_aggregates.operators import (
    AggregateOp,
    default_operators_for,
)

if TYPE_CHECKING:
    from django.db.models import Model
    from django.db.models.fields import Field


# ---------------------------------------------------------------------------
# Custom scalars
# ---------------------------------------------------------------------------

# Postgres widens ``SUM(int_col)`` to ``bigint`` (8-byte); the GraphQL
# 32-bit ``Int`` scalar would silently overflow at 2**31 - 1. We emit a
# string-encoded ``BigInt`` scalar so JS clients past
# ``Number.MAX_SAFE_INTEGER`` (2**53) survive end-to-end. Used as the
# output type for ``SUM`` over integer Django fields. Per SPEC § 5.
BigInt = strawberry.scalar(
    int,
    name="BigInt",
    description=(
        "64-bit signed integer encoded as a JSON string on the wire to "
        "survive JavaScript Number.MAX_SAFE_INTEGER (2**53). Used for "
        "SUM over IntegerField/SmallIntegerField/PositiveIntegerField/"
        "PositiveSmallIntegerField, where Postgres widens to bigint."
    ),
    serialize=str,
    parse_value=int,
)


# ---------------------------------------------------------------------------
# Federation v2 helpers (SPEC § 18).
# ---------------------------------------------------------------------------
#
# When ``enable_federation=True`` the consumer must build the schema with
# ``strawberry.federation.Schema(...)`` (NOT ``strawberry.Schema``) for
# directives to print and for the ``_service`` introspection to be valid.
# Library does not control schema construction; we only emit
# federation-decorated types.

def _type_decorator(enable_federation: bool) -> Any:
    """Return the right object-type decorator for the federation flag."""
    if enable_federation:
        return strawberry.federation.type
    return strawberry.type


def _input_decorator(enable_federation: bool) -> Any:
    """Return the right input-type decorator for the federation flag."""
    if enable_federation:
        return strawberry.federation.input
    return strawberry.input


# ---------------------------------------------------------------------------
# Direction / nulls position enums (no equivalents in strawberry-django).
# ---------------------------------------------------------------------------

@strawberry.enum
class OrderDirection(enum.Enum):
    """Direction for aggregate / group-by ordering."""

    ASC  = "asc"
    DESC = "desc"


@strawberry.enum
class NullsPosition(enum.Enum):
    """NULL position for ordered aggregate / group-by results."""

    FIRST = "first"
    LAST  = "last"


@strawberry.enum
class Granularity(enum.Enum):
    """Union of TIME and NUMBER granularity tokens.

    The TIME track members emit DateTime via ``date_trunc``; the NUMBER
    track emit Int via ``date_part``. Per SPEC § 7.
    """

    YEAR              = TimeGranularity.YEAR.value
    QUARTER           = TimeGranularity.QUARTER.value
    MONTH             = TimeGranularity.MONTH.value
    WEEK              = TimeGranularity.WEEK.value
    DAY               = TimeGranularity.DAY.value
    HOUR              = TimeGranularity.HOUR.value
    MINUTE            = TimeGranularity.MINUTE.value
    SECOND            = TimeGranularity.SECOND.value
    YEAR_NUMBER       = NumberGranularity.YEAR_NUMBER.value
    QUARTER_NUMBER    = NumberGranularity.QUARTER_NUMBER.value
    MONTH_NUMBER      = NumberGranularity.MONTH_NUMBER.value
    ISO_WEEK_NUMBER   = NumberGranularity.ISO_WEEK_NUMBER.value
    DAY_OF_YEAR       = NumberGranularity.DAY_OF_YEAR.value
    DAY_OF_MONTH      = NumberGranularity.DAY_OF_MONTH.value
    DAY_OF_WEEK       = NumberGranularity.DAY_OF_WEEK.value
    HOUR_NUMBER       = NumberGranularity.HOUR_NUMBER.value
    MINUTE_NUMBER     = NumberGranularity.MINUTE_NUMBER.value
    SECOND_NUMBER     = NumberGranularity.SECOND_NUMBER.value


# ---------------------------------------------------------------------------
# Constants — canonical orders mandated by SPEC § 12 / CLAUDE.md Rule 2.
# ---------------------------------------------------------------------------

# Per-row operators (no field-distributed nested type).
_ROW_OPERATORS: tuple[AggregateOp, ...] = (
    AggregateOp.COUNT,
    AggregateOp.COUNT_DISTINCT,
    AggregateOp.COUNT_DISTINCT_TUPLE,
)

# Operators that materialize a `<Model>{Op}Fields` nested type.
#
# ``PERCENTILE_CONT`` / ``PERCENTILE_DISC`` are listed for canonical
# emission ordering (SPEC § 12) but :func:`_emit_nested_operator_types`
# skips their nested type — they go method-style on ``<Model>Aggregate``
# instead so the client can pass a ``fraction`` argument per call.
_FIELD_OPERATORS: tuple[AggregateOp, ...] = (
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
    AggregateOp.BOOL_AND,
    AggregateOp.BOOL_OR,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
)

# Operators wired as method-style fields on ``<Model>Aggregate`` —
# never emit a nested ``<Model>{Op}Fields`` type. They take an extra
# argument (``fraction``) the regular nested-dict shape can't carry.
_METHOD_STYLE_OPERATORS: frozenset[AggregateOp] = frozenset({
    AggregateOp.PERCENTILE_CONT,
    AggregateOp.PERCENTILE_DISC,
})

# Comparisons emitted into HAVING input — same canonical order as
# CLAUDE.md Critical Rule 2 demands for determinism.
_HAVING_COMPARISONS: tuple[str, ...] = (
    "Gt", "Lt", "Lte", "Gte", "Eq", "Neq", "In", "NotIn",
)

_COMPARISON_LOOKUP: dict[str, str] = {
    "Gt": "gt", "Lt": "lt", "Lte": "lte", "Gte": "gte",
    "Eq": "eq", "Neq": "neq", "In": "in", "NotIn": "not_in",
}


# ---------------------------------------------------------------------------
# Field-type → Python-type maps for output-type derivation.
# ---------------------------------------------------------------------------

_NUMERIC_TYPES: frozenset[str] = frozenset({
    "IntegerField", "BigIntegerField", "PositiveIntegerField",
    "PositiveSmallIntegerField", "SmallIntegerField",
    "FloatField", "DecimalField", "DurationField",
})

_INTEGRAL_TYPES: frozenset[str] = frozenset({
    "IntegerField", "BigIntegerField", "PositiveIntegerField",
    "PositiveSmallIntegerField", "SmallIntegerField",
})


def _natural_python_type(field: Field) -> Any:
    """Map a Django field to its natural Python output type.

    Used for MIN/MAX/ARRAY_AGG (where the result type matches the
    field type) and for the group_key composite type.
    """
    name = type(field).__name__
    if name == "DecimalField":
        return decimal.Decimal
    if name in {"FloatField"}:
        return float
    if name in _INTEGRAL_TYPES:
        return int
    if name == "BooleanField":
        return bool
    if name == "DateField":
        return datetime.date
    if name == "DateTimeField":
        return datetime.datetime
    if name == "TimeField":
        return datetime.time
    if name == "UUIDField":
        return uuid.UUID
    if name == "DurationField":
        return datetime.timedelta
    if name in {"AutoField", "BigAutoField", "SmallAutoField"}:
        return strawberry.ID
    if name in {"ForeignKey", "OneToOneField"}:
        return strawberry.ID
    # CharField / TextField / EmailField / URLField / SlugField fall
    # through to str. Any unexpected field type also defaults to str —
    # safer than crashing schema generation.
    return str


def _aggregate_python_type(op: AggregateOp, field: Field) -> Any:
    """Output Python type for ``(op, field)`` — drives nested-type fields."""
    if op in {AggregateOp.MIN, AggregateOp.MAX}:
        return _natural_python_type(field)
    if op is AggregateOp.SUM:
        if type(field).__name__ == "DecimalField":
            return decimal.Decimal
        if type(field).__name__ == "FloatField":
            return float
        if type(field).__name__ in _INTEGRAL_TYPES:
            # Integer-field SUM widens to Postgres ``bigint``; the
            # 32-bit GraphQL ``Int`` would silently overflow above
            # 2**31 - 1, and JS ``Number`` is unsafe past 2**53. Emit
            # the custom ``BigInt`` scalar (string on the wire) for
            # all integer field types — including ``BigIntegerField``
            # whose natural Python type is also ``int`` but whose SUM
            # result is unbounded.
            return BigInt
        return _natural_python_type(field)
    if op is AggregateOp.AVG:
        if type(field).__name__ == "DecimalField":
            return decimal.Decimal
        return float
    if op in {
        AggregateOp.STDDEV,
        AggregateOp.VARIANCE,
        AggregateOp.STDDEV_POP,
        AggregateOp.VAR_POP,
    }:
        return float
    if op in {AggregateOp.PERCENTILE_CONT, AggregateOp.PERCENTILE_DISC}:
        # Method-style fields define their own return type; the
        # nested-type emitter never asks for these values, but we
        # answer for callers that introspect the helper directly.
        # ``percentile_disc`` returns ``Float`` in v1.0 (cast); full
        # type-faithful resolution lands in v1.x — see SPEC § 5.1.
        return float
    if op is AggregateOp.MODE:
        # ``MODE() WITHIN GROUP (ORDER BY col)`` returns the column
        # type — same shape as ``MIN``/``MAX``.
        return _natural_python_type(field)
    if op in {AggregateOp.BOOL_AND, AggregateOp.BOOL_OR}:
        return bool
    if op is AggregateOp.ARRAY_AGG:
        # `list[X]` runtime construct is fine for dataclass field type
        # but mypy doesn't accept dynamic subscripting; suppressed.
        item_type = _natural_python_type(field)
        return list[item_type]  # type: ignore[misc,valid-type]
    if op is AggregateOp.STRING_AGG:
        return str
    raise ValueError(f"Unhandled operator {op!r}")  # defensive


# ---------------------------------------------------------------------------
# Allowlist resolution
# ---------------------------------------------------------------------------

def _resolve_aggregate_fields(
    model: type[Model],
    aggregate_fields: list[str] | None,
) -> list[str]:
    """When ``aggregate_fields`` is None, default to all concrete model
    fields whose default operator allowlist is non-empty.
    """
    if aggregate_fields is not None:
        return list(aggregate_fields)
    eligible: list[str] = []
    for field in model._meta.get_fields():
        # Skip reverse relations (auto-created) and many-to-many.
        if getattr(field, "auto_created", False) and not getattr(
            field, "primary_key", False,
        ):
            continue
        if getattr(field, "many_to_many", False) \
                or getattr(field, "one_to_many", False):
            continue
        type_name = type(field).__name__
        if default_operators_for(type_name):
            eligible.append(field.name)
    return eligible


def _resolve_group_by_fields(
    model: type[Model],
    group_by_fields: list[str] | None,
) -> list[str]:
    if group_by_fields is not None:
        return list(group_by_fields)
    eligible: list[str] = []
    for field in model._meta.get_fields():
        if getattr(field, "auto_created", False) and not getattr(
            field, "primary_key", False,
        ):
            continue
        if getattr(field, "many_to_many", False) \
                or getattr(field, "one_to_many", False):
            continue
        eligible.append(field.name)
    return eligible


def _allowed_ops_for(
    model: type[Model],
    field_name: str,
    overrides: dict[str, tuple[AggregateOp, ...]],
) -> tuple[AggregateOp, ...]:
    if field_name in overrides:
        return overrides[field_name]
    field = model._meta.get_field(field_name)
    return default_operators_for(type(field).__name__)


# ---------------------------------------------------------------------------
# Type-emission helpers
# ---------------------------------------------------------------------------

def _make_dataclass(
    name: str,
    fields: list[tuple[str, Any, Any]],
) -> type:
    """Build a dataclass with ``Optional`` defaults.

    ``fields`` is ``[(name, type, default)]``; pass
    ``dataclasses.MISSING`` for required fields.
    """
    dc_fields = [
        (n, t, dataclasses.field(default=d)) if d is not dataclasses.MISSING
        else (n, t)
        for (n, t, d) in fields
    ]
    return dataclasses.make_dataclass(name, dc_fields)


# ---------------------------------------------------------------------------
# make_aggregate_type
# ---------------------------------------------------------------------------

def make_aggregate_type(
    model: type[Model],
    *,
    name: str | None = None,
    aggregate_fields: list[str] | None = None,
    operators: dict[str, tuple[AggregateOp, ...]] | None = None,
    enable_federation: bool = False,
) -> type:
    """Build the ``<Model>Aggregate`` strawberry type.

    Per SPEC § 4: ``count``, ``count_distinct(field: ...)`` plus one
    nullable nested type per field-distributed operator. Nested types
    are emitted in canonical order (``sum, avg, min, max, stddev,
    variance, bool_and, bool_or, array_agg, string_agg``) per Rule 2.

    When ``enable_federation=True`` the emitted type is decorated with
    :func:`strawberry.federation.type` instead of :func:`strawberry.type`
    so it is recognized by an Apollo Federation v2 gateway. The aggregate
    container itself receives no ``@key`` directive in v1.0 — see
    SPEC § 18 for the deferred ``@key`` design discussion. Nested
    operator types (``<Model>SumFields`` etc.) are also federation-decorated
    so their schema-printer payload remains consistent.
    """
    name      = name or model.__name__
    overrides = dict(sorted((operators or {}).items()))
    fields    = _resolve_aggregate_fields(model, aggregate_fields)

    nested_types = _emit_nested_operator_types(
        model, name, fields, overrides,
        enable_federation=enable_federation,
    )
    countable_enum = _emit_countable_enum(model, name, fields, overrides)

    aggregate_dc_fields: list[tuple[str, Any, Any]] = [
        ("count", int, 0),
    ]
    for op in _FIELD_OPERATORS:
        if op in nested_types:
            aggregate_dc_fields.append((
                op.value, nested_types[op] | None, None,
            ))

    cls = _make_dataclass(f"{name}Aggregate", aggregate_dc_fields)

    # count_distinct is a method-style field (takes a `field` argument)
    # so we attach it via class body, not the dataclass field list.
    #
    # Accepts EITHER ``field: <Enum>`` (single column — emits
    # ``COUNT(DISTINCT a)``) OR ``fields: [<Enum>!]`` (multi-column
    # tuple — emits ``COUNT(DISTINCT (a, b, c))`` per SPEC § 5
    # Hasura-style sub-section). Exactly one of the two must be set;
    # both-set or neither-set raises a ``ValueError`` at resolver
    # entry. The single-field shape is backward-compatible with the
    # pre-Stream-8 wire format.
    def _count_distinct_resolver(
        self: Any,
        field: Any = strawberry.UNSET,
        fields: Any = strawberry.UNSET,
    ) -> int:
        single_set = (
            field is not strawberry.UNSET and field is not None
        )
        multi_set = (
            fields is not strawberry.UNSET and fields is not None
        )
        if single_set == multi_set:
            raise ValueError(
                "countDistinct: pass exactly one of `field` "
                "(single-column) or `fields` (multi-column tuple). "
                f"Got field={field!r}, fields={fields!r}.",
            )
        if single_set:
            backing = getattr(self, "__count_distinct__", None) or {}
            return int(backing.get(field.value, 0))
        # Multi-column tuple — key on a sorted tuple of field-name
        # strings so wire-input order does not affect the lookup.
        tuple_backing = (
            getattr(self, "__count_distinct_tuple__", None) or {}
        )
        key = tuple(sorted(f.value for f in fields))
        return int(tuple_backing.get(key, 0))

    _count_distinct_resolver.__annotations__ = {
        "self":   Any,
        "field":  countable_enum | None,
        # ``list[countable_enum] | None`` is a runtime-built type
        # Strawberry consumes correctly, but mypy can't statically
        # subscript a ``Variable`` by ``list[...]`` so we suppress.
        "fields": list[countable_enum] | None,  # type: ignore[valid-type]
        "return": int,
    }
    cls.count_distinct = strawberry.field(  # type: ignore[attr-defined]
        resolver=_count_distinct_resolver,
        description=(
            "COUNT(DISTINCT <field>) for a single column, or "
            "COUNT(DISTINCT (a, b, c)) for a multi-column tuple. "
            "Pass exactly one of `field` or `fields`."
        ),
    )

    # PERCENTILE_CONT / PERCENTILE_DISC are method-style for the same
    # reason as ``count_distinct``: they take an extra argument
    # (``fraction``) the regular nested-dict shape can't carry. Both
    # share a single ``<Model>PercentileField`` enum listing fields
    # whose allowlist includes ``PERCENTILE_CONT`` (or
    # ``PERCENTILE_DISC``). Wired only when at least one field in the
    # allowlist supports either operator — otherwise the enum would
    # have no members and Strawberry would fail at schema build time.
    percentile_enum = _emit_percentile_enum(
        model, name, fields, overrides,
    )
    if percentile_enum is not None:
        def _percentile_cont_resolver(
            self: Any, field: Any, fraction: float,
        ) -> float | None:
            backing = (
                getattr(self, "__percentile_cont__", None) or {}
            )
            value = backing.get((field.value, float(fraction)))
            return None if value is None else float(value)

        _percentile_cont_resolver.__annotations__ = {
            "self":     Any,
            "field":    percentile_enum,
            "fraction": float,
            "return":   float | None,
        }
        cls.percentile_cont = strawberry.field(  # type: ignore[attr-defined]
            resolver=_percentile_cont_resolver,
            description=(
                "PERCENTILE_CONT(fraction) WITHIN GROUP (ORDER BY field) — "
                "interpolated percentile. Postgres only."
            ),
        )

        def _percentile_disc_resolver(
            self: Any, field: Any, fraction: float,
        ) -> float | None:
            backing = (
                getattr(self, "__percentile_disc__", None) or {}
            )
            value = backing.get((field.value, float(fraction)))
            return None if value is None else float(value)

        _percentile_disc_resolver.__annotations__ = {
            "self":     Any,
            "field":    percentile_enum,
            "fraction": float,
            "return":   float | None,
        }
        cls.percentile_disc = strawberry.field(  # type: ignore[attr-defined]
            resolver=_percentile_disc_resolver,
            description=(
                "PERCENTILE_DISC(fraction) WITHIN GROUP (ORDER BY field) — "
                "discrete percentile. Returns Float in v1.0; full type "
                "fidelity in v1.x. Postgres only."
            ),
        )

    return _type_decorator(enable_federation)(cls)


def _emit_nested_operator_types(
    model: type[Model],
    name: str,
    fields: list[str],
    overrides: dict[str, tuple[AggregateOp, ...]],
    *,
    enable_federation: bool = False,
) -> dict[AggregateOp, type]:
    """Build the per-operator ``<Model>{Op}Fields`` types.

    Only operators that have at least one applicable field on the
    allowlist materialize. Field declaration order within each nested
    type follows ``fields`` (caller-controlled, deterministic).
    """
    decorator = _type_decorator(enable_federation)
    out: dict[AggregateOp, type] = {}
    for op in _FIELD_OPERATORS:
        # PERCENTILE_CONT / PERCENTILE_DISC are method-style — they
        # receive the ``field`` and ``fraction`` as resolver args and
        # return a single ``Float`` directly. No nested type.
        if op in _METHOD_STYLE_OPERATORS:
            continue
        op_fields: list[tuple[str, Any, Any]] = []
        for field_name in fields:
            allowed = _allowed_ops_for(model, field_name, overrides)
            if op not in allowed:
                continue
            field = model._meta.get_field(field_name)
            py_type = _aggregate_python_type(op, field)  # type: ignore[arg-type]
            op_fields.append((field_name, (py_type | None), None))
        if not op_fields:
            continue
        nested_name = f"{name}{_op_class_suffix(op)}Fields"
        cls = _make_dataclass(nested_name, op_fields)
        out[op] = decorator(cls)
    return out


def _op_class_suffix(op: AggregateOp) -> str:
    """``AggregateOp.SUM`` → ``"Sum"``; ``BOOL_AND`` → ``"BoolAnd"``."""
    return "".join(part.capitalize() for part in op.value.split("_"))


def _emit_percentile_enum(
    model: type[Model],
    name: str,
    fields: list[str],
    overrides: dict[str, tuple[AggregateOp, ...]],
) -> type[enum.Enum] | None:
    """Build ``<Model>PercentileField`` — fields whose allowlist allows
    either percentile op.

    Shared by ``percentileCont`` and ``percentileDisc`` resolvers; if
    a field allows one but not the other, the resolver still works
    (the request raises ``OperatorNotSupportedError`` only at compile
    time when the op is dispatched against an unallowed field). For
    v1.0 we keep the enum permissive; finer per-op enums are deferred.

    Returns ``None`` when no field on the allowlist supports either
    percentile op — in that case the caller must skip method-field
    emission entirely (Strawberry rejects an empty enum).
    """
    members: list[tuple[str, str]] = []
    for field_name in fields:
        allowed = _allowed_ops_for(model, field_name, overrides)
        if (
            AggregateOp.PERCENTILE_CONT in allowed
            or AggregateOp.PERCENTILE_DISC in allowed
        ):
            members.append((field_name.upper(), field_name))
    if not members:
        return None
    enum_cls = enum.Enum(  # type: ignore[misc]
        f"{name}PercentileField", members,
    )
    return strawberry.enum(enum_cls)


def _emit_countable_enum(
    model: type[Model],
    name: str,
    fields: list[str],
    overrides: dict[str, tuple[AggregateOp, ...]],
) -> type[enum.Enum]:
    """Build ``<Model>CountableField`` — fields whose allowlist includes
    ``COUNT`` or ``COUNT_DISTINCT`` (in practice all model PKs / FKs).
    """
    members: list[tuple[str, str]] = [("ID", "id")]
    for field_name in fields:
        allowed = _allowed_ops_for(model, field_name, overrides)
        if AggregateOp.COUNT_DISTINCT in allowed or not allowed:
            # Always allow plain field-name distinct counts on
            # allowlisted fields; they're row-level, not value-level.
            pass
        members.append((field_name.upper(), field_name))
    # Deduplicate while preserving canonical order: ID first, then
    # field declaration order.
    seen: set[str] = set()
    unique_members: list[tuple[str, str]] = []
    for k, v in members:
        if v in seen:
            continue
        seen.add(v)
        unique_members.append((k, v))
    enum_cls = enum.Enum(  # type: ignore[misc]
        f"{name}CountableField", unique_members,
    )
    return strawberry.enum(enum_cls)


# ---------------------------------------------------------------------------
# make_grouped_type
# ---------------------------------------------------------------------------

def make_grouped_type(
    model: type[Model],
    *,
    name: str | None = None,
    aggregate_type: type | None = None,
    aggregate_fields: list[str] | None = None,
    group_by_fields: list[str] | None = None,
    operators: dict[str, tuple[AggregateOp, ...]] | None = None,
    enable_federation: bool = False,
) -> tuple[type, type, type]:
    """Build ``<Model>GroupKey``, ``<Model>Grouped``, and
    ``<Model>GroupedResult`` types.

    Returns ``(group_key_type, grouped_type, grouped_result_type)``.
    The grouped type is FLAT — no ``subgroups`` recursion. Multi-level
    group_by produces multiple result rows with composite keys (SPEC §4).

    When ``enable_federation=True`` all three returned types are
    decorated with :func:`strawberry.federation.type`. Foreign-key
    fields on ``<Model>GroupKey`` (e.g. ``customer_id``) are marked
    ``@external`` since their canonical ownership lives in another
    subgraph — see SPEC § 18.
    """
    name      = name or model.__name__
    overrides = dict(sorted((operators or {}).items()))
    g_fields  = _resolve_group_by_fields(model, group_by_fields)
    a_fields  = _resolve_aggregate_fields(model, aggregate_fields)

    group_key_cls = _emit_group_key(
        model, name, g_fields, enable_federation=enable_federation,
    )
    nested_types  = _emit_nested_operator_types(
        model, name, a_fields, overrides,
        enable_federation=enable_federation,
    )

    grouped_dc_fields: list[tuple[str, Any, Any]] = [
        ("key", group_key_cls, dataclasses.MISSING),
        ("count", int, 0),
    ]
    for op in _FIELD_OPERATORS:
        if op in nested_types:
            grouped_dc_fields.append(
                (op.value, nested_types[op] | None, None),
            )
    decorator = _type_decorator(enable_federation)
    grouped_cls = decorator(_make_dataclass(
        f"{name}Grouped", grouped_dc_fields,
    ))

    # GroupedResult mirrors the OffsetPaginated shape (results +
    # page_info + total_count) without subclassing the generic — that
    # way the resolver can hand back precomputed values rather than a
    # queryset (which doesn't apply to dict-based aggregation rows).
    grouped_result_cls = decorator(_make_dataclass(
        f"{name}GroupedResult",
        [
            ("results", list[grouped_cls], dataclasses.MISSING),  # type: ignore[valid-type]
            ("page_info", OffsetPaginationInfo, dataclasses.MISSING),
            ("total_count", int, 0),
        ],
    ))

    return group_key_cls, grouped_cls, grouped_result_cls


def _emit_group_key(
    model: type[Model],
    name: str,
    g_fields: list[str],
    *,
    enable_federation: bool = False,
) -> type:
    """Build ``<Model>GroupKey`` — every allowlisted group_by field as
    Optional, plus bucket fields for date/datetime entries.

    Bucket field names follow the alias convention
    :func:`compiler.group_by_alias` (e.g. ``created_at_month``,
    ``created_at_day_of_week``). Caller-side resolver populates only
    the keys present in the actual ``group_by`` request.

    When ``enable_federation=True``, FK ``<name>_id`` fields are marked
    ``@external`` because their canonical record lives in another
    subgraph (SPEC § 18). The non-federation path emits plain dataclass
    fields, identical to v0.x output.
    """
    key_fields: list[tuple[str, Any, Any]] = []
    fk_field_names: list[str] = []
    for field_name in g_fields:
        field = model._meta.get_field(field_name)
        py_type = _natural_python_type(field)  # type: ignore[arg-type]
        # FK fields surface as `<name>_id` per SPEC § 4.
        if getattr(field, "many_to_one", False):
            fk_id_name = f"{field_name}_id"
            key_fields.append((fk_id_name, (py_type | None), None))
            fk_field_names.append(fk_id_name)
        else:
            key_fields.append((field_name, (py_type | None), None))

        # Date/datetime fields: emit per-granularity bucket aliases
        # so the resolver can populate whichever the user requested.
        if type(field).__name__ in {"DateField", "DateTimeField"}:
            for time_grain in TimeGranularity:
                key_fields.append((
                    f"{field_name}_{time_grain.value}",
                    (datetime.datetime | None),
                    None,
                ))
            for num_grain in NumberGranularity:
                key_fields.append((
                    f"{field_name}_{num_grain.value}",
                    (int | None),
                    None,
                ))

    cls = _make_dataclass(f"{name}GroupKey", key_fields)

    if enable_federation:
        # Re-bind FK `<name>_id` attributes as federation fields with
        # ``@external``. Done AFTER ``make_dataclass`` because passing
        # a federation field as a dataclass default does not mark the
        # resulting attribute as external — the federation directive
        # is attached only when the attribute is later read by the
        # ``strawberry.federation.type`` decorator.
        for fk_name in fk_field_names:
            setattr(
                cls, fk_name,
                strawberry.federation.field(default=None, external=True),
            )
    return _type_decorator(enable_federation)(cls)


# ---------------------------------------------------------------------------
# make_having_input
# ---------------------------------------------------------------------------

def make_having_input(
    model: type[Model],
    *,
    name: str | None = None,
    aggregate_fields: list[str] | None = None,
    operators: dict[str, tuple[AggregateOp, ...]] | None = None,
    enable_federation: bool = False,
) -> type:
    """Build the ``<Model>Having`` strawberry input type.

    Emits one input field per ``(measure, comparison)`` tuple where
    ``measure`` is ``count`` or ``<op>_<field>`` and ``comparison`` is
    one of the canonical 8. Fields are ``(T | None)`` so callers can
    selectively set comparisons. Per SPEC § 8.

    ``enable_federation=True`` swaps :func:`strawberry.input` for
    :func:`strawberry.federation.input`. Federation v2 input types do
    not accept ``@external`` (it is an output-only directive); the flag
    is forwarded for symmetry and so the resulting input is registered
    consistently in the federation subgraph.
    """
    name      = name or model.__name__
    overrides = dict(sorted((operators or {}).items()))
    fields    = _resolve_aggregate_fields(model, aggregate_fields)

    measures: list[tuple[str, Any]] = []  # (measure_name, value_type)

    # count is per-row, value_type=int.
    measures.append(("count", int))

    for op in _FIELD_OPERATORS:
        # Method-style ops (PERCENTILE_CONT/DISC) emit no HAVING field
        # in v1.0 — their canonical alias encodes the fraction
        # (``percentile_cont_total_50``) which the static HAVING-input
        # shape can't carry. HAVING on percentiles is deferred.
        if op in _METHOD_STYLE_OPERATORS:
            continue
        for field_name in fields:
            allowed = _allowed_ops_for(model, field_name, overrides)
            if op not in allowed:
                continue
            field = model._meta.get_field(field_name)
            value_type = _aggregate_python_type(
                op, field,  # type: ignore[arg-type]
            )
            measures.append((f"{op.value}_{field_name}", value_type))

    having_dc_fields: list[tuple[str, Any, Any]] = []
    for measure, value_type in measures:
        for cmp in _HAVING_COMPARISONS:
            input_field = f"{measure}_{_COMPARISON_LOOKUP[cmp]}"
            if cmp in {"In", "NotIn"}:
                py_type: Any = list[value_type]  # type: ignore[valid-type]
            else:
                py_type = value_type
            having_dc_fields.append(
                (input_field, (py_type | None), None),
            )

    cls = _make_dataclass(f"{name}Having", having_dc_fields)
    return _input_decorator(enable_federation)(cls)


# ---------------------------------------------------------------------------
# make_group_by_spec
# ---------------------------------------------------------------------------

def make_group_by_spec(
    model: type[Model],
    *,
    name: str | None = None,
    group_by_fields: list[str] | None = None,
    enable_federation: bool = False,
) -> tuple[type, type]:
    """Build ``<Model>GroupBySpec`` (input) + ``<Model>GroupableField``
    (enum). Returns ``(spec_type, enum_type)``.

    The spec input has fields ``field: <Model>GroupableField!`` and
    ``granularity: Granularity`` (nullable; required only on date /
    datetime fields per SPEC § 6).

    ``enable_federation=True`` swaps the input decorator to
    :func:`strawberry.federation.input` so the type is registered with
    the federation subgraph. The enum is unchanged.
    """
    name     = name or model.__name__
    g_fields = _resolve_group_by_fields(model, group_by_fields)

    members = [(field_name.upper(), field_name) for field_name in g_fields]
    enum_cls = enum.Enum(  # type: ignore[misc]
        f"{name}GroupableField", members,
    )
    field_enum = strawberry.enum(enum_cls)

    spec_cls = _make_dataclass(
        f"{name}GroupBySpec",
        [
            ("field", field_enum, dataclasses.MISSING),
            ("granularity", (Granularity | None), None),
        ],
    )
    return _input_decorator(enable_federation)(spec_cls), field_enum


# ---------------------------------------------------------------------------
# make_group_order_input
# ---------------------------------------------------------------------------

def make_group_order_input(
    model: type[Model],
    *,
    name: str | None = None,
    enable_federation: bool = False,
) -> type:
    """Build ``<Model>GroupOrder`` — order input for groupBy results.

    Per SPEC § 9: ``field: String!`` (resolved at parse time against
    aggregate aliases / group_by aliases / plain field allowlist),
    ``direction: OrderDirection!``, ``nulls: NullsPosition``.

    ``enable_federation=True`` swaps the input decorator to
    :func:`strawberry.federation.input` (SPEC § 18).
    """
    name = name or model.__name__
    cls = _make_dataclass(
        f"{name}GroupOrder",
        [
            ("field", str, dataclasses.MISSING),
            ("direction", OrderDirection, OrderDirection.ASC),
            ("nulls", (NullsPosition | None), None),
        ],
    )
    return _input_decorator(enable_federation)(cls)
