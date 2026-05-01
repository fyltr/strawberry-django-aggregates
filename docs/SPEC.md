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
- Cursor pagination on grouped results was deferred in early drafts; v1.0 ships an opt-in Relay-style connection alongside the offset shape. See § 4 cursor-pagination.
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
  every:           OrderBoolAndFields   # SQL-standard alias for boolAnd
  some:            OrderBoolOrFields    # SQL-standard alias for boolOr
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
  customerId:       ID
  status:           OrderStatusEnum
  createdAt:        DateTime      # bucketed if granularity arg used
  createdAtMonth:   DateTime      # populated when groupBy uses MONTH granularity
  createdAtMonthRange: BucketRange  # half-open [from, to) sibling — TIME granularity only
  dayOfWeek:        Int           # populated when bucketed via NumberGranularity
}

type BucketRange {
  from: DateTime!                 # inclusive bucket start
  to:   DateTime!                 # exclusive bucket end (next bucket's start)
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

### 4.1 · Cursor pagination on grouped results

The default offset paginator (`<Model>GroupedResult { results, pageInfo, totalCount }`) is sufficient for analytics dashboards that fit on a single page. Long-tail group cardinality (thousands of customers, daily-bucketed years of data) wants Relay-style cursor pagination: no `count(*)` on every page, stable cursors that survive new-row insertions, infinite-scroll wire shape JS clients already speak.

`AggregateBuilder` accepts an opt-in `pagination_style` argument:

| Value | Field name | Return type |
| --- | --- | --- |
| `"offset"` (default) | `<model>GroupBy` | `<Model>GroupedResult` |
| `"cursor"` | `<model>GroupBy` | `<Model>GroupedConnection` |
| `"both"` | `<model>GroupBy` (offset) and `<model>GroupByConnection` (cursor) | both |

The default is `"offset"` so existing consumers see byte-identical SDL — Critical Rule 2 (determinism) holds for unchanged inputs.

```graphql
type OrderGroupedConnection {
  edges:      [OrderGroupedEdge!]!
  pageInfo:   PageInfo!
  totalCount: Int!
}

type OrderGroupedEdge {
  cursor: String!
  node:   OrderGrouped!
}

type PageInfo {
  hasNextPage:     Boolean!
  hasPreviousPage: Boolean!
  startCursor:     String
  endCursor:       String
}
```

`PageInfo` is the standard `strawberry.relay.PageInfo` re-export — no library-specific duplicate. Same shape as the rest of the consumer's Relay surface.

**Connection arguments.** `first: Int`, `after: String`, `last: Int`, `before: String` (Relay convention; `first` and `last` mutually exclusive, both non-negative). Plus `groupBy`, `having`, `weekStart`, and `filter` (when wired) for parity with the offset field. Out of scope on the cursor field for v1.0: `orderBy` (would break keyset semantics — the canonical group-alias ordering is forced for stability) and `fill` (the dense spine has no obvious cursor encoding for filler buckets — use the offset variant).

**Cursor format.** Opaque base64-URL-safe encoding of a JSON list of group-by alias values in canonical order — the same order the caller supplies in `group_by` (mirrors `compiler.group_by_alias`). Datetimes / dates / times serialize as ISO-8601 strings tagged with one-letter type markers (`["dt", "2026-05-01T00:00:00+00:00"]`); `Decimal` survives via a `["dec", "100.00"]` tag. Pure stdlib (`base64`, `json`, `datetime`, `decimal`) — no Django, no Strawberry. The encoder is deterministic: same input ⇒ byte-identical output.

```python
from strawberry_django_aggregates import (
    encode_group_cursor, decode_group_cursor,
)

cursor = encode_group_cursor([42, "paid", datetime(2026, 5, 1, tzinfo=UTC)])
# → "WyA0Mi..." (opaque)
decode_group_cursor(cursor)
# → [42, "paid", datetime(2026, 5, 1, tzinfo=UTC)]
```

**Keyset semantics.** Forward pagination (`after`) translates to `(a, b, c) > (cursor_a, cursor_b, cursor_c)` over the canonical group aliases; backward pagination (`before`) is the symmetric `< (...)`. Django ORM has no row-constructor support, so the tuple comparison is unrolled into the standard disjunction-of-conjunctions:

```
Q(a__gt=av) | (Q(a=av) & Q(b__gt=bv)) | (Q(a=av) & Q(b=bv) & Q(c__gt=cv))
```

This works uniformly for single-level (`group_by=[("customer", None)]`) and multi-level (`group_by=[("customer", None), ("status", None), ("created_at", MONTH)]`) — v1.0 supports both. The keyset filter applies to the annotated/values queryset, so date-bucketed columns compare against their truncated bucket boundary (`created_at_month >= 2026-06-01`), not the underlying timestamp. NULL handling is strict — SQL three-valued logic excludes any row where a group alias is NULL from the comparison; this is documented behaviour, not a footgun.

**`hasNextPage` detection.** The resolver fetches `page_size + 1` rows and trims the trailing extra before encoding edges; the extra row's presence drives `hasNextPage` (forward) or `hasPreviousPage` (backward). The other side of the page-info pair is set from the cursor presence: `after` set ⇒ `hasPreviousPage=true` (forward pagination), `before` set ⇒ `hasNextPage=true` (backward pagination).

**`totalCount`.** Same DB-side `COUNT(DISTINCT ...)` path as the offset field's `totalCount` — no Python-side row materialization. The count reflects the post-`having` group cardinality and ignores `first`/`last`/`after`/`before` (the page is a window over the total).

**Determinism.** `pagination_style="offset"` (default) emits byte-identical SDL to pre-Stream-11 builds — the determinism test passes unchanged. `pagination_style="cursor"` and `pagination_style="both"` produce stable SDL across two generations of the same builder (Critical Rule 2). The cursor encoding is also stable across Python versions and operating systems (no timestamps, no PRNG, no insertion-order dependence).

## 5 · Aggregate operator vocabulary

```
AggregateOp = StrEnum:
    COUNT                = "count"
    COUNT_DISTINCT       = "count_distinct"
    COUNT_DISTINCT_TUPLE = "count_distinct_tuple"  # multi-column distinct
    SUM                  = "sum"
    AVG                  = "avg"
    MIN                  = "min"
    MAX                  = "max"
    STDDEV               = "stddev"           # Postgres only — sample stddev
    VARIANCE             = "variance"         # Postgres only — sample variance
    STDDEV_POP           = "stddev_pop"       # Postgres only — population stddev
    VAR_POP              = "var_pop"          # Postgres only — population variance
    PERCENTILE_CONT      = "percentile_cont"  # Postgres only — interpolated percentile
    PERCENTILE_DISC      = "percentile_disc"  # Postgres only — discrete percentile
    MODE                 = "mode"             # Postgres only — most-frequent value
    BOOL_AND             = "bool_and"
    BOOL_OR              = "bool_or"
    ARRAY_AGG            = "array_agg"        # Postgres only
    STRING_AGG           = "string_agg"       # Postgres only
```

Direct mapping to SQL — no operator outside this enum reaches the database:

| Operator | SQL | Database support | Result type |
|---|---|---|---|
| `count` | `COUNT(*)` | All | `Int!` |
| `count_distinct` | `COUNT(DISTINCT col)` | All | `Int!` |
| `count_distinct_tuple` | `COUNT(DISTINCT (a, b, c))` (PG); `COUNT(DISTINCT COALESCE(a, '\0') \|\| char(1) \|\| ...)` (SQLite emulation) | All | `Int!` |
| `sum` | `SUM(col)` | All | `BigInt` for integer field types (`IntegerField`, `SmallIntegerField`, `PositiveIntegerField`, `PositiveSmallIntegerField`, `BigIntegerField`); `Decimal` for `DecimalField`; `Float` for `FloatField`; `Duration` for `DurationField` |
| `avg` | `AVG(col)` | All | `Float` (or `Decimal` for `DecimalField`) |
| `min` / `max` | `MIN(col)` / `MAX(col)` | All | type of `col` |
| `stddev` | `STDDEV_SAMP(col)` | **Postgres only** | `Float` |
| `variance` | `VAR_SAMP(col)` | **Postgres only** | `Float` |
| `stddev_pop` | `STDDEV_POP(col)` | **Postgres only** | `Float` |
| `var_pop` | `VAR_POP(col)` | **Postgres only** | `Float` |
| `percentile_cont` | `PERCENTILE_CONT(<fraction>) WITHIN GROUP (ORDER BY col)` | **Postgres only** | `Float` |
| `percentile_disc` | `PERCENTILE_DISC(<fraction>) WITHIN GROUP (ORDER BY col)` | **Postgres only** | `Float` (cast in v1.0; column type in v1.x) |
| `mode` | `MODE() WITHIN GROUP (ORDER BY col)` | **Postgres only** | type of `col` |
| `bool_and` | `BOOL_AND(col)` | Postgres native; SQLite emulated via `MIN(col::int)::bool` | `Boolean` |
| `bool_or` | `BOOL_OR(col)` | Postgres native; SQLite emulated via `MAX(col::int)::bool` | `Boolean` |
| `array_agg` | `ARRAY_AGG(col ORDER BY <pk>)` | **Postgres only** | `[ID!]` or `[String!]` etc. |
| `string_agg` | `STRING_AGG(col, ',' ORDER BY <pk>)` | **Postgres only** | `String` |

**`BigInt` for integer-field SUM.** Postgres widens `SUM(int_col)` to 8-byte `bigint`, which overflows the 32-bit GraphQL `Int` scalar (max 2³¹ − 1 ≈ 2.1 billion). Even `bigint`-fitting values escape the JavaScript `Number` safe range past 2⁵³ ≈ 9 × 10¹⁵. We emit a custom `BigInt` scalar that serializes as a JSON string on the wire so JS/TS clients survive end-to-end without precision loss; clients re-parse with `BigInt()` (TS) or `int()` (Python). The same scalar applies whether the underlying field is `IntegerField`, `SmallIntegerField`, `PositiveIntegerField`, `PositiveSmallIntegerField`, or `BigIntegerField`.

**`array_agg` returns IDs only — never auto-hydrated.** Odoo's `recordset` operator browse-resolves to live records and is a serialization landmine in GraphQL (think: 10000 element arrays expanded into nested objects on every group). We refuse the auto-hydration. Clients refetch by ID and Strawberry's `DjangoOptimizerExtension` batches the lookup.

**SQL-standard `every` / `some` aliases.** SQL:1999 names the boolean
aggregates `every(col)` and `some(col)` (with `bool_and` / `bool_or` as
PostgreSQL aliases for the same functions). For wire-level
familiarity, `<Model>Aggregate` and `<Model>Grouped` expose `every:
<Model>BoolAndFields` and `some: <Model>BoolOrFields` as siblings of
`boolAnd` / `boolOr`. The aliases are pure wire surface — no new
`AggregateOp` member, no new SQL emission, no new nested type. The
selection walker translates `every` → `BOOL_AND` and `some` →
`BOOL_OR` before reaching the compiler; the result-shaping code
populates both the canonical and alias dataclass attributes from the
same value. Clients may select either name (or both — they return
identical payloads). The alias fields are absent when the model
allowlist excludes BOOL_AND / BOOL_OR (no boolean fields in scope).

### Per-field-type defaults

Field types map to default operator allowlists. Consumers can narrow further via the `operators` arg:

| Django field type | Default operators |
|---|---|
| `IntegerField`, `BigIntegerField`, `FloatField`, `DecimalField`, `DurationField` | `sum, avg, min, max, stddev, variance, stddev_pop, var_pop, percentile_cont, percentile_disc, mode` |
| `DateField`, `DateTimeField`, `TimeField` | `min, max, percentile_disc, mode` |
| `BooleanField` | `bool_and, bool_or` |
| `CharField`, `TextField`, `EmailField`, `URLField`, `SlugField` | `min, max, mode, array_agg, string_agg` |
| `UUIDField`, `AutoField`, `BigAutoField` | `array_agg` (by ID) |
| `ForeignKey`, `OneToOneField` | `array_agg` (by FK ID) |
| `ManyToManyField`, reverse FK | **none** — would row-multiply |

`count` and `count_distinct` are always present at the type level (operate on rows, not on a measure).

### NULL semantics

Standard SQL three-valued-logic applies. Every emitted measure field on
`<Model>SumFields`, `<Model>AvgFields`, `<Model>MinFields`,
`<Model>MaxFields`, `<Model>StddevFields`, `<Model>VarianceFields`,
`<Model>BoolAndFields`, and `<Model>BoolOrFields` is therefore nullable
(`T | None`) — direct reflection of the SQL outcomes below.

- **`count`** — counts rows in the group. Equivalent to `COUNT(*)`.
  Implementation uses `COUNT(<pk>)`, which is identical because primary
  keys are non-null by definition. Never returns NULL; an empty group is
  simply absent from the result set (no row emitted with `count = 0`
  unless empty-bucket filling is enabled — see § 7.1 once it lands).
- **`count_distinct(field)`** — `COUNT(DISTINCT col)`, counting **non-null
  distinct values**. NULL is not a distinct value and is excluded.
  Returns `0` for an all-NULL group, never NULL itself. Multi-column
  `count_distinct(fields:)` follows the same rule under standard SQL on
  PostgreSQL — any tuple containing a NULL is excluded — but the SQLite
  emulation diverges. See § 5.2 for the full SQLite caveat.
- **`sum`, `avg`, `min`, `max`, `stddev`, `variance`, `stddev_pop`,
  `var_pop`** — skip NULL inputs. An empty group, or a group whose
  measure column is entirely NULL, returns NULL. **`sum` does NOT default
  to 0** for an all-NULL group; callers wanting `0` must coalesce
  client-side or pre-aggregate with `Coalesce(F('total'), 0)` before
  passing the queryset in.
- **`bool_and` / `bool_or`** — skip NULL inputs. An all-NULL group
  returns NULL, not `True`/`False`. Mixed-NULL groups behave per SQL: a
  single `FALSE` short-circuits `bool_and` to `FALSE` regardless of
  NULLs; a single `TRUE` short-circuits `bool_or` to `TRUE`.
- **`array_agg` / `string_agg`** — **include NULLs by default in
  PostgreSQL** (`ARRAY_AGG(col)` produces an array containing NULL
  entries; `STRING_AGG(col, ',')` skips NULLs but the surrounding rows
  are unaffected). This is a known footgun — callers wanting non-null
  values only should pre-filter the queryset
  (`qs.exclude(<field>__isnull=True)`). The library does not currently
  emit `FILTER (WHERE col IS NOT NULL)` clauses; this is documented
  behavior, not a bug.

### 5.1 · Ordered-set aggregates — `percentile_cont`, `percentile_disc`, `mode`

PostgreSQL's ordered-set aggregates take a per-call literal that does
not fit the `(operator, field)` 2-tuple shape of the rest of the
catalog. We split the wire surface in two:

**`mode`** is a regular nested-type op. `<Model>ModeFields` carries one
field per allowlisted column whose type permits a most-frequent value
(numeric / string / date). The result type matches the column type —
same shape as `min` / `max`.

**`percentile_cont` / `percentile_disc`** are **method-style fields** on
`<Model>Aggregate`, mirroring the `count_distinct(field:)` pattern:

```graphql
type OrderAggregate {
  count:           Int!
  countDistinct(field: OrderCountableField!): Int!
  percentileCont(
    field: OrderPercentileField!
    fraction: Float!
  ): Float
  percentileDisc(
    field: OrderPercentileField!
    fraction: Float!
  ): Float
  # ... regular nested-type fields below
}
```

`fraction` must be in `[0, 1]`; out-of-range values raise `ValueError`
at resolver entry, before any SQL fires.

**`percentileDisc` returns `Float` in v1.0**, even when the underlying
column is `Decimal`, `Date`, or any other type. The discrete-percentile
result is cast to `Float` to keep the wire surface uniform with
`percentileCont`. Callers who need full type fidelity (e.g. a
date-column percentile returning `DateTime`) should compute in
application code or wait for v1.x — the type-faithful resolution
requires per-field-type method emission and is out of v1.0 scope.

**Alias scheme.** Multiple percentile calls can coexist in one query
because the SQL alias encodes the fraction:

| Call | Alias |
|---|---|
| `percentile_cont(total, fraction=0.5)` | `percentile_cont_total_50` |
| `percentile_cont(total, fraction=0.95)` | `percentile_cont_total_95` |
| `percentile_disc(total, fraction=0.999)` | `percentile_disc_total_999` |
| `percentile_cont(total, fraction=0.05)` | `percentile_cont_total_5` |
| `mode(total)` | `mode_total` |

The fraction is rendered as `int(round(fraction * 1000))` with a
trailing-zero collapse so common P50 / P95 / P99 read naturally
(`50` / `95` / `99`) while finer-grained fractions like P99.9 keep
their precision (`999`).

**Backend channel.** `compute_aggregation` accepts a parallel-dict
`op_args` keyed by the bare `<op>_<field>` alias (no fraction suffix —
that suffix is *derived* from the fraction itself):

```python
compute_aggregation(
    qs,
    aggregates=[
        (AggregateOp.PERCENTILE_CONT, "total"),
        (AggregateOp.PERCENTILE_DISC, "total"),
    ],
    op_args={
        "percentile_cont_total": {"fraction": 0.5},
        "percentile_disc_total": {"fraction": 0.95},
    },
)
```

`op_args` is the only escape hatch for per-call kwargs that don't fit
the 2-tuple shape; refactoring `aggregates` to a 3-tuple was
considered and rejected as too invasive for v1.0.

**HAVING and ordering** on percentile measures are deferred. The
fraction-suffixed alias does not roundtrip cleanly through the static
`<Model>Having` input shape, and ordering by a percentile column would
require percentile-aware translation of the order term. Method-style
fields cover the common usage (P50 / P95 / P99 at top level). Filter
or order on the underlying queryset directly if you need either.

**`mode` works in HAVING and ordering** like any other regular nested
op — its alias is the simple `mode_<field>` form.

### 5.2 · Multi-column `count_distinct` (Hasura-style)

The `<Model>Aggregate` type exposes a single `countDistinct` field that
accepts EITHER a single `field: <Enum>` argument (single-column distinct
— emits `COUNT(DISTINCT col)`) OR a `fields: [<Enum>!]` argument
(multi-column tuple distinct — emits `COUNT(DISTINCT (a, b, c))`):

```graphql
type OrderAggregate {
  count: Int!
  countDistinct(
    field: OrderCountableField,
    fields: [OrderCountableField!],
  ): Int!
  # ...
}
```

**Mutual exclusion** — exactly one of `field` / `fields` must be set.
Both-set or neither-set raises a `ValueError` at resolver entry, before
any SQL fires. This is enforced in two places: the resolver in
`types.py` raises the user-facing error; the wire-walker in
`builder.py` skips the request to avoid building a contradictory SQL
annotation.

**Backend operator.** Internally the multi-column shape dispatches via
the new `AggregateOp.COUNT_DISTINCT_TUPLE` enum member (canonical
position immediately after `COUNT_DISTINCT`). The compiler accepts a
`__`-joined field-path (e.g. `"customer__status"`) and validates each
segment against the model. The wire layer canonicalizes the input
`fields` list into a sorted-tuple key so `[CUSTOMER, STATUS]` and
`[STATUS, CUSTOMER]` produce the same SQL alias and result lookup.

**SQL alias scheme.** `(COUNT_DISTINCT_TUPLE, "customer__status")` →
alias `count_distinct_tuple_customer__status`. The double-underscore
separator is preserved verbatim in the alias so the alias maps 1:1 to
the canonical sorted-tuple of segment names.

**SQLite emulation caveat.** PostgreSQL renders the row constructor
natively: `COUNT(DISTINCT (a, b, c))`, with standard-SQL semantics —
**any tuple containing a NULL is excluded from the distinct set**.
SQLite has no row constructor in DISTINCT contexts, so the library
emulates the operator via NULL-sentinel-coalesced concatenation:

```sql
COUNT(DISTINCT
  COALESCE(CAST(a AS TEXT), <\x00>) || <\x01> ||
  COALESCE(CAST(b AS TEXT), <\x00>) || <\x01> ||
  COALESCE(CAST(c AS TEXT), <\x00>)
)
```

(Sentinel = `\x00` / NUL byte; separator = `\x01` / SOH byte.)

The emulation **diverges from PG when any tuple column is NULL**:

- **PostgreSQL** excludes NULL-containing tuples from the distinct set.
  `COUNT(DISTINCT (a, b))` over `[(1, NULL), (2, NULL), (1, 2)]` is `1`.
- **SQLite emulation** treats NULLs as a sentinel-coded value, so
  NULL-containing tuples DO contribute to the distinct count, just as
  one shared "NULL-coded" tuple per unique sentinel-pattern.
  `COUNT(DISTINCT ...)` over the same data is `3`.

This is deliberately undocumented as "fixed" — papering over the
SQLite divergence with a `FILTER (WHERE ... IS NOT NULL)` clause would
silently change PG results too. Callers wanting NULL exclusion on
SQLite should pre-filter via `qs.exclude(<col>__isnull=True)`.

**HAVING and ordering** on `count_distinct_tuple` are not exposed in
v1.0 — the alias shape (`count_distinct_tuple_customer__status`) does
not roundtrip cleanly through the static `<Model>Having` input shape
the same way `count_distinct_<field>` does. Both can be added in v1.x
if a user need surfaces.

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

### Bucket range siblings — `<field>_<granularity>_range`

For each TIME-granularity bucket in `group_by`, the emitted `<Model>GroupKey`
carries a sibling `<field>_<granularity>_range: BucketRange { from: DateTime!, to: DateTime! }`
half-open interval. `from` is inclusive, `to` is exclusive (the start of
the next bucket). The interval is computed in the resolver from the
bucketed value plus the granularity — no extra SQL — and shares the
bucketed value's `tzinfo`, so a `tz="Asia/Tokyo"` query returns
Tokyo-local boundaries (e.g. `2026-05-01 00:00+09:00` →
`2026-06-01 00:00+09:00`).

NUMBER granularity (`DAY_OF_WEEK`, `MONTH_NUMBER`, etc.) is degenerate
— there is no contiguous range for "all Tuesdays" — and gets no range
sibling.

#### `bucket_range(value, granularity, week_start=1)` — backend primitive

The same arithmetic is exposed as a public helper for callers outside
the GraphQL resolver:

```python
from strawberry_django_aggregates import TimeGranularity, bucket_range

bucket_range(datetime(2026, 5, 1, tzinfo=UTC), TimeGranularity.MONTH)
# → (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC))
```

Pure stdlib, no Django / Strawberry imports — callable from any Python
context (DRF view, Celery task, MCP tool, plain `manage.py shell`).
Lives in `compiler.py` to honour CLAUDE.md Critical Rule 9 (no GraphQL
coupling on the framework-agnostic primitives).

The optional `week_start` parameter (1=Mon…7=Sun, ISO default) is
accepted for symmetry with `compute_aggregation` and is validated
fail-loud. The `value` passed in is already the truncated bucket
boundary in the user's chosen first-day-of-week (the SQL truncation
uses the same `week_start`), so the returned interval is always +7
days for `WEEK` regardless of the parameter — the helper just needs
the value for validation feedback.

