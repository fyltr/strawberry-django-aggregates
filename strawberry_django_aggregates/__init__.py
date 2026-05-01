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
- Errors: :class:`AggregateError`, :class:`OperatorNotSupportedError`,
  :class:`OrderFieldNotAllowed`, :class:`AggregationAcrossRelationError`.

See ``docs/SPEC.md`` for the full contract.
"""

from __future__ import annotations

from strawberry_django_aggregates.builder import AggregateBuilder
from strawberry_django_aggregates.compiler import compute_aggregation
from strawberry_django_aggregates.errors import (
    AggregateError,
    AggregationAcrossRelationError,
    OperatorNotSupportedError,
    OrderFieldNotAllowed,
)
from strawberry_django_aggregates.granularity import (
    NumberGranularity,
    TimeGranularity,
)
from strawberry_django_aggregates.operators import (
    AggregateOp,
    default_operators_for,
)
from strawberry_django_aggregates.ordering import parse_aggregate_order
from strawberry_django_aggregates.types import (
    make_aggregate_type,
    make_group_by_spec,
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
    "make_having_input",
    "make_group_by_spec",
    # Backend primitive
    "compute_aggregation",
    # Vocabularies
    "AggregateOp",
    "TimeGranularity",
    "NumberGranularity",
    "default_operators_for",
    # Ordering
    "parse_aggregate_order",
    # Errors
    "AggregateError",
    "OperatorNotSupportedError",
    "OrderFieldNotAllowed",
    "AggregationAcrossRelationError",
]
