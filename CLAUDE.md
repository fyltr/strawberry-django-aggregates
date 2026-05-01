# CLAUDE.md

Guidance for Claude Code working in the `strawberry-django-aggregates` repository.

> The single source of truth for behaviour is [`docs/SPEC.md`](./docs/SPEC.md). This file
> is the *meta* contract — what to read, what to never violate, how to verify.

---

# Pre-work

1. **Read `docs/SPEC.md` first.** It defines the operator catalog, granularity tracks,
   timezone semantics, HAVING shape, ordering rules, `compute_aggregation` signature,
   and the consolidated Odoo-derived footgun audit. Every non-trivial change must
   trace back to a SPEC section. If a behaviour isn't specified, propose a SPEC
   change before coding.

2. **Don't trust your memory of file contents.** After 10+ messages, re-read any file
   before editing it. Auto-compaction silently destroys context.

3. **For files >300 LOC, read in chunks.** The Read tool's default 2000-line cap can
   be lower than you think with line-wrapping; use `offset`/`limit` for large files.

---

# Critical Rules — project invariants

These are load-bearing. Violations are silent correctness bugs that corrupt analytics
output. Treat them as inviolable until SPEC.md says otherwise.

## 1. Permission-naive — the library does NOT enforce row-level access

The `compute_aggregation` primitive accepts a queryset and trusts it. If you find
yourself adding a `user` parameter, an `actor` parameter, an `accessible_by` call,
or any concept of identity inside this package — **stop**. Permission scoping is
the caller's job (django-guardian, django-rules, django-rebac, plain
`filter(owner=...)` — they all compose). Adding identity would break that
composition and turn this from a generic library into an angee-specific component.

If REBAC integration ever becomes desirable, it lives in a separate `[rebac]`
extra exporting an adapter — never inline.

## 2. Determinism is load-bearing

Same `(model, aggregate_fields, group_by_fields, operators)` ⇒ byte-identical
SDL. The determinism test (`tests/test_determinism.py`, when wired) generates
twice and `schema.print()`-diffs. Hard rules:

- No timestamps. No `datetime.now()`, no `time.time()`.
- No PRNG. No `random.*`, no `uuid4()`.
- Sort dict iteration: `sorted(d.items())`.
- Sort set iteration: `sorted(s)`.
- Operator nested types emitted in canonical order: `count, count_distinct, sum,
  avg, min, max, stddev, variance, bool_and, bool_or, array_agg, string_agg`.
- HAVING comparisons emitted in canonical order: `Gt, Lt, Lte, Gte, Eq, Neq, In,
  NotIn`.
- Don't rely on Python dict insertion order across pickle/JSON round-trips.

## 3. Strict operator whitelist — `AggregateOp` enum is the contract

Never accept arbitrary SQL fragments, never `eval()` user input into SQL, never
`format()` user-supplied strings into queries. The enum is the universe. If you
can't put an operator in the enum, don't ship it. New operators require a SPEC
section + tests + per-field-type default-allowlist update.

## 4. No auto-traversal of one-to-many or many-to-many for measures

`SUM(parent.children__field)` silently row-multiplies and corrupts every measure
in the same query. **Refuse the request by default** with
`AggregationAcrossRelationError`. The error message must point to the explicit
alternative ("query the child model with the parent FK in `group_by`") AND the
opt-in flag (below). `array_agg` is the historical escape hatch for
"give me child IDs per parent group" and it returns IDs only — never
auto-hydrate.

This is Odoo's load-bearing design choice (`_read_group` refuses for the same
reason) and we follow as the default.

An explicit opt-in flag `allow_relation_traversal=True` exists on
`compute_aggregation` for callers who genuinely need a Subquery-emitted measure
over a one-to-many or many-to-many relation. When set, each traversing measure
is compiled into a correlated `Subquery` per measure (one scalar `Subquery` per
measure, not a JOIN), so each child fan-out is collapsed inside its own
subquery before the outer aggregate runs and other measures on the outer
queryset are unaffected. v1.0 restricts the supported operators to `SUM`,
`AVG`, `MIN`, `MAX`, `COUNT`, `COUNT_DISTINCT` and applies only to MEASURES —
`group_by` paths still cannot traverse relations even with the flag. Default
behaviour is unchanged.

