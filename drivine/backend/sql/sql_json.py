"""Values as a SQL store's JSON holds them, written as TypeScript's Drivine writes them with JSON.stringify."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic_core import to_jsonable_python


def storable(value: Any) -> Any:
    """
    A value as a node's JSON holds it: a datetime as ISO 8601 in UTC to the millisecond (as JavaScript's
    `Date.toISOString` writes it, so a store written from Node and from Python compares alike), a naive one taken as
    UTC; a date or time as its ISO text; an Enum as its value; a set or tuple as a list; a Decimal as the whole number
    or the float it is, and one that is no finite number as the float it stands for: a NaN, a signalling one too, as
    NaN, as a compiled statement binds it (see shared_compiler.written), and an infinity as the float's (#303).
    """
    if isinstance(value, datetime):
        utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return storable(value.value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [storable(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {str(key): storable(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, Decimal):
        if value.is_nan():  # A signalling NaN too, which float() and a comparison refuse.
            return math.nan
        if value.is_infinite():
            return float(value)
        return int(value) if value == value.to_integral_value() else float(value)
    return value


_HELD = "\u0000drivine:held:"
"""
What a held value stands for in the text json.dumps writes, before its own text is spliced in its place: with a token
of the call's own after it, so no text of the value's is taken for one.
"""


def to_json(value: Any) -> str:
    """
    The value as JSON text, compact as JSON.stringify writes it, so that text compared as text is alike. A value the
    store layout wrote (value_layout.Held) is its own text, spliced in as it is.
    """
    held: list[str] = []
    mark = f"{_HELD}{uuid.uuid4().hex}:"

    def default(it: Any) -> Any:
        from drivine.backend.sql.value_layout import Held

        if isinstance(it, Held):
            held.append(it.text)
            return f"{mark}{len(held) - 1}"
        return to_jsonable_python(it)

    text = json.dumps(storable(value), separators=(",", ":"), ensure_ascii=False, default=default)
    for i, it in enumerate(held):
        text = text.replace(json.dumps(f"{mark}{i}"), it, 1)
    return text
