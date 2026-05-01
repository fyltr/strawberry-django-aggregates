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

import datetime
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
    ExpressionWrapper,
    F,
    FloatField,
    Func,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    StdDev,
    Subquery,
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
    validate_week_start,
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


# ---------------------------------------------------------------------------
# Bucket-range helper — half-open [from, to) interval for a TIME bucket.
# ---------------------------------------------------------------------------
#
# Pure-stdlib. NO Django, NO Strawberry imports. Lives here because
# Stream 5's design has it as a shared helper imported by both the
# resolver (in ``builder.py``) and any future caller — see CLAUDE.md
# Critical Rule 9: ``compiler.py`` stays framework-agnostic.
#
# The input ``value`` is a bucketed datetime — i.e. the result of a
# ``date_trunc(<granularity>, ts)`` already in the user's tz. The
# returned ``[from, to)`` interval is computed in the value's own
# tzinfo so callers querying with ``tz="Asia/Tokyo"`` see Tokyo-local
# boundaries (e.g. ``2026-05-01 00:00+09:00`` → ``2026-06-01 00:00+09:00``).
#
# Manual stdlib month arithmetic is used (no ``dateutil.relativedelta``
# dependency) — it's a couple of lines and keeps the dep list tight.

def _add_months(value: datetime.datetime, months: int) -> datetime.datetime:
    """Add ``months`` to ``value`` preserving day=1 / time-of-day.

    Used for YEAR / QUARTER / MONTH bucket boundaries — the input
    has already been ``date_trunc``'d so day == 1 and the time-of-day
    is 00:00:00 (in the value's tz). We therefore don't need the
    Odoo-style "clamp day to month length" logic.
    """
    total = value.month - 1 + months
    new_year = value.year + total // 12
    new_month = total % 12 + 1
    return value.replace(year=new_year, month=new_month)


