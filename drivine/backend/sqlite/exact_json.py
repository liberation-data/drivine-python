"""
JSON read and written with its numbers as the decimals they are written as: so a property no step writes keeps its text,
exactly (`1e3` stays `1e3`, `-0` stays `-0`), and two values are the same where Postgres's `jsonb` says so (`1.5` is
`1.50`). Drivine for TypeScript keeps the text alike; drivine4j writes such a number as Java's BigDecimal does, `1E+3`
(liberation-data/drivine4j#541). The text written is compact, and leaves unescaped what JSON does not require escaped,
as drivine4j's Jackson writes it: a label is found by its text.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any, cast


class _Decimal(Decimal):
    """A number with a fraction or an exponent, which keeps the text it was read from."""

    text: str

    def __new__(cls, text: str) -> _Decimal:
        number = super().__new__(cls, text)
        number.text = text
        return number


class _Int(int):
    """A whole number, which keeps the text it was read from."""

    text: str

    def __new__(cls, text: str) -> _Int:
        number = super().__new__(cls, text)
        number.text = text
        return number


def loads(text: str) -> Any:
    """The JSON text, each number as the decimal it is written as, keeping that text."""
    return json.loads(text, parse_float=_Decimal, parse_int=_Int)


def dumps(value: Any) -> str:
    """The value as compact JSON, a Decimal as the text it was read from."""
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
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, (_Int, _Decimal)):
        out.append(value.text)
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{value} is no JSON number.")
        out.append(str(value))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value} is no JSON number.")
        out.append(repr(value))
    elif isinstance(value, dict):
        out.append("{")
        for i, (key, item) in enumerate(cast(dict[str, Any], value).items()):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _write(item, out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(cast(list[Any], value)):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise TypeError(f"{type(value).__name__} is not JSON.")


def same(a: Any, b: Any) -> bool:
    """
    Whether `a` and `b` hold the same JSON: a number equal to a number however it is written, and a boolean only a
    boolean, never the number Python takes it for.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, Decimal, float)) and isinstance(b, (int, Decimal, float)):
        return Decimal(a) == Decimal(b)
    if isinstance(a, dict) and isinstance(b, dict):
        x, y = cast(dict[str, Any], a), cast(dict[str, Any], b)
        return x.keys() == y.keys() and all(same(x[key], y[key]) for key in x)
    if isinstance(a, list) and isinstance(b, list):
        x, y = cast(list[Any], a), cast(list[Any], b)
        return len(x) == len(y) and all(same(i, j) for i, j in zip(x, y, strict=True))
    if type(a) is not type(b):  # pyright: ignore[reportUnknownArgumentType]
        return False
    return a == b
