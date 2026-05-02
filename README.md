# strawberry-django-aggregates

Hasura-shape aggregations over Django querysets in Strawberry GraphQL.

`count` · `count_distinct` · `sum` · `avg` · `min` · `max` · `stddev` · `variance` · `bool_and` · `bool_or` · `array_agg` · `string_agg` — composed with multi-level `group_by`, `having` filters, and ordering on aggregate aliases. Inspired by [Hasura](https://hasura.io/docs/latest/api-reference/graphql-api/query/#aggregateobject)'s `<table>_aggregate`, [PostGraphile](https://github.com/graphile/pg-aggregates)'s `pg-aggregates`, and [Odoo 18](https://github.com/odoo/odoo/blob/18.0/odoo/models.py)'s `_read_group`. Built for [`strawberry-django`](https://strawberry-django.readthedocs.io) over PostgreSQL and SQLite.

```python
from decimal import Decimal
from datetime import datetime
from strawberry import auto
import strawberry, strawberry_django
from strawberry_django_aggregates import AggregateBuilder

from .models import Order

@strawberry_django.type(Order)
class OrderType:
    id: auto
    customer: "CustomerType"
    total: Decimal
    status: str
    created_at: datetime

# One call wires count/sum/avg/min/max + group_by + having into the schema:
order_aggs = AggregateBuilder(
    model=Order,
    aggregate_fields=["total"],
    group_by_fields=["customer", "status", "created_at"],
).build()

@strawberry.type
class Query:
    orders_aggregate = order_aggs.aggregate_field
    orders_group_by  = order_aggs.group_by_field
```

Generates a fully-typed GraphQL surface:

```graphql
type Query {
  ordersAggregate(filter: OrderFilter): OrderAggregate!
  ordersGroupBy(
    filter:    OrderFilter
    groupBy:   [OrderGroupBySpec!]!
    having:    OrderHaving
    orderBy:   [OrderGroupOrder!]
    pagination: OffsetPagination
  ): OrderGroupedResult!
}

type OrderAggregate {
  count:           Int!
  countDistinct(field: OrderCountableField!): Int!
  sum:             OrderSumFields
  avg:             OrderAvgFields
  min:             OrderMinFields
  max:             OrderMaxFields
  stddev:          OrderStddevFields    # Postgres only
  variance:        OrderVarianceFields  # Postgres only
}

type OrderGrouped {
  key:   OrderGroupKey!   # composite — every requested groupBy field present
  count: Int!
  sum:   OrderSumFields
  # ... no recursive subgroups field — flat results
}
```

## Features

- **Hasura-canonical schema shape.** `<Model>Aggregate { count, countDistinct, sum, avg, min, max, stddev, variance, boolAnd, boolOr, arrayAgg, stringAgg }`.
- **Odoo-grade group-by.** Multi-level via composite keys (flat result rows), dual date-granularity tracks (`date_trunc` returning `DateTime` AND `date_part::int` returning `Int`), timezone-correct bucketing.
- **HAVING with aggregate aliases.** `{ countGt: 5, sumTotalGt: 1000 }` — typed inputs generated per measure.
- **Ordering on aggregates.** `[{ field: "total:sum", direction: DESC }]` — fail-loud on unknown terms (Odoo's pre-17 silent-drop bug avoided).
- **Standalone backend primitive.** `compute_aggregation(qs, group_by, aggregates, having, order_by, ...)` is callable from any Python context — DRF view, Celery task, admin script, MCP tool — not just GraphQL resolvers.
- **Determinism.** Type generation produces byte-identical SDL for the same inputs.
- **No magic.** Every operator, every granularity, every type is whitelisted.

## Non-goals

- **Cross-database aggregation.** PostgreSQL + SQLite only. SQLite degrades gracefully on `array_agg`/`string_agg`/`stddev`/`variance` — those operators raise `OperatorNotSupportedError` at resolver entry.
- **Auto-traversal of one-to-many / many-to-many for measures.** This is the silent row-multiplication footgun [Odoo refuses to ship](https://github.com/odoo/odoo/blob/18.0/odoo/models.py); we follow. `array_agg` is the explicit escape hatch.
- **Permission integration.** The library expects a pre-scoped queryset — the caller has already applied `accessible_by(user)` or equivalent. This keeps the library compatible with django-guardian, django-rules, [django-rebac](#) (when shipped), or hand-rolled permission systems.

## Status

Beta (v0.2.1). The schema shape, operator vocabulary, and `compute_aggregation` signature are stable for early adopters, but minor-level iteration is still expected before a 1.0 stability commitment — see [`docs/SPEC.md`](./docs/SPEC.md) § 16. Runtime: Python 3.13, Django 6.0.

## Documentation

- Full contract: [`docs/SPEC.md`](./docs/SPEC.md) — operator catalog, granularity tracks, HAVING semantics, ordering rules, timezone handling, and the Odoo-derived footgun audit.
- Naming and wire vocabulary: [`docs/TERMINOLOGY.md`](./docs/TERMINOLOGY.md)
- Contributor quality gate: [`CONTRIBUTING.md`](./CONTRIBUTING.md)

## License

BSD-3-Clause.