def bucket_range(
    value: datetime.datetime,
    granularity: TimeGranularity,
    week_start: int = 1,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Compute the half-open ``[from, to)`` interval for a bucketed
    datetime ``value`` at the given ``granularity``.

    ``value`` is expected to be the truncated bucket boundary itself
    (already aligned to the start of its bucket in its own tzinfo).
    ``from_`` is returned as ``value`` unchanged; ``to`` is the start
    of the next bucket. Both share the same ``tzinfo``.

    For ``WEEK``, the SQL truncation already accounts for the user-
    supplied ``week_start`` (1=Mon…7=Sun, ISO default), so the input
    ``value`` is already the start of the user's week. The interval
    is therefore always +7 days regardless of ``week_start``; the
    parameter is accepted for symmetry with the resolver call site
    and is validated for fail-loud feedback when callers pass a bad
    value to the helper directly.

    Examples (all in the value's tzinfo):

    - ``bucket_range(2026-05-01 00:00, MONTH)`` →
      ``(2026-05-01 00:00, 2026-06-01 00:00)``
    - ``bucket_range(2026-05-04 00:00, WEEK)`` (Mon-start) →
      ``(2026-05-04 00:00, 2026-05-11 00:00)``
    - ``bucket_range(2026-05-03 00:00, WEEK, week_start=7)`` (Sun) →
      ``(2026-05-03 00:00, 2026-05-10 00:00)``
    - ``bucket_range(2026-05-01 14:00, HOUR)`` →
      ``(2026-05-01 14:00, 2026-05-01 15:00)``
    """
    validate_week_start(week_start)
    if granularity is TimeGranularity.YEAR:
        return value, _add_months(value, 12)
    if granularity is TimeGranularity.QUARTER:
        return value, _add_months(value, 3)
    if granularity is TimeGranularity.MONTH:
        return value, _add_months(value, 1)
    if granularity is TimeGranularity.WEEK:
        return value, value + datetime.timedelta(days=7)
    if granularity is TimeGranularity.DAY:
        return value, value + datetime.timedelta(days=1)
    if granularity is TimeGranularity.HOUR:
        return value, value + datetime.timedelta(hours=1)
    if granularity is TimeGranularity.MINUTE:
        return value, value + datetime.timedelta(minutes=1)
    if granularity is TimeGranularity.SECOND:
        return value, value + datetime.timedelta(seconds=1)
    raise ValueError(  # defensive — exhaustive over TimeGranularity
        f"Unknown TimeGranularity {granularity!r}.",
    )


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
    week_start: int = 1,
    respect_comodel_ordering: bool = False,
    op_args:    dict[str, dict[str, Any]] | None = None,
    fill:       bool = False,
    fill_min:   datetime.datetime | None = None,
    fill_max:   datetime.datetime | None = None,
    allow_relation_traversal: bool = False,
) -> list[dict[str, Any]]:
    """Compile a queryset into an aggregation query.

    See ``docs/SPEC.md`` § 10 for the full contract. Permission-naive —
    the queryset must already be scoped by the caller.

    When ``respect_comodel_ordering`` is ``True``, ordering by an FK
    group-by alias (e.g. ``customer_id``) appends the comodel's
    ``Meta.ordering`` as additional ORDER BY tiebreakers. Mirrors Odoo
    ``_order_field_to_sql`` (``odoo/models.py:2253``). Off by default
    so the determinism contract for existing callers is unchanged.

    ``week_start`` selects the locale-aware first day of the week for
    ``TimeGranularity.WEEK`` bucketing and ``NumberGranularity.DAY_OF_WEEK``
    extraction. ``1=Monday`` (ISO default) … ``7=Sunday``. Mirrors Odoo
    ``odoo/models.py:2142-2168``. Out-of-range values raise ``ValueError``
    at the top of the function.

    ``op_args`` is the parallel-dict channel for per-call operator
    arguments that don't fit the ``(op, field)`` 2-tuple shape — chiefly
    the ``fraction`` for ``PERCENTILE_CONT`` / ``PERCENTILE_DISC``.
    Keyed by alias (e.g. ``"percentile_cont_total_50"``), value is a
    dict of kwargs forwarded to the underlying Django ``Aggregate``
    subclass. Aliases that don't appear here fall back to operator
    defaults (currently only the percentile ops require a fraction —
    ``MODE`` and the rest take no extra args).

    ``allow_relation_traversal`` is the opt-in escape hatch for measures
    that traverse a one-to-many or many-to-many relation (e.g.
    ``SUM("items__price")`` on an ``Order`` queryset). Default is
    ``False`` — the compiler refuses with
    :class:`~strawberry_django_aggregates.errors.AggregationAcrossRelationError`,
    preserving the no-row-multiplication contract (CLAUDE.md Critical
    Rule 4). When set to ``True``, the compiler emits a correlated
    ``Subquery`` per traversing measure
    (``Subquery(Child.objects.filter(parent_fk=OuterRef('pk'))
    .values('parent_fk').annotate(_=AGG('field')).values('_'))``) which
    dodges silent row-multiplication: each measure is computed in its
    own scalar subquery, so additional measures on the outer queryset
    are unaffected by the child fan-out.

    Restrictions when ``allow_relation_traversal=True``:

    - Only ``SUM`` / ``AVG`` / ``MIN`` / ``MAX`` / ``COUNT`` /
      ``COUNT_DISTINCT`` are supported with relation-traversing field
      paths in v1.0. The other operators
      (``STDDEV`` / ``VARIANCE`` / ``STDDEV_POP`` / ``VAR_POP`` /
      ``PERCENTILE_*`` / ``MODE`` / ``ARRAY_AGG`` / ``STRING_AGG`` /
      ``BOOL_AND`` / ``BOOL_OR`` / ``COUNT_DISTINCT_TUPLE``) raise an
      :class:`~strawberry_django_aggregates.errors.AggregateError` with
      a clear v1.0 limitation message.
    - The flag applies to MEASURES only. ``group_by`` paths still
      cannot traverse one-to-many or many-to-many relations even with
      the flag — that would row-multiply the outer query and corrupt
      every measure regardless of subquery isolation.
    - This flag lives on the backend primitive only. ``AggregateBuilder``
      / GraphQL surface does NOT expose it (CLAUDE.md Critical Rule 9 +
      Rule 4 separation).

    ``fill`` enables empty-bucket filling (SPEC § 7.2). When ``True``,
    ``group_by`` MUST contain exactly one ``TimeGranularity`` entry —
    multi-level group_by + fill is a v1.x feature; v1.0 raises with a
    clear message. The result includes one row per bucket between the
    data's min and max (or ``fill_min`` / ``fill_max`` overrides), with
    ``count: 0`` and all other measures ``None`` for any bucket that
    had no underlying rows. HAVING applies BEFORE filling — filtered-
    out rows are not back-filled. ``offset`` and ``limit`` apply AFTER
    filling. Ordering: filled rows are sorted ascending by the bucket
    alias by default; an explicit ``order_by`` re-applies on the
    filled list.
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
    week_start  = validate_week_start(week_start)

    if having and not group_by:
        raise AggregateError(
            "HAVING requires a non-empty `group_by` — there is nothing "
            "to filter without group buckets. Add a `group_by` or "
            "filter on the queryset directly with `.filter(...)`."
        )

    if fill:
        _validate_fill_spec(group_by)
    elif fill_min is not None or fill_max is not None:
        # ``fill_min`` / ``fill_max`` without ``fill=True`` is a usage
        # error — the bounds have nowhere to apply. Fail loud rather
        # than silently ignoring.
        raise AggregateError(
            "fill_min / fill_max require fill=True. Pass fill=True to "
            "enable empty-bucket filling, or remove the bounds.",
        )

    if allow_relation_traversal:
        _validate_relation_traversal_ops(aggregates)
    _validate_postgres_only(aggregates, vendor)

    group_annotations, group_aliases = _build_group_by_annotations(
        model, group_by, tzinfo, week_start,
    )

    aggregate_annotations = _build_aggregate_annotations(
        model, aggregates, vendor, op_args,
        allow_relation_traversal=allow_relation_traversal,
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

    # Apply user-supplied ordering before fill so the pre-fill rows are
    # in the order the user asked for. Filling re-sorts ascending by
    # the bucket alias if no explicit order_by was supplied; otherwise
    # the explicit ordering is re-applied to the filled list at the end.
    if order_terms and not fill:
        qs = qs.order_by(*order_terms)

    # Offset / limit BEFORE fill would defeat the dense-spine contract —
    # the slicing would discard filler buckets. Apply AFTER fill instead.
    if not fill and (offset or limit is not None):
        stop = (offset + limit) if limit is not None else None
        qs = qs[offset:stop]

    rows = list(qs)

    if fill:
        # Range bounds. When the caller supplies explicit ``fill_min`` /
        # ``fill_max``, they take precedence. Otherwise we derive bounds
        # from the POST-HAVING rows — fill operates on the data that
        # passed HAVING, so a HAVING-filtered bucket is neither emitted
        # nor back-filled. SPEC § 7.2 documents this ordering.
        bucket_field = group_by[0][0] if group_by else None
        resolved_min, resolved_max = _resolve_fill_bounds(
            queryset, bucket_field, fill_min, fill_max,
            post_having_rows=rows if having_q is not None else None,
            bucket_alias=(
                f"{bucket_field}_{group_by[0][1].value}"
                if bucket_field is not None
                and isinstance(group_by[0][1], TimeGranularity)
                else None
            ),
        )
        from strawberry_django_aggregates.fill import fill_bucket_results
        # ``aggregate_annotations`` keys are the canonical aliases the
        # rows carry. ``aggregate_alias(COUNT, None)`` resolves to
        # ``"count"``, so the list already includes the count alias if
        # the caller projected COUNT — which the GraphQL builder always
        # does. Defensively fall back to a bare ``["count"]`` when the
        # caller passed no aggregates at all (rare for the primitive,
        # impossible from the wire).
        agg_aliases = list(aggregate_annotations.keys()) or ["count"]
        rows = fill_bucket_results(
            rows,
            group_by,
            agg_aliases,
            resolved_min,
            resolved_max,
            week_start,
        )
        if order_terms:
            rows = _apply_order_to_rows(rows, order_by)
        if offset or limit is not None:
            stop = (offset + limit) if limit is not None else len(rows)
            rows = rows[offset:stop]

    return rows


def _validate_fill_spec(
    group_by: list[tuple[str, Granularity | None]],
) -> None:
    """Enforce the v1.0 single-TIME-granularity restriction for fill.

    Raises :class:`AggregateError` when ``fill=True`` is requested with
    a ``group_by`` shape we can't fill in v1.0:

    - Empty ``group_by`` — nothing to fill.
    - Multi-entry ``group_by`` — multi-level fill is a v1.x feature.
    - The single entry's granularity is ``None`` or a ``NumberGranularity``
      — fill is meaningless without contiguous bucket arithmetic.
    """
    if not group_by:
        raise AggregateError(
            "fill=True requires exactly one TIME-granularity bucket in "
            "group_by; got an empty group_by.",
        )
    if len(group_by) != 1:
        raise AggregateError(
            "fill=True with multi-level group_by is not supported in "
            "v1.0. Pass exactly one (field, TimeGranularity) entry. "
            "Multi-level fill is a v1.x feature — track the SPEC § 7.2 "
            "roadmap.",
        )
    field_path, granularity = group_by[0]
    if not isinstance(granularity, TimeGranularity):
        raise AggregateError(
            f"fill=True requires a TimeGranularity bucket; got "
            f"{type(granularity).__name__ if granularity else 'None'} "
            f"on field `{field_path}`.",
        )


def _resolve_fill_bounds(
    queryset: QuerySet,
    bucket_field: str | None,
    fill_min: datetime.datetime | None,
    fill_max: datetime.datetime | None,
    post_having_rows: list[dict[str, Any]] | None = None,
    bucket_alias: str | None = None,
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """Resolve the spine endpoints for empty-bucket filling.

    Priority order for each endpoint independently:

    1. Explicit ``fill_min`` / ``fill_max`` from the caller.
    2. ``min`` / ``max`` of the post-HAVING result rows (when HAVING
       was applied) — keyed off ``bucket_alias``. Mirrors the SPEC
       § 7.2 decision: HAVING applies BEFORE fill, so a HAVING-filtered
       bucket extends neither the data nor the spine.
    3. ``min`` / ``max`` of the underlying queryset — issued via a
       single ``aggregate()`` call on the same queryset the main
       aggregation walks (already permission-scoped per CLAUDE.md
       Critical Rule 1).

    Returns ``(None, None)`` only when the queryset is empty AND no
    explicit endpoints were given. The caller treats that as "nothing
    to fill" and returns the rows untouched.
    """
    if fill_min is not None and fill_max is not None:
        return fill_min, fill_max

    # Post-HAVING bounds take precedence over raw queryset bounds.
    post_min: datetime.datetime | None = None
    post_max: datetime.datetime | None = None
    if post_having_rows is not None and bucket_alias is not None:
        bucket_values = [
            r[bucket_alias] for r in post_having_rows
            if isinstance(r.get(bucket_alias), datetime.datetime)
        ]
        if bucket_values:
            post_min = min(bucket_values)
            post_max = max(bucket_values)

    # Fall back to the raw queryset only when post-HAVING bounds are
    # absent. Skipping the SQL when both endpoints can be resolved from
    # cheaper sources keeps the cost down for the common HAVING+fill
    # case (one extra COUNT instead of one extra COUNT + one MIN/MAX).
    raw_min: datetime.datetime | None = None
    raw_max: datetime.datetime | None = None
    need_raw = (
        bucket_field is not None
        and (
            (fill_min is None and post_min is None)
            or (fill_max is None and post_max is None)
        )
    )
    if need_raw:
        bounds = queryset.aggregate(
            _fill_min=Min(bucket_field), _fill_max=Max(bucket_field),
        )
        raw_min = bounds.get("_fill_min")
        raw_max = bounds.get("_fill_max")

    resolved_min = (
        fill_min if fill_min is not None
        else post_min if post_min is not None
        else raw_min
    )
    resolved_max = (
        fill_max if fill_max is not None
        else post_max if post_max is not None
        else raw_max
    )
    return resolved_min, resolved_max


def _apply_order_to_rows(
    rows: list[dict[str, Any]],
    order_by: list[tuple[str, str, str | None]],
) -> list[dict[str, Any]]:
    """Re-apply user ``order_by`` terms to a Python list of result rows.

    Used after empty-bucket filling — the SQL ORDER BY can't reach the
    in-memory filler rows. Each ``(alias, direction, nulls)`` triple is
    applied via a stable sort, in reverse priority so the highest-
    priority key wins.

    ``nulls="first"`` / ``"last"`` is honoured; the default mirrors
    SQL's ``ASC`` (nulls last) and ``DESC`` (nulls first) — same shape
    the SQL backend would emit.
    """
    if not order_by:
        return rows
    # Stable sort applied in reverse priority order.
    out = list(rows)
    for alias, direction, nulls in reversed(order_by):
        reverse = direction == "desc"
        if nulls is None:
            nulls_last = not reverse  # ASC → last; DESC → first
        else:
            nulls_last = nulls == "last"

        def keyfn(
            row: dict[str, Any],
            _alias: str = alias,
            _nulls_last: bool = nulls_last,
        ) -> tuple[int, Any]:
            v = row.get(_alias)
            if v is None:
                return (1 if _nulls_last else -1, v)
            return (0, v)

        out.sort(key=keyfn, reverse=reverse)
    return out


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


def _validate_relation_traversal_ops(
    aggregates: list[tuple[AggregateOp, str | None]],
) -> None:
    """Reject relation-traversing measures whose operator is not
    supported with ``allow_relation_traversal=True`` in v1.0.

    Runs BEFORE the PG-only vendor check so the v1.0-limitation
    message wins over a generic "PG-only" message on non-PG vendors.
    Operators whose path is non-traversing skip this check.
    """
    for op, field_path in aggregates:
        if field_path is None or "__" not in field_path:
            continue
        if op is AggregateOp.COUNT_DISTINCT_TUPLE:
            # COUNT_DISTINCT_TUPLE encodes a multi-segment "fields
            # tuple" syntactically as ``a__b__c`` but does not
            # traverse a relation — segments are sibling fields on
            # the same model. Validated elsewhere.
            continue
        if op not in _RELATION_TRAVERSAL_OPS:
            raise AggregateError(
                f"Operator {op.value!r} is not supported with "
                f"`allow_relation_traversal=True` in v1.0. "
                f"Supported operators for relation-traversing "
                f"measures: "
                f"{sorted(o.value for o in _RELATION_TRAVERSAL_OPS)}. "
                f"Either use one of those operators or query the "
                f"child model directly with the parent FK in "
                f"`group_by`."
            )


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
    model: Any,
    field_path: str,
    error_cls: type[Exception],
    *,
    allow_relation: bool = False,
) -> Field:
    """Resolve a field name, optionally walking a relation path.

    Default (``allow_relation=False``): refuses any ``__``-traversing
    path with :class:`AggregationAcrossRelationError`. Single-segment
    names resolve via ``model._meta.get_field``.

    With ``allow_relation=True``: walks each segment, validating that
    the prior segment exposes a relation (FK forward, FK reverse o2m,
    or m2m) and resolves to the leaf field. Used by the relation-
    traversal-opt-in measure path; the leaf field is what the operator
    is applied to, while the relation chain itself is rebuilt by
    :func:`_resolve_traversal_chain` for ``Subquery`` emission.
    """
    if "__" in field_path:
        if not allow_relation:
            raise AggregationAcrossRelationError(
                f"Cannot aggregate across relation `{field_path}` from "
                f"`{model.__name__}` — would cause silent row "
                f"multiplication. Query the related model directly "
                f"with the parent FK in `group_by` instead, or pass "
                f"`allow_relation_traversal=True` to "
                f"`compute_aggregation` to opt into Subquery-emitted "
                f"measures."
            )
        segments = field_path.split("__")
        current_model: Any = model
        for i, segment in enumerate(segments):
            try:
                f = current_model._meta.get_field(segment)
            except Exception as exc:
                raise error_cls(
                    f"Field `{segment}` (in path `{field_path}`) not "
                    f"found on `{current_model.__name__}`."
                ) from exc
            is_last = i == len(segments) - 1
            if is_last:
                return f
            related = getattr(f, "related_model", None)
            if related is None:
                raise error_cls(
                    f"Segment `{segment}` in path `{field_path}` on "
                    f"`{current_model.__name__}` is not a relation; "
                    f"cannot traverse further."
                )
            current_model = related
        # Defensive — segments is non-empty by virtue of "__" in path.
        raise error_cls(
            f"Could not resolve relation path `{field_path}` from "
            f"`{model.__name__}`."
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


def _resolve_traversal_chain(
    model: Any, field_path: str,
) -> tuple[list[Any], Any, str]:
    """Walk a ``__``-traversing path and return the relation chain.

    Returns ``(chain_fields, leaf_model, leaf_field_name)`` where
    ``chain_fields`` is the list of relation fields traversed in order
    (one entry per ``__``-separated segment except the leaf), and
    ``leaf_model`` is the model carrying the leaf scalar field.

    The traversal validates each non-leaf segment is a relation; the
    leaf segment must be a scalar (non-relation). All errors here are
    raised as :class:`AggregationAcrossRelationError` because the
    caller has already opted in to traversal — the only failures left
    are malformed paths.
    """
    segments = field_path.split("__")
    if len(segments) < 2:
        raise AggregationAcrossRelationError(
            f"Path `{field_path}` does not traverse a relation."
        )
    chain: list[Any] = []
    current_model: Any = model
    for segment in segments[:-1]:
        try:
            f = current_model._meta.get_field(segment)
        except Exception as exc:
            raise AggregationAcrossRelationError(
                f"Segment `{segment}` in path `{field_path}` not "
                f"found on `{current_model.__name__}`."
            ) from exc
        related = getattr(f, "related_model", None)
        if related is None:
            raise AggregationAcrossRelationError(
                f"Segment `{segment}` in path `{field_path}` on "
                f"`{current_model.__name__}` is not a relation; "
                f"cannot traverse further."
            )
        chain.append(f)
        current_model = related
    leaf_name = segments[-1]
    try:
        leaf_field = current_model._meta.get_field(leaf_name)
    except Exception as exc:
        raise AggregationAcrossRelationError(
            f"Leaf field `{leaf_name}` (in path `{field_path}`) not "
            f"found on `{current_model.__name__}`."
        ) from exc
    if getattr(leaf_field, "is_relation", False):
        raise AggregationAcrossRelationError(
            f"Leaf segment `{leaf_name}` in path `{field_path}` is a "
            f"relation, not a scalar field — cannot aggregate over a "
            f"relation directly."
        )
    return chain, current_model, leaf_name


# ---------------------------------------------------------------------------
# group_by annotations
# ---------------------------------------------------------------------------

def _build_group_by_annotations(
    model: type,
    group_by: list[tuple[str, Granularity | None]],
    tzinfo: ZoneInfo,
    week_start: int = 1,
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
                field_path, granularity, field, tzinfo, week_start,
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
    week_start: int = 1,
) -> Any:
    """Build the Django expression for a group_by spec.

    For non-bucketed specs we emit ``F(field_path)``. For date buckets
    we emit ``Trunc`` / ``Extract`` with ``tzinfo=`` so Django's backend
    inserts the ``AT TIME ZONE`` wrap *before* truncation
    (postgres/operations.py:135–138; mirrors Odoo's wrap order).

    ``week_start`` (1=Mon…7=Sun) shifts the WEEK-bucket boundary and
    rotates the DAY_OF_WEEK numeric extraction so the user-supplied
    first day of week is encoded as ``1``. Mirrors Odoo
    ``odoo/models.py:2142-2168``. Default ``1`` is ISO (Mon) and emits
    the same SQL as before this stream — no behaviour change.
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
        if granularity is TimeGranularity.WEEK:
            return _trunc_week_shifted(
                field_path, field, tz_kw, week_start,
            )
        return Trunc(field_path, granularity.value, **tz_kw)

    if isinstance(granularity, NumberGranularity):
        if granularity is NumberGranularity.DAY_OF_YEAR:
            return _ExtractDayOfYear(field_path, **tz_kw)
        if granularity is NumberGranularity.DAY_OF_WEEK:
            return _extract_day_of_week_rotated(
                field_path, tz_kw, week_start,
            )
        return Extract(field_path, _NUMBER_LOOKUP[granularity], **tz_kw)

    raise GranularityNotApplicable(  # defensive
        f"Unknown granularity {granularity!r}."
    )


def _trunc_week_shifted(
    field_path: str,
    field: Field,
    tz_kw: dict[str, Any],
    week_start: int,
) -> Any:
    """Emit a Trunc('week') that respects ``week_start``.

    Shift the source by ``offset = (8 - week_start) % 7`` days, run
    ``Trunc('week')`` (which is Monday-start on both PG and SQLite),
    then shift back by the same offset. When ``offset == 0`` (the ISO
    Monday default) we skip the arithmetic entirely so the determinism
    contract for existing callers is preserved — same SQL as before
    this stream.

    Date fields (no time component) need an ``output_field`` hint on
    the ``ExpressionWrapper`` so Django picks the right SQL shape.
    """
    offset = (8 - week_start) % 7
    if offset == 0:
        return Trunc(field_path, "week", **tz_kw)

    delta = datetime.timedelta(days=offset)
    out_field: Any = (
        DateTimeField() if isinstance(field, DateTimeField) else DateField()
    )
    shifted_in = ExpressionWrapper(
        F(field_path) + delta, output_field=out_field,
    )
    truncated = Trunc(shifted_in, "week", **tz_kw)
    return ExpressionWrapper(
        truncated - delta, output_field=out_field,
    )


def _extract_day_of_week_rotated(
    field_path: str,
    tz_kw: dict[str, Any],
    week_start: int,
) -> Any:
    """Emit a DAY_OF_WEEK extraction rotated by ``week_start``.

    ISO ``iso_week_day`` returns 1=Mon..7=Sun. Rotate so that the
    user's ``week_start`` is encoded as ``1``:

        ((iso_dow - week_start) % 7) + 1

    For ``week_start == 1`` the rotation is a no-op
    (``((d - 1) % 7) + 1 == d``) — skip the arithmetic so the SQL is
    identical to the pre-stream-6 ISO emission.
    """
    base = Extract(field_path, "iso_week_day", **tz_kw)
    if week_start == 1:
        return base
    # ``%`` in Django expressions follows Python semantics on PG
    # (where MOD is non-negative for positive divisors) — `iso_dow`
    # is in [1, 7] and `week_start` in [1, 7] so the subtraction is
    # in [-6, 6]; we add `+ 7` before the modulo to avoid relying on
    # any vendor's interpretation of negative modulo.
    return ExpressionWrapper(
        ((base - Value(week_start) + Value(7)) % Value(7)) + Value(1),
        output_field=IntegerField(),
    )


# ---------------------------------------------------------------------------
# aggregate annotations
# ---------------------------------------------------------------------------

# Operators that are supported with ``allow_relation_traversal=True``
# in v1.0. Subquery emission uses these straightforwardly; the rest are
# either ordered-set aggregates, vendor-specific composites, or already-
# tuple operators that don't compose cleanly with a one-measure-per-
# subquery shape — so we refuse them at resolver entry rather than
# emitting incorrect SQL.
_RELATION_TRAVERSAL_OPS: frozenset[AggregateOp] = frozenset({
    AggregateOp.COUNT,
    AggregateOp.COUNT_DISTINCT,
    AggregateOp.SUM,
    AggregateOp.AVG,
    AggregateOp.MIN,
    AggregateOp.MAX,
})


def _build_aggregate_annotations(
    model: type,
    aggregates: list[tuple[AggregateOp, str | None]],
    vendor: str,
    op_args: dict[str, dict[str, Any]],
    *,
    allow_relation_traversal: bool = False,
) -> dict[str, Aggregate]:
    annotations: dict[str, Aggregate] = {}
    for op, field_path in aggregates:
        field: Field | None
        traverses_relation = (
            field_path is not None
            and "__" in field_path
            and op is not AggregateOp.COUNT_DISTINCT_TUPLE
        )
        if op is AggregateOp.COUNT_DISTINCT_TUPLE:
            # Multi-segment path encoded as ``a__b__c`` — validate each
            # segment as a single-field name on the model. The
            # COUNT_DISTINCT_TUPLE branch in
            # :func:`_build_aggregate_expression` operates on the raw
            # segment list, not on a single Field. Tuple semantics
            # don't compose with the single-measure subquery emission,
            # so the relation-traversal flag does not extend here.
            assert field_path is not None
            for segment in field_path.split("__"):
                _resolve_field(model, segment, GroupByFieldNotAllowed)
            field = None
        elif traverses_relation:
            assert field_path is not None
            # ``_resolve_field`` raises ``AggregationAcrossRelationError``
            # when ``allow_relation`` is ``False`` (the default refusal
            # path); when ``True`` it walks the chain and returns the
            # leaf field. Either branch produces a typed error or a
            # validated leaf field — no separate fail-loud needed here.
            field = _resolve_field(
                model, field_path, GroupByFieldNotAllowed,
                allow_relation=allow_relation_traversal,
            )
            # Operator-supported check runs AFTER path validation so
            # invalid paths still surface their (more useful) typed
            # error; ``_validate_relation_traversal_ops`` at the top of
            # ``compute_aggregation`` covers the same set, but is
            # repeated here so callers building annotations directly
            # cannot bypass the check.
            if op not in _RELATION_TRAVERSAL_OPS:
                raise AggregateError(
                    f"Operator {op.value!r} is not supported with "
                    f"`allow_relation_traversal=True` in v1.0. "
                    f"Supported operators for relation-traversing "
                    f"measures: "
                    f"{sorted(o.value for o in _RELATION_TRAVERSAL_OPS)}. "
                    f"Either use one of those operators or query the "
                    f"child model directly with the parent FK in "
                    f"`group_by`."
                )
        elif field_path is not None:
            field = _resolve_field(
                model, field_path, GroupByFieldNotAllowed,
            )
        else:
            field = None
        extra: dict[str, Any] = {}
        if op in {AggregateOp.PERCENTILE_CONT, AggregateOp.PERCENTILE_DISC}:
            assert field_path is not None
            fraction = _require_fraction(op_args, op, field_path)
            extra["fraction"] = fraction
        alias = aggregate_alias(op, field_path, **extra)
        if traverses_relation:
            assert field_path is not None
            annotations[alias] = _build_relation_traversal_subquery(
                model, op, field_path,
            )
        else:
            annotations[alias] = _build_aggregate_expression(
                op, field_path, vendor, field, extra,
            )
    return annotations


def _build_relation_traversal_subquery(
    model: Any, op: AggregateOp, field_path: str,
) -> Aggregate:
    """Emit a correlated ``Subquery``-wrapped aggregate for a measure
    that traverses a one-to-many or many-to-many relation.

    Strategy: for each outer row, a correlated ``Subquery`` computes
    the per-row child aggregate (e.g. ``SUM(items.price)`` for that
    one order). The outer aggregate then folds those per-row values
    across the GROUP BY / ``.aggregate()`` scope.

    For a path like ``items__price`` on an ``Order`` queryset with
    ``op=SUM``:

    .. code-block:: sql

        -- Conceptually:
        SELECT SUM(per_order_sum)
        FROM (
            SELECT
                "tests_order"."id",
                (
                    SELECT SUM(U0."price")
                    FROM "tests_orderitem" U0
                    WHERE U0."order_id" = "tests_order"."id"
                    GROUP BY U0."order_id"
                ) AS per_order_sum
            FROM "tests_order"
        )

    The Subquery wrapper isolates the child fan-out from the outer
    query — the outer SUM iterates over OUTER rows (one per Order),
    so any other measure on Order (e.g. ``SUM(total)``) is computed
    against the same un-multiplied row set. CLAUDE.md Critical Rule 4
    is preserved precisely because each child fan-out is collapsed
    inside its own scalar subquery before the outer aggregation runs.

    Only ``COUNT`` / ``COUNT_DISTINCT`` / ``SUM`` / ``AVG`` / ``MIN``
    / ``MAX`` are accepted at the call site (validated in
    :func:`_build_aggregate_annotations`).
    """
    chain, leaf_model, leaf_name = _resolve_traversal_chain(
        model, field_path,
    )
    # The first segment in the chain is the relation off the OUTER
    # model. We need the FK accessor on the LEAF subquery side that
    # points back to the outer model's pk. For a reverse o2m, the
    # ``field`` attribute on the ManyToOneRel descriptor is the
    # forward FK on the child; its ``name`` is the lookup we want.
    first = chain[0]
    if getattr(first, "one_to_many", False):
        # Reverse FK: ``Order.items`` → ``OrderItem.order``.
        outer_filter_name = first.field.name
    elif getattr(first, "many_to_many", False):
        # m2m: ``remote_field.name`` is the m2m accessor on the
        # related model that points at the outer model.
        outer_filter_name = first.remote_field.name
    elif getattr(first, "many_to_one", False):
        # Forward FK on the outer model — pathological for an o2m
        # measure but the user opted in. Match the leaf's own pk
        # against the outer FK column.
        outer_filter_name = "pk"
    else:
        outer_filter_name = getattr(
            getattr(first, "remote_field", None), "name", "pk",
        ) or "pk"

    # Inner ``__``-path through the rest of the chain to the leaf
    # field. For ``items__price`` on Order, chain[1:] is empty and
    # the inner path is just ``"price"``.
    inner_segments: list[str] = [f.name for f in chain[1:]]
    inner_segments.append(leaf_name)
    inner_path = "__".join(inner_segments)

    # Subquery groups the leaf rows by the outer-FK column (so the
    # subquery yields exactly one row per outer parent), applies the
    # inner aggregate, and projects the scalar. ``.values(fk)`` before
    # ``.annotate(...)`` is the canonical Django GROUP BY shape.
    inner_qs = leaf_model.objects.filter(
        **{outer_filter_name: OuterRef("pk")}
    ).values(outer_filter_name)

    inner_agg: Aggregate
    if op is AggregateOp.COUNT:
        inner_agg = Count("pk")
    elif op is AggregateOp.COUNT_DISTINCT:
        inner_agg = Count(inner_path, distinct=True)
    elif op is AggregateOp.SUM:
        inner_agg = Sum(inner_path)
    elif op is AggregateOp.AVG:
        inner_agg = Avg(inner_path)
    elif op is AggregateOp.MIN:
        inner_agg = Min(inner_path)
    elif op is AggregateOp.MAX:
        inner_agg = Max(inner_path)
    else:  # pragma: no cover — guarded at the call site.
        raise AggregateError(
            f"Operator {op.value!r} is not supported with "
            f"`allow_relation_traversal=True` in v1.0."
        )

    inner_qs = inner_qs.annotate(_=inner_agg).values("_")
    inner_output = _output_field_for_traversal(op, leaf_model, leaf_name)
    per_row = Subquery(inner_qs, output_field=inner_output)

    # Wrap the per-row Subquery in the outer aggregate so the outer
    # query folds per-row values across the GROUP BY / aggregate
    # scope. SUM stays SUM (sum-of-sums == sum); COUNT becomes SUM
    # (sum-of-per-row-counts == total count); AVG / MIN / MAX use
    # their corresponding outer aggregate. This is what isolates
    # the child fan-out from any other measure on the outer query.
    outer_kwargs: dict[str, Any] = {}
    if inner_output is not None:
        outer_kwargs["output_field"] = inner_output
    if op in (AggregateOp.COUNT, AggregateOp.COUNT_DISTINCT, AggregateOp.SUM):
        # Outer SUM folds per-row Subquery values across the GROUP BY
        # / aggregate scope. SQL ``SUM(...)`` ignores NULL inputs by
        # default, so a parent row with zero children (NULL Subquery
        # value) does not poison the outer total — it contributes
        # nothing, which matches the "no children → no contribution"
        # semantics callers expect.
        return Sum(per_row, **outer_kwargs)
    if op is AggregateOp.AVG:
        return Avg(per_row, **outer_kwargs)
    if op is AggregateOp.MIN:
        return Min(per_row, **outer_kwargs)
    if op is AggregateOp.MAX:
        return Max(per_row, **outer_kwargs)
    raise AggregateError(  # pragma: no cover — guarded at the call site.
        f"Operator {op.value!r} is not supported with "
        f"`allow_relation_traversal=True` in v1.0."
    )


def _output_field_for_traversal(
    op: AggregateOp, leaf_model: Any, leaf_name: str,
) -> Any:
    """Resolve the output_field for a Subquery-emitted aggregate.

    COUNT / COUNT_DISTINCT always return integer rowcounts. SUM / AVG /
    MIN / MAX inherit the leaf field's natural output type (Decimal,
    Float, Integer, etc.) — Django's ``_output_field_or_none`` hook is
    the documented way to ask the field what it would emit.
    """
    if op in (AggregateOp.COUNT, AggregateOp.COUNT_DISTINCT):
        return IntegerField()
    try:
        leaf_field = leaf_model._meta.get_field(leaf_name)
    except Exception:
        return None
    of = getattr(leaf_field, "_output_field_or_none", None)
    return of() if callable(of) else None


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
