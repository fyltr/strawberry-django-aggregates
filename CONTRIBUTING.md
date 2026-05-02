# Contributing

Thank you for contributing to `strawberry-django-aggregates`.

## Quality gate (run before opening a PR)

Use the same command chain locally that CI enforces:

```bash
uv run ruff check .
uv run mypy strawberry_django_aggregates/
uv run pytest
```

## Recommended failure triage order

1. `ruff` (fastest feedback; formatting/lint correctness)
2. `mypy` (type-safety regressions)
3. `pytest` (behavior/cross-module integration)

## Version/runtime expectations

- Python: 3.13
- Django: 6.0
- Backends: SQLite (always) and PostgreSQL (Postgres-only operators
  raise `OperatorNotSupportedError` at resolver entry on SQLite — see
  `docs/SPEC.md` § 5).

## Determinism expectations

Any change that affects type generation must preserve deterministic SDL:

- same inputs must emit byte-identical schema output,
- avoid non-deterministic ordering in iteration (sort dict/set traversal),
- avoid time/PRNG-dependent values in emitted type names or defaults.

See `CLAUDE.md` Critical Rule 2 and `docs/SPEC.md` § 12 for the full
determinism contract.