**Do not surface this flag through `AggregateBuilder` or GraphQL** — it stays
a primitive-level escape hatch (Critical Rule 9 separation; the wiring layer
constructs a `compute_aggregation` call with the flag set if it has a vetted
use case, never exposes a generic "let any client traverse anything" knob).

## 5. Timezone wrap BEFORE truncate

For any date-bucketed group_by:

```sql
date_trunc('month', timezone(<user_tz>, timezone('UTC', col)))
```

Cast UTC-stored timestamp → user tz → THEN `date_trunc`. Truncating UTC first
mis-buckets any timestamp near a date boundary in the user's tz ("May 1 in
Tokyo" buckets as April 30 UTC). Mirrors `odoo/models.py:2685–2727` exactly.
SQLite has limited tz support; document the degradation, don't paper over it.

## 6. Fail-loud on unknown order terms

`parse_aggregate_order` resolves against three namespaces (aggregate aliases,
group-by paths, plain field allowlist). Unknown terms raise
`OrderFieldNotAllowed`. **Never silently drop.** Odoo's pre-17 `read_group`
silently dropped unknown terms and was a years-long source of "why isn't my
query ordered?" reports.

## 7. `array_agg` returns IDs only — never auto-hydrate

Odoo's `recordset` operator browse-resolves to live records and is a
serialization landmine in GraphQL. `<Model>ArrayAggFields` returns `[ID!]`,
`[String!]`, etc. Clients refetch by ID; Strawberry's `DjangoOptimizerExtension`
batches the lookup. Don't add an auto-hydration mode "for convenience."

## 8. Postgres-only operators raise at resolver entry, not at SQL exec

`stddev`, `variance`, `array_agg`, `string_agg` raise `OperatorNotSupportedError`
on non-Postgres connections. Detect the connection vendor at the top of the
resolver and fail with a clear message naming the operator and the vendor. Don't
let the failure happen mid-SQL — the error message becomes a database-vendor
error, not a usable diagnostic.

## 9. `compiler.py` has zero GraphQL coupling

`compute_aggregation` must be callable from any Python context: DRF view,
Celery task, admin script, MCP tool, plain `python manage.py shell`. Therefore
`compiler.py` does NOT import from `strawberry`, `strawberry_django`, or
anything in `types.py`/`builder.py`. The only Strawberry imports are inside
`types.py` (which builds Strawberry types) and `builder.py` (which wires
resolver fields). If you need to change this, propose a SPEC change first.

## 10. Public API is the SemVer contract

The `AggregateOp` enum members, the `TimeGranularity` / `NumberGranularity`
enums, the `compute_aggregation()` signature, the `make_*_type` signatures, and
the `AggregateBuilder` constructor signature are part of the SemVer contract.
Breaking changes bump major. New operator additions are minor (additive). Renames
are major. The SDL emission shape inherits Strawberry's evolution semantics
(deprecate fields, never break them in a minor).

---

# Project Structure

```
strawberry-django-aggregates/
├── pyproject.toml             # BSD-3, Py 3.13+, Django 5.0–6.0
├── README.md                  # short pitch + example
├── LICENSE                    # BSD-3-Clause
├── strawberry_django_aggregates/
│   ├── __init__.py            # public API re-exports — every new export here
│   ├── builder.py             # AggregateBuilder + BuiltAggregates
│   ├── types.py               # make_aggregate_type / _grouped_type / _having_input / _group_by_spec
│   ├── operators.py           # AggregateOp enum + default_operators_for(field_type)
│   ├── granularity.py         # TimeGranularity / NumberGranularity + NUMBER_GRANULARITY_PART
│   ├── compiler.py            # compute_aggregation backend primitive — NO GraphQL imports
│   ├── ordering.py            # parse_aggregate_order — fail-loud
│   └── errors.py              # AggregateError hierarchy
├── tests/
│   ├── conftest.py            # in-memory SQLite Django setup
│   └── test_*.py              # one file per concern (correctness, tz, having, …)
└── docs/
    └── SPEC.md                # source of truth — read first
```

---

# Common pitfalls

