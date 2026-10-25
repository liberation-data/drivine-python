from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from drivine.drivine_error import DrivineError

OBJECT = "_cy_params"
"""The marker every parameter is bound at, as one JSON object, where they are bound `in_object`."""


class Parameters:
    """
    The parameters of a statement as the SQL binds them. A Cypher parameter is a value, so it is bound as the JSON text
    of its value and read as JSON, and compares as a property it is compared with does. A count (`SKIP`, `LIMIT`) is
    bound as the number it is. A parameter the statement names and the caller did not bind is refused, as on a graph
    engine.

    With `in_object`, every parameter is bound at once, as one JSON object at OBJECT, and `as_json` and `as_count` are
    given a parameter's name and read it from there: a procedure takes its parameters so.
    """

    def __init__(
        self,
        given: Mapping[str, Any],
        as_json: Callable[[str, str | None], str],
        as_count: Callable[[str], str],
        in_object: bool = False,
        text: Callable[[Any], str] | None = None,
        as_path: Callable[[str, str | None, Sequence[str], Any], str] | None = None,
    ) -> None:
        """
        `as_json` is the bound JSON text at a marker, or with `in_object` the parameter of a name, as the store's JSON:
        given the marker, and the text bound at it, or the name and None. `as_count` is the bound whole number at a
        marker, or with `in_object` the parameter of a name, as the store's number. `text` is the JSON text a value is
        bound as: `cypher_json`'s, unless a store writes it otherwise. `as_path` is the value at a path of keys in a
        parameter that is a map, read straight from the text it is bound as: given the marker and the text as `as_json`
        is given them, the keys, and the value at them, of JSON's own kinds (see `jsonable`); None where the store reads
        the parameter, then each key of it.
        """
        # An iterator is read once, here: a statement may name its parameter more than once.
        self._given: dict[str, Any] = {
            name: list(cast(Iterator[Any], it)) if isinstance(it, Iterator) else it for name, it in given.items()
        }
        self._as_json = as_json
        self._as_count = as_count
        self._in_object = in_object
        self._text = text or cypher_json
        self._as_path = as_path
        self._bound: dict[str, Any] = {}
        self._trees: dict[str, Any] = {}

    @property
    def values(self) -> dict[str, Any]:
        """What the SQL binds."""
        return (
            {OBJECT: self._text({name: self._tree(name) for name in self._given})} if self._in_object else self._bound
        )

    def json(self, name: str) -> str:
        """The parameter, as JSON."""
        value = self.value(name)
        if self._in_object:
            return self._as_json(name, None)
        key = f"_cy_{name}"
        written = None if value is None else self._text(self._tree(name))
        self._bound[key] = written
        return self._as_json(f"${key}", written)

    def json_at(self, name: str, keys: Sequence[str]) -> str | None:
        """
        The value at the keys in the parameter, as JSON, where the store reads it from the text the parameter is bound
        as (see `as_path`) and the parameter is a map that holds a map at each key but the last; None otherwise.
        """
        read = self._as_path
        if read is None:
            return None
        # Walked as its text is written, each key a text: see `_tree`.
        tree = self._tree(name)
        map_: Any = tree
        for key in keys[:-1]:
            map_ = cast(dict[str, Any], map_).get(key) if isinstance(map_, dict) else None
        if not isinstance(map_, dict):
            return None
        value = cast(dict[str, Any], map_).get(keys[-1])
        if self._in_object:
            return read(name, None, keys, value)
        key = f"_cy_{name}"
        written = self._text(tree)
        self._bound[key] = written
        return read(f"${key}", written, keys, value)

    def _tree(self, name: str) -> Any:
        """
        The parameter as JSON's own kinds (see `jsonable`), worked out once, and its text written from it, which
        `jsonable` gives again: an iterator in a map the parameter holds is read once, though the statement reads the
        parameter, or a value in it, more than once.
        """
        if name not in self._trees:
            self._trees[name] = jsonable(self.value(name))
        return self._trees[name]

    def count(self, name: str) -> str:
        """The parameter, as a whole number."""
        value = self.value(name)
        if self._in_object:
            return self._as_count(name)
        key = f"_cyn_{name}"
        self._bound[key] = value
        return self._as_count(f"${key}")

    def value(self, name: str) -> Any:
        """The value bound to the parameter, as the caller gave it."""
        if name not in self._given:
            raise DrivineError(f"The statement has a parameter ${name}, and no value is bound to it.")
        return self._given[name]


def cypher_json(value: Any) -> str:
    """
    The value as JSON text, as drivine4j writes a parameter: compact, a date, a time or a moment as its ISO 8601 text, a
    zoned one with its offset, and a timedelta as an ISO 8601 duration, as the Neo4j driver sends each. A set, a tuple
    or an iterator is a list, and bytes are their base64 text, as drivine4j writes a byte array.
    """
    return json_text(jsonable(value))


def json_text(value: Any) -> str:
    """A value of JSON's own kinds, as `jsonable` gives one, as compact JSON text."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def jsonable(value: Any) -> Any:
    """The value as JSON's own kinds, as `cypher_json` writes it."""
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return _duration(value)
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (list, tuple, set, frozenset, Iterator)):
        return [_jsonable(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _duration(value: timedelta) -> str:
    """
    A timedelta as an ISO 8601 duration of its days and its time, as the Neo4j driver sends one, its days apart:
    `P1DT2H`, `P-1DT23H` for an hour less, `PT0S` for none.
    """
    if not value:
        return "PT0S"
    hours, rest = divmod(value.seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    fraction = Decimal(seconds) + Decimal(value.microseconds) / 1_000_000
    time = "".join(
        f"{amount}{unit}"
        for amount, unit in ((hours, "H"), (minutes, "M"), (format(fraction.normalize(), "f"), "S"))
        if amount and amount != "0"
    )
    return f"P{f'{value.days}D' if value.days else ''}{f'T{time}' if time else ''}"
