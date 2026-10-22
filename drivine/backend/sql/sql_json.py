"""Values as a SQL store's JSON holds them, written as TypeScript's Drivine writes them with JSON.stringify."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic_core import to_jsonable_python


def storable(value: Any) -> Any:
    """
    A value as a node's JSON holds it: a datetime as ISO 8601 in UTC to the millisecond (as JavaScript's
    `Date.toISOString` writes it, so a store written from Node and from Python compares alike), a naive one taken as
    UTC; a date or time as its ISO text; an Enum as its value; a set or tuple as a list.
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
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def to_json(value: Any) -> str:
    """The value as JSON text, compact as JSON.stringify writes it, so that text compared as text is alike."""
    return json.dumps(storable(value), separators=(",", ":"), ensure_ascii=False, default=to_jsonable_python)
