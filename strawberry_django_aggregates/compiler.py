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
    CharField,
    Count,
    DateField,
    DateTimeField,
    F,
    FloatField,
    Func,
    IntegerField,
    Max,
    Min,
    Q,
    StdDev,
    Sum,
    TimeField,
    Value,
    Variance,
)
from django.db.models.functions import Coalesce, Concat, Extract, Trunc
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
    AggregateOp.STDDEV_POP,
    AggregateOp.VAR_POP,
    AggregateOp.PERCENTILE_CONT,
    AggregateOp.PERCENTILE_DISC,
    AggregateOp.MODE,
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
# Ordered-set aggregates — PERCENTILE_CONT / PERCENTILE_DISC / MODE
# ---------------------------------------------------------------------------
#
# Django ships no `PercentileCont` / `PercentileDisc` / `Mode` aggregates,
# so we subclass :class:`Aggregate` and emit the ordered-set syntax via
# the template. PG-only — :func:`_validate_postgres_only` raises before
# any of these can hit SQLite. The ``fraction`` literal is rendered into
# the SQL template (NOT bound as a parameter) because PG's grammar
# requires a literal there; we coerce to ``float`` and re-render via
# ``str(float(...))`` so the only character classes that reach the
# template are digits, ``.``, and ``-``. No user-supplied string ever
# touches the template — see SPEC § 5.1 + CLAUDE.md Critical Rule 9.


def _validate_fraction(fraction: float) -> float:
    f = float(fraction)
    if not 0.0 <= f <= 1.0:
        raise ValueError(
            f"`fraction` must be in [0, 1], got {fraction!r}.",
        )
    return f


class _PercentileCont(Aggregate):
    """``PERCENTILE_CONT(<fraction>) WITHIN GROUP (ORDER BY <expr>)``.

    Continuous percentile — interpolates between adjacent values when
    the fraction does not land on a row boundary. PG-only.
    """

    function = "PERCENTILE_CONT"
    template = (
        "%(function)s(%(fraction)s) WITHIN GROUP (ORDER BY %(expressions)s)"
    )
    output_field = FloatField()

    def __init__(
        self, expression: Any, fraction: float, **extra: Any,
    ) -> None:
        f = _validate_fraction(fraction)
        super().__init__(expression, fraction=str(f), **extra)


class _PercentileDisc(Aggregate):
    """``PERCENTILE_DISC(<fraction>) WITHIN GROUP (ORDER BY <expr>)``.

    Discrete percentile — returns the first row value whose cumulative
    distribution meets the fraction. PG-only.

    Output is cast to ``Float`` for v1.0 — see SPEC § 5.1. Full
    type-faithful resolution (return the column type) lands in v1.x.
    """

    function = "PERCENTILE_DISC"
    template = (
        "%(function)s(%(fraction)s) WITHIN GROUP (ORDER BY %(expressions)s)"
    )
    output_field = FloatField()

    def __init__(
        self, expression: Any, fraction: float, **extra: Any,
    ) -> None:
        f = _validate_fraction(fraction)
        super().__init__(expression, fraction=str(f), **extra)


class _Mode(Aggregate):
    """``MODE() WITHIN GROUP (ORDER BY <expr>)``.

    Most-frequent value. Returns the column type — output_field is
    inferred from the source expression at SQL-generation time.
    PG-only.
    """

    function = "MODE"
    template = "%(function)s() WITHIN GROUP (ORDER BY %(expressions)s)"


# ---------------------------------------------------------------------------
# Multi-column COUNT(DISTINCT (a, b, c)) — Hasura-style ``count_distinct``
# with a tuple of fields. PG renders the SQL row constructor natively;
# SQLite has no row-constructor in DISTINCT, so we emulate via a
# null-sentinel-coalesced concatenation. The emulation diverges from PG
# semantics when any column in the tuple is NULL — see SPEC § 5 caveat.
# ---------------------------------------------------------------------------