### 7.1 · Locale-aware week start

`compute_aggregation(..., week_start=N)` selects the first day of the
week for `TimeGranularity.WEEK` bucketing and `NumberGranularity.DAY_OF_WEEK`
extraction. `1=Monday` (ISO 8601 default) … `7=Sunday`. Mirrors Odoo
`odoo/models.py:2142-2168` — different countries pin the first day of
the week differently (US/Canada/Japan: Sunday; most of EU: Monday;
Iran/Saudi Arabia: Saturday) and analytics queries that group by week
need to honour the consumer's choice without forcing them to compute
shifted bucket boundaries client-side.

**WEEK bucketing.** PostgreSQL's `date_trunc('week', ts)` and Django's
`Trunc('week')` both return the Monday-start week. To shift to a
different first day, the library applies an offset of
`offset = (8 - week_start) % 7` days:

```sql
-- week_start=7 (Sunday-start), offset=1
date_trunc('week', col + INTERVAL '1 day') - INTERVAL '1 day'
```

When `week_start == 1` (offset 0) the shift is skipped entirely — the
emitted SQL is identical to the pre-`week_start` behaviour, so the
determinism contract for existing callers is preserved. SQLite uses
the same arithmetic via `ExpressionWrapper(F(col) + timedelta(...))`.

