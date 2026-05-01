# strawberry-django-aggregates — SPEC

> Hasura-shape aggregations over Django querysets in Strawberry GraphQL.
>
> Status: **draft**. Source of truth for the implementation in
> `strawberry_django_aggregates/`. Inspired by Hasura's `<table>_aggregate`,
> PostGraphile's `pg-aggregates`, and Odoo 18's `_read_group`.

This spec is the contract. It mirrors Odoo 18's `_read_group` semantics where they're load-bearing (composite-key flat results, dual date-granularity tracks, timezone-correct bucketing, HAVING on aggregate aliases, ordering on aggregates) and ports the Hasura/PostGraphile schema shape that GraphQL frontends already understand.

## 1 · Goals and non-goals

**Goals.**
- Drop-in companion to `strawberry-django`. One `AggregateBuilder` call per type emits `<Model>Aggregate`, `<Model>Grouped`, `<Model>Having`, `<Model>GroupBySpec`, and the two query fields that consume them.
- Hasura-canonical schema shape: `count` + `count_distinct` + `sum`/`avg`/`min`/`max`/`stddev`/`variance` + `bool_and`/`bool_or` + `array_agg`/`string_agg`. One nested type per operator family (`<Model>SumFields`, etc.) so fields are typed end-to-end on the wire.
- Odoo-grade `group_by`: multi-level via composite keys (flat result rows), dual date-granularity tracks (`date_trunc` returning DateTime AND `date_part::int` returning Int), timezone-correct bucketing.
- HAVING with aggregate aliases. `<Model>Having` is a typed input with one entry per `(measure, comparison)` pair.
- Ordering on aggregates. `[{ field: "total:sum", direction: DESC }]`. **Fail-loud** on unknown terms.
- Standalone backend primitive — `compute_aggregation(qs, ...)` is callable from any Python context (DRF, Celery, admin, MCP). The GraphQL resolver is a thin presentation wrapper.
- Determinism. Same inputs ⇒ byte-identical SDL emission.
- Strict whitelisting. Every operator, every granularity, every field is on an allowlist; no arbitrary SQL.