class _Tuple(Func):
    """SQL row-constructor — ``(a, b, c)``.

    Used as the inner expression of a ``COUNT(DISTINCT ...)`` so that
    PostgreSQL evaluates DISTINCT against the tuple as a whole.
    Emitting a row constructor (not a function call) needs an empty
    ``function`` string; the template provides the parens.
    """

    function = ""
    template = "(%(expressions)s)"
    arg_joiner = ", "


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
    respect_comodel_ordering: bool = False,
    op_args:    dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compile a queryset into an aggregation query.

    See ``docs/SPEC.md`` § 10 for the full contract. Permission-naive —
    the queryset must already be scoped by the caller.

    When ``respect_comodel_ordering`` is ``True``, ordering by an FK
    group-by alias (e.g. ``customer_id``) appends the comodel's
    ``Meta.ordering`` as additional ORDER BY tiebreakers. Mirrors Odoo
    ``_order_field_to_sql`` (``odoo/models.py:2253``). Off by default
    so the determinism contract for existing callers is unchanged.

    ``op_args`` is the parallel-dict channel for per-call operator
    arguments that don't fit the ``(op, field)`` 2-tuple shape — chiefly
    the ``fraction`` for ``PERCENTILE_CONT`` / ``PERCENTILE_DISC``.
    Keyed by alias (e.g. ``"percentile_cont_total_50"``), value is a
    dict of kwargs forwarded to the underlying Django ``Aggregate``
    subclass. Aliases that don't appear here fall back to operator
    defaults (currently only the percentile ops require a fraction —
    ``MODE`` and the rest take no extra args).
    """
    group_by    = group_by    or []
    aggregates  = aggregates  or []
    having      = having      or {}
    order_by    = order_by    or []
    op_args     = op_args     or {}
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
        model, aggregates, vendor, op_args,
    )

    having_q = _build_having_q(having, aggregate_annotations.keys())

    order_terms = _build_order_terms(
        order_by,
        group_aliases,
        list(aggregate_annotations.keys()),
        model=model,
        respect_comodel_ordering=respect_comodel_ordering,
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
    op_args: dict[str, dict[str, Any]],
) -> dict[str, Aggregate]:
    annotations: dict[str, Aggregate] = {}
    for op, field_path in aggregates:
        field: Field | None
        if op is AggregateOp.COUNT_DISTINCT_TUPLE:
            # Multi-segment path encoded as ``a__b__c`` — validate each
            # segment as a single-field name on the model. The
            # COUNT_DISTINCT_TUPLE branch in
            # :func:`_build_aggregate_expression` operates on the raw
            # segment list, not on a single Field.
            assert field_path is not None
            for segment in field_path.split("__"):
                _resolve_field(model, segment, GroupByFieldNotAllowed)
            field = None
        elif field_path is not None:
            field = _resolve_field(model, field_path, GroupByFieldNotAllowed)
        else:
            field = None
        extra: dict[str, Any] = {}
        if op in {AggregateOp.PERCENTILE_CONT, AggregateOp.PERCENTILE_DISC}:
            assert field_path is not None
            fraction = _require_fraction(op_args, op, field_path)
            extra["fraction"] = fraction
        alias = aggregate_alias(op, field_path, **extra)
        annotations[alias] = _build_aggregate_expression(
            op, field_path, vendor, field, extra,
        )
    return annotations


def _require_fraction(
    op_args: dict[str, dict[str, Any]],
    op: AggregateOp,
    field_path: str,
) -> float:
    """Resolve the ``fraction`` arg for a percentile op.

    Looks up by the bare ``<op>_<field>`` alias *without* the trailing
    ``_<NN>`` percentile suffix — that suffix is *derived* from the
    fraction, so ``op_args`` must be keyed by the underlying call site,
    not the resulting SQL alias. Raises if missing or out of range.
    """
    base_alias = f"{op.value}_{field_path}"
    args = op_args.get(base_alias)
    if not args or "fraction" not in args:
        raise ValueError(
            f"Operator {op.value!r} on field `{field_path}` requires a "
            f"`fraction` argument in [0, 1]. Pass it via "
            f"`op_args={{{base_alias!r}: {{'fraction': <float>}}}}`.",
        )
    return _validate_fraction(args["fraction"])


def aggregate_alias(
    op: AggregateOp, field_path: str | None, **extra: Any,
) -> str:
    """Canonical alias for an ``(op, field)`` aggregate spec.

    - ``(COUNT, None)`` → ``"count"``
    - ``(COUNT_DISTINCT, "customer")`` → ``"count_distinct_customer"``
    - ``(COUNT_DISTINCT_TUPLE, "customer__status")`` →
      ``"count_distinct_tuple_customer__status"``
    - ``(SUM, "total")`` → ``"sum_total"``
    - ``(PERCENTILE_CONT, "total", fraction=0.5)`` →
      ``"percentile_cont_total_50"``
    - ``(PERCENTILE_DISC, "total", fraction=0.95)`` →
      ``"percentile_disc_total_95"``
    - ``(MODE, "total")`` → ``"mode_total"``

    Percentile aliases include a ``_<NN>`` suffix encoding the fraction
    as an integer percentile (``0.5`` → ``50``; ``0.05`` → ``5``;
    ``0.999`` → ``999``). This lets multiple percentile calls coexist
    in the same query without alias collisions.

    ``COUNT_DISTINCT_TUPLE`` aliases preserve the ``__``-joined
    field-path string verbatim — the path encodes the canonical
    sorted-tuple of field names so that the same call shape always
    produces the same alias regardless of input order at the wire.
    """
    if op is AggregateOp.COUNT:
        return "count"
    if field_path is None:
        raise ValueError(
            f"Operator {op.value!r} requires a field path."
        )
    if op in {AggregateOp.PERCENTILE_CONT, AggregateOp.PERCENTILE_DISC}:
        if "fraction" not in extra:
            raise ValueError(
                f"Operator {op.value!r} requires a `fraction` keyword "
                f"argument to compute its alias.",
            )
        return (
            f"{op.value}_{field_path}_"
            f"{_fraction_to_alias_suffix(extra['fraction'])}"
        )
    return f"{op.value}_{field_path}"


def _fraction_to_alias_suffix(fraction: float) -> str:
    """Encode a fraction in [0, 1] as an integer-percentile suffix.

    ``0.5`` → ``"50"``; ``0.95`` → ``"95"``; ``0.999`` → ``"999"``.
    Strips trailing zeros after the integer part so common percentiles
    have stable two-digit aliases.
    """
    f = _validate_fraction(fraction)
    # Multiply up to 1000 (handles fractions like 0.001) then strip
    # trailing zeros down to two digits minimum so common P50/P95/P99
    # have predictable aliases.
    millis = int(round(f * 1000))
    # Most callers use values like 0.5 / 0.95 / 0.99 — collapse to "50"
    # / "95" / "99" rather than "500" / "950" / "990".
    if millis % 10 == 0:
        return str(millis // 10)
    return str(millis)


def _build_aggregate_expression(
    op: AggregateOp,
    field_path: str | None,
    vendor: str,
    field: Field | None = None,
    extra: dict[str, Any] | None = None,
) -> Aggregate:
    extra = extra or {}
    if op is AggregateOp.COUNT:
        return Count("pk")
    if op is AggregateOp.COUNT_DISTINCT:
        assert field_path is not None
        return Count(field_path, distinct=True)
    if op is AggregateOp.COUNT_DISTINCT_TUPLE:
        assert field_path is not None
        segments = field_path.split("__")
        return _build_count_distinct_tuple(segments, vendor)
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
    if op is AggregateOp.STDDEV_POP:
        return StdDev(field_path, sample=False)  # type: ignore[arg-type]
    if op is AggregateOp.VAR_POP:
        return Variance(field_path, sample=False)  # type: ignore[arg-type]
    if op is AggregateOp.PERCENTILE_CONT:
        assert field_path is not None
        return _PercentileCont(field_path, fraction=extra["fraction"])
    if op is AggregateOp.PERCENTILE_DISC:
        assert field_path is not None
        return _PercentileDisc(field_path, fraction=extra["fraction"])
    if op is AggregateOp.MODE:
        assert field_path is not None
        # Match the source field's natural output type so MODE over a
        # CharField returns String, MODE over a DateField returns Date,
        # etc. ``_output_field_or_none`` is Django's documented hook.
        kwargs: dict[str, Any] = {}
        if field is not None:
            of = getattr(field, "_output_field_or_none", None)
            of_value = of() if callable(of) else None
            if of_value is not None:
                kwargs["output_field"] = of_value
        return _Mode(field_path, **kwargs)
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


# Sentinel used in the SQLite emulation of multi-column COUNT(DISTINCT).
# NUL byte (``\x00``) is the conventional "definitely not in user data"
# choice; no human-readable string column should legitimately contain
# it. The SQLite caveat (any tuple containing a NULL column collapses
# to a SHARED sentinel-coded tuple, so its distinct contribution
# differs from PG's "any NULL excludes the tuple") is documented in
# SPEC § 5. The separator (``\x01``) is a different control byte so a
# value that happens to contain the sentinel literally cannot collide
# with the boundary marker.
_TUPLE_NULL_SENTINEL = "\x00"
_TUPLE_SEPARATOR = "\x01"


def _build_count_distinct_tuple(
    segments: list[str], vendor: str,
) -> Aggregate:
    """Emit ``COUNT(DISTINCT (<segments>))`` per vendor.

    PostgreSQL: native row constructor — ``COUNT(DISTINCT (a, b, c))``.
    Standard-SQL semantics — any tuple where any column is NULL is
    excluded from the distinct set.

    SQLite: emulated via NULL-sentinel-coalesced concatenation, since
    SQLite has no row constructor in DISTINCT contexts. The emulation
    diverges from PG when any column is NULL — see SPEC § 5 caveat.
    """
    if vendor == "postgresql":
        return Count(_Tuple(*[F(seg) for seg in segments]), distinct=True)

    # SQLite emulation: COUNT(DISTINCT COALESCE(a, '\\0') || '\\1' || ...).
    # Each column is wrapped in COALESCE → sentinel; columns are joined
    # with a separator that differs from the sentinel so a value
    # containing the sentinel literally cannot collide with the
    # boundary. Cast to text via ``output_field=CharField()`` on the
    # COALESCE so non-string columns (FKs, dates, decimals) render
    # uniformly.
    parts: list[Any] = []
    for i, seg in enumerate(segments):
        if i > 0:
            parts.append(Value(_TUPLE_SEPARATOR))
        parts.append(
            Coalesce(
                F(seg),
                Value(_TUPLE_NULL_SENTINEL),
                output_field=CharField(),
            ),
        )
    if len(parts) == 1:
        # Single-segment tuple is unusual — the caller should use
        # COUNT_DISTINCT instead — but for symmetry we still produce
        # COUNT(DISTINCT COALESCE(a, '\\0')) without a Concat wrapper
        # (Concat with one arg is a no-op in Django but adds a CASE).
        return Count(parts[0], distinct=True)
    return Count(Concat(*parts, output_field=CharField()), distinct=True)


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
    *,
    model: type | None = None,
    respect_comodel_ordering: bool = False,
) -> list[Any]:
    """Translate ``[(alias, direction, nulls)]`` into queryset-ready
    expressions, validating each alias against the group_by + aggregate
    namespaces. Raises :class:`OrderFieldNotAllowed` on miss.

    When ``respect_comodel_ordering`` is ``True`` and ``model`` is
    given, terms resolving to a group-by FK alias (``customer_id``)
    are followed by the comodel's intrinsic ``Meta.ordering`` as
    tiebreakers. The added terms are always-valid by construction
    (they come from the comodel's own meta) so they bypass the
    user-facing allowlist check.
    """
    if not order_by:
        return []
    # Local import — ordering.py already imports from compiler at
    # call time via aggregate_aliases_from_spec; keep the dependency
    # one-way at module load.
    from strawberry_django_aggregates.ordering import (
        comodel_ordering_terms,
    )
    valid = set(group_aliases) | set(aggregate_aliases)
    group_alias_set = set(group_aliases)
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
        if (
            respect_comodel_ordering
            and model is not None
            and alias in group_alias_set
        ):
            for extra in comodel_ordering_terms(model, alias):
                terms.append(_term_to_expression(extra))
    return terms


def _term_to_expression(term: str) -> Any:
    """Translate ``"customer__name"`` / ``"-customer__rating"`` into
    a Django ``F().asc()`` / ``F().desc()`` expression.

    Used for comodel-derived ordering tiebreakers — these terms are
    always plain ``Meta.ordering`` strings, never user input.
    """
    if term.startswith("-"):
        return F(term[1:]).desc()
    return F(term).asc()
