"""
Cypher's values as a compiled statement holds them on SQLite, the JSON text of each, and what Cypher does with them
that SQLite's JSON functions do not: compare them, add them, and order them. drivine4j's `SqliteCypherValues`, which
reads and writes them with Jackson, and so does this: a whole number is an int of any size, one written with a point
or an exponent a double (`1.50` is 1.5), a map keeps its keys in the order written, and a double is written as Java
writes one (`1.0E7`). A text a function is given that is SQL's null, or JSON's, is no value.

An infinity is the number SQLite writes for one, `9e999`, which reads as the infinity. NaN, which SQLite's JSON holds as
no number, is NAN_TEXT, `{"$numberDouble":"NaN"}`, as the shared compiler's statements write it: a number equal to
nothing, itself included, and ordered with nothing. Only that one map is NaN: a map with another key beside it, or
another text, as Extended JSON's `{"$numberDouble": "2.0"}`, is a map as any is. As the spec's host.yaml pins it, and
Drivine for TypeScript's `SqliteCypherValues`.
"""

from __future__ import annotations

import json
import math
import struct
from typing import Any, cast

from drivine.backend.sqlite.sqlite_arguments import as_double, java_double


def _refused(constant: str) -> Any:
    raise ValueError(f"{constant} is no JSON value.")


_DECODER = json.JSONDecoder(parse_constant=_refused)
_SPACES = " \t\n\r"

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


def read(text: str | None) -> Any:
    """
    The JSON text read, as Jackson's `readTree` reads it: its first value, what follows a map, a list or a text left
    unread. None where the text is null, JSON's null, or holds only spaces: a None within a list or map is JSON's null.
    Text that is not JSON fails.
    """
    if text is None:
        return None
    start = len(text) - len(text.lstrip(_SPACES))
    if start == len(text):
        return None
    value, end = _DECODER.raw_decode(text, start)
    scalar = not isinstance(value, (str, list, dict))
    if scalar and end < len(text) and text[end] not in _SPACES:
        # Jackson reads a number or a word at the root to its end: `1x` and `01` are not JSON.
        raise ValueError(f"Unexpected character {text[end]!r} after the root value of JSON text: {text}")
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


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numeric(value: Any) -> bool:
    """Whether the value is a number, NaN among them."""
    return is_number(value) or is_nan(value)


def _number_order(a: int | float, b: int | float) -> int:
    """
    Where the number `a` comes beside `b`, by their exact values, as drivine4j compares them as `BigDecimal`s: a whole
    number past 2^53 beside a double by every digit, as Python compares an int and a float. An infinity is the float
    it is.
    """
    return 0 if a == b else -1 if a < b else 1


