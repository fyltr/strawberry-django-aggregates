"""Exception hierarchy for strawberry-django-aggregates.

All errors descend from :class:`AggregateError` so consumers can catch
the family without depending on individual subclasses.
"""

from __future__ import annotations


class AggregateError(Exception):
    """Base class for all errors raised by strawberry-django-aggregates."""


class OperatorNotSupportedError(AggregateError):
    """Raised when an operator is unsupported on the current database vendor.

    Postgres-only operators (``array_agg``, ``string_agg``, ``stddev``,
    ``variance``) raise this at resolver entry on SQLite connections —
    not at SQL execution time. The message names the operator and the
    detected connection vendor.
    """


class OrderFieldNotAllowed(AggregateError):
    """Raised when an order term does not resolve to a known field or alias.

    Mirrors Odoo's post-17 fail-loud behaviour. The pre-17 implementation
    silently dropped unknown terms; we refuse the request instead.
    """


class AggregationAcrossRelationError(AggregateError):
    """Raised when an aggregate measure references a one-to-many / m2m path.

    By default, auto-traversal is refused: it would cause silent row
    multiplication corrupting every measure in the same query. The
    canonical alternative is to query the child model with the parent FK
    in ``group_by``. ``array_agg`` is the explicit escape hatch for
    "give me child IDs per parent group."

    For callers that genuinely need a measure across a one-to-many or
    many-to-many relation, the backend primitive
    :func:`strawberry_django_aggregates.compute_aggregation` accepts
    ``allow_relation_traversal=True``. When set, the compiler emits a
    correlated ``Subquery`` per measure (one ``Subquery`` per measure,
    not a JOIN), which avoids row-multiplication. This flag lives only
    on the primitive — it is intentionally not surfaced through
    ``AggregateBuilder`` / GraphQL (Critical Rule 9 separation).
    """


class HavingFieldNotAllowed(AggregateError):
    """Raised when a HAVING input references an unknown aggregate alias."""


class GroupByFieldNotAllowed(AggregateError):
    """Raised when a group-by spec references a field not in the allowlist."""


class GranularityNotApplicable(AggregateError):
    """Raised when granularity is set on a non-date / non-datetime field."""
