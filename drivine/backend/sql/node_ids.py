"""
A node's id as a SQL store holds it, in the store layout 2 (liberation-data/drivine-cypher-spec#161), and an id given to
find a node by, made into that: drivine4j's `NodeIds`, and Drivine for TypeScript's.

A node holds its id as JSON: a number as a number, a text as a text, a temporal value as its tag. A range index or a
uniqueness constraint is over that JSON (Postgres) or over its class and key (Oracle and SQLite), so a statement that
finds a node by its id compares the property with the id's JSON, as the shared module writes the comparison (`has_id`),
and the index answers it.

An id given as text, as a load or a delete by id may give it, is first made a value of the NodeId field's own type
(`typed_id`). Text that is no value of the type, `"abc"` for an int id, finds no node, as on Neo4j, where it is never
equal to a number: no statement is run for it.
"""

from __future__ import annotations

import json
import logging
import re
import types
import typing
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, cast

from drivine.backend.sql.sql_graph import SqlGraph
from drivine.backend.sql.value_layout import held_key
from drivine.cypher.shared_compiler import id_sql
from drivine.model.fragment_model import FragmentModel

logger = logging.getLogger("drivine.node_ids")


class _NoNode:
    """What `typed_id` gives for an id that finds no node: None, or text that is no value of the id's type."""

    def __repr__(self) -> str:
        return "NO_NODE"


NO_NODE: Any = _NoNode()
"""What `typed_id` gives for an id that finds no node: None, or text that is no value of the id's type."""

_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?", re.ASCII)
"""
A decimal number's text, as JSON writes one: no hexadecimal, no spaces, no sign but a minus, no empty text, and only
ASCII digits (`\\d` alone matches any script's, which `int` and `float` read too).
"""

_INTEGER = re.compile(r"-?(0|[1-9]\d*)", re.ASCII)
"""A whole number's text, as JSON writes one: no fraction and no exponent."""

_DATE = r"\d{4}-\d{2}-\d{2}"
_TIME = r"(?P<hour>\d{2}):\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?"
_ISO_DATE = re.compile(_DATE)
_ISO_TIME = re.compile(_TIME)
_ISO_DATE_TIME = re.compile(f"{_DATE}T{_TIME}")
"""
ISO 8601 text of a date, a time and a date and time, in the extended form a save writes, to the microsecond Python
holds: `fromisoformat` alone reads more (the basic form `20240101`, a week date, a space for the `T`, an hour alone)
and rolls the hour 24 into the next day, which drivine4j's strict ISO reading refuses.
"""

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
"""A UUID's text as Jackson reads one: its 36 characters, its hex digits of either case."""


def typed_id(model: FragmentModel, given: Any) -> Any:
    """
    `given` as a node of `model` holds its id: text given for an id of another type read as that type, the NodeId
    field's annotation (`model.node_id_type`). An int field's `"6"` is 6, a float field's `"6.5"` 6.5, a Decimal field's
    `"6.5"` that Decimal, a bool field's `"true"` True, a date, datetime or time field's ISO text that value, and a UUID
    field's text that UUID. Any other value, and text for a `str` id or one of another type, is as it is given. NO_NODE
    for None, and for text that is no value of the type.
    """
    if given is None:
        return NO_NODE
    if not isinstance(given, str):
        return given
    read = _reader(model.node_id_type)
    return read(given) if read is not None else given


def _reader(kind: Any) -> Callable[[str], Any] | None:
    """How text is read as a value of `kind`, the first of the readers whose type it is; None to keep it as text."""
    kind = _unwrapped(kind)
    if not isinstance(kind, type) or issubclass(kind, str):
        return None
    for it, read in _READERS:
        if issubclass(kind, it):
            return read
    return None


def _unwrapped(kind: Any) -> Any:
    """`kind` without None beside it: `int | None` is `int`. Any other union is as it is, and read as no one type."""
    if typing.get_origin(kind) in (typing.Union, types.UnionType):
        others = [it for it in typing.get_args(kind) if it is not type(None)]
        return others[0] if len(others) == 1 else kind
    return kind


def _int(text: str) -> Any:
    """
    A whole number's text as the int it is, `"6"`; NO_NODE otherwise, `"6.0"` and `"6e1"` too, as drivine4j's Jackson
    refuses them for a Long field. Drivine for TypeScript reads them for a `number` field, a double, as it does for a
    float field here; its `bigint` field is read as this is.
    """
    return int(text) if _INTEGER.fullmatch(text) else NO_NODE


def _float(text: str) -> Any:
    if not _NUMBER.fullmatch(text):
        return NO_NODE
    number = float(text)
    return number if number not in (float("inf"), float("-inf")) else NO_NODE