def equal(a: Any, b: Any) -> bool:
    """
    Whether `a` and `b` are equal as Cypher compares them: a number equal to a number of the same value whatever its
    kind (`1` and `1.0`), a list to a list of equal items in the same order, a map to a map of the same keys with equal
    values in any order, and nothing to a value of another kind: NaN to nothing, itself included. JSON's null is equal
    to itself here: see compare.
    """
    if is_nan(a) or is_nan(b):
        return False
    if is_number(a) and is_number(b):
        return _number_order(a, b) == 0
    if isinstance(a, list) and isinstance(b, list):
        x, y = cast(list[Any], a), cast(list[Any], b)
        return len(x) == len(y) and all(equal(i, j) for i, j in zip(x, y, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        m, n = cast(dict[str, Any], a), cast(dict[str, Any], b)
        return len(m) == len(n) and all(key in n and equal(value, n[key]) for key, value in m.items())
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return False
    return type(cast(object, a)) is type(cast(object, b)) and a == b


def compare(a: Any, b: Any) -> bool | None:
    """
    Whether `a` and `b` are equal as Cypher compares values that may be or hold a null: None where either is null; a
    list to a list, and a map to a map, item by item and key by key, False where a pair is False (lists of different
    lengths and maps of different keys among them), else None where a pair is None, else True; NaN False beside any;
    any other two as equal compares them.
    """
    if a is None or b is None:
        return None
    if is_nan(a) or is_nan(b):
        return False
    if isinstance(a, list) and isinstance(b, list):
        x, y = cast(list[Any], a), cast(list[Any], b)
        return False if len(x) != len(y) else _all(list(zip(x, y, strict=True)))
    if isinstance(a, dict) and isinstance(b, dict):
        m, n = cast(dict[str, Any], a), cast(dict[str, Any], b)
        if len(m) != len(n) or any(key not in n for key in m):
            return False
        return _all([(value, n[key]) for key, value in m.items()])
    return equal(a, b)


def _all(pairs: list[tuple[Any, Any]]) -> bool | None:
    unknown = False
    for x, y in pairs:
        pair = compare(x, y)
        if pair is False:
            return False
        unknown = unknown or pair is None
    return None if unknown else True


def contains(props: Any, map: Any) -> bool:
    """Whether the map `props` holds each entry of the map `map`, with an equal value: a null is held only by a null."""
    if not isinstance(props, dict) or not isinstance(map, dict):
        return False
    held, wanted = cast(dict[str, Any], props), cast(dict[str, Any], map)
    return all(key in held and equal(held[key], value) for key, value in wanted.items())


_NOTHING = object()


def add(a: Any, b: Any) -> Any:
    """
    `a + b`, as Cypher adds two values: numbers summed, a whole number where both are, of any size, exactly, NaN among
    them, and a sum of floats past a double's range an infinity; lists joined, or an item put at a list's end or start;
    a text and a text or a number written one after the other, NaN and an infinity as Java writes them. The module's
    NOTHING for any other two.

    drivine4j adds two whole numbers in a long, and in a BigInteger where they overflow one; one already past a long it
    adds by its low 64 bits (liberation-data/drivine4j#547). Here, as in Drivine for TypeScript, they are added exactly.
    """
    if _numeric(a) and _numeric(b):
        if isinstance(a, int) and isinstance(b, int):
            return a + b
        return _double(a) + _double(b)
    if isinstance(a, list):
        return [*cast(list[Any], a), *(cast(list[Any], b) if isinstance(b, list) else [b])]
    if isinstance(b, list):
        return [a, *cast(list[Any], b)]
    if (isinstance(a, str) or _numeric(a)) and (isinstance(b, str) or _numeric(b)):
        return _added_text(a) + _added_text(b)
    return NOTHING


def _double(value: Any) -> float:
    """A number as a double: NaN for NaN's map."""
    return math.nan if is_nan(value) else as_double(value)


def _added_text(value: Any) -> str:
    """The text a text or a number is put together with another as: NaN's map as `NaN`."""
    return NAN_VALUE if is_nan(value) else as_text(value)


NOTHING: Any = _NOTHING
"""What add gives of two values Cypher does not add."""


def as_text(value: str | int | float) -> str:
    """A number or a text as Jackson's `asText` writes it: a double as Java writes one."""
    if isinstance(value, str):
        return value
    return java_double(value) if isinstance(value, float) else str(value)


def _utf16(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def canonical(value: Any) -> Any:
    """
    The value with each map in it, at any depth, with its keys in order: by their UTF-16 units, as Kotlin's
    `sortedBy` orders texts.
    """
    if isinstance(value, dict):
        entries = cast(dict[str, Any], value)
        return {key: canonical(entries[key]) for key in sorted(entries, key=_utf16)}
    if isinstance(value, list):
        return [canonical(it) for it in cast(list[Any], value)]
    return value


def order(a: Any, b: Any) -> int | None:
    """
    Where `a` comes beside `b` as Cypher orders two values in a comparison (`<`, `>=` …): -1 before, 0 equal, 1 after.
    A list beside a list item by item, the first pair that is not equal deciding, and a list that the other starts with
    first; a number beside a number, a text beside a text by its code points, and a truth value beside a truth value.
    None where they are not ordered: a null, NaN, values of two kinds, or two maps that differ, which Cypher orders and
    the compiler does not yet. An infinity is ordered as the float it is.
    """
    if a is None or b is None or is_nan(a) or is_nan(b):
        return None
    if isinstance(a, list) and isinstance(b, list):
        x, y = cast(list[Any], a), cast(list[Any], b)
        for i in range(min(len(x), len(y))):
            pair = order(x[i], y[i])
            if pair != 0:
                return pair
        return _sign(len(x) - len(y))
    if isinstance(a, dict) and isinstance(b, dict):
        return 0 if compare(a, b) is True else None
    if isinstance(a, bool) and isinstance(b, bool):
        return _sign(int(a) - int(b))
    if is_number(a) and is_number(b):
        return _number_order(cast(float, a), cast(float, b))
    if isinstance(a, str) and isinstance(b, str):
        # A Python text compares by its code points, as Cypher orders texts.
        return 0 if a == b else -1 if a < b else 1
    return None


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


_END = "0"
"""The end of a list in a sort key, which comes before any item's rank."""


def sort_key(text: str) -> str | None:
    """
    The JSON text as text that orders, as SQLite orders text, as Cypher orders lists, where it is a list: item by item,
    a list that begins another first, and items of different kinds as Cypher orders kinds (a map, a list, a text, a
    truth value, a number, then null last). Each item is its kind's rank, then itself: a number by the bits of its
    double, so their text orders as the numbers do, NaN after them all, an infinity's too; a text up to a mark below
    any character; a list its own items, then its end, which comes before any item. None where the value is no value; a
    value that is no list, its text.
    """
    value = read(text)
    if value is None:
        return None
    if not isinstance(value, list):
        return text
    out: list[str] = []
    _key_of_list(cast(list[Any], value), out)
    return "".join(out)


def _key_of_list(items: list[Any], out: list[str]) -> None:
    for item in items:
        _key_of_item(item, out)
    out.append(_END)


def _key_of_item(item: Any, out: list[str]) -> None:
    if is_nan(item):
        # NaN is a number, whose bits come after every other's, an infinity's too.
        out.append("5")
        out.append(_key_of_number(math.nan))
    elif isinstance(item, dict):
        out.append("1")
        _key_of_text(write(canonical(item)), out)
    elif isinstance(item, list):
        out.append("2")
        _key_of_list(cast(list[Any], item), out)
    elif isinstance(item, str):
        out.append("3")
        _key_of_text(item, out)
    elif isinstance(item, bool):
        out.append("41" if item else "40")
    elif is_number(item):
        out.append("5")
        out.append(_key_of_number(as_double(item)))
    else:
        out.append("6")


def _key_of_text(text: str, out: list[str]) -> None:
    """The text, its two lowest characters escaped, then a mark below any character it holds."""
    out.append(text.replace("\u0002", "\u0002\u0003").replace("\u0001", "\u0002\u0002"))
    out.append("\u0001")


def _key_of_number(value: float) -> str:
    """The double's bits as 16 hexadecimal digits that order as the doubles do: a positive one's sign set, a negative
    one's bits flipped."""
    bits = struct.unpack(">q", struct.pack(">d", 0.0 if value == 0.0 else value))[0]
    if math.isnan(value):
        bits = 0x7FF8000000000000
    ordered = ~bits if bits < 0 else bits | -(2**63)
    return f"{ordered & 0xFFFFFFFFFFFFFFFF:016X}"
