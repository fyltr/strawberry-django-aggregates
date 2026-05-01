"""Backend primitive — :func:`compute_aggregation`.

Mirrors Odoo's ``BaseModel._read_group`` (``odoo/models.py:1965``):
backend-first API returning flat composite-key result rows. Callable
from any Python context — DRF view, Celery task, admin script, MCP tool
— not just GraphQL resolvers.

Separation of concerns: this primitive returns rows; the GraphQL
resolver (in :mod:`strawberry_django_aggregates.builder`) is the
presentation wrapper that shapes those rows into the Strawberry types.

This module imports from Django and the local error / vocabulary
modules only — never from ``strawberry`` or ``strawberry_django``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connections
from django.db.models import (
    Aggregate,
    Avg,
    BooleanField,
    Count,
    DateField,
    DateTimeField,
    F,
    Func,
    IntegerField,
    Max,
    Min,
    Q,
    StdDev,
    Sum,
    TimeField,
    Variance,
)
from django.db.models.functions import Extract, Trunc
from django.db.models.functions.datetime import TimezoneMixin

from strawberry_django_aggregates.errors import (
    AggregateError,
    AggregationAcrossRelationError,
    GranularityNotApplicable,
    GroupByFieldNotAllowed,
    HavingFieldNotAllowed,
    OperatorNotSupportedError,
    OrderFieldNotAllowed,
)
from strawberry_django_aggregates.granularity import (
    Granularity,
    NumberGranularity,
    TimeGranularity,
)
from strawberry_django_aggregates.operators import AggregateOp

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.db.models.fields import Field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Operators that compile to Postgres-only SQL constructs. Detected at
# resolver entry; we raise OperatorNotSupportedError before any SQL fires
# rather than letting the database emit an opaque vendor error.
_POSTGRES_ONLY_OPS: frozenset[AggregateOp] = frozenset({
    AggregateOp.STDDEV,
    AggregateOp.VARIANCE,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
})

# HAVING comparison whitelist — mirrors Odoo's _read_group_having
# SUPPORTED tuple (odoo/models.py:2189) translated to Django lookups.
# Canonical emission order per CLAUDE.md Critical Rule 2.
HAVING_COMPARISONS: tuple[str, ...] = (
    "gt", "lt", "lte", "gte", "eq", "neq", "in", "not_in",
)

# Mapping NumberGranularity → Django Extract lookup_name.
# Django's Trunc/Extract handle the tz wrap correctly — postgres backend
# emits `DATE_TRUNC(kind, col AT TIME ZONE tz)` and likewise for EXTRACT
# (see django/db/backends/postgresql/operations.py:135).
_NUMBER_LOOKUP: dict[NumberGranularity, str] = {
    NumberGranularity.YEAR_NUMBER:     "year",
    NumberGranularity.QUARTER_NUMBER:  "quarter",
    NumberGranularity.MONTH_NUMBER:    "month",
    NumberGranularity.ISO_WEEK_NUMBER: "week",
    NumberGranularity.DAY_OF_MONTH:    "day",
    NumberGranularity.DAY_OF_WEEK:     "iso_week_day",
    NumberGranularity.HOUR_NUMBER:     "hour",
    NumberGranularity.MINUTE_NUMBER:   "minute",
    NumberGranularity.SECOND_NUMBER:   "second",
    # DAY_OF_YEAR uses a custom Func below (Django has no builtin).
}


class _ExtractDayOfYear(TimezoneMixin, Func):
    """Day-of-year (1–366) extractor — uniform across PG and SQLite.

    Django ships ``ExtractWeek``, ``ExtractIsoWeekDay``, etc. but no
    builtin for DOY. We inherit :class:`TimezoneMixin` so ``tzinfo`` is
    threaded through the same ``AT TIME ZONE`` wrap Django uses for
    ``Extract`` / ``Trunc`` (postgres operations.py:135). Without that
    mixin, ``tz="Asia/Tokyo"`` would be silently dropped — exactly the
    failure mode CLAUDE.md Critical Rule 6 forbids.
    """

    output_field = IntegerField()
    function = ""
    template = "EXTRACT(DOY FROM %(expressions)s)"

    def __init__(self, expression: Any, tzinfo: Any = None) -> None:
        self.tzinfo = tzinfo
        super().__init__(expression)

    def as_sql(  # type: ignore[override]
        self, compiler: Any, connection: Any, **extra: Any,
    ) -> Any:
        sql, params = compiler.compile(self.source_expressions[0])
        sql, params = connection.ops._convert_sql_to_tz(
            sql, tuple(params), self.get_tzname(),
        )
        return f"EXTRACT(DOY FROM {sql})", params

    def as_sqlite(  # type: ignore[override]
        self, compiler: Any, connection: Any, **extra: Any,
    ) -> Any:
        # SQLite has no AT TIME ZONE; tz handling on SQLite is best-
        # effort per SPEC. ``strftime('%j', ...)`` returns the day of
        # year (1–366) as a string, which we cast to int.
        sql, params = compiler.compile(self.source_expressions[0])
        return (
            f"CAST(strftime('%%j', {sql}) AS INTEGER)",
            tuple(params),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_aggregation(
    queryset: QuerySet,
    *,
    group_by:   list[tuple[str, Granularity | None]] | None = None,
    aggregates: list[tuple[AggregateOp, str | None]]   | None = None,
    having:     dict[str, Any]                          | None = None,
    order_by:   list[tuple[str, str, str | None]]      | None = None,
    offset:     int = 0,
    limit:      int | None = None,
    tz:         str | None = None,
) -> list[dict[str, Any]]:
    """Compile a queryset into an aggregation query.

    See ``docs/SPEC.md`` § 10 for the full contract. Permission-naive —
    the queryset must already be scoped by the caller.
    """
    group_by    = group_by    or []
    aggregates  = aggregates  or []
    having      = having      or {}
    order_by    = order_by    or []
    vendor      = connections[queryset.db].vendor
    model       = queryset.model
    tz_name     = tz or settings.TIME_ZONE
    tzinfo      = _resolve_tzinfo(tz_name)

    if having and not group_by:
        raise AggregateError(
            "HAVING requires a non-empty `group_by` — there is nothing "
            "to filter without group buckets. Add a `group_by` or "
            "filter on the queryset directly with `.filter(...)`."
        )

    _validate_postgres_only(aggregates, vendor)

    group_annotations, group_aliases = _build_group_by_annotations(
        model, group_by, tzinfo,
    )

    aggregate_annotations = _build_aggregate_annotations(
        model, aggregates, vendor,
    )

    having_q = _build_having_q(having, aggregate_annotations.keys())

    order_terms = _build_order_terms(
        order_by, group_aliases, list(aggregate_annotations.keys()),
    )

    qs = queryset
    has_grouping = bool(group_aliases)
    if has_grouping:
        if group_annotations:
            qs = qs.annotate(**group_annotations)
        qs = qs.values(*group_aliases)
        qs = qs.annotate(**aggregate_annotations)
    else:
        # Single-row aggregate. Use .aggregate() — Django returns a
        # plain dict and skips the GROUP BY altogether. (HAVING with
        # no group_by was rejected at the top of compute_aggregation.)
        return [qs.aggregate(**aggregate_annotations)]

    if having_q is not None:
        qs = qs.filter(having_q)

    if order_terms:
        qs = qs.order_by(*order_terms)

    if offset or limit is not None:
        stop = (offset + limit) if limit is not None else None
        qs = qs[offset:stop]

    return list(qs)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _resolve_tzinfo(tz_name: str) -> ZoneInfo:
    """Resolve an IANA tz name into a ZoneInfo.

    Raises ``ZoneInfoNotFoundError`` for invalid names — the standard
    exception is appropriate here; the caller surfaces it as a
    user-facing error if needed.
    """
    return ZoneInfo(tz_name)


def _validate_postgres_only(
    aggregates: list[tuple[AggregateOp, str | None]],
    vendor: str,
) -> None:
    if vendor == "postgresql":
        return
    for op, _ in aggregates:
        if op in _POSTGRES_ONLY_OPS:
            raise OperatorNotSupportedError(
                f"Aggregate operator {op.value!r} requires PostgreSQL "
                f"— current connection vendor is {vendor!r}. "
                f"Switch to a PostgreSQL connection, or compute the "
                f"value in application code (for stddev/variance, "
                f"fetch the rows and compute in Python; for "
                f"array_agg / string_agg, query the child rows "
                f"directly)."
            )


def _resolve_field(
    model: Any, field_path: str, error_cls: type[Exception],
) -> Field:
    """Resolve a *single-segment* field name. Raises if it traverses a
    relation (``__`` in path) or is not an attribute of the model.
    """
    if "__" in field_path:
        raise AggregationAcrossRelationError(
            f"Cannot aggregate across relation `{field_path}` from "
            f"`{model.__name__}` — would cause silent row multiplication. "
            f"Query the related model directly with the parent FK in "
            f"`group_by` instead."
        )
    try:
        return model._meta.get_field(field_path)
    except Exception as exc:
        raise error_cls(
            f"Field `{field_path}` not found on `{model.__name__}`."
        ) from exc


def _is_relation_to_many(field: Field) -> bool:
    return bool(getattr(field, "one_to_many", False)
                or getattr(field, "many_to_many", False))


# ---------------------------------------------------------------------------
# group_by annotations
# ---------------------------------------------------------------------------

def _build_group_by_annotations(
    model: type,
    group_by: list[tuple[str, Granularity | None]],
    tzinfo: ZoneInfo,
) -> tuple[dict[str, Any], list[str]]:
    """Build the ``.annotate()`` kwargs that materialize each group_by
    spec, plus the canonical alias list to feed into ``.values()``.

    Non-bucket group_by entries are NOT annotated — Django rejects
    annotation aliases that collide with concrete model columns
    (e.g. annotating ``customer_id`` on a model with a ``customer``
    FK). They go straight into ``.values(attname)`` and Django emits
    them as a GROUP BY column.
    """
    annotations: dict[str, Any] = {}
    aliases:     list[str]      = []

    for field_path, granularity in group_by:
        field = _resolve_field(model, field_path, GroupByFieldNotAllowed)
        if _is_relation_to_many(field):
            raise AggregationAcrossRelationError(
                f"Cannot group by relation `{field_path}` from "
                f"`{model.__name__}` — would row-multiply."
            )
        alias = group_by_alias(field_path, granularity, field)
        if granularity is not None:
            annotations[alias] = _build_group_by_expression(
                field_path, granularity, field, tzinfo,
            )
        aliases.append(alias)

    return annotations, aliases


def group_by_alias(
    field_path: str,
    granularity: Granularity | None,
    field: Field | None = None,
) -> str:
    """Canonical output alias for a (field, granularity) pair.

    - ``("customer", None)`` with FK field → ``"customer_id"``
    - ``("status",   None)`` with plain field → ``"status"``
    - ``("created_at", TimeGranularity.MONTH)`` → ``"created_at_month"``
    - ``("created_at", NumberGranularity.DAY_OF_WEEK)`` →
      ``"created_at_day_of_week"``
    """
    if granularity is not None:
        return f"{field_path}_{granularity.value}"
    if field is not None and getattr(field, "is_relation", False) \
            and getattr(field, "many_to_one", False):
        return f"{field_path}_id"
    return field_path


def _build_group_by_expression(
    field_path: str,
    granularity: Granularity | None,
    field: Field,
    tzinfo: ZoneInfo,
) -> Any:
    """Build the Django expression for a group_by spec.

    For non-bucketed specs we emit ``F(field_path)``. For date buckets
    we emit ``Trunc`` / ``Extract`` with ``tzinfo=`` so Django's backend
    inserts the ``AT TIME ZONE`` wrap *before* truncation
    (postgres/operations.py:135–138; mirrors Odoo's wrap order).
    """
    if granularity is None:
        return F(field_path)

    if not isinstance(field, (DateField, DateTimeField, TimeField)):
        raise GranularityNotApplicable(
            f"Granularity {granularity!r} cannot apply to field "
            f"`{field_path}` of type {type(field).__name__}."
        )

    # Trunc/Extract emit `AT TIME ZONE` *before* truncation when
    # ``tzinfo`` is set — the wrap order Odoo gets right
    # (postgres operations.py:135). Date fields don't carry a tz.
    is_dt = isinstance(field, DateTimeField)
    tz_kw: dict[str, Any] = {"tzinfo": tzinfo} if is_dt else {}

    if isinstance(granularity, TimeGranularity):
        return Trunc(field_path, granularity.value, **tz_kw)

    if isinstance(granularity, NumberGranularity):
        if granularity is NumberGranularity.DAY_OF_YEAR:
            return _ExtractDayOfYear(field_path, **tz_kw)
        return Extract(field_path, _NUMBER_LOOKUP[granularity], **tz_kw)

    raise GranularityNotApplicable(  # defensive
        f"Unknown granularity {granularity!r}."
    )


# ---------------------------------------------------------------------------
# aggregate annotations
# ---------------------------------------------------------------------------

def _build_aggregate_annotations(
    model: type,
    aggregates: list[tuple[AggregateOp, str | None]],
    vendor: str,
) -> dict[str, Aggregate]:
    annotations: dict[str, Aggregate] = {}
    for op, field_path in aggregates:
        if field_path is not None:
            _resolve_field(model, field_path, GroupByFieldNotAllowed)
        alias = aggregate_alias(op, field_path)
        annotations[alias] = _build_aggregate_expression(
            op, field_path, vendor,
        )
    return annotations


def aggregate_alias(
    op: AggregateOp, field_path: str | None,
) -> str:
    """Canonical alias for an ``(op, field)`` aggregate spec.

    - ``(COUNT, None)`` → ``"count"``
    - ``(COUNT_DISTINCT, "customer")`` → ``"count_distinct_customer"``
    - ``(SUM, "total")`` → ``"sum_total"``
    """
    if op is AggregateOp.COUNT:
        return "count"
    if field_path is None:
        raise ValueError(
            f"Operator {op.value!r} requires a field path."
        )
    return f"{op.value}_{field_path}"


def _build_aggregate_expression(
    op: AggregateOp, field_path: str | None, vendor: str,
) -> Aggregate:
    if op is AggregateOp.COUNT:
        return Count("pk")
    if op is AggregateOp.COUNT_DISTINCT:
        assert field_path is not None
        return Count(field_path, distinct=True)
    if op is AggregateOp.SUM:
        return Sum(field_path)  # type: ignore[arg-type]
    if op is AggregateOp.AVG:
        return Avg(field_path)  # type: ignore[arg-type]
    if op is AggregateOp.MIN:
        return Min(field_path)  # type: ignore[arg-type]
    if op is AggregateOp.MAX:
        return Max(field_path)  # type: ignore[arg-type]
    if op is AggregateOp.STDDEV:
        return StdDev(field_path, sample=True)  # type: ignore[arg-type]
    if op is AggregateOp.VARIANCE:
        return Variance(field_path, sample=True)  # type: ignore[arg-type]
    if op is AggregateOp.BOOL_AND:
        return _bool_and(field_path, vendor)
    if op is AggregateOp.BOOL_OR:
        return _bool_or(field_path, vendor)
    if op is AggregateOp.ARRAY_AGG:
        from django.contrib.postgres.aggregates import ArrayAgg
        return ArrayAgg(field_path)  # type: ignore[arg-type]
    if op is AggregateOp.STRING_AGG:
        from django.contrib.postgres.aggregates import StringAgg
        return StringAgg(field_path, delimiter=",")  # type: ignore[arg-type]
    raise ValueError(f"Unknown aggregate operator {op!r}")  # defensive


def _bool_and(field_path: str | None, vendor: str) -> Aggregate:
    if vendor == "postgresql":
        from django.contrib.postgres.aggregates import BoolAnd
        return BoolAnd(field_path)
    # SQLite emulation: MIN(bool) is False if any False, True if all True.
    return Min(field_path, output_field=BooleanField())  # type: ignore[arg-type]


def _bool_or(field_path: str | None, vendor: str) -> Aggregate:
    if vendor == "postgresql":
        from django.contrib.postgres.aggregates import BoolOr
        return BoolOr(field_path)
    return Max(field_path, output_field=BooleanField())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HAVING
# ---------------------------------------------------------------------------

# Map our canonical comparison tokens → Django ORM lookup suffix.
_HAVING_LOOKUP: dict[str, str] = {
    "gt":     "__gt",
    "lt":     "__lt",
    "gte":    "__gte",
    "lte":    "__lte",
    "eq":     "",        # exact match
    "neq":    "",        # negated below
    "in":     "__in",
    "not_in": "__in",    # negated below
}

_HAVING_NEGATED: frozenset[str] = frozenset({"neq", "not_in"})


def _build_having_q(
    having: dict[str, Any], aggregate_aliases: Any,
) -> Q | None:
    """Translate a ``{"<alias>__<comparison>": value}`` dict to a Django Q.

    ``aggregate_aliases`` is iterable; we copy to a set for membership
    checks. Unknown aliases or comparisons raise
    :class:`HavingFieldNotAllowed` so the caller fails loud.
    """
    if not having:
        return None
    valid_aliases = set(aggregate_aliases)
    q = Q()
    for key, value in having.items():
        alias, comparison = _split_having_key(key)
        if alias not in valid_aliases:
            raise HavingFieldNotAllowed(
                f"HAVING references unknown aggregate alias `{alias}`. "
                f"Known: {sorted(valid_aliases)}."
            )
        if comparison not in _HAVING_LOOKUP:
            raise HavingFieldNotAllowed(
                f"Unknown HAVING comparison `{comparison}`. "
                f"Allowed: {list(HAVING_COMPARISONS)}."
            )
        lookup = _HAVING_LOOKUP[comparison]
        clause = Q(**{f"{alias}{lookup}": value})
        if comparison in _HAVING_NEGATED:
            clause = ~clause
        q &= clause
    return q


def _split_having_key(key: str) -> tuple[str, str]:
    """Split ``"sum_total__gt"`` → ``("sum_total", "gt")``.

    Comparison is the last ``__``-separated segment matched against
    :data:`HAVING_COMPARISONS`. Multi-segment comparisons (``not_in``)
    are matched first.
    """
    for comparison in sorted(HAVING_COMPARISONS, key=len, reverse=True):
        suffix = f"__{comparison}"
        if key.endswith(suffix):
            return key[: -len(suffix)], comparison
    raise HavingFieldNotAllowed(
        f"HAVING key `{key}` has no recognized comparison suffix. "
        f"Allowed: {list(HAVING_COMPARISONS)}."
    )


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------

def _build_order_terms(
    order_by: list[tuple[str, str, str | None]],
    group_aliases: list[str],
    aggregate_aliases: list[str],
) -> list[Any]:
    """Translate ``[(alias, direction, nulls)]`` into queryset-ready
    expressions, validating each alias against the group_by + aggregate
    namespaces. Raises :class:`OrderFieldNotAllowed` on miss.
    """
    if not order_by:
        return []
    valid = set(group_aliases) | set(aggregate_aliases)
    terms: list[Any] = []
    for alias, direction, nulls in order_by:
        if alias not in valid:
            raise OrderFieldNotAllowed(
                f"Order term `{alias}` is not a valid aggregate alias "
                f"({sorted(aggregate_aliases)}) nor group_by alias "
                f"({sorted(group_aliases)})."
            )
        nulls_first = True if nulls == "first" else None
        nulls_last = True if nulls == "last" else None
        expr = F(alias)
        if direction == "desc":
            terms.append(expr.desc(
                nulls_first=nulls_first, nulls_last=nulls_last,
            ))
        else:
            terms.append(expr.asc(
                nulls_first=nulls_first, nulls_last=nulls_last,
            ))
    return terms
