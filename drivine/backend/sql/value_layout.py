"""
Values as store layout 2 holds them on a SQL store (drivine-cypher-spec#161), written and read by the shared module, as
drivine4j's `ValueLayout` and Drivine for TypeScript's write and read them, so the three libraries hold and read a value
alike. host.yaml's write and read steps pin it.

- A value a save writes (`write`): each temporal value its tag (`{"$t": "date", "v": "2024-01-01", "s": …}`), each map
  key that begins with `$` with one more `$`, at any depth, bytes the list of their signed values, and each float as the
  store holds it: on Postgres a whole one with its fraction, on SQLite an infinity as `9e999`; NaN and an infinity the
  store holds as no number are refused by name. It is the store's JSON text, spliced into the statement's own as it is
  (see `Held`, and sql_json.to_json), so a float's text is never read back and written again otherwise.
- A column a row gives (`read`): each tag read as its text and each escaped key unescaped, at any depth.

The module is asked only of a value or a column it has something to decide of: a save's value that holds nothing but
whole numbers, texts, booleans and nulls under keys that don't begin with `$` is written as it is, and a column whose
text holds neither `"$` nor `\\u0024` is read as it is. The module's ValueLayoutTest pins that every tag and escaped key
it writes holds `"$`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, cast

from pydantic_core import to_jsonable_python

from drivine.drivine_error import DrivineError

SqlStore = Literal["postgres", "oracle", "sqlite"]

ORACLE_SAVES_AS: SqlStore = "postgres"
"""
How a save's values are written on Oracle until drivine4j settles a save's floats there (liberation-data/drivine4j#830):
each float a JSON number, as Drivine for TypeScript writes them, and NaN and the infinities refused by name. Oracle's
own spelling, `{"$numberDouble": …}`, needs the save's PL/SQL to parse its binds as extended JSON, which the goldens it
shares with Drivine for TypeScript don't yet. Tags, escaped keys and bytes are the module's as on every store.

Held, and tracked by test_value_layout's ORACLE_FLOATS: a saved 2.0 reads back as 2 on Oracle until the saves take
#830's binary-double form, which they do, with Drivine for TypeScript and the shared goldens, before 0.9.0 ships.
"""


@dataclass(frozen=True)
class Held:
    """A value as the store's JSON text, spliced into a statement's JSON as it is (see sql_json.to_json)."""

    text: str


def store_of(graph: Any) -> SqlStore:
    """The store a graph of a SQL backend is kept on, as the module names it."""
    from drivine.backend.oracle.oracle_graph import OracleGraph
    from drivine.backend.sqlite.sqlite_graph import SqliteGraph

    if isinstance(graph, SqliteGraph):
        return "sqlite"
    return "oracle" if isinstance(graph, OracleGraph) else "postgres"


def held_key(name: str) -> str:
    """A property's name as the store layout holds it: one more `$` before a name that begins with one."""
    return f"${name}" if name.startswith("$") else name


def write(value: Any, store: SqlStore) -> Any:
    """`value` as a save writes it on `store` (see the module): as it is, or the module's text of it, `Held`."""
    prepared = _prepared(value)
    if _plain(prepared):
        return prepared
    from drivine.cypher.shared_compiler import module_text, written

    asked = ORACLE_SAVES_AS if store == "oracle" else store
    given = module_text({"write": written(prepared), "store": asked}, store)
    opening = '{"value":'
    if not given.startswith(opening):
        error = json.loads(given).get("error", given)
        if asked != store:
            # Oracle's saves written as Postgres's are (ORACLE_SAVES_AS): its refusal names the store it is.
            error = error.replace("Postgres's `jsonb`", "a save on Oracle")
        raise DrivineError(f"A value can't be written in the store layout: {error}")
    return Held(given[len(opening) : -1])


def write_props(props: Mapping[str, Any], store: SqlStore) -> dict[str, Any]:
    """The properties a save writes, each by its name as the layout holds it, and each value as `write` writes it."""
    return {held_key(name): write(value, store) for name, value in props.items()}


def _prepared(value: Any) -> Any:
    """
    `value` with what the module isn't given as it is made plain, as a save has written it: an Enum its value, a list,
    tuple, set or iterator the list of its items, a map's keys texts, and any other object (a UUID, a model) its JSON
    as pydantic writes it. The kinds the module writes otherwise are kept: floats, Decimals, temporal values and bytes.
    """
    if value is None or isinstance(value, (bool, int, float, str, Decimal, date, time, timedelta)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(cast(Any, value))
    if isinstance(value, Enum):
        return _prepared(value.value)
    if isinstance(value, Mapping):
        return {str(key): _prepared(it) for key, it in cast(Mapping[Any, Any], value).items()}
    if isinstance(value, (list, tuple, set, frozenset, Iterator)):
        return [_prepared(it) for it in cast(Any, value)]
    return _prepared(to_jsonable_python(value))


def _plain(value: Any) -> bool:
    """Whether the layout holds `value` as plain JSON writes it: only whole numbers, texts, booleans and nulls."""
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, Decimal):
        return value.is_finite() and value == value.to_integral_value()
    if isinstance(value, Mapping):
        entries = cast(Mapping[str, Any], value)
        return all(not key.startswith("$") and _plain(it) for key, it in entries.items())
    if isinstance(value, list):
        return all(_plain(it) for it in cast(list[Any], value))
    # A float, a temporal value or bytes: each store holds it its own way.
    return False


def read(text: str, parse: Callable[[str], Any] = json.loads) -> Any:
    """
    A column's JSON text read in the store layout: as `parse` reads it, then, where it holds anything of the layout,
    by the module, so a number `parse` reads its own way (a whole number past a long's range a float) is read alike
    whether or not the column holds a tag or an escaped key.
    """
    if '"$' not in text and "\\u0024" not in text:
        return parse(text)
    return _read_by_module(text) if parse is json.loads else _read_through(parse(text))


def read_after(text: str, raw: Callable[[Any], Any], parse: Callable[[str], Any] = json.loads) -> Any:
    """
    A column read in the store layout, where the store writes some values its own way in the column's JSON (SQLite's
    NaN, `{"$numberDouble": "NaN"}`): `raw` reads those on the column as `parse` read it, where such a map's key has one
    `$` and a map of the user's with that key has two, as the layout escapes it; the module then reads the rest. Read
    the other way round, the module's unescaping would hand `raw` a user's map as the store's own.
    """
    value = raw(parse(text))
    if '"$' not in text and "\\u0024" not in text:
        return value
    return _read_through(value)


_FLOAT = "\u0000drivine:float:"
"""What a float JSON would misread stands for while it crosses to the module and back: NaN, an infinity, -0.0."""

_TEXT = "\u0000drivine:text:"
"""What a text that begins as a stand-in does crosses with before it, so it comes back the text it was."""


def _read_through(value: Any) -> Any:
    """`value`, read already, read in the store layout by the module, each float JSON would misread kept as it is."""
    return _unmarked(_read_by_module(json.dumps(_marked(value), ensure_ascii=False, separators=(",", ":"))))


def _marked(value: Any) -> Any:
    if isinstance(value, float) and (
        math.isnan(value) or math.isinf(value) or (not value and math.copysign(1, value) < 0)
    ):
        return f"{_FLOAT}{value!r}"
    if isinstance(value, str) and (value.startswith(_FLOAT) or value.startswith(_TEXT)):
        return f"{_TEXT}{value}"
    if isinstance(value, Mapping):
        return {key: _marked(it) for key, it in cast(Mapping[str, Any], value).items()}
    if isinstance(value, list):
        return [_marked(it) for it in cast(list[Any], value)]
    return value


def _unmarked(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith(_FLOAT):
            return float(value[len(_FLOAT) :])
        return value[len(_TEXT) :] if value.startswith(_TEXT) else value
    if isinstance(value, Mapping):
        return {key: _unmarked(it) for key, it in cast(Mapping[str, Any], value).items()}
    if isinstance(value, list):
        return [_unmarked(it) for it in cast(list[Any], value)]
    return value


def _read_by_module(text: str) -> Any:
    from drivine.cypher.shared_compiler import module_text

    given = json.loads(module_text({"read": text, "typed": False}, "sqlite"))
    if "error" in given:
        raise DrivineError(f"A row's value can't be read in the store layout: {given['error']}")
    return given["value"]
