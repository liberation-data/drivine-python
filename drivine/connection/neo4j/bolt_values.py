"""
Parameters as the Bolt engines take them: a `time` with an offset made one the Neo4j driver can send (#565).

The driver packs a `time` with a zone by asking its `tzinfo` for the offset of the time itself
(`value.tzinfo.utcoffset(value)`), and the standard library's `timezone` answers only for a `datetime`, or None: the
driver raises before anything reaches the store (neo4j/neo4j-python-driver#1380). On Neo4j such a time is given a
fixed offset of Drivine's own that answers for a time too, so it is sent and stored as a Neo4j Time of that offset,
and read back as a `time` of it.
Memgraph has no Time (its Bolt decoder has no Time structure, as liberation-data/drivine4j#862 found), so it is sent
the time as text of one width, as drivine4j writes it (`10:11:12.000000000+02:00`, see `fixed_width_time`), where
FalkorDB is sent its ISO text: a field declared `time` reads either back as the time. A time
without an offset, which the driver sends as a LocalTime, is left as it is.

A UUID is sent as its text, as drivine4j and Drivine for TypeScript hold one on every engine (#583): the driver sends a
UUID only over Bolt 6.1, and Neo4j's aligned store format can't hold one, so a save of a UUID NodeId raised. A field
declared `UUID` reads the text back as the UUID, and a lookup by one binds the same text.
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta, tzinfo
from typing import Any

from drivine.connection.database_type import DatabaseType


class FixedOffset(tzinfo):
    """An offset from UTC that answers for a `time` as for a `datetime`, as the Neo4j driver asks it to."""

    __slots__ = ("_offset",)

    def __init__(self, offset: timedelta) -> None:
        self._offset = offset

    def utcoffset(self, _: object) -> timedelta:
        return self._offset

    def dst(self, _: object) -> timedelta | None:
        return None

    def tzname(self, _: object) -> str | None:
        return None

    def __repr__(self) -> str:
        return f"FixedOffset({self._offset!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FixedOffset) and other._offset == self._offset

    def __hash__(self) -> int:
        return hash(self._offset)

    def __reduce__(self) -> tuple[Any, ...]:
        return FixedOffset, (self._offset,)


def bolt_parameters(parameters: dict[str, Any], engine: DatabaseType) -> dict[str, Any]:
    """
    `parameters` with each `time` with an offset, and each UUID, in lists and maps too, as `engine` takes it: see the
    module.
    """
    as_text = engine is DatabaseType.MEMGRAPH
    if not any(_holds_time(it) for it in parameters.values()):
        return parameters
    return {name: _value(it, as_text) for name, it in parameters.items()}


def _holds_time(value: Any) -> bool:
    """Whether `value`, or a list or map in it, holds a `time` or a UUID, which are sent otherwise (see the module)."""
    if isinstance(value, (time, uuid.UUID)):
        return True
    if isinstance(value, (list, tuple)):
        return any(_holds_time(it) for it in value)  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return any(_holds_time(it) for it in value.values())  # pyright: ignore[reportUnknownVariableType]
    return False


def _value(value: Any, as_text: bool) -> Any:
    if isinstance(value, time):
        return _time(value, as_text)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_value(it, as_text) for it in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {key: _value(it, as_text) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def fixed_width_time(value: time, offset: timedelta) -> str:
    """
    A time with an offset as drivine4j writes one as text (its `TemporalCoercer`): its seconds always, all nine digits
    of the second, and its offset, `Z` where it is none (`10:11:12.000000000+02:00`). The engine compares the text as
    text, which sorts as time only when every value is the same width: the offset follows the fraction, and `Z` sorts
    after `.`.
    """
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, rest = divmod(abs(total), 3600)
    minutes, seconds = divmod(rest, 60)
    zone = "Z" if total == 0 else f"{sign}{hours:02d}:{minutes:02d}" + (f":{seconds:02d}" if seconds else "")
    return f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}.{value.microsecond:06d}000{zone}"


def _time(value: time, as_text: bool) -> Any:
    """
    A time as the engine takes it. One whose zone gives no offset, as a named zone gives none for a time alone, is
    naive, as Python counts it: it is sent without one, as its ISO text writes it.
    """
    if value.tzinfo is None:
        return value
    offset = value.utcoffset()
    if offset is None:
        return value.replace(tzinfo=None)
    if as_text:
        return fixed_width_time(value, offset)
    return value if isinstance(value.tzinfo, FixedOffset) else value.replace(tzinfo=FixedOffset(offset))
