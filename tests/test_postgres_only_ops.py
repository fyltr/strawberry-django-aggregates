"""Postgres-only operators raise at resolver entry on SQLite — SPEC § 5
and CLAUDE.md Critical Rule 8.

The failure must surface as :class:`OperatorNotSupportedError` with the
operator and vendor named — *not* as a database-vendor error from the
SQL execution stage.
"""

from __future__ import annotations

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import (
    OperatorNotSupportedError,
)


@pytest.mark.django_db
@pytest.mark.parametrize("op", [
    AggregateOp.STDDEV,
    AggregateOp.VARIANCE,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
])
def test_pg_only_op_raises_on_sqlite(sample_orders, op):
    from tests.models import Order

    with pytest.raises(OperatorNotSupportedError) as exc_info:
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(op, "total")],
        )
    msg = str(exc_info.value)
    assert op.value in msg
    assert "sqlite" in msg.lower()
