"""strawberry-django-aggregates — Hasura aggregations for Django.

Public surface:

- :class:`AggregateBuilder` — convenience builder; emits all aggregate
  types and the corresponding strawberry fields for a given model.
- :func:`make_aggregate_type` — generate the ``<Model>Aggregate`` type.
- :func:`make_grouped_type` — generate the ``<Model>Grouped`` /
  ``<Model>GroupedResult`` types.
- :func:`make_having_input` — generate the ``<Model>Having`` input.
- :func:`make_group_by_spec` — generate the ``<Model>GroupBySpec`` input.
- :func:`compute_aggregation` — backend primitive; returns flat
  composite-key result rows. Callable outside GraphQL.
- :class:`AggregateOp` — enum of aggregate operators.
- :class:`TimeGranularity`, :class:`NumberGranularity` — enums of date
  bucketing tokens (``date_trunc`` and ``date_part::int`` respectively).
- :func:`parse_aggregate_order` — parser for ``"<field>:<op>"`` order
  terms; raises on unknown.
- :data:`BigInt` — string-encoded 64-bit signed integer scalar. Output
  type for ``SUM`` over integer Django fields (Postgres widens to
  ``bigint``; the 32-bit GraphQL ``Int`` would silently overflow).
- :class:`BucketRange` + :func:`bucket_range` — half-open ``[from, to)``
  interval emitted as a sibling on ``<Model>GroupKey`` for each
  TIME-granularity bucket (SPEC § 7).
- :func:`generate_bucket_spine` + :func:`fill_bucket_results` —
  empty-bucket filling helpers powering ``compute_aggregation(fill=True)``
  (SPEC § 7.2). Pure stdlib; callable from any Python context.
- :func:`encode_group_cursor` + :func:`decode_group_cursor` — opaque
  cursor encoding for Relay-style grouped pagination (SPEC § 4
  cursor-pagination). Pure stdlib; the same encoding is used by the
  builder's connection field.
- :func:`register_relation_aggregate` — attach a
  ``<relation>Aggregate(filter: ...)`` field on an existing
  strawberry-django parent type so callers can issue
  ``customer.ordersAggregate { count, sum { total } }``. Per-row
  resolver in v1.0; dataloader-based batching is a v1.x improvement
  (SPEC § 4.2 / Stream 9).
- :func:`make_grouped_connection_type` — emit Relay-style
  ``<Model>GroupedConnection`` / ``<Model>GroupedEdge`` types.
- Errors: :class:`AggregateError`, :class:`OperatorNotSupportedError`,
  :class:`OrderFieldNotAllowed`, :class:`AggregationAcrossRelationError`.

See ``docs/SPEC.md`` for the full contract.
"""

from __future__ import annotations

from strawberry_django_aggregates.builder import AggregateBuilder
from strawberry_django_aggregates.compiler import (
    bucket_range,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import (
    AggregateError,
    AggregationAcrossRelationError,
    JSONPathNotAllowed,
    OperatorNotSupportedError,
    OrderFieldNotAllowed,
)
from strawberry_django_aggregates.fill import (
    fill_bucket_results,
    generate_bucket_spine,
)
from strawberry_django_aggregates.granularity import (
    NumberGranularity,
    TimeGranularity,
    validate_week_start,
)
from strawberry_django_aggregates.operators import (
    AggregateOp,
    default_operators_for,
)
from strawberry_django_aggregates.ordering import (
    comodel_ordering_terms,
    parse_aggregate_order,
)
from strawberry_django_aggregates.pagination import (
    decode_group_cursor,
    encode_group_cursor,
)
from strawberry_django_aggregates.relations import (
    register_relation_aggregate,
)
from strawberry_django_aggregates.types import (
    BigInt,
    BucketRange,
    make_aggregate_type,
    make_group_by_spec,
    make_grouped_connection_type,
    make_grouped_type,
    make_having_input,
)

__version__ = "0.1.0"

__all__ = [
    # Builder (high-level)
    "AggregateBuilder",
    # Type generators
    "make_aggregate_type",
    "make_grouped_type",
    "make_grouped_connection_type",
    "make_having_input",
    "make_group_by_spec",
    # Backend primitive
    "compute_aggregation",
    # Vocabularies
    "AggregateOp",
    "TimeGranularity",
    "NumberGranularity",
    "validate_week_start",
    "default_operators_for",
    # Custom scalars
    "BigInt",
    # Bucket range — half-open [from, to) interval for TIME-granularity
    # group-by buckets (SPEC § 7 / Stream 5).
    "BucketRange",
    "bucket_range",
    # Empty-bucket filling — dense bucket spine over date-bucketed
    # results (SPEC § 7.2 / Stream 7).
    "fill_bucket_results",
    "generate_bucket_spine",
    # Cursor pagination — opaque base64 / JSON encoding over the
    # canonical-order group-by alias values (SPEC § 4 / Stream 11).
    "encode_group_cursor",
    "decode_group_cursor",
    # Cross-relation aggregate field — attach
    # ``<relation>Aggregate(filter: ...)`` to a strawberry-django
    # parent type (SPEC § 4.2 / Stream 9).
    "register_relation_aggregate",
    # Ordering
    "parse_aggregate_order",
    "comodel_ordering_terms",
    # Errors
    "AggregateError",
    "OperatorNotSupportedError",
    "OrderFieldNotAllowed",
    "AggregationAcrossRelationError",
    "JSONPathNotAllowed",
]