**DAY_OF_WEEK rotation.** ISO `EXTRACT(ISODOW FROM col)` returns
1=Monday..7=Sunday. With `week_start=N` the numeric encoding rotates
so the user's first day is `1`:

```python
((iso_dow - week_start) % 7) + 1
```

When `week_start == 1` the rotation is a no-op (skipped, same SQL as
before). For `week_start=7` (Sunday-first), Sunday returns `1`, Monday
`2`, …, Saturday `7`.

**GraphQL surface.** The grouped field accepts `weekStart: Int` (default
omitted; compiler default of 1 / ISO Monday applies). The argument is
emitted unconditionally on every grouped resolver — no flag — so the
SDL is stable. Out-of-range values (`< 1`, `> 7`, non-int) raise
`ValueError` at resolver entry, before any SQL fires.

```graphql
ordersGroupBy(
  groupBy: [{ field: CREATED_AT, granularity: WEEK }],
  weekStart: 7
) { results { key { createdAtWeek } count } }
```

**`bucket_range` interaction.** With `week_start=N` the SQL truncation
already returns the user's first-day-of-week as the bucket boundary,
so the resolver passes that same value plus `week_start` to
`bucket_range` for symmetry — the helper still adds 7 days, but the
parameter is validated for fail-loud feedback when callers use the
helper directly.

