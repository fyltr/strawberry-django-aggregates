"""Cursor pagination helpers for grouped aggregation results.

Pure stdlib — no Django, no Strawberry — so the encoding is callable
from any Python context (DRF view, Celery task, MCP tool, plain
``manage.py shell``). Mirrors the framework-agnostic policy of
``compiler.py`` per CLAUDE.md Critical Rule 9.

Cursor wire format
------------------

Cursors are an opaque base64-encoded JSON list of group-by alias values
in canonical order (the order the caller supplies in ``group_by`` is
preserved verbatim — see :func:`compiler.group_by_alias`). The list
encodes the **trailing** value of an emitted page row, so the next
page's keyset filter is ``(a, b, c) > (cursor_a, cursor_b, cursor_c)``.

JSON does not natively serialize :class:`datetime.datetime` /
:class:`datetime.date`, so the encoder converts them to ISO-8601
strings tagged with a one-letter type marker
(``["dt", "2026-05-01T00:00:00+00:00"]`` /
``["d", "2026-05-01"]``). The decoder restores the original Python
type. Other primitive types (``int``, ``str``, ``bool``, ``None``) and
:class:`decimal.Decimal` round-trip as themselves; ``Decimal`` is
encoded as ``["dec", "100.00"]`` so the value survives the JSON
``float`` conversion losslessly.

The encoding is stable across Python versions and operating systems —
no timestamps, no PRNG, no insertion-order dependence. Two encodes of
the same value list produce byte-identical strings (CLAUDE.md Critical
Rule 2).
"""

from __future__ import annotations

import base64
import datetime
import decimal
import json
from typing import Any

# Type markers for non-JSON-native values. One-letter codes keep the
# encoded blob compact; the suffixed string is the ISO / decimal payload.
_MARKER_DATETIME = "dt"
_MARKER_DATE     = "d"
_MARKER_TIME     = "t"
_MARKER_DECIMAL  = "dec"


def _encode_value(value: Any) -> Any:
    """Convert a Python value into a JSON-serializable form.

    Datetimes / dates / times become tagged 2-tuples; decimals are
    string-encoded under their own marker; everything else passes
    through unchanged.
    """
    if isinstance(value, datetime.datetime):
        return [_MARKER_DATETIME, value.isoformat()]
    # ``datetime.date`` check must come AFTER the datetime check —
    # ``datetime`` subclasses ``date`` in stdlib.
    if isinstance(value, datetime.date):
        return [_MARKER_DATE, value.isoformat()]
    if isinstance(value, datetime.time):
        return [_MARKER_TIME, value.isoformat()]
    if isinstance(value, decimal.Decimal):
        return [_MARKER_DECIMAL, str(value)]
    return value


def _decode_value(value: Any) -> Any:
    """Inverse of :func:`_encode_value`. Tagged 2-tuples become the
    original Python type; bare values pass through.

    Tagged values are emitted by :func:`_encode_value` as 2-element
    lists ``[marker, payload]`` where marker is a known one-letter
    string. Any other 2-element list (e.g. ``["a", "b"]`` from user
    data) passes through unchanged because the marker check rejects
    unknown markers.
    """
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
    ):
        marker, payload = value
        if marker == _MARKER_DATETIME:
            return datetime.datetime.fromisoformat(payload)
        if marker == _MARKER_DATE:
            return datetime.date.fromisoformat(payload)
        if marker == _MARKER_TIME:
            return datetime.time.fromisoformat(payload)
        if marker == _MARKER_DECIMAL:
            return decimal.Decimal(payload)
    return value


def encode_group_cursor(values: list[Any]) -> str:
    """Encode a list of group-by alias values into an opaque cursor.

    ``values`` is the canonical-order tuple of group-key values for a
    single emitted row (``[customer_id, status, created_at_month]`` for
    a multi-level group_by). The result is URL-safe base64 over the
    JSON serialization of the (type-tagged) values list.

    Pure function — same input ⇒ byte-identical output, no side
    effects, no I/O.
    """
    encoded = [_encode_value(v) for v in values]
    # ``sort_keys=True`` is irrelevant here (we serialize a list, not
    # a dict) but ``separators`` forces compact output so cursors
    # stay short on the wire. ``ensure_ascii=False`` would shorten
    # cursors carrying non-ASCII strings but we keep the default for
    # determinism across locales.
    payload = json.dumps(encoded, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_group_cursor(cursor: str) -> list[Any]:
    """Decode a cursor produced by :func:`encode_group_cursor`.

    Raises :class:`ValueError` for any malformed input — invalid
    base64, non-JSON payload, non-list root, or a tagged value with an
    unknown marker. The error message intentionally does NOT include
    the offending cursor (it could be user-supplied), only a generic
    diagnosis suitable for surfacing to a GraphQL error.
    """
    if not isinstance(cursor, str):
        raise ValueError("cursor must be a string")
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("malformed cursor: invalid base64") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed cursor: invalid JSON payload") from exc
    if not isinstance(decoded, list):
        raise ValueError("malformed cursor: payload must be a JSON list")
    return [_decode_value(v) for v in decoded]