def _decimal(text: str) -> Any:
    """
    Number text as the Decimal it is, as drivine4j's Jackson reads text for a BigDecimal field (`new BigDecimal`): its
    digits as written, NaN and the infinities not being one.
    """
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", text):
        return NO_NODE
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the pattern admits only what Decimal reads
        return NO_NODE


def _bool(text: str) -> Any:
    return True if text == "true" else False if text == "false" else NO_NODE


def _iso(pattern: re.Pattern[str], read: Callable[[str], Any]) -> Callable[[str], Any]:
    """ISO text that `pattern` matches whole, of an hour before 24, read by `read`; NO_NODE for any other text."""

    def reading(text: str) -> Any:
        matched = pattern.fullmatch(text)
        if matched is None or int(matched.groupdict().get("hour") or 0) > 23:
            return NO_NODE
        try:
            return read(text)
        except ValueError:  # A day the month hasn't, a minute past 59.
            return NO_NODE

    return reading


def _uuid(text: str) -> Any:
    return uuid.UUID(text) if _UUID.fullmatch(text) else NO_NODE


_READERS: list[tuple[type, Callable[[str], Any]]] = [
    # bool before int, which it is a kind of; datetime before date, likewise.
    (bool, _bool),
    (int, _int),
    (float, _float),
    (Decimal, _decimal),
    (datetime, _iso(_ISO_DATE_TIME, datetime.fromisoformat)),
    (date, _iso(_ISO_DATE, date.fromisoformat)),
    (time, _iso(_ISO_TIME, time.fromisoformat)),
    (uuid.UUID, _uuid),
]
"""How text is read as a value of each type an id's text is read as; NO_NODE where it is no value of the type."""


def is_sql_backend(backend: Any) -> bool:
    """Whether `backend` keeps its graph in SQL tables, where an id is found as `typed_id` reads it."""
    return isinstance(getattr(backend, "graph", None), SqlGraph)


def lookup_id(backend: Any, model: FragmentModel, given: Any) -> Any:
    """
    The id a lookup of a node of `model` by `given` binds on `backend`: on a SQL store `given` read as the id's type
    (`typed_id`), and NO_NODE where it finds no node, which the caller runs no statement for, saying why at DEBUG where
    the text can't be read (a None id says nothing); on a graph engine `given` as it is, as the engine compares it.
    """
    if not is_sql_backend(backend):
        return given
    id_ = typed_id(model, given)
    if id_ is NO_NODE and given is not None:
        logger.debug(
            "No %s is looked for by the id %r: it is no value of %s's type, so no node has it.",
            model.type.__name__,
            given,
            model.node_id_field,
        )
    return id_


def has_id(store: str, alias: str, property: str, json_sql: str) -> str:
    """
    Whether the property `property` of the node row `alias` is the id whose JSON, as the node holds it, is `json_sql`,
    the SQL of a bound value: the shared module's comparison (`id_sql`), which the store's index over the id answers.
    """
    return id_sql(store, "hasId", alias, held_key(property), json_sql)


def temporal_text(layout: str) -> str | None:
    """
    The id as the text a write plan keys its node by, within the plan, where `layout` is the JSON text the store's
    layout holds it as: a temporal value's tag (`{"$t": …, "v": …}`) as its text `v`, as the module writes it and reads
    a stored target's id back as (drivine4j's `NodeIds.temporalText`); None for any other id.
    """
    if not layout.startswith('{"$t"'):
        return None
    tag: Any = json.loads(layout)
    text: Any = cast(dict[str, Any], tag).get("v") if isinstance(tag, dict) else None
    return text if isinstance(text, str) else None


def read_back_key(id: Any) -> Any:
    """
    `id` as a batch reads a node's id back from the store, where it hands each object its stamp: a temporal id (a date,
    a time or a duration) as its tag's text `v`, which is what a tag is read as (see value_layout.read), a UUID as its
    text, a Decimal with a fraction as the float it is held as and an Enum as its value is, as each is read back, and
    any other id as it is. A date id the object holds as a `date` is read back as its text, so the two are matched by
    that text.
    """
    if isinstance(id, Enum):
        return read_back_key(id.value)
    if isinstance(id, uuid.UUID):
        return str(id)
    if isinstance(id, Decimal) and id.is_finite() and id != id.to_integral_value():
        return float(id)
    if not isinstance(id, (date, time, timedelta)):
        return id
    from drivine.backend.sql.sql_json import to_json
    from drivine.backend.sql.value_layout import write

    return temporal_text(to_json(write(id, "sqlite"))) or id