### 7.2 · Empty-bucket filling

`compute_aggregation(..., fill=True)` returns a *dense* bucket spine —
every contiguous bucket between the data's min and max appears in the
output, with `count: 0` and all measures `None` for buckets that had
no underlying rows. Mirrors the Hasura `fill` argument and the Apache
Superset "show empty buckets" toggle; closes a long-standing reporting
ergonomics gap (sparse line charts that mis-render gaps as zero
slopes, etc.).

```python
compute_aggregation(
    Order.objects.all(),
    group_by=[("created_at", TimeGranularity.MONTH)],
    aggregates=[(AggregateOp.COUNT, None), (AggregateOp.SUM, "total")],
    fill=True,
)
# → [
#     {"created_at_month": ..., "count": 5, "sum_total": Decimal(...)},
#     {"created_at_month": ..., "count": 0, "sum_total": None},  # filled
#     ...
# ]
```

**v1.0 restriction — single TIME-granularity bucket.** The `group_by`
spec MUST contain *exactly one* entry whose granularity is a
`TimeGranularity` member. Multi-level group_by + fill (e.g. fill the
month spine independently per `customer_id`) is a v1.x feature; v1.0
raises `AggregateError` with a message naming the restriction. Empty
`group_by`, multiple TIME-granularity entries, `NumberGranularity`,
and non-granular entries all raise.

**HAVING runs BEFORE fill.** When the caller supplies a `having`
dict, HAVING is applied to the populated rows first; the spine is then
derived from the post-HAVING data range and filled with zero-count
rows where appropriate. Filled rows have `count = 0` and all measures
`None`, so they wouldn't satisfy any HAVING comparison anyway — but
the ordering matters because:

- A populated bucket whose aggregate doesn't pass HAVING is *removed*
  and is NOT back-filled with a zero row.
- A bucket between two HAVING-passing buckets IS filled.

Example: orders in January (sum=100), March (sum=200), and June
(sum=400). With `having={"sum_total__gt": 150}` and `fill=True`:

- January is filtered out by HAVING.
- March, April, May, June appear — March/June populated, April/May
  filled with `count: 0`.
- The spine starts at March (post-HAVING data min), not January.

Callers who want the spine to extend over the *original* data range
must pass an explicit `fill_min` / `fill_max`.

**Composition with `order_by` and `offset` / `limit`.** Filled rows
sort ascending by the bucket alias by default. An explicit `order_by`
re-applies on the filled list (Python-side stable sort). `offset` and
`limit` both apply AFTER fill — pagination over the dense spine, not
over the populated rows. Counting the same way: `totalCount` reflects
the dense bucket count, not the populated-row count.

