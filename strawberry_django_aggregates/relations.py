"""Cross-relation aggregate field — Stream 9.

Exposes ``<relation>Aggregate(filter: ...)`` as a field on existing
strawberry-django parent types. The new helper
:func:`register_relation_aggregate` integrates with strawberry-django
parent types directly — the parent does NOT need to use
:class:`~strawberry_django_aggregates.builder.AggregateBuilder`.

Example::

    from strawberry_django_aggregates import (
        AggregateBuilder, register_relation_aggregate,
    )

    order_built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
    ).build()

    @strawberry_django.type(Customer)
    class CustomerType:
        id: int
        name: str

    register_relation_aggregate(CustomerType, "orders", order_built)

    @strawberry.type
    class Query:
        customers: list[CustomerType] = strawberry_django.field()

The schema now exposes
``Customer.ordersAggregate { count, sum { total, quantity } }`` —
each customer's row computes the child aggregate filtered by the
reverse FK. An optional ``filter`` argument composes via
:func:`strawberry_django.filters.apply` if a child filter input was
declared.

Per-row resolver invocation: each parent row triggers one aggregate
query against the child queryset. For v1.0 we accept this N+1 cost
and document it as a v1.x improvement target — proper dataloader-
based batching is a separate stream. Pre-scoped permissions still
apply because the child's ``_default_manager`` is the queryset
source; callers wanting tighter scoping pass ``get_queryset`` (a
callable ``(info, parent) -> QuerySet``) per-registration.

CLAUDE.md Critical Rule 1 holds: this module imports nothing from
``django.contrib.auth`` and never inspects an ``info.context.user``.
Permission scoping is the consumer's responsibility — see
``get_queryset`` below.

CLAUDE.md Critical Rule 4 holds: this is the **explicit subquery**
path (one aggregate query per parent row, child queryset filtered
by the reverse FK), not auto-traversal of measures. Default
``compute_aggregation`` settings apply — no
``allow_relation_traversal`` flag is passed. The child queryset is
single-model from the child's perspective.

CLAUDE.md Critical Rule 9 holds: the resolver is the only
GraphQL-aware piece in this module; the heavy lifting delegates to
:func:`compute_aggregation` (framework-agnostic) and
:func:`shape_aggregate_row` (extracted from
:class:`AggregateBuilder` for reuse).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import strawberry
import strawberry_django
from strawberry_django.filters import apply as apply_filters

from strawberry_django_aggregates.builder import (
    BuiltAggregates,
    shape_aggregate_row,
)
from strawberry_django_aggregates.compiler import compute_aggregation
from strawberry_django_aggregates.operators import AggregateOp

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet


__all__ = ["register_relation_aggregate"]


def register_relation_aggregate(
    parent_type: type,
    relation_name: str,
    child_built: BuiltAggregates,
    *,
    field_name: str | None = None,
    filter_type: type | None = None,
    get_queryset: Callable[[Any, Any], QuerySet] | None = None,
) -> None:
    """Attach a ``<relation>Aggregate`` field to ``parent_type``.

    Parameters
    ----------
    parent_type
        A strawberry type wrapping a Django model — typically
        produced by :func:`strawberry_django.type`. The underlying
        model is resolved via the
        ``__strawberry_django_definition__`` attribute that
        strawberry-django attaches.
    relation_name
        The Django reverse-accessor name on the parent model
        pointing at the child (e.g. ``"orders"`` for
        ``Customer.orders`` where ``Order.customer`` is a FK with
        ``related_name="orders"``). The reverse field is looked up
        via ``parent_model._meta.get_field(relation_name)``; we
        accept :class:`ManyToOneRel` (``related_name`` of an FK) and
        :class:`ManyToManyRel`. Forward relations and one-to-one
        reverse relations are out of scope in v1.0 — they don't
        need an aggregate field (the parent has at most one child).
    child_built
        The :class:`BuiltAggregates` object returned by
        :meth:`AggregateBuilder.build` for the child model.
    field_name
        GraphQL field name override. Defaults to
        ``f"{relation_name}_aggregate"`` (Strawberry will camelCase
        to ``f"{relation_name}Aggregate"`` on the wire).
    filter_type
        Optional strawberry-django filter input type for the child.
        When provided, the field accepts a ``filter: <FilterType>``
        argument applied via :func:`strawberry_django.filters.apply`
        before aggregation.
    get_queryset
        Optional callable ``(info, parent_instance) -> QuerySet``
        for per-registration queryset scoping. When ``None`` (the
        default), the resolver uses
        ``getattr(parent_instance, relation_name).all()`` — the
        Django reverse-accessor manager pre-filtered by the parent
        FK. Permission-naive: if you need ``accessible_by(user)``
        semantics, override here.

    Raises
    ------
    TypeError
        If ``parent_type`` is not a strawberry-django type (lacks
        ``__strawberry_django_definition__``).
    ValueError
        If ``relation_name`` does not resolve to a reverse
        many-to-one or many-to-many descriptor on the parent model.

    Notes
    -----
    Mutates ``parent_type`` in place — re-registers a strawberry
    field. Idempotent for the SDL: registering the same
    ``(parent_type, relation_name, child_built)`` tuple twice yields
    byte-identical SDL output (CLAUDE.md Critical Rule 2). The
    second registration overwrites the first cleanly.

    The N+1 query cost: each parent row triggers one aggregate
    query against the child queryset. For analytics dashboards
    showing tens of parents this is acceptable; for long-tail
    parent lists, prefer the top-level child aggregate field
    (``ordersAggregate(filter: { customer: { id: $id } })``) which
    runs in one query. Proper dataloader-based batching is a v1.x
    improvement target — see SPEC § 4.2.
    """
    parent_model = _resolve_parent_model(parent_type)
    reverse_field = _resolve_reverse_field(parent_model, relation_name)
    aggregate_type = child_built.aggregate_type

    fname = field_name or f"{relation_name}_aggregate"

    resolver = _make_relation_resolver(
        parent_model=parent_model,
        relation_name=relation_name,
        reverse_field=reverse_field,
        child_model=reverse_field.related_model,
        aggregate_type=aggregate_type,
        filter_type=filter_type,
        get_queryset=get_queryset,
        json_paths=child_built.json_paths,
    )
    field_obj = strawberry_django.field(
        resolver=resolver,
        # Same rationale as the rest of the package: our resolvers
        # project via ``.values(...)`` inside ``compute_aggregation``,
        # so strawberry-django's optimizer would either no-op or
        # conflict with the GROUP BY semantics. Disable per-field
        # so the parent-level optimizer still works for the parent's
        # own fields.
        disable_optimization=True,
    )

    setattr(parent_type, fname, field_obj)
    # Re-process the strawberry definition so the new field is
    # picked up by the schema printer. Strawberry caches fields on
    # the ``__strawberry_definition__`` object computed at decorator
    # time; mutating ``parent_type`` after the fact is supported
    # only when we also extend the cached field list. The cleanest
    # approach is to reach into the definition's ``fields`` list and
    # add our ``StrawberryField`` directly — same shape strawberry
    # produces for declared class attributes.
    _attach_field_to_definition(parent_type, fname, field_obj)


# ---------------------------------------------------------------------------
# Resolver factory
# ---------------------------------------------------------------------------


def _make_relation_resolver(
    *,
    parent_model: type[Model],
    relation_name: str,
    reverse_field: Any,
    child_model: type[Model],
    aggregate_type: type,
    filter_type: type | None,
    get_queryset: Callable[[Any, Any], QuerySet] | None,
    json_paths: dict[str, str] | None = None,
) -> Callable[..., Any]:
    """Build the per-row strawberry resolver for the relation
    aggregate field.

    The resolver is closure-captured over the ``aggregate_type`` and
    relation metadata so it can be attached to many parent types
    without per-call re-resolution.

    The resolver's first positional argument is ``self`` —
    Strawberry binds this to the parent dataclass instance whose
    underlying Django model row we aggregate against. Strawberry-
    django's default field resolver hydrates ``self`` from the row
    DB attribute when the type was decorated with
    :func:`strawberry_django.type`, so ``getattr(self, "pk", None)``
    returns the live Django ``Customer`` instance's pk.
    """
    # The selection-walker code lives on AggregateBuilder; instead of
    # re-implementing it, we synthesize a thin shim that mirrors the
    # public _OP_FROM_WIRE / a_fields path. The relation resolver
    # builds a list of requested ops by walking ``info.selected_fields``
    # exactly the way the top-level aggregate field does.
    from strawberry_django_aggregates.builder import (
        _OP_FROM_WIRE,
        _to_camel_alias,
    )
    from strawberry_django_aggregates.types import _resolve_aggregate_fields

    # ``json_paths`` is forwarded from the originating ``BuiltAggregates``
    # so JSON-path measures (Stream 17) and percentile measures
    # (Stream 14) work through the relation aggregate path the same way
    # they do on the top-level aggregate field.

    # Compute once per registration: which (op, field) pairs the schema
    # admits for the child. This is the same allowlist the top-level
    # aggregate field uses.
    a_fields = _resolve_aggregate_fields(
        child_model, None, json_paths,
    )

    def _json_alias_to_dotted(name: str) -> str:
        """Translate a wire-side JSON-path alias back to dotted form.

        Accepts BOTH ``"metadata__region"`` (group_by enum value /
        Python alias) and ``"metadata_Region"`` (Strawberry's GraphQL
        camelCasing of the same Python identifier on nested-operator
        types) and maps either to ``"metadata.region"`` IFF
        ``metadata.region`` was declared in ``json_paths``. Names that
        don't match any declared JSON path round-trip unchanged so
        plain field names continue to work.
        """
        if not json_paths:
            return name
        for dotted in json_paths:
            alias = dotted.replace(".", "__")
            if alias == name or _to_camel_alias(alias) == name:
                return dotted
        return name

    def _walk_selections(node: Any) -> Any:
        for child in getattr(node, "selections", None) or []:
            if hasattr(child, "alias"):
                yield child
            else:
                yield from _walk_selections(child)

    def _extract_ops(
        agg_sel: Any,
        op_args: dict[str, dict[str, Any]],
    ) -> list[tuple[AggregateOp, str | None]]:
        """Walk an Aggregate selection and emit ``(op, field)`` pairs.

        Mirrors :meth:`AggregateBuilder._extract_ops_from_grouped`.
        ``count_distinct`` reads its argument the same way; method-
        style percentile fields populate ``op_args`` with the supplied
        ``fraction``. JSON-path aliases (Strawberry's camelCased
        ``metadata_Region`` or the underscore-form ``metadata__region``)
        are mapped back to their dotted form via
        :func:`_json_alias_to_dotted` so the compiler routes them
        through :func:`_resolve_json_path`.
        """
        out: list[tuple[AggregateOp, str | None]] = []
        for inner in _walk_selections(agg_sel):
            inner_name = getattr(inner, "name", None)
            if inner_name is None:
                continue
            op = _OP_FROM_WIRE.get(inner_name)
            if op is None:
                continue
            if op is AggregateOp.COUNT:
                out.append((op, None))
                continue
            if op is AggregateOp.COUNT_DISTINCT:
                args = getattr(inner, "arguments", {}) or {}
                field_arg = args.get("field")
                fields_arg = args.get("fields")
                single_set = field_arg is not None
                multi_set = (
                    fields_arg is not None and len(fields_arg) > 0
                )
                if single_set == multi_set:
                    continue
                if single_set:
                    fname = _arg_to_path(field_arg)
                    if fname is not None:
                        out.append((op, fname))
                    continue
                if fields_arg is None:
                    continue
                names = [_arg_to_path(f) for f in fields_arg]
                clean = [n for n in names if n is not None]
                if not clean:
                    continue
                joined = "__".join(sorted(clean))
                out.append((AggregateOp.COUNT_DISTINCT_TUPLE, joined))
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
                fname = _arg_to_path(field_arg)
                if fname is None:
                    continue
                fname = _json_alias_to_dotted(fname)
                out.append((op, fname))
                # ``op_args`` is keyed by the base alias
                # (``percentile_cont_total``), NOT the alias with the
                # fraction suffix — see ``compiler._require_fraction``.
                base_alias = f"{op.value}_{fname}"
                op_args.setdefault(
                    base_alias, {"fraction": fraction_arg},
                )
                continue
            for f in _walk_selections(inner):
                fname = getattr(f, "name", None)
                if fname is None:
                    continue
                # Try the wire name verbatim first; if that doesn't match
                # the allowlist, try translating it as a JSON-path alias.
                # ``a_fields`` already contains JSON-path entries in
                # dotted form when ``json_paths`` was configured.
                if fname in a_fields:
                    out.append((op, fname))
                    continue
                dotted = _json_alias_to_dotted(fname)
                if dotted != fname and dotted in a_fields:
                    out.append((op, dotted))
        return out

    def _resolve_child_queryset(
        info: Any, parent_instance: Any,
    ) -> QuerySet:
        if get_queryset is not None:
            return get_queryset(info, parent_instance)
        # Reverse accessor — works for both ManyToOneRel
        # (``customer.orders``) and ManyToManyRel descriptors.
        # ``.all()`` materializes it as a QuerySet bound to the
        # child manager already filtered by the parent FK.
        manager = getattr(parent_instance, relation_name)
        return manager.all()

    def _walk_and_compute(qs: QuerySet, info: Any) -> Any:
        op_args: dict[str, dict[str, Any]] = {}
        requested: list[tuple[AggregateOp, str | None]] = [
            (AggregateOp.COUNT, None),
        ]
        for sel in getattr(info, "selected_fields", []) or []:
            requested.extend(_extract_ops(sel, op_args))
        seen: set[tuple[Any, Any]] = set()
        deduped: list[tuple[AggregateOp, str | None]] = []
        for entry in requested:
            if entry in seen:
                continue
            seen.add(entry)
            deduped.append(entry)
        rows = compute_aggregation(
            qs, aggregates=deduped,
            op_args=op_args or None,
            json_paths=json_paths,
        )
        row = rows[0] if rows else {}
        return shape_aggregate_row(
            aggregate_type, row, deduped,
            op_args=op_args or None,
            json_paths=json_paths,
        )

    if filter_type is not None:
        def resolver(
            self: Any, info: strawberry.Info, filter: Any = None,
        ) -> Any:
            qs = _resolve_child_queryset(info, self)
            if filter is not None:
                qs = apply_filters(filter, qs, info=info)
            return _walk_and_compute(qs, info)

        resolver.__annotations__ = {
            "self":   Any,
            "info":   strawberry.Info,
            "filter": filter_type | None,
            "return": aggregate_type,
        }
    else:
        def resolver(  # type: ignore[misc]
            self: Any, info: strawberry.Info,
        ) -> Any:
            qs = _resolve_child_queryset(info, self)
            return _walk_and_compute(qs, info)

        resolver.__annotations__ = {
            "self":   Any,
            "info":   strawberry.Info,
            "return": aggregate_type,
        }
    return resolver


# ---------------------------------------------------------------------------
# Internal — model / field resolution
# ---------------------------------------------------------------------------


def _resolve_parent_model(parent_type: type) -> type[Model]:
    """Resolve the Django model for a strawberry-django parent type.

    strawberry-django attaches a ``__strawberry_django_definition__``
    descriptor at decorator time whose ``.model`` is the underlying
    Django model class. We don't fall back to the strawberry
    definition because Stream 9's contract requires the parent to
    be a strawberry-django type (we need a Django model to look up
    the reverse accessor).
    """
    defn = getattr(parent_type, "__strawberry_django_definition__", None)
    if defn is None:
        raise TypeError(
            f"`{parent_type.__name__}` is not a strawberry-django "
            "type. `register_relation_aggregate` requires the parent "
            "to be decorated with `@strawberry_django.type(<Model>)` "
            "so the underlying Django model can be resolved.",
        )
    return defn.model


def _resolve_reverse_field(
    parent_model: type[Model], relation_name: str,
) -> Any:
    """Resolve the reverse-accessor descriptor on the parent model.

    Accepts ``ManyToOneRel`` (FK reverse) and ``ManyToManyRel``
    (M2M reverse) — both expose ``.related_model`` and
    ``.field.attname``. Rejects forward FKs / O2O / scalar fields
    with a clear error.
    """
    from django.db.models.fields.reverse_related import (
        ManyToManyRel,
        ManyToOneRel,
    )

    try:
        field = parent_model._meta.get_field(relation_name)
    except Exception as exc:  # FieldDoesNotExist
        raise ValueError(
            f"`{parent_model.__name__}` has no field "
            f"`{relation_name}`. Pass the reverse-accessor name "
            f"declared via `related_name=...` on the FK on the "
            f"child model.",
        ) from exc
    if not isinstance(field, (ManyToOneRel, ManyToManyRel)):
        raise ValueError(
            f"`{parent_model.__name__}.{relation_name}` is "
            f"`{type(field).__name__}`, not a reverse one-to-many "
            f"or many-to-many. `register_relation_aggregate` only "
            f"makes sense for reverse FK / M2M relations where the "
            f"parent has many children. Forward FKs and scalar "
            f"fields don't need an aggregate field.",
        )
    return field


def _attach_field_to_definition(
    parent_type: type, fname: str, field_obj: Any,
) -> None:
    """Splice ``field_obj`` into ``parent_type.__strawberry_definition__``.

    Strawberry's :func:`strawberry.type` decorator scans the class
    body once at decoration time and freezes the field list on the
    ``StrawberryObjectDefinition`` object. ``setattr(parent_type,
    fname, field_obj)`` alone updates the class but the cached
    definition (used by the schema printer) doesn't see the new
    attribute. We bind the field's ``python_name`` (so Strawberry
    derives the camelCase GraphQL name from it instead of the
    resolver function's ``__name__``) and append it to the cached
    fields list.

    Re-registering the same name is idempotent — we replace any
    pre-existing field with that name.
    """
    defn = parent_type.__strawberry_definition__  # type: ignore[attr-defined]
    # Setting python_name drives Strawberry's auto-camelCase logic
    # for the GraphQL field name. Without this the field comes out
    # as the resolver-function ``__name__`` (``"resolver"`` here).
    field_obj.python_name = fname
    # Drop any existing field with the same Python name so a
    # re-registration is idempotent (Critical Rule 2: byte-
    # identical SDL across two registrations).
    defn.fields = [f for f in defn.fields if f.python_name != fname]
    defn.fields.append(field_obj)


def _arg_to_path(arg: Any) -> str | None:
    """Resolve a CountableField enum argument to a model field name."""
    if hasattr(arg, "value"):
        return str(arg.value)
    if isinstance(arg, str):
        return arg.lower() if arg.isupper() else arg
    return None