**Non-goals (v1).**
- Cross-database aggregation. PostgreSQL + SQLite only. Postgres-only operators (`array_agg`, `string_agg`, `stddev`, `variance`) raise `OperatorNotSupportedError` at resolver entry on SQLite.
- Auto-traversal of one-to-many / many-to-many relations for measures. Silent row multiplication corrupts every measure in the same query — Odoo refuses this and so do we. `array_agg` is the explicit escape hatch.
- Permission integration. The library expects a pre-scoped queryset; the caller has already applied `accessible_by(user)` or `filter(owner=request.user)`. This keeps it compatible with django-guardian, django-rules, [django-rebac](#) (when published), or hand-rolled permission systems.
- Cursor pagination on grouped results. Offset only.
- Aggregation-of-relation as a top-level field on the parent (e.g. `order.itemsAggregate`). Use a top-level `<child>Aggregate(filter: { parent: { id: $id } })` query on the child model instead.

## 2 · Why a separate library

Strawberry-django's built-ins stop at `annotate={"book_count": Count("books")}` returning a single scalar. There's no Hasura-shaped wrapper, no group-by surface, no HAVING, no aggregate-aware ordering, no date-bucketing. The 2026 ecosystem search turned up exactly one candidate (`strawberry-graphql/strawberry-orm`) — alpha, 6 stars, breaking-changes flagged. PostGraphile's `pg-aggregates` is the spiritual reference but TypeScript-only. graphene-django has nothing comparable. The gap is real and worth filling.

This library deliberately has no `Angee*`, no `REBAC*`, no opinion about the host framework. The minimum surface is: pass a queryset and a field allowlist; receive Strawberry types and resolvers.

## 3 · Architecture

```
┌────────────────────────────────────────────────────────────┐
│ Consumer code                                              │
│   • Django models                                          │
│   • strawberry-django types                                │
│   • One AggregateBuilder per type (or direct make_*)      │
└────────────────────────────────────────────────────────────┘
                           ↓
┌────────────────────────────────────────────────────────────┐
│ strawberry_django_aggregates                               │
│   builder.py     — AggregateBuilder convenience            │
│   types.py       — make_aggregate_type / _grouped_type /    │
│                    _having_input / _group_by_spec          │
│   operators.py   — AggregateOp enum + per-field-type       │
│                    default allowlists                      │
│   granularity.py — TimeGranularity / NumberGranularity     │
│   compiler.py    — compute_aggregation backend primitive   │
│   ordering.py    — parse_aggregate_order (fail-loud)       │
│   errors.py      — exception hierarchy                      │
└────────────────────────────────────────────────────────────┘
                           ↓
┌────────────────────────────────────────────────────────────┐
│ strawberry-django + strawberry-graphql                     │
│   • @strawberry_django.type / .filter / .order             │
│   • DjangoOptimizerExtension                               │
└────────────────────────────────────────────────────────────┘
                           ↓
┌────────────────────────────────────────────────────────────┐
│ Django ORM (PostgreSQL or SQLite)                          │
└────────────────────────────────────────────────────────────┘
```

Two layers, sharply separated:

1. **Backend primitive (`compute_aggregation`)** — pure ORM. Takes a queryset, returns a list of dicts. No GraphQL, no Strawberry, no I/O. Callable from any Python context.
2. **Type generators + resolver wrappers** — emit Strawberry types and the field functions that drive GraphQL queries through (1).

This mirrors Odoo's `_read_group` (backend primitive returning flat tuples) vs `web_read_group` (presentation wrapper returning dicts with display names). The separation is what makes the library testable and reusable outside GraphQL contexts.

## 4 · Schema shape — the Hasura model

For a Django model `Order` with `aggregate_fields=["total"]` and `group_by_fields=["customer", "status", "created_at"]`, the builder emits:

```graphql
type OrderAggregate {
  count:           Int!
  countDistinct(field: OrderCountableField!): Int!
  sum:             OrderSumFields
  avg:             OrderAvgFields
  min:             OrderMinFields
  max:             OrderMaxFields
  stddev:          OrderStddevFields    # Postgres only
  variance:        OrderVarianceFields  # Postgres only
  boolAnd:         OrderBoolAndFields   # for boolean fields
  boolOr:          OrderBoolOrFields    # for boolean fields
  arrayAgg:        OrderArrayAggFields  # Postgres only — returns ID/string lists
  stringAgg:       OrderStringAggFields # Postgres only
}

# Per-operator nested types — only fields whose type supports the op appear:
type OrderSumFields    { total: Decimal }
type OrderAvgFields    { total: Float }
type OrderMinFields    { total: Decimal, createdAt: DateTime }
type OrderMaxFields    { total: Decimal, createdAt: DateTime }
type OrderStddevFields { total: Float }
type OrderVarianceFields { total: Float }

type OrderGrouped {
  key:    OrderGroupKey!         # composite — every requested groupBy field present
  count:  Int!
  sum:    OrderSumFields
  avg:    OrderAvgFields
  min:    OrderMinFields
  max:    OrderMaxFields
  # NO subgroups field — flat results, client folds for tree UI
}

type OrderGroupedResult {
  results:  [OrderGrouped!]!
  pageInfo: OffsetPageInfo!
}

type OrderGroupKey {
  customerId:  ID
  status:      OrderStatusEnum
  createdAt:   DateTime          # bucketed if granularity arg used
  dayOfWeek:   Int               # populated when bucketed via NumberGranularity
}

input OrderGroupBySpec {
  field:        OrderGroupableField!
  granularity:  Granularity     # nullable; required only on date/datetime fields
}

input OrderHaving {
  countGt:        Int    countLt:        Int    countEq:        Int
  countDistinctGt: Int   countDistinctLt: Int   countDistinctEq: Int
  sumTotalGt:     Decimal  sumTotalLt:    Decimal  sumTotalEq:    Decimal
  avgTotalGt:     Float    avgTotalLt:    Float    avgTotalEq:    Float
  # ... one entry per (op, field, comparison) tuple from the allowlist
}

enum OrderGroupableField { CUSTOMER, STATUS, CREATED_AT }
enum OrderCountableField { ID, CUSTOMER, STATUS }
enum Granularity {
  # TIME track — date_trunc → DateTime
  YEAR, QUARTER, MONTH, WEEK, DAY, HOUR, MINUTE, SECOND,
  # NUMBER track — date_part::int → Int
  YEAR_NUMBER, QUARTER_NUMBER, MONTH_NUMBER, ISO_WEEK_NUMBER,
  DAY_OF_YEAR, DAY_OF_MONTH, DAY_OF_WEEK,
  HOUR_NUMBER, MINUTE_NUMBER, SECOND_NUMBER,
}
```

**Flat-result design.** No recursive `subgroups: [OrderGrouped!]` field. Pagination of trees is hellish; pagination of flat lists is trivial. Multi-level `group_by` produces multiple result rows; client-side folding for tree UIs is the caller's job. This matches Odoo's `_read_group` return shape (`list[tuple]` with composite keys) and PostGraphile's `groupedAggregates`.

## 5 · Aggregate operator vocabulary

```
AggregateOp = StrEnum:
    COUNT          = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM            = "sum"
    AVG            = "avg"
    MIN            = "min"
    MAX            = "max"
    STDDEV         = "stddev"          # Postgres only
    VARIANCE       = "variance"        # Postgres only
    BOOL_AND       = "bool_and"
    BOOL_OR        = "bool_or"
    ARRAY_AGG      = "array_agg"       # Postgres only
    STRING_AGG     = "string_agg"      # Postgres only
```

Direct mapping to SQL — no operator outside this enum reaches the database:

| Operator | SQL | Database support | Result type |
|---|---|---|---|
| `count` | `COUNT(*)` | All | `Int!` |
| `count_distinct` | `COUNT(DISTINCT col)` | All | `Int!` |
| `sum` | `SUM(col)` | All | numeric type of `col` |
| `avg` | `AVG(col)` | All | `Float` (or `Decimal` for `DecimalField`) |
| `min` / `max` | `MIN(col)` / `MAX(col)` | All | type of `col` |
| `stddev` | `STDDEV_SAMP(col)` | **Postgres only** | `Float` |
| `variance` | `VAR_SAMP(col)` | **Postgres only** | `Float` |
| `bool_and` | `BOOL_AND(col)` | Postgres native; SQLite emulated via `MIN(col::int)::bool` | `Boolean` |
| `bool_or` | `BOOL_OR(col)` | Postgres native; SQLite emulated via `MAX(col::int)::bool` | `Boolean` |
| `array_agg` | `ARRAY_AGG(col ORDER BY <pk>)` | **Postgres only** | `[ID!]` or `[String!]` etc. |
| `string_agg` | `STRING_AGG(col, ',' ORDER BY <pk>)` | **Postgres only** | `String` |

**`array_agg` returns IDs only — never auto-hydrated.** Odoo's `recordset` operator browse-resolves to live records and is a serialization landmine in GraphQL (think: 10000 element arrays expanded into nested objects on every group). We refuse the auto-hydration. Clients refetch by ID and Strawberry's `DjangoOptimizerExtension` batches the lookup.

### Per-field-type defaults

Field types map to default operator allowlists. Consumers can narrow further via the `operators` arg:

| Django field type | Default operators |
|---|---|
| `IntegerField`, `BigIntegerField`, `FloatField`, `DecimalField`, `DurationField` | `sum, avg, min, max, stddev, variance` |
| `DateField`, `DateTimeField`, `TimeField` | `min, max` |
| `BooleanField` | `bool_and, bool_or` |
| `CharField`, `TextField`, `EmailField`, `URLField`, `SlugField` | `min, max, array_agg, string_agg` |
| `UUIDField`, `AutoField`, `BigAutoField` | `array_agg` (by ID) |
| `ForeignKey`, `OneToOneField` | `array_agg` (by FK ID) |
| `ManyToManyField`, reverse FK | **none** — would row-multiply |

`count` and `count_distinct` are always present at the type level (operate on rows, not on a measure).

## 6 · Group-by spec

The wire format is the typed input object — no Odoo-style `field:granularity` strings:

```graphql
ordersGroupBy(groupBy: [
  { field: CREATED_AT, granularity: MONTH },
  { field: CUSTOMER }
])
```

Server resolves to a list of `(field_name, granularity_or_None)` tuples and passes through to `compute_aggregation`.

Multi-level group-by is requested simply by passing multiple specs. The result is a flat list with one row per composite-key bucket.

## 7 · Date granularity — TIME and NUMBER tracks

Two parallel tracks, mirroring Odoo's `READ_GROUP_TIME_GRANULARITY` and `READ_GROUP_NUMBER_GRANULARITY` (`odoo/models.py:217`):

```python
class TimeGranularity(StrEnum):
    """date_trunc(granularity, ts) — returns DateTime."""
    YEAR    = "year"
    QUARTER = "quarter"
    MONTH   = "month"
    WEEK    = "week"
    DAY     = "day"
    HOUR    = "hour"
    MINUTE  = "minute"
    SECOND  = "second"

class NumberGranularity(StrEnum):
    """date_part(part, ts)::int — returns Int."""
    YEAR_NUMBER     = "year_number"
    QUARTER_NUMBER  = "quarter_number"
    MONTH_NUMBER    = "month_number"
    ISO_WEEK_NUMBER = "iso_week_number"
    DAY_OF_YEAR     = "day_of_year"
    DAY_OF_MONTH    = "day_of_month"
    DAY_OF_WEEK     = "day_of_week"
    HOUR_NUMBER     = "hour_number"
    MINUTE_NUMBER   = "minute_number"
    SECOND_NUMBER   = "second_number"
```

The NUMBER track is the canonical way to ask "orders per day-of-week" or "signups per hour-of-day across all dates" (cohort/heatmap analytics). Odoo learned this in 17.3 (PR #159528) and we ship it day-1.

### Timezone correctness

Critical detail Odoo got right (`odoo/models.py:2685–2727`) and we copy verbatim. From the implementation:

```python
# Two-stage tz wrap, then truncate
sql_expr = SQL("timezone(%s, timezone('UTC', %s))", tz, sql_expr)
sql_expr = SQL("date_trunc(%s, %s::timestamp)", granularity, sql_expr)
```

Order matters: cast UTC-stored timestamp → user tz → THEN `date_trunc`. Otherwise "May 1 in Tokyo" buckets as April 30 UTC. The `tz` argument to `compute_aggregation` is an IANA timezone name; defaults to `settings.TIME_ZONE` when omitted. SQLite is best-effort (its `datetime()` doesn't have full IANA tz support) — for production analytics, use Postgres.

## 8 · HAVING

Generated `<Model>Having` input — flat fields, one per `(measure, comparison)` pair:

```graphql
input OrderHaving {
  countGt:        Int
  countLt:        Int
  countEq:        Int
  countDistinctGt: Int
  # ...
  sumTotalGt:     Decimal
  sumTotalLt:     Decimal
  sumTotalEq:     Decimal
  avgTotalGt:     Float
  # ... and so on for min/max/stddev/variance
}
```

Comparisons whitelist matches Odoo: `Gt, Lt, Lte, Gte, Eq, Neq, In, NotIn`. Server translates to `qs.filter(...)` *after* `.annotate(...)`, which compiles to SQL `HAVING`.

References to unknown aliases raise `HavingFieldNotAllowed` — the input type enforces this at parse time, and `compute_aggregation` revalidates defensively.

## 9 · Ordering on aggregates

`<Model>GroupOrder` accepts `field: String!` resolving in priority order against:

1. Aggregate aliases (`__count` for COUNT(*), `count_distinct`, `<op>_<field>` for sum/avg/min/max/stddev/variance).
2. Group-by field paths — including bucketed forms like `created_at_month` or `day_of_week`.
3. Plain field paths declared in the order-by allowlist.

Unknown values raise `OrderFieldNotAllowed` at validation time. **Fail-loud is the contract** — Odoo's pre-17 `read_group` silently dropped unknown order terms and was a recurring source of "why isn't this query ordered?" bug reports.

```python
# Examples of accepted forms:
"total:sum desc"                 # aggregate alias (Odoo flavor)
"sum_total desc"                 # aggregate alias (snake_case)
"-sum_total"                     # aggregate alias (Django flavor)
"count desc"                     # COUNT(*)
"customer_id"                    # group-by field
"created_at_month desc"          # bucketed group-by
```

The parser is in `ordering.py::parse_aggregate_order`.

### 9.1 · Comodel-derived tiebreakers (opt-in)

When the user orders by a foreign-key group-by alias (`customer_id`), the
default behaviour is to ORDER BY that integer column directly — rows come out
in raw FK-ID order, which surprises consumers who expect alphabetical customer
listings. Setting `respect_comodel_ordering=True` on `compute_aggregation` (or
on `AggregateBuilder`) walks to the comodel and appends its `Meta.ordering` as
additional ORDER BY tiebreakers. Mirrors Odoo `_order_field_to_sql`
(`odoo/models.py:2253`).

```python
class Customer(models.Model):
    name = models.CharField(max_length=100)
    class Meta:
        ordering = ["name"]

# order_by [("customer_id", "asc", None)] with the flag set emits:
#   ORDER BY orders.customer_id ASC, customers.name ASC
```

The flag is **opt-in** to keep determinism for the existing test corpus and
to avoid surprising consumers with implicit JOINs they didn't ask for. The
appended terms are always-valid by construction (they come from the comodel's
own `Meta`) and bypass the user-facing fail-loud check — Critical Rule 6 is
unaffected because the strict allowlist still applies to the user's primary
order term.

When the comodel has no `Meta.ordering`, the flag is a no-op. Non-string
ordering entries (`F` expressions, `OrderBy` objects) are skipped — the
intrinsic ordering is a best-effort tiebreaker, never load-bearing.

## 10 · The backend primitive — `compute_aggregation`

```python
from strawberry_django_aggregates import (
    AggregateOp, TimeGranularity, NumberGranularity, compute_aggregation,
)

rows = compute_aggregation(
    qs,                                    # pre-scoped queryset
    group_by=[
        ("created_at", TimeGranularity.MONTH),
        ("customer", None),
    ],
    aggregates=[
        (AggregateOp.COUNT, None),
        (AggregateOp.SUM, "total"),
        (AggregateOp.AVG, "total"),
    ],
    having={"sum_total__gt": Decimal("1000")},
    order_by=[("sum_total", "desc", None)],
    offset=0,
    limit=50,
    tz="Asia/Tokyo",
)

# rows = [
#   {"created_at_month": date(2026,5,1), "customer_id": 5, "count": 42, "sum_total": Decimal("8192.00"), "avg_total": Decimal("195.04")},
#   ...
# ]
```

Returns a flat list of dicts. Group-by keys live alongside aggregate aliases on the same row. Multi-level group-by produces multiple rows; the resolver wraps each row in `<Model>Grouped`.

`compute_aggregation` is permission-naive — the queryset must already be scoped by the caller. This is the same separation of concerns the rest of the Django ecosystem uses (managers/querysets do the scoping, query libraries compose).

### Errors

| Error | Trigger |
|---|---|
| `OperatorNotSupportedError` | Postgres-only operator on a non-Postgres connection |
| `AggregationAcrossRelationError` | `field_path` traverses a one-to-many or m2m relation |
| `OrderFieldNotAllowed` | `order_by` references an unknown alias |
| `GroupByFieldNotAllowed` | `group_by` references a field not in the allowlist |
| `HavingFieldNotAllowed` | `having` references an unknown alias |
| `GranularityNotApplicable` | `granularity` is set on a non-date / non-datetime field |

## 11 · Why no auto-traversal for o2m / m2m measures

`SUM(order.items__price)` would silently row-multiply (one row per item, summed multiple times) and corrupt every other measure in the same query. **Refuse the request.** Mirrors Odoo's design choice. The error message points to the explicit alternative:

```
AggregationAcrossRelationError:
  Cannot aggregate `items__price` from `Order` — would cause row multiplication.
  Use a top-level `itemsAggregate(filter: { order: { id: $orderId } })` query
  on the child model instead.
```

`array_agg` is the explicit escape hatch for "give me all child IDs per parent group" — but it returns `[ID!]`, not auto-hydrated objects. Clients refetch by ID and the optimizer batches the lookup.

## 12 · Determinism

Type generation produces the same SDL for the same inputs. Rules:

- Iterate field allowlists in declaration order (preserves Python dict insertion order).
- Iterate operator overrides in `sorted()` key order.
- Emit operator nested types (`<Model>SumFields` etc.) in canonical operator order: `count, count_distinct, sum, avg, min, max, stddev, variance, bool_and, bool_or, array_agg, string_agg`.
- Emit HAVING input fields by `(measure, comparison)` in canonical comparison order: `Gt, Lt, Lte, Gte, Eq, Neq, In, NotIn`.
- No timestamps, no PRNG, no `datetime.now()`, no `uuid4()`, no insertion-order-sensitive iteration.

A determinism test (`tests/test_determinism.py`) generates types twice and compares `schema.print()` byte-for-byte.

## 13 · Footguns and how we avoid them

Lessons from Odoo's `_read_group` history. Each item is encoded in implementation policy.

| # | Source footgun | Origin | Mitigation |
|---|---|---|---|
| 1 | `lazy=True` on `read_group` confused callers for years; removed in 17 | Odoo PR #110737 | Never ship a "lazy" mode. One query, all groupBy keys at once. Drill-down is opt-in via repeated calls. |
| 2 | Auto-traversal of o2m/m2m for aggregation silently row-multiplies | Odoo design choice | Refuse with `AggregationAcrossRelationError`; document the child-model alternative. `array_agg` is the escape hatch. |
| 3 | Order on aggregates was broken pre-17 (silently dropped unknown terms) | Odoo PR #110737 changelog | Fail-loud on unknown order terms. `OrderFieldNotAllowed`. |
| 4 | Pivot-view N+1 RPCs per drill-down level | Odoo `pivot_model.js` | Default to one query, all keys at once (graph-view pattern). Drill-down is the caller's choice. |
| 5 | `recordset` aggregate auto-hydrates browse records — serialization landmine | Odoo `_read_group` `recordset` op | `array_agg` returns `[ID!]` only. Clients refetch by ID; optimizer batches. |
| 6 | Timezone bucketing wrong (truncating UTC instead of user_tz) | Pre-`models.py:2685` era | UTC → user_tz wrap → THEN `date_trunc`. Verbatim from Odoo's modern implementation. |
| 7 | `_read_group` permission bypass via raises on inaccessible m2o targets | Odoo issue #21842 | Library is permission-naive — caller must pre-scope the queryset. Documented prominently. |
| 8 | `sudo` overload (`sudo(user)`) deprecated for confusion | Modern `BaseModel.sudo` | Library has no escalation primitive. Permission concerns belong in the caller. |
| 9 | Operator string-eval into SQL (classic injection vector) | n/a | Strict whitelist (`AggregateOp` enum). Operators map 1:1 to safe SQL fragments. |

## 14 · Implementation layout

```
strawberry_django_aggregates/
├── __init__.py            # public API re-exports
├── builder.py             # AggregateBuilder + BuiltAggregates
├── types.py               # make_aggregate_type / _grouped_type / _having_input / _group_by_spec
├── operators.py           # AggregateOp enum + per-field-type defaults
├── granularity.py         # TimeGranularity / NumberGranularity + part mapping
├── compiler.py            # compute_aggregation backend primitive
├── ordering.py            # parse_aggregate_order (fail-loud)
└── errors.py              # exception hierarchy

tests/
├── conftest.py            # in-memory SQLite setup
├── test_aggregate_correctness.py   # vs Postgres reference outputs
├── test_groupby_timezones.py       # Tokyo bucketing across DST
├── test_having_aliases.py          # __count, sum_total
├── test_order_on_aggregates.py     # fail-loud on unknown
├── test_determinism.py             # generate × 2 → byte-diff SDL
├── test_no_relation_traversal.py   # AggregationAcrossRelationError raises
└── test_postgres_only_ops.py       # OperatorNotSupportedError on SQLite
```

## 15 · What this spec doesn't decide

- **Cursor pagination on grouped results.** Offset only for v1.
- **Aggregate-of-relation as a parent field** (e.g. `order.itemsAggregate`). Would need a `Subquery` emission strategy. Documented alternative (top-level child query) covers most cases. Possible v2.
- **Strawberry Federation v2 directives** on emitted types (`@key`, `@external`, etc.). The types are plain Strawberry types and play with Federation, but there's no first-class wiring yet.
- **Window functions** (`ROW_NUMBER`, `RANK`, `LAG`, etc.). Adjacent feature space; would need a new `<Model>Windowed` shape. Out of scope.
- **Streaming / chunked group-by** for cardinality > 100k group buckets. Memory pressure is real; v2 might add a `chunk_size` parameter to `compute_aggregation` and a streaming resolver.

## 16 · Versioning

The `AggregateOp` enum and the `compute_aggregation` signature are part of the SemVer contract — breaking changes bump major. The Strawberry types it emits inherit Strawberry's evolution semantics (deprecate fields, never break them in a minor release). The library tracks `strawberry-graphql-django` minor versions; major bumps there may force a major bump here.

---

**Implementation phasing.** Phase 1: `compute_aggregation` + tests (~1 week). Phase 2: type generators (`make_*`) + tests (~1 week). Phase 3: `AggregateBuilder` convenience + integration tests against a real Strawberry schema (~3 days). Total ~2.5 weeks for a clean v0.1.0.