**`fill_min` / `fill_max` overrides.** Both default to `None`. When
either is `None`, that endpoint is derived from the data (post-HAVING
when HAVING is in play, otherwise the queryset's raw min/max). Both
endpoints are floored to the granularity bucket they sit in — callers
can pass arbitrary "now"-style datetimes without `date_trunc`'ing
first. Passing `fill_min` / `fill_max` without `fill=True` raises
`AggregateError` (silent ignore would be a footgun).

**Implementation strategy.** Post-process Python merge over the
populated rows rather than SQL `generate_series` + `LATERAL JOIN`.
Reasons: works uniformly on PostgreSQL and SQLite (no vendor-specific
SQL); easier to test and audit; `generate_series` + LEFT JOIN
interacts badly with HAVING; cardinality is bounded for analytics
shapes.

**GraphQL surface.**

```graphql
ordersGroupBy(
  groupBy: [{ field: CREATED_AT, granularity: MONTH }]
  fill: true
  fillMin: "2026-01-01T00:00:00+00:00"
  fillMax: "2026-12-31T00:00:00+00:00"
) {
  results { key { createdAtMonth createdAtMonthRange { from to } } count }
  totalCount
}
```

`fill: Boolean = false`, `fillMin: DateTime`, `fillMax: DateTime` are
emitted unconditionally on every grouped resolver — no flag — so the
SDL stays stable. Bound validation and TIME-granularity restriction
enforcement happen at resolver entry, before any SQL fires.

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

## 11 · Why no auto-traversal for o2m / m2m measures (and the explicit opt-in)

### Default behaviour: refuse

`SUM(order.items__price)` would silently row-multiply (one row per item, summed multiple times via the implicit JOIN) and corrupt every other measure in the same query. **By default, `compute_aggregation` refuses** any measure whose ``field_path`` traverses a one-to-many or many-to-many relation. Mirrors Odoo's design choice. The error message points at both the canonical alternative AND the explicit opt-in flag (below):

```
AggregationAcrossRelationError:
  Cannot aggregate across relation `items__price` from `Order` — would cause
  silent row multiplication. Query the related model directly with the parent
  FK in `group_by` instead, or pass `allow_relation_traversal=True` to
  `compute_aggregation` to opt into Subquery-emitted measures.
```

`array_agg` remains the explicit escape hatch for "give me all child IDs per parent group" — it returns `[ID!]`, not auto-hydrated objects. Clients refetch by ID and the optimizer batches the lookup.

### Opt-in: `allow_relation_traversal=True`

Callers who need a measure across a relation can pass `allow_relation_traversal=True` on the backend primitive `compute_aggregation`. When set:

- Each relation-traversing measure is compiled into a correlated `Subquery` per measure (one scalar `Subquery` per measure, not a JOIN). The subquery groups the leaf rows by the outer-FK column so it yields exactly one value per parent; the outer aggregate then folds those per-row values across the GROUP BY / `.aggregate()` scope.
- The Subquery wrapper isolates each child fan-out — independent measures on the outer queryset (e.g. another `SUM("total")` on `Order`) are computed against the un-multiplied outer rows, preserving their values exactly. This is precisely the row-multiplication trap the default behaviour avoids.

Conceptual SQL shape (PG; SQLite emits a cast-wrapped equivalent):

```sql
SELECT SUM(per_order_sum) AS sum_items__price
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
```

#### v1.0 restrictions

The flag is intentionally narrow in v1.0:

- **Supported operators**: `SUM`, `AVG`, `MIN`, `MAX`, `COUNT`, `COUNT_DISTINCT` only. Other operators (`STDDEV`, `VARIANCE`, `STDDEV_POP`, `VAR_POP`, `PERCENTILE_CONT`, `PERCENTILE_DISC`, `MODE`, `ARRAY_AGG`, `STRING_AGG`, `BOOL_AND`, `BOOL_OR`, `COUNT_DISTINCT_TUPLE`) raise `AggregateError` with a clear v1.0-limitation message rather than emitting incorrect SQL. The unsupported-op check runs BEFORE the Postgres-only vendor check, so the v1.0 message wins on every vendor.
- **Measures only**: `group_by` paths still cannot traverse one-to-many or many-to-many relations even with the flag set. Group-by traversal would row-multiply the OUTER query and corrupt every measure regardless of any subquery isolation downstream.
- **Empty children**: a parent row with zero matching children produces a `NULL` in the per-row Subquery (the inner `GROUP BY` yields no rows). The outer `SUM` ignores `NULL` inputs by default — for `COUNT` / `COUNT_DISTINCT` this surfaces as `None` in result rows when the parent group has zero children. Callers who need a `0` surface should `COALESCE` post-fetch.

#### Where the flag lives

This flag is on the **backend primitive only**. `AggregateBuilder` and the GraphQL surface do **not** expose it. Two reasons:

1. **CLAUDE.md Critical Rule 9** — `compiler.py` is framework-agnostic. The flag is a primitive concern about how an aggregate is compiled to SQL, not a GraphQL-shape concern.
2. **CLAUDE.md Critical Rule 4** — the row-multiplication trap is the default protection. Wire-side callers should not be able to opt themselves into it; if a wire-side use case needs traversal, the wiring layer (e.g. django-angee) constructs a `compute_aggregation` call with the flag set and exposes a vetted resolver, never a generic "let any client traverse anything" knob.

## 12 · Determinism

Type generation produces the same SDL for the same inputs. Rules:

- Iterate field allowlists in declaration order (preserves Python dict insertion order).
- Iterate operator overrides in `sorted()` key order.
- Emit operator nested types (`<Model>SumFields` etc.) in canonical operator order: `count, count_distinct, count_distinct_tuple, sum, avg, min, max, stddev, variance, stddev_pop, var_pop, percentile_cont, percentile_disc, mode, bool_and, bool_or, array_agg, string_agg`. (`count_distinct_tuple` is row-level — no nested type — and is emitted as part of the same `countDistinct(...)` method-style field via the optional `fields` argument; see § 5.2. `percentile_cont` and `percentile_disc` occupy slots in the canonical order so the enum stays stable, but they are emitted as method-style fields on `<Model>Aggregate` — no nested type — per § 5.1.)
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

- **Cursor pagination on grouped results.** Offset only in early drafts; v1.0 ships an opt-in Relay-style connection alongside the offset shape (§ 4 cursor-pagination).
- **Aggregate-of-relation as a parent field** (e.g. `order.itemsAggregate`). Would need a `Subquery` emission strategy. Documented alternative (top-level child query) covers most cases. Possible v2.
- **Apollo Federation v2 first-class wiring.** v1.0 ships an opt-in `enable_federation` flag on `AggregateBuilder` that emits `@external` on `<Model>GroupKey` FK fields and decorates emitted types with `strawberry.federation.type`. Full `@key` / `@requires` / `@provides` semantics are deferred — see § 18.
- **Window functions** (`ROW_NUMBER`, `RANK`, `LAG`, etc.). Adjacent feature space; would need a new `<Model>Windowed` shape. Out of scope.
- **Streaming / chunked group-by** for cardinality > 100k group buckets. Memory pressure is real; v2 might add a `chunk_size` parameter to `compute_aggregation` and a streaming resolver.

## 16 · Versioning

The `AggregateOp` enum and the `compute_aggregation` signature are part of the SemVer contract — breaking changes bump major. The Strawberry types it emits inherit Strawberry's evolution semantics (deprecate fields, never break them in a minor release). The library tracks `strawberry-graphql-django` minor versions; major bumps there may force a major bump here.

## 18 · Apollo Federation v2 support

Federation v2 is an additive, opt-in concern. The library does not control schema construction (the consumer does) — we only emit federation-decorated types and trust the consumer to wire `strawberry.federation.Schema`.

### The flag

`AggregateBuilder(enable_federation=True)` switches the emitted types to their `strawberry.federation.*` counterparts:

| When `enable_federation=False` (default) | When `enable_federation=True`           |
| ---------------------------------------- | ---------------------------------------- |
| `strawberry.type`                        | `strawberry.federation.type`             |
| `strawberry.input`                       | `strawberry.federation.input`            |
| FK group-key fields are plain dataclass attrs | FK `<name>_id` fields use `strawberry.federation.field(external=True)` |

The flag forwards to every type-generator (`make_aggregate_type`, `make_grouped_type`, `make_having_input`, `make_group_by_spec`, `make_group_order_input`).

### What v1.0 actually emits

When `enable_federation=True` and the consumer constructs the schema with `strawberry.federation.Schema(...)`:

- `<Model>Aggregate`, `<Model>Grouped`, `<Model>GroupedResult`, `<Model>SumFields` and the rest of the nested-operator types are decorated with `strawberry.federation.type`. They register with the federation subgraph but carry **no `@key` directive** in v1.0.
- `<Model>GroupKey` foreign-key columns (e.g. `customerId` from `customer = ForeignKey(...)`) are emitted with `@external`, telling the gateway that the canonical record for that ID lives in another subgraph.
- `<Model>Having`, `<Model>GroupBySpec`, `<Model>GroupOrder` are decorated with `strawberry.federation.input`.

Schema sample (federation on, FK group-key field):

```graphql
type OrderGroupKey {
  customerId: Int @external
  status: String
  createdAt: DateTime
  # ... bucket aliases
}
```

### Schema construction is the consumer's job

The library never constructs a `Schema`. The consumer must use `strawberry.federation.Schema` (NOT plain `strawberry.Schema`) for `@external` and the federation `_service` field to print correctly. With a plain `strawberry.Schema`, the federation directives may still appear in the SDL but the gateway's federation-protocol introspection (`_service { sdl }`) will not be wired. Document this in the consumer's schema-wiring code.

### Why no `@key` on `<Model>Aggregate` in v1.0

A `@key` directive identifies an entity that subgraphs can reference by an opaque key. Aggregate result containers (`OrderAggregate`, `OrderGrouped`) have no natural identity — they are derived projections of a queryset, not entities. The keying semantics over an aggregate result are not yet a settled best practice in the Apollo Federation community, and shipping a half-baked default `@key` would be harder to remove later than to add. Consumers who need an entity-shaped aggregate (rare; usually they want to register the FK target as the entity and let the gateway compose) can register their own `@key` post-decoration.

### v1.1 roadmap

- `@key` on `<Model>Aggregate` once the keying convention stabilizes (likely a derived key over the group-by tuple).
- `@requires` / `@provides` for cases where one subgraph needs additional fields from another to compute an aggregate.
- Per-field federation directive overrides via the `operators` / per-field-config surface.

### Determinism

The `enable_federation` branch is fully deterministic — same inputs produce byte-identical SDL across two generations. The federation `field(external=True)` re-binding happens during `_emit_group_key` before the `strawberry.federation.type` decorator runs, so the emitted type signature is stable.

---

**Implementation phasing.** Phase 1: `compute_aggregation` + tests (~1 week). Phase 2: type generators (`make_*`) + tests (~1 week). Phase 3: `AggregateBuilder` convenience + integration tests against a real Strawberry schema (~3 days). Total ~2.5 weeks for a clean v0.1.0.