- **Adding `from django.contrib.auth import get_user_model` anywhere.** Forbidden.
  See Critical Rule 1. The library is permission-naive. If you need an actor,
  the caller passes a pre-scoped queryset.
- **Adding `info` parameter to `compute_aggregation`.** That couples it to
  Strawberry. Critical Rule 9. The GraphQL resolver is a thin wrapper that
  reads from `info.context` and passes plain values down.
- **`from strawberry import ...` inside `compiler.py` or `operators.py` or
  `granularity.py` or `errors.py` or `ordering.py`.** Those modules are
  framework-agnostic. Strawberry imports live only in `types.py` and `builder.py`.
- **Adding a "lazy" mode for grouped queries.** Odoo's `lazy=True` was confusing
  for years and was removed in 17. We never ship it.
- **Tempted to auto-emit a Subquery for `parent.children__measure` by default.**
  No. Critical Rule 4 — default refuses. The opt-in
  `allow_relation_traversal=True` flag exists on `compute_aggregation` for the
  vetted use case; it does not change the default and is not surfaced through
  `AggregateBuilder` / GraphQL.
- **Using `f"{field}"` or `% field` to build SQL.** Use `django.db.models.functions`
  and `Aggregate` subclasses. Never string-format SQL.
- **Letting an operator string reach the database.** All operator dispatch happens
  through the `AggregateOp` enum → a static dict mapping to Django ORM constructs.
- **Adding a `group_operator` / `aggregator` Field metadata reader** like Odoo's.
  Per-field overrides happen via the `operators` dict argument to
  `AggregateBuilder` / `make_*` — explicit, not magic.
- **Forgetting the SQLite test path.** Postgres-only operators must raise on
  SQLite at resolver entry. Test fixture set up in `conftest.py` uses
  in-memory SQLite — you can validate "does it raise correctly" without docker.

---

# Workflow

When implementing or modifying behaviour:

1. **Read the relevant SPEC section.** If the behaviour isn't there, add a SPEC
   section before coding.
2. **One concern per PR.** Operator additions, granularity changes, builder
   ergonomics, and bug fixes are separate changes.
3. **Run the verification chain** before reporting complete:
   - `uv run ruff check .`
   - `uv run mypy strawberry_django_aggregates/`
   - `uv run pytest`
4. **Determinism test.** Any change to type emission must keep
   `tests/test_determinism.py` passing — generate × 2, byte-diff SDL.
5. **Invoke `django-code-reviewer` agent on every non-trivial change.**
   MUST be used proactively after writing or modifying any code in this
   repo — not on demand. Run it *after* ruff/mypy/pytest pass (the agent
   expects compileable code). It reviews against Django conventions,
   Python type hints, anti-patterns, security issues, N+1 queries, and
   architecture violations; it reports by priority with specific
   `file:line` references. Address all High/Medium findings before
   reporting the task complete; document any deferred Low findings.
6. **No backwards-compat shims** during 0.x. SemVer contract holds from 1.0.

# Tooling commands

```bash
uv sync --group dev                              # install workspace + dev tools
uv run pytest                                    # all tests; conftest.py spins up an in-memory SQLite Django
uv run pytest tests/test_groupby_timezones.py    # one file
uv run ruff check .                              # lint (line length 79)
uv run ruff format .                             # format
uv run mypy strawberry_django_aggregates/        # type-check
```

---

# Relationship with django-angee

This library was carved out of `django-angee`'s `specs/angee/GRAPHQL.md` § 7 to
keep aggregation generic and reusable. `django-angee` consumes us via
`AggregateBuilder` from a per-model wiring layer (~50 LOC of `AngeeMeta` →
`AggregateBuilder` argument translation). We do **not** know about angee:
no `AngeeMeta` references, no `AngeeModelBase`, no `class REBAC:`, no
`accessible_by`. The relationship is one-way. If a feature request would only
benefit angee, it doesn't belong here — propose it as an angee-side wiring
change instead.

The hand-off doc on the angee side is at
`/Users/alexis/Work/fyltr/django-angee/specs/angee/GRAPHQL.md` § 7. Read that
when an angee consumer reports an integration issue; the issue is usually in
the wiring layer, not in this library.
