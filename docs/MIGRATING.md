# Migrating from v0.1 to v1.0

v1.0 is the stable major release and the SemVer baseline going forward.
Most of the surface is additive — existing v0.1 callers will keep working
unchanged. This guide collects the few breaking changes and the most
common upgrade tasks.

## At a glance

| Change                                                    | Impact                                |
|-----------------------------------------------------------|---------------------------------------|
| `SUM(IntegerField)` → `BigInt` (was `Int`) in SDL         | **Re-typegen GraphQL clients**        |
| `AggregateOp` enum gained 5 new members in canonical order | Recompile any code switching on the enum |
| `compute_aggregation` return type widens to a union       | Only matters if you used `chunk_size` |
| New non-trivial-default kwargs across the surface         | All default to backward-compatible values |

Everything else (every / some aliases, BucketRange, percentile / mode,
multi-column count_distinct, fill_temporal, cursor pagination, Federation,
streaming, JSONB groupby, comodel ordering, relation traversal opt-in,
cross-relation aggregate field) is additive — existing schemas and
consumers that don't opt into the new args see no SDL or runtime change.

## Breaking change #1 — `SUM` on integer Django fields now emits `BigInt`

**What changed.** `SUM(IntegerField)`, `SUM(SmallIntegerField)`,
`SUM(PositiveIntegerField)`, `SUM(PositiveSmallIntegerField)`, and
`SUM(BigIntegerField)` now emit a custom `BigInt` scalar in SDL. The wire
encoding is a JSON string (e.g. `"6000000000"`) — JavaScript clients past
`Number.MAX_SAFE_INTEGER` (2⁵³) survive end-to-end. The compiler primitive
still returns Python `int` natively; the string encoding lives at the
Strawberry scalar layer.

**Why.** PostgreSQL widens `SUM(int4)` → `int8` (`bigint`). The 32-bit
GraphQL `Int` scalar overflows at 2³¹ — silent corruption for any
analytics-shaped query summing a large number of small integers.

**What to do.**

1. Re-run your GraphQL codegen / schema-codegen step — `BigInt` is a new
   scalar in your schema.
2. Decide your client-side handling. Two safe options:
   - Treat `BigInt` as a string and `BigInt(value)` cast at the call site
     where the number is consumed. Most type-safe.
   - Use a client-side `bigint`-aware GraphQL codec (`graphql-scalars`,
     `apollo-link-scalars`, etc.) to deserialize to a native `BigInt` /
     `Long` type.
3. If you have any tests asserting integer SUM results as `Int` literals
   (e.g. `expect(payload.sum.quantity).toBe(42)`), they'll need to compare
   against the string form (`"42"`) or coerce.

The SDL diff is mechanical:

```diff
 type OrderSumFields {
   total: Decimal
-  quantity: Int
+  quantity: BigInt
 }
+
+scalar BigInt
```

Decimal- and Float-typed `SUM` outputs are unchanged.

## Breaking change #2 — `AggregateOp` enum has new members in canonical order

**What changed.** Five enum members added in canonical positions:

```python
class AggregateOp(StrEnum):
    COUNT
    COUNT_DISTINCT
    COUNT_DISTINCT_TUPLE  # new
    SUM
    AVG
    MIN
    MAX
    STDDEV
    VARIANCE
    STDDEV_POP            # new
    VAR_POP               # new
    PERCENTILE_CONT       # new
    PERCENTILE_DISC       # new
    MODE                  # new
    BOOL_AND
    BOOL_OR
    ARRAY_AGG
    STRING_AGG
```

**Why.** v1.0 is the one chance to reorder canonical positions without a
major bump. Future v1.x adds operators at the end; reordering existing
members would be a v2 break.

**What to do.**

- If you `match` / `switch` on `AggregateOp`, add cases for the new members
  or rely on a default branch that raises (preferred — fail-loud).
- If you build a UI listing operators, the new ones will show up
  automatically once you re-import.
- The new `<Model>StddevPopFields`, `<Model>VarPopFields`,
  `<Model>ModeFields` nested types appear in SDL when the model has
  applicable fields. New `percentileCont(field, fraction)` and
  `percentileDisc(field, fraction)` method-style fields appear on
  `<Model>Aggregate`.

## Breaking change #3 — `compute_aggregation` return type widens

**What changed.** The return type of `compute_aggregation` is now
`list[dict[str, Any]] | Iterator[list[dict[str, Any]]]`. The iterator
variant is only returned when the caller passes `chunk_size=N`.

**What to do.** Nothing, unless you've added `chunk_size` to a call site.
The default behaviour (`chunk_size=None`) returns a `list[dict]` exactly
as before. Type-checkers may flag the new union; narrow with an
`isinstance(result, list)` check or by passing `chunk_size=None`
explicitly.

## Common upgrade tasks (additive — no migration required)

These are not breaking; flagged here so v0.1 callers know what's new and
how to opt in:

- **Cursor pagination on grouped results.** Pass
  `pagination_style="cursor"` (or `"both"`) to `AggregateBuilder` to emit
  `<Model>GroupedConnection`. Existing offset usage unaffected.
- **Federation v2.** Pass `enable_federation=True` to `AggregateBuilder` to
  emit `@external` on FK group-key fields. Schema must be built with
  `strawberry.federation.Schema(...)` for directives to print.
- **Locale week_start.** Pass `weekStart: Int` (1..7) at query time on the
  grouped field. ISO Monday=1 is the default and matches v0.1 behavior.
- **Fill temporal.** Pass `fill: true` plus optional `fillMin` / `fillMax`
  at query time on the grouped field.
- **Multi-column count_distinct.** Pass `fields: [...]` on the
  `countDistinct` field; the existing `field: ...` shape still works.
- **JSONB property groupby.** Pass
  `json_paths={"metadata.amount": "Decimal", ...}` to `AggregateBuilder` to
  allowlist dotted JSON paths.
- **Cross-relation aggregate.** Call
  `register_relation_aggregate(ParentType, "children", child_built)` after
  declaring your strawberry-django parent types.
- **Streaming chunks.** Pass `chunk_size=1000` to `compute_aggregation` —
  backend-only; not exposed on the GraphQL surface (use cursor pagination
  for client-facing chunking).
- **Comodel ordering.** Pass `respect_comodel_ordering=True` to
  `compute_aggregation` or `AggregateBuilder` to traverse a FK comodel's
  `Meta.ordering`.
- **Relation traversal opt-in.** Pass `allow_relation_traversal=True` to
  `compute_aggregation` for o2m/m2m measure paths via `Subquery` emission.
  Default still refuses (Critical Rule 4 spirit).

## When to read SPEC.md

For any non-obvious behavior — NULL semantics, tz wrap, determinism rules,
the operator-vocabulary table, the canonical-emission order — `docs/SPEC.md`
is the source of truth and will be kept up to date through every v1.x
release.
