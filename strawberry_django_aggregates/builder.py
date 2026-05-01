"""High-level convenience builder.

Most consumers reach for :class:`AggregateBuilder` rather than calling
the lower-level type generators directly. The builder bundles:

- All four type generators (``make_aggregate_type``,
  ``make_grouped_type``, ``make_having_input``, ``make_group_by_spec``)
- Two strawberry resolver fields (``aggregate_field`` and
  ``group_by_field``) ready to attach to a ``Query`` type
- Optional integration with strawberry-django filter inputs via
  :func:`strawberry_django.filters.apply`

Lower-level type generators remain available for consumers who need
finer control (see :mod:`strawberry_django_aggregates.types`).
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any

import strawberry
import strawberry_django
from strawberry_django.filters import apply as apply_filters
from strawberry_django.pagination import (
    OffsetPaginationInfo,
    OffsetPaginationInput,
)

from strawberry_django_aggregates.compiler import (
    HAVING_COMPARISONS,
    bucket_range,
    compute_aggregation,
    group_by_alias,
)
from strawberry_django_aggregates.errors import OrderFieldNotAllowed
from strawberry_django_aggregates.granularity import (
    NumberGranularity,
    TimeGranularity,
)
from strawberry_django_aggregates.operators import (
    AggregateOp,
)
from strawberry_django_aggregates.ordering import (
    aggregate_aliases_from_spec,
    parse_aggregate_order,
)
from strawberry_django_aggregates.types import (
    BucketRange,
    make_aggregate_type,
    make_group_by_spec,
    make_group_order_input,
    make_grouped_type,
    make_having_input,
)

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet


def _to_camel(snake: str) -> str:
    """``"sum"`` → ``"sum"``; ``"bool_and"`` → ``"boolAnd"``."""
    head, *tail = snake.split("_")
    return head + "".join(w.capitalize() for w in tail)


# GraphQL camelCase wire-name → AggregateOp. Used by the resolver to
# walk ``info.selected_fields`` and figure out which (op, field) pairs
# to ask the compiler for.
#
# Includes SQL-standard aliases ``every`` ≡ ``bool_and`` and
# ``some`` ≡ ``bool_or`` (Stream 4). These are wire-only — no new
# :class:`AggregateOp` member is introduced; the canonical enum stays
# stable. See SPEC § 5.
_OP_FROM_WIRE: dict[str, AggregateOp] = {
    _to_camel(op.value): op for op in AggregateOp
}
_OP_FROM_WIRE["every"] = AggregateOp.BOOL_AND
_OP_FROM_WIRE["some"] = AggregateOp.BOOL_OR

# Guard against a future operator whose camelCased name collides with
# an existing one (would silently shadow a member in
# ``_OP_FROM_WIRE``). Iterate the source-of-truth enum and assert each
# camelCased name maps back to itself; aliases (``every`` / ``some``)
# are validated separately so the count check tolerates wire-level
# aliases without losing collision detection.
for _op in AggregateOp:
    assert _OP_FROM_WIRE.get(_to_camel(_op.value)) is _op, (
        f"AggregateOp member {_op!r} camelCases to a name that "
        f"collides with another entry in _OP_FROM_WIRE."
    )
assert _OP_FROM_WIRE["every"] is AggregateOp.BOOL_AND
assert _OP_FROM_WIRE["some"] is AggregateOp.BOOL_OR
del _op


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AggregateBuilder:
    """Convenience builder — emits all aggregate types and resolver fields.

    Parameters
    ----------
    model : Django model class.
    aggregate_fields : fields eligible for sum/avg/min/max-style
        aggregates. Defaults to all numeric/boolean/date/string fields.
    group_by_fields : fields eligible for ``group_by``. Defaults to all
        plain fields plus FK references.
    operators : per-field operator overrides. Keys are field names;
        values are tuples of permitted :class:`AggregateOp`.
    name_prefix : optional prefix for emitted type names (defaults to
        ``model.__name__``).
    filter_type : optional strawberry-django filter input type. When
        provided, both emitted resolver fields accept ``filter`` and
        compose via :func:`strawberry_django.filters.apply`.
    get_queryset : optional callable ``(info) -> QuerySet`` for
        permission scoping. Defaults to
        ``model._default_manager.all()`` — callers wanting
        ``accessible_by(user)`` semantics override this hook.
    enable_federation : when ``True``, emit Apollo Federation v2
        directives on the generated types — currently ``@external`` on
        the foreign-key ``<name>_id`` fields of ``<Model>GroupKey`` (so
        a Federation gateway knows those IDs are owned by another
        subgraph). The aggregate / grouped containers themselves are
        decorated with :func:`strawberry.federation.type` but carry no
        ``@key`` directive in v1.0; consumers register their own entity
        ``@key`` if they need cross-subgraph composition. Consumers
        MUST construct the schema with
        :class:`strawberry.federation.Schema` for directives to print.
        See SPEC § 18 for the design rationale and v1.1 roadmap.
    """

    model:            type[Model]
    aggregate_fields: list[str] | None = None
    group_by_fields:  list[str] | None = None
    operators:        dict[str, tuple[AggregateOp, ...]] = dc_field(
        default_factory=dict,
    )
    name_prefix:      str | None = None
    filter_type:      type | None = None
    enable_federation: bool = False
    get_queryset:     Callable[[Any], QuerySet] | None = None
    respect_comodel_ordering: bool = False

    def build(self) -> BuiltAggregates:
        """Generate all types and return them along with attached fields."""
        name = self.name_prefix or self.model.__name__

        aggregate_type = make_aggregate_type(
            self.model,
            name=name,
            aggregate_fields=self.aggregate_fields,
            operators=self.operators,
            enable_federation=self.enable_federation,
        )
        having_input = make_having_input(
            self.model,
            name=name,
            aggregate_fields=self.aggregate_fields,
            operators=self.operators,
            enable_federation=self.enable_federation,
        )
        group_by_spec, groupable_field_enum = make_group_by_spec(
            self.model,
            name=name,
            group_by_fields=self.group_by_fields,
            enable_federation=self.enable_federation,
        )
        group_key_type, grouped_type, grouped_result_type = (
            make_grouped_type(
                self.model,
                name=name,
                aggregate_type=aggregate_type,
                aggregate_fields=self.aggregate_fields,
                group_by_fields=self.group_by_fields,
                operators=self.operators,
                enable_federation=self.enable_federation,
            )
        )
        group_order_input = make_group_order_input(
            self.model, name=name,
            enable_federation=self.enable_federation,
        )

        aggregate_field = self._build_aggregate_field(
            aggregate_type=aggregate_type,
        )
        group_by_field = self._build_group_by_field(
            group_by_spec=group_by_spec,
            having_input=having_input,
            group_order_input=group_order_input,
            grouped_type=grouped_type,
            group_key_type=group_key_type,
            grouped_result_type=grouped_result_type,
        )

        return BuiltAggregates(
            aggregate_type=aggregate_type,
            grouped_type=grouped_type,
            grouped_result_type=grouped_result_type,
            group_key_type=group_key_type,
            having_input=having_input,
            group_by_spec=group_by_spec,
            groupable_field_enum=groupable_field_enum,
            aggregate_field=aggregate_field,
            group_by_field=group_by_field,
        )

    # ------- aggregate field (no group_by) --------------------------------
    #
    # ``disable_optimization=True`` on every emitted resolver field:
    # strawberry-django's :class:`DjangoOptimizerExtension` rewrites
    # querysets via ``select_related`` / ``only`` / ``prefetch_related``
    # based on the GraphQL projection. Our resolvers project via
    # ``.values(...)`` inside ``compute_aggregation``, so any optimizer
    # hints would either be discarded or conflict with the GROUP BY.

    def _build_aggregate_field(self, *, aggregate_type: type) -> Any:
        builder = self
        filter_type = self.filter_type
        a_fields = builder._a_fields()

        def resolver(
            info: strawberry.Info, filter: Any = None,
        ) -> Any:
            qs = builder._resolve_queryset(info)
            if filter is not None:
                qs = apply_filters(filter, qs, info=info)
            op_args: dict[str, dict[str, Any]] = {}
            requested = builder._requested_aggregate_ops(
                info, a_fields, op_args=op_args,
            )
            rows = compute_aggregation(
                qs, aggregates=requested, op_args=op_args,
            )
            row = rows[0] if rows else {}
            return builder._shape_aggregate(
                aggregate_type, row, requested, op_args=op_args,
            )

        annotations: dict[str, Any] = {
            "info":   strawberry.Info,
            "return": aggregate_type,
        }
        if filter_type is not None:
            annotations["filter"] = filter_type | None
            resolver.__annotations__ = annotations
            return strawberry_django.field(
                resolver=resolver, disable_optimization=True,
            )

        # Drop the unused `filter` arg when no filter type was wired —
        # otherwise strawberry surfaces it as an `Any` arg in the schema.
        def resolver_no_filter(info: strawberry.Info) -> Any:
            return resolver(info=info, filter=None)

        resolver_no_filter.__annotations__ = annotations
        return strawberry_django.field(
            resolver=resolver_no_filter, disable_optimization=True,
        )

    # ------- group_by field ----------------------------------------------

    def _build_group_by_field(
        self, *,
        group_by_spec: type,
        having_input: type,
        group_order_input: type,
        grouped_type: type,
        group_key_type: type,
        grouped_result_type: type,
    ) -> Any:
        builder = self
        filter_type = self.filter_type
        a_fields = builder._a_fields()

        def resolver(
            info: strawberry.Info,
            group_by: Any,
            filter:    Any = None,
            having:    Any = None,
            order_by:  Any = None,
            pagination: OffsetPaginationInput | None = None,
            week_start: int = 1,
            fill: bool = False,
            fill_min: datetime.datetime | None = None,
            fill_max: datetime.datetime | None = None,
        ) -> Any:
            qs = builder._resolve_queryset(info)
            if filter is not None:
                qs = apply_filters(filter, qs, info=info)

            spec = builder._translate_group_by(group_by)
            op_args: dict[str, dict[str, Any]] = {}
            requested = builder._requested_aggregate_ops_grouped(
                info, a_fields, op_args=op_args,
            )
            having_dict = builder._translate_having(having, requested)
            order_terms = builder._translate_order_by(
                order_by, spec, requested,
            )
            pagination = pagination or OffsetPaginationInput()

            limit  = pagination.limit if isinstance(
                pagination.limit, int,
            ) else None
            offset = pagination.offset or 0

            # Locale-aware week-start. Validate at the resolver
            # boundary so a bad value fails fast before any SQL.
            ws = builder._resolve_week_start(week_start)

            # Strawberry passes ``UNSET`` for omitted optional inputs;
            # normalize to None so downstream code can use plain
            # ``is None`` checks.
            fmin = (
                fill_min if fill_min not in (None, strawberry.UNSET)
                else None
            )
            fmax = (
                fill_max if fill_max not in (None, strawberry.UNSET)
                else None
            )

            rows = compute_aggregation(
                qs,
                group_by=spec,
                aggregates=requested,
                having=having_dict,
                order_by=order_terms,
                offset=offset,
                limit=limit,
                respect_comodel_ordering=builder.respect_comodel_ordering,
                op_args=op_args,
                week_start=ws,
                fill=fill,
                fill_min=fmin,
                fill_max=fmax,
            )
            if fill:
                # Filling expands the row set with zero-count buckets,
                # so the count optimization (DB-side ``DISTINCT``) is
                # invalid — it would only count non-empty buckets.
                # Recompute the total over the dense, filtered, but
                # un-paginated row set.
                total = builder._count_filled_groups(
                    qs, spec, requested, having_dict, op_args=op_args,
                    week_start=ws, fill_min=fmin, fill_max=fmax,
                )
            else:
                total = builder._count_groups(
                    qs, spec, requested, having_dict, op_args=op_args,
                    week_start=ws,
                )
            grouped_rows = [
                builder._shape_grouped(
                    grouped_type, group_key_type, row, requested, spec,
                    op_args=op_args,
                    week_start=ws,
                )
                for row in rows
            ]
            return grouped_result_type(
                results=grouped_rows,
                page_info=OffsetPaginationInfo(
                    offset=offset, limit=limit,
                ),
                total_count=total,
            )

        annotations: dict[str, Any] = {
            "info":     strawberry.Info,
            "group_by": list[group_by_spec],  # type: ignore[valid-type]
        }
        if filter_type is not None:
            annotations["filter"] = filter_type | None
        annotations["having"]     = having_input | None
        annotations["order_by"]   = (
            list[group_order_input] | None  # type: ignore[valid-type]
        )
        annotations["pagination"] = OffsetPaginationInput | None
        annotations["week_start"] = int
        annotations["fill"]       = bool
        annotations["fill_min"]   = datetime.datetime | None
        annotations["fill_max"]   = datetime.datetime | None
        annotations["return"]     = grouped_result_type

        if filter_type is None:
            # Drop the `filter` parameter when no filter type is wired.
            def resolver_no_filter(
                info: strawberry.Info,
                group_by: Any,
                having:    Any = None,
                order_by:  Any = None,
                pagination: OffsetPaginationInput | None = None,
                week_start: int = 1,
                fill: bool = False,
                fill_min: datetime.datetime | None = None,
                fill_max: datetime.datetime | None = None,
            ) -> Any:
                return resolver(
                    info=info, group_by=group_by, filter=None,
                    having=having, order_by=order_by,
                    pagination=pagination, week_start=week_start,
                    fill=fill, fill_min=fill_min, fill_max=fill_max,
                )
            resolver_no_filter.__annotations__ = annotations
            return strawberry_django.field(
                resolver=resolver_no_filter,
                disable_optimization=True,
            )

        resolver.__annotations__ = annotations
        return strawberry_django.field(
            resolver=resolver, disable_optimization=True,
        )

    @staticmethod
    def _resolve_week_start(value: Any) -> int:
        """Validate the resolver-arg ``week_start`` (1=Mon..7=Sun).

        Goes through :func:`granularity.validate_week_start` so out-
        of-range or non-int values raise ``ValueError`` before any
        SQL fires. The GraphQL default is ``1`` (ISO Monday) — same
        behaviour as before this stream.
        """
        from strawberry_django_aggregates.granularity import (
            validate_week_start,
        )
        return validate_week_start(value)

    # ------- helpers (queryset / shaping / translation) -------------------

    def _resolve_queryset(self, info: Any) -> QuerySet:
        if self.get_queryset is not None:
            return self.get_queryset(info)
        return self.model._default_manager.all()

    def _a_fields(self) -> list[str]:
        from strawberry_django_aggregates.types import (
            _resolve_aggregate_fields,
        )
        return _resolve_aggregate_fields(self.model, self.aggregate_fields)

    def _requested_aggregate_ops(
        self, info: Any, a_fields: list[str],
        op_args: dict[str, dict[str, Any]] | None = None,
    ) -> list[tuple[AggregateOp, str | None]]:
        """Inspect ``info.selected_fields`` and emit only the (op, field)
        pairs the client requested. Always include ``count`` so the
        non-null ``Int!`` field has a value; nested types only contribute
        ops the schema actually asks for.

        When ``op_args`` is provided, percentile method-style fields'
        ``fraction`` arguments are recorded under their bare
        ``<op>_<field>`` alias (the percentile-suffix is derived later
        in :func:`compiler.aggregate_alias`).
        """
        requested: list[tuple[AggregateOp, str | None]] = [
            (AggregateOp.COUNT, None),
        ]
        for entry in self._iter_selected_ops(
            info, a_fields, op_args=op_args,
        ):
            requested.append(entry)
        # Deduplicate in case the same (op, field) appears twice.
        seen: set[tuple[Any, Any]] = set()
        out: list[tuple[AggregateOp, str | None]] = []
        for entry in requested:
            if entry in seen:
                continue
            seen.add(entry)
            out.append(entry)
        return out

    def _requested_aggregate_ops_grouped(
        self, info: Any, a_fields: list[str],
        op_args: dict[str, dict[str, Any]] | None = None,
    ) -> list[tuple[AggregateOp, str | None]]:
        """Walk the GraphQL selection set for the grouped resolver.

        Selection shape:
        ``ordersGroupBy → results → Grouped → {count, sum {…}, avg {…}}``.
        The ``results`` node's children are the Grouped fields — that
        is the Grouped node passed to :meth:`_extract_ops_from_grouped`.
        """
        requested: list[tuple[AggregateOp, str | None]] = [
            (AggregateOp.COUNT, None),
        ]
        for sel in getattr(info, "selected_fields", []) or []:
            for sub in sel.selections or []:
                if getattr(sub, "name", None) != "results":
                    continue
                requested.extend(
                    self._extract_ops_from_grouped(
                        sub, a_fields, op_args=op_args,
                    ),
                )
        seen: set[tuple[Any, Any]] = set()
        deduped: list[tuple[AggregateOp, str | None]] = []
        for entry in requested:
            if entry in seen:
                continue
            seen.add(entry)
            deduped.append(entry)
        return deduped

    def _iter_selected_ops(
        self, info: Any, a_fields: list[str],
        op_args: dict[str, dict[str, Any]] | None = None,
    ) -> Iterable[tuple[AggregateOp, str | None]]:
        for sel in getattr(info, "selected_fields", []) or []:
            yield from self._extract_ops_from_grouped(
                sel, a_fields, op_args=op_args,
            )

    @staticmethod
    def _flatten_selections(node: Any) -> Iterable[Any]:
        """Yield direct ``SelectedField`` children of ``node``,
        descending through ``InlineFragment`` / ``FragmentSpread``.

        ``info.selected_fields`` returns
        ``Selection = SelectedField | InlineFragment | FragmentSpread``.
        ``InlineFragment`` lacks ``.name``; ``FragmentSpread`` has
        ``.name`` but it's the fragment name, not a GraphQL field name.
        Fragments commonly hold the actual operator selections —
        without this flattening we silently under-request aggregates
        for any client using ``...Frag`` or ``...on Type``.
        """
        for child in getattr(node, "selections", None) or []:
            # SelectedField has both .name and .selections; fragments
            # have .selections but their .name (if any) is a fragment
            # spread name, marked by the lack of a .alias attribute.
            if hasattr(child, "alias"):
                yield child
            else:
                # InlineFragment / FragmentSpread — recurse.
                yield from AggregateBuilder._flatten_selections(child)

    def _extract_ops_from_grouped(
        self, grouped_sel: Any, a_fields: list[str],
        op_args: dict[str, dict[str, Any]] | None = None,
    ) -> Iterable[tuple[AggregateOp, str | None]]:
        """Inspect a Grouped-or-Aggregate selection set and yield
        ``(op, field)`` for each aggregate measure the client requested.

        GraphQL field names are camelCase on the wire; we map back to
        snake_case via :data:`_OP_FROM_WIRE`. ``count`` is yielded with
        ``field=None``; ``count_distinct`` and ``percentileCont`` /
        ``percentileDisc`` read the ``field`` (and ``fraction``) argument
        from the GraphQL operation. Fragments are flattened by
        :meth:`_flatten_selections`.

        Percentile fractions are written into ``op_args`` keyed by the
        bare ``<op>_<field>`` alias (no fraction suffix). The compiler
        derives the fraction-suffixed final alias from there.
        """
        for inner in self._flatten_selections(grouped_sel):
            inner_name = getattr(inner, "name", None)
            if inner_name is None:
                continue
            op = _OP_FROM_WIRE.get(inner_name)
            if op is None:
                continue
            if op is AggregateOp.COUNT:
                yield (op, None)
                continue
            if op is AggregateOp.COUNT_DISTINCT:
                # ``countDistinct`` accepts EITHER ``field: Enum`` for
                # single-column distinct (emits COUNT_DISTINCT) OR
                # ``fields: [Enum!]`` for multi-column tuple distinct
                # (emits COUNT_DISTINCT_TUPLE). Per SPEC § 5
                # Hasura-style sub-section. Mutual exclusion is
                # enforced at the resolver level (in ``types.py``); we
                # additionally guard here so the SQL annotation isn't
                # built with an empty / contradictory spec.
                args = getattr(inner, "arguments", {}) or {}
                field_arg = args.get("field")
                fields_arg = args.get("fields")
                single_set = field_arg is not None
                multi_set = fields_arg is not None and len(fields_arg) > 0
                if single_set == multi_set:
                    # Both set or neither set — let the resolver raise
                    # the user-facing error. Don't queue any annotation.
                    continue
                if single_set:
                    fname = self._countable_field_to_path(field_arg)
                    if fname is not None:
                        yield (op, fname)
                    continue
                # Multi-column tuple — canonicalize via sorted-tuple of
                # field names so wire-input order doesn't change the
                # SQL alias. ``__`` is the segment separator in the
                # resulting field-path (compiler expects that shape).
                if fields_arg is None:
                    continue
                names = [
                    self._countable_field_to_path(f) for f in fields_arg
                ]
                clean = [n for n in names if n is not None]
                if not clean:
                    continue
                joined = "__".join(sorted(clean))
                yield (AggregateOp.COUNT_DISTINCT_TUPLE, joined)
                continue
            if op in {
                AggregateOp.PERCENTILE_CONT,
                AggregateOp.PERCENTILE_DISC,
            }:
                args = getattr(inner, "arguments", {}) or {}
                field_arg = args.get("field")
                fraction_arg = args.get("fraction")
                if field_arg is None or fraction_arg is None:
                    continue
                fname = self._countable_field_to_path(field_arg)
                if fname is None:
                    continue
                if op_args is not None:
                    base = f"{op.value}_{fname}"
                    op_args[base] = {"fraction": float(fraction_arg)}
                yield (op, fname)
                continue
            for f in self._flatten_selections(inner):
                fname = getattr(f, "name", None)
                if fname in a_fields:
                    yield (op, fname)

    def _countable_field_to_path(self, arg: Any) -> str | None:
        """Resolve a ``CountableField`` argument to a model field name.

        Strawberry passes the deserialized enum **member** (e.g.
        ``OrderCountableField.TOTAL`` whose ``.value`` is ``"total"``)
        or, in some paths, the raw NAME string. Handle both.
        """
        if hasattr(arg, "value"):
            return str(arg.value)
        if isinstance(arg, str):
            # Best-effort — uppercase-NAME lookup against the enum on
            # BuiltAggregates, but that lives in builder.build() output.
            # Without the enum here, accept the lowercase form.
            return arg.lower() if arg.isupper() else arg
        return None

    def _translate_group_by(
        self, specs: list[Any],
    ) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for s in specs:
            field_name = (
                s.field.value if hasattr(s.field, "value") else s.field
            )
            grain = s.granularity
            if grain is None:
                out.append((field_name, None))
                continue
            grain_value = grain.value if hasattr(grain, "value") else grain
            granularity = _resolve_granularity(grain_value)
            out.append((field_name, granularity))
        return out

    def _translate_having(
        self, having: Any | None,
        requested: list[tuple[AggregateOp, str | None]],
    ) -> dict[str, Any]:
        """Translate the ``<Model>Having`` input into the dict format
        ``compute_aggregation`` expects, auto-extending ``requested``
        with any aggregate measure the user filters on but didn't
        project.

        Mirrors Hasura/Odoo idiom: HAVING is independent of SELECT
        projection. Without the auto-extend, a query like
        ``ordersGroupBy(having: { sumTotalGt: 100 }) { results { count } }``
        would silently filter on nothing because ``sum_total`` isn't in
        ``requested``. The user mutates ``requested`` in place — caller
        sees the extended list when shaping rows.
        """
        if having is None:
            return {}
        seen: set[tuple[Any, Any]] = {(op, fp) for op, fp in requested}
        out: dict[str, Any] = {}
        for f in dataclasses.fields(having):
            value = getattr(having, f.name)
            if value is None or value is strawberry.UNSET:
                continue
            measure, comparison = _split_having_input_field(f.name)
            op, field_path = _measure_to_op_field(measure)
            if (op, field_path) not in seen:
                requested.append((op, field_path))
                seen.add((op, field_path))
            out[f"{measure}__{comparison}"] = value
        return out

    def _translate_order_by(
        self,
        order_by: list[Any] | None,
        spec: list[tuple[str, Any]],
        requested: list[tuple[AggregateOp, str | None]],
    ) -> list[tuple[str, str, str | None]]:
        if not order_by:
            return []
        group_aliases = [
            group_by_alias(fp, gr, None) for fp, gr in spec
        ]
        agg_aliases = aggregate_aliases_from_spec(
            [(op, fp) for op, fp in requested],
        )
        out: list[tuple[str, str, str | None]] = []
        for o in order_by:
            canonical, parsed_direction = parse_aggregate_order(
                o.field,
                group_by_fields=group_aliases,
                aggregate_aliases=agg_aliases,
            )
            input_direction = (
                o.direction.value
                if (hasattr(o, "direction") and o.direction is not None)
                else None
            )
            # Fail-loud on contradictory directions: e.g. user passes
            # "-sum_total" with direction=ASC. One of these two paths
            # is wrong; we don't silently pick a winner.
            field_str = str(o.field)
            field_has_explicit_dir = (
                field_str.startswith("-")
                or field_str.lower().endswith(" desc")
                or field_str.lower().endswith(" asc")
            )
            if (
                input_direction is not None
                and field_has_explicit_dir
                and input_direction != parsed_direction
            ):
                raise OrderFieldNotAllowed(
                    f"Order term `{o.field}` has an embedded "
                    f"direction ({parsed_direction!r}) that "
                    f"contradicts the explicit `direction` argument "
                    f"({input_direction!r}). Pass the direction in "
                    f"exactly one place.",
                )
            direction = input_direction or parsed_direction
            nulls = (
                o.nulls.value
                if (hasattr(o, "nulls") and o.nulls is not None)
                else None
            )
            out.append((canonical, direction, nulls))
        return out

    def _count_filled_groups(
        self,
        qs: QuerySet,
        spec: list[tuple[str, Any]],
        requested: list[tuple[AggregateOp, str | None]],
        having_dict: dict[str, Any],
        op_args: dict[str, dict[str, Any]] | None = None,
        week_start: int = 1,
        fill_min: datetime.datetime | None = None,
        fill_max: datetime.datetime | None = None,
    ) -> int:
        """Total bucket count after empty-bucket filling, ignoring
        offset/limit.

        The standard ``_count_groups`` path emits ``SELECT COUNT(*) FROM
        (SELECT DISTINCT ...)`` which only sees populated buckets — it
        would under-count when ``fill=True`` is in effect. We compute
        the total by running the full filled aggregation (without
        offset/limit) and taking ``len`` of the result. Cardinality is
        bounded for analytics queries, so the cost is acceptable; if
        this becomes a hot path in v1.x, swap in a SQL ``generate_series``
        path that COUNTs the spine directly.
        """
        from strawberry_django_aggregates.compiler import compute_aggregation
        rows = compute_aggregation(
            qs,
            group_by=spec,
            aggregates=requested,
            having=having_dict,
            op_args=op_args or {},
            week_start=week_start,
            fill=True,
            fill_min=fill_min,
            fill_max=fill_max,
        )
        return len(rows)

    def _count_groups(
        self,
        qs: QuerySet,
        spec: list[tuple[str, Any]],
        requested: list[tuple[AggregateOp, str | None]],
        having_dict: dict[str, Any],
        op_args: dict[str, dict[str, Any]] | None = None,
        week_start: int = 1,
    ) -> int:
        """Total distinct group buckets matching the request, ignoring
        offset/limit.

        - **No HAVING:** ``qs.values(*group_aliases).distinct().count()``
          — DB-side de-dup, single ``SELECT COUNT(*) FROM (SELECT
          DISTINCT ...)``.
        - **With HAVING:** wrap the aggregated queryset in ``.count()``
          — DB-side count of post-aggregate rows.

        Either way: no Python-side row materialization. The previous
        version called :func:`compute_aggregation` and ``len()``-ed
        the list, which fetched every group row.

        ``week_start`` mirrors ``compute_aggregation`` so the COUNT
        groups by the same WEEK / DAY_OF_WEEK boundaries the data
        query uses. Counting with a different ``week_start`` would
        report a different bucket cardinality than the page returns.
        """
        from django.conf import settings
        from django.db import connections

        from strawberry_django_aggregates.compiler import (
            _build_aggregate_annotations,
            _build_group_by_annotations,
            _build_having_q,
            _resolve_tzinfo,
        )

        if not spec:
            return 1

        vendor = connections[qs.db].vendor
        tzinfo = _resolve_tzinfo(settings.TIME_ZONE)
        group_ann, group_aliases = _build_group_by_annotations(
            qs.model, spec, tzinfo, week_start,
        )

        if not having_dict:
            cqs = qs
            if group_ann:
                cqs = cqs.annotate(**group_ann)
            return cqs.values(*group_aliases).distinct().count()

        agg_ann = _build_aggregate_annotations(
            qs.model, requested, vendor, op_args or {},
        )
        cqs = qs
        if group_ann:
            cqs = cqs.annotate(**group_ann)
        cqs = cqs.values(*group_aliases).annotate(**agg_ann)
        having_q = _build_having_q(having_dict, agg_ann.keys())
        if having_q is not None:
            cqs = cqs.filter(having_q)
        return cqs.count()

    def _shape_aggregate(
        self,
        aggregate_type: type,
        row: dict[str, Any],
        requested: list[tuple[AggregateOp, str | None]],
        op_args: dict[str, dict[str, Any]] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"count": int(row.get("count", 0) or 0)}
        kwargs.update(
            self._build_nested_op_kwargs(
                aggregate_type, row, requested,
            ),
        )
        instance = aggregate_type(**kwargs)
        # Stash count_distinct lookup table for the resolver method —
        # both the single-column and the multi-column tuple shapes
        # are populated. Single-column keys on the field-name string;
        # multi-column keys on a sorted tuple of field-name strings
        # (matches the wire-side canonicalization).
        self._populate_count_distinct_backing(instance, row, requested)
        # Stash percentile backing dicts keyed by ``(field, fraction)``
        # so the method-style resolver can pull the right SQL alias —
        # the alias was assigned via the fraction-derived suffix in
        # :func:`compiler.aggregate_alias`, not by the bare field name.
        self._populate_percentile_backing(instance, row, requested, op_args)
        return instance

    def _shape_grouped(
        self,
        grouped_type: type,
        group_key_type: type,
        row: dict[str, Any],
        requested: list[tuple[AggregateOp, str | None]],
        spec: list[tuple[str, Any]],
        op_args: dict[str, dict[str, Any]] | None = None,
        week_start: int = 1,
    ) -> Any:
        key_kwargs: dict[str, Any] = {}
        for fp, grain in spec:
            field = self.model._meta.get_field(fp)
            alias = group_by_alias(
                fp, grain, field,  # type: ignore[arg-type]
            )
            value = row.get(alias)
            key_kwargs[alias] = value
            # TIME granularity: emit the half-open ``[from, to)``
            # interval as a sibling ``<alias>_range: BucketRange`` per
            # SPEC § 7 (Stream 5). NUMBER granularity has no
            # contiguous range and gets no sibling. ``value`` may be
            # None if the row had a NULL for the underlying date — in
            # that case the range stays None too.
            if isinstance(grain, TimeGranularity) and value is not None:
                from_, to = bucket_range(value, grain, week_start)
                key_kwargs[f"{alias}_range"] = BucketRange(
                    from_=from_, to=to,
                )
        key = group_key_type(**key_kwargs)

        kwargs: dict[str, Any] = {
            "key": key,
            "count": int(row.get("count", 0) or 0),
        }
        kwargs.update(
            self._build_nested_op_kwargs(
                grouped_type, row, requested,
            ),
        )
        instance = grouped_type(**kwargs)
        # Grouped types also expose method-style ops in v1.x, but for
        # v1.0 the percentile fields are only on the top-level aggregate
        # type — populate the backing dicts defensively so a future
        # method-style addition Just Works™ without re-wiring the
        # shaping path.
        self._populate_count_distinct_backing(instance, row, requested)
        self._populate_percentile_backing(instance, row, requested, op_args)
        return instance

    @staticmethod
    def _populate_count_distinct_backing(
        instance: Any,
        row: dict[str, Any],
        requested: list[tuple[AggregateOp, str | None]],
    ) -> None:
        """Walk requested ops and populate the two count_distinct
        backing dicts on ``instance``:

        - ``__count_distinct__[<field_name>] = N``  (single-column)
        - ``__count_distinct_tuple__[(a, b, c)] = N``  (multi-column,
          keyed on a sorted tuple of field-name strings — matches the
          canonicalization in ``_extract_ops_from_grouped`` and the
          resolver lookup in ``types.py``).
        """
        cd_single: dict[str, int] = {}
        cd_tuple: dict[tuple[str, ...], int] = {}
        for op, fp in requested:
            if fp is None:
                continue
            if op is AggregateOp.COUNT_DISTINCT:
                cd_single[fp] = int(
                    row.get(f"count_distinct_{fp}", 0) or 0,
                )
            elif op is AggregateOp.COUNT_DISTINCT_TUPLE:
                key = tuple(sorted(fp.split("__")))
                alias = f"count_distinct_tuple_{fp}"
                cd_tuple[key] = int(row.get(alias, 0) or 0)
        instance.__count_distinct__ = cd_single  # type: ignore[attr-defined]
        instance.__count_distinct_tuple__ = cd_tuple  # type: ignore[attr-defined]

    @staticmethod
    def _populate_percentile_backing(
        instance: Any,
        row: dict[str, Any],
        requested: list[tuple[AggregateOp, str | None]],
        op_args: dict[str, dict[str, Any]] | None,
    ) -> None:
        """Walk the requested ops, locate each percentile call's SQL
        alias (which encodes the fraction), and stash the row value
        in ``instance.__percentile_cont__`` /
        ``instance.__percentile_disc__`` keyed by ``(field, fraction)``.
        """
        from strawberry_django_aggregates.compiler import (
            aggregate_alias as _alias,
        )
        if op_args is None:
            return
        cont: dict[tuple[str, float], Any] = {}
        disc: dict[tuple[str, float], Any] = {}
        for op, fp in requested:
            if fp is None:
                continue
            if op is AggregateOp.PERCENTILE_CONT:
                base = f"{op.value}_{fp}"
                args = op_args.get(base)
                if not args or "fraction" not in args:
                    continue
                fraction = float(args["fraction"])
                alias = _alias(op, fp, fraction=fraction)
                cont[(fp, fraction)] = row.get(alias)
            elif op is AggregateOp.PERCENTILE_DISC:
                base = f"{op.value}_{fp}"
                args = op_args.get(base)
                if not args or "fraction" not in args:
                    continue
                fraction = float(args["fraction"])
                alias = _alias(op, fp, fraction=fraction)
                disc[(fp, fraction)] = row.get(alias)
        instance.__percentile_cont__ = cont  # type: ignore[attr-defined]
        instance.__percentile_disc__ = disc  # type: ignore[attr-defined]

    def _build_nested_op_kwargs(
        self,
        owner_type: type,
        row: dict[str, Any],
        requested: list[tuple[AggregateOp, str | None]],
    ) -> dict[str, Any]:
        """Build ``{op_name: NestedFieldsType(...)}`` kwargs for the
        nested operator types attached to ``owner_type``.

        ``COUNT``, ``COUNT_DISTINCT``, ``PERCENTILE_CONT``, and
        ``PERCENTILE_DISC`` are method-style — they bind through
        backing dicts on the instance, not the dataclass field map —
        and are skipped here.
        """
        # Group requested aggregates by op.
        by_op: dict[AggregateOp, dict[str, Any]] = {}
        for op, fp in requested:
            if op in {
                AggregateOp.COUNT,
                AggregateOp.COUNT_DISTINCT,
                AggregateOp.COUNT_DISTINCT_TUPLE,
                AggregateOp.PERCENTILE_CONT,
                AggregateOp.PERCENTILE_DISC,
            }:
                continue
            assert fp is not None
            by_op.setdefault(op, {})[fp] = row.get(f"{op.value}_{fp}")

        out: dict[str, Any] = {}
        for op, fields_dict in by_op.items():
            nested_attr = op.value
            nested_type_field = next(
                (
                    f for f in dataclasses.fields(owner_type)
                    if f.name == nested_attr
                ),
                None,
            )
            if nested_type_field is None:
                continue
            nested_type = _unwrap_optional(nested_type_field.type)
            instance = nested_type(**fields_dict)
            out[nested_attr] = instance
            # SQL-standard wire aliases (Stream 4): ``every`` mirrors
            # ``bool_and`` and ``some`` mirrors ``bool_or``. The owner
            # type only declares these fields when its allowlist
            # admits BOOL_AND / BOOL_OR, so we guard with a field
            # presence check before assigning. Both aliases share the
            # SAME nested-type instance — no duplicate construction.
            if op is AggregateOp.BOOL_AND and any(
                f.name == "every" for f in dataclasses.fields(owner_type)
            ):
                out["every"] = instance
            elif op is AggregateOp.BOOL_OR and any(
                f.name == "some" for f in dataclasses.fields(owner_type)
            ):
                out["some"] = instance
        return out


@dataclass
class BuiltAggregates:
    """Output of :meth:`AggregateBuilder.build`."""

    aggregate_type:       type
    grouped_type:         type
    grouped_result_type:  type
    group_key_type:       type
    having_input:         type
    group_by_spec:        type
    groupable_field_enum: type
    aggregate_field:      Any
    group_by_field:       Any


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _resolve_granularity(value: str) -> Any:
    """Map a wire-level Granularity enum value back to the typed
    :class:`TimeGranularity` / :class:`NumberGranularity` member.
    """
    try:
        return TimeGranularity(value)
    except ValueError:
        pass
    return NumberGranularity(value)


def _split_having_input_field(name: str) -> tuple[str, str]:
    """``"sum_total_gt"`` → ``("sum_total", "gt")``.

    The comparison suffix is matched against the canonical 8 from
    :data:`compiler.HAVING_COMPARISONS` (longest first to disambiguate
    ``not_in`` from ``in``).
    """
    for cmp_token in sorted(HAVING_COMPARISONS, key=len, reverse=True):
        suffix = f"_{cmp_token}"
        if name.endswith(suffix):
            return name[: -len(suffix)], cmp_token
    raise ValueError(
        f"HAVING input field `{name}` has no recognized comparison suffix.",
    )


def _measure_to_op_field(measure: str) -> tuple[AggregateOp, str | None]:
    """``"count"`` → ``(COUNT, None)``;
    ``"sum_total"`` → ``(SUM, "total")``;
    ``"count_distinct_customer"`` → ``(COUNT_DISTINCT, "customer")``.

    Inverse of :func:`compiler.aggregate_alias`. Raises ``ValueError``
    if no operator prefix matches — that means the SDL emitted a HAVING
    field whose measure isn't in :class:`AggregateOp`, which is a bug
    in :func:`make_having_input`.
    """
    if measure == "count":
        return AggregateOp.COUNT, None
    # Match longest op value first so "count_distinct" beats "count".
    for op in sorted(AggregateOp, key=lambda o: -len(o.value)):
        prefix = f"{op.value}_"
        if measure.startswith(prefix):
            return op, measure[len(prefix):]
    raise ValueError(
        f"Cannot decode HAVING measure `{measure}` to an "
        f"(operator, field) pair.",
    )


def _unwrap_optional(annotation: Any) -> Any:
    """``Optional[T]`` → ``T``; bare ``T`` → ``T``."""
    import typing
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is type(None) or str(
        origin,
    ) == "types.UnionType":
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation
