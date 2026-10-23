from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

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
        as_json: Callable[[str], str],
        as_count: Callable[[str], str],
        in_object: bool = False,
    ) -> None:
        """
        `as_json` is the bound JSON text at a marker, or with `in_object` the parameter of a name, as the store's JSON;
        `as_count` the bound whole number at a marker, or with `in_object` the parameter of a name, as the store's
        number.
        """
        self._given = given
        self._as_json = as_json
        self._as_count = as_count
        self._in_object = in_object
        self._bound: dict[str, Any] = {}

    @property
    def values(self) -> dict[str, Any]:
        """What the SQL binds."""
        return {OBJECT: cypher_json(dict(self._given))} if self._in_object else self._bound

    def json(self, name: str) -> str:
        """The parameter, as JSON."""
        value = self.value(name)
        if self._in_object:
            return self._as_json(name)
        key = f"_cy_{name}"
        self._bound[key] = None if value is None else cypher_json(value)
        return self._as_json(f"${key}")

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
    zoned one with its offset, and a timedelta as an ISO 8601 duration, as the Neo4j driver sends each.
    """
    return json.dumps(_jsonable(value), separators=(",", ":"), ensure_ascii=False)


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
    if isinstance(value, (list, tuple, set, frozenset)):
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
