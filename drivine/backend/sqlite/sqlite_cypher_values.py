"""
Cypher's values as a compiled statement's rows hold them on SQLite, the JSON text of each: written as drivine4j's
`SqliteCypherValues` writes them with Jackson, a double as Java writes one (`1.0E7`), for the functions drivine-python
registers itself (see sqlite_cypher_functions). The shared module's extension compares, adds and orders them.

An infinity is the number SQLite writes for one, `9e999`, which reads as the infinity. NaN, which SQLite's JSON holds as
no number, is NAN_TEXT, `{"$numberDouble":"NaN"}`, as the shared compiler's statements write it. Only that one map is
NaN: a map with another key beside it, or another text, as Extended JSON's `{"$numberDouble": "2.0"}`, is a map as any
is. As the spec's host.yaml pins it.
"""

from __future__ import annotations

import math
from typing import Any, cast

from drivine.backend.sqlite.sqlite_arguments import java_double

NAN_KEY = "$numberDouble"
NAN_VALUE = "NaN"
NAN_TEXT = '{"$numberDouble":"NaN"}'
"""NaN as SQLite's JSON text holds it, where its JSON holds no NaN: the one map that is NaN."""

INFINITY = "9e999"
"""An infinity as SQLite's JSON writes it, a number past any double's, which reads as the infinity."""


def is_nan(value: Any) -> bool:
    """Whether the JSON value is NaN: the map of NAN_TEXT, and no other."""
    if not isinstance(value, dict):
        return False
    entries = cast(dict[str, Any], value)
    return len(entries) == 1 and entries.get(NAN_KEY) == NAN_VALUE


def with_nan(value: Any) -> Any:
    """The JSON value with each map in it, at any depth, that is exactly NAN_TEXT's, NaN."""
    if isinstance(value, list):
        return [with_nan(it) for it in cast(list[Any], value)]
    if isinstance(value, dict):
        if is_nan(value):
            return math.nan
        return {key: with_nan(it) for key, it in cast(dict[str, Any], value).items()}
    return value


def write(value: Any) -> str:
    """The value as JSON text, as Jackson writes it: compact, a double as Java writes one."""
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(quoted(value))
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        # NaN and the infinities as SQLite's JSON holds them, so a value written is read back as the number it is.
        if math.isnan(value):
            out.append(NAN_TEXT)
        elif math.isinf(value):
            out.append(INFINITY if value > 0 else f"-{INFINITY}")
        else:
            out.append(java_double(value))
    elif isinstance(value, dict):
        out.append("{")
        for i, (key, item) in enumerate(cast(dict[str, Any], value).items()):
            if i:
                out.append(",")
            out.append(quoted(key))
            out.append(":")
            _write(item, out)
        out.append("}")
    else:
        out.append("[")
        for i, item in enumerate(cast(list[Any], value)):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")


_SHORT_ESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r", '"': '\\"', "\\": "\\\\"}


def quoted(text: str) -> str:
    """The text as Jackson writes a JSON text: a control character as `\\n`, or `\\u001F` in upper case."""
    written = ['"']
    for c in text:
        if c in _SHORT_ESCAPES:
            written.append(_SHORT_ESCAPES[c])
        elif c < " ":
            written.append(f"\\u{ord(c):04X}")
        else:
            written.append(c)
    written.append('"')
    return "".join(written)
