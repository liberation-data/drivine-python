"""
An argument of a function Drivine registers on a SQLite connection, read as SQLite's `sqlite3_value_text`,
`sqlite3_value_double` and `sqlite3_value_int64` read one, as drivine4j's functions read theirs. Python's `sqlite3`
hands a function each argument as the value SQLite holds: an int, a float, a str, bytes or None.

A real number read as text is written as the SQLite drivine4j links (3.53) writes one: to 15 significant digits where
those read back as the same double, else to 17. SQLite works the 17 out by its own arithmetic, which may miss the
exact decimal in the last digit (`1.0 / 3` is `0.33333333333333332`, exactly `…31`): here they are the exact digits.
Either reads back as the same double. SQLite 3.49, which Python 3.13 links, writes 15 digits always; a function here is
given the double itself, so it gives what drivine4j does whatever SQLite Python links.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any, cast

MIN_LONG = -(2**63)
MAX_LONG = 2**63 - 1

_NUMERIC = re.compile(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
"""The number a text starts with, as SQLite reads one: spaces before it allowed."""

_WHOLE = re.compile(r"\s*([+-]?)(\d+)")
"""The whole number a text starts with, as SQLite's `sqlite3Atoi64` reads one."""


def text(value: Any) -> str | None:
    """The argument as text, as `sqlite3_value_text` gives it: null where it is null."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return real_text(value)
    return bytes(cast(bytes, value)).decode("utf-8", "replace")


def real(value: Any) -> float:
    """The argument as a real number, as `sqlite3_value_double` gives it: a text's leading number, else 0."""
    if value is None:
        return 0.0
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return _float(value)
    found = _NUMERIC.match(text(value) or "")
    return float(found.group(0)) if found else 0.0


def integer(value: Any) -> int:
    """
    The argument as a whole number, as `sqlite3_value_int64` gives it: a real one cut toward zero within a long, a
    text's leading whole number, clamped to a long.
    """
    if value is None:
        return 0
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return 0
        if value <= MIN_LONG:
            return MIN_LONG
        if value >= 2**63:
            return MAX_LONG
        return int(value)
    found = _WHOLE.match(text(value) or "")
    if found is None:
        return 0
    whole = int(found.group(2)) * (-1 if found.group(1) == "-" else 1)
    return max(MIN_LONG, min(MAX_LONG, whole))


def int32(value: int) -> int:
    """A long's lowest 32 bits, as Kotlin's `toInt()` and `sqlite3_value_int` keep them."""
    low = value & 0xFFFFFFFF
    return low - 2**32 if low >= 2**31 else low


def is_real(value: Any) -> bool:
    """Whether SQLite holds the argument as a real number."""
    return isinstance(value, float)


def is_integer(value: Any) -> bool:
    """Whether SQLite holds the argument as a whole number."""
    return isinstance(value, int) and not isinstance(value, bool)


def real_text(value: float) -> str:
    """
    The real number as the SQLite drivine4j links writes it as text, `%!.15g` where that reads back as the number,
    else `%!.17g`: a point and a digit after it at least, `2.0`, and an exponent of two digits at least, `1.0e+20`.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    written = f"{value:.15g}"
    if float(written) != value:
        written = f"{value:.17g}"
    mantissa, _, exponent = written.partition("e")
    if "." not in mantissa and mantissa not in ("inf", "-inf"):
        mantissa += ".0"
    return f"{mantissa}e{exponent}" if exponent else mantissa


def java_double(value: float) -> str:
    """
    A double as Java's `Double.toString` writes it (and so Jackson): from its shortest decimal, with no exponent from
    10^-3 up to 10^7, a point and a digit after it at least, `2.0`, and else as `1.0E7`, `1.2E-4`; NaN and the
    infinities by Java's names.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0.0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    shortest = Decimal(float.__repr__(value))
    if 1e-3 <= abs(value) < 1e7:
        written = format(shortest, "f")
        return written if "." in written else f"{written}.0"
    # The shortest digits, with the point after the first. Java writes two at least, the two nearest the double where
    # one would do: 5e-324 as 4.9E-324.
    sign, figures, power = shortest.as_tuple()
    digits = "".join(str(it) for it in figures).rstrip("0") or "0"
    exponent = cast(int, power) + len(figures) - 1
    if len(digits) == 1:
        mantissa, exponent_text = f"{abs(value):.1e}".split("e")
        return f"{'-' if sign else ''}{mantissa}E{int(exponent_text)}"
    return f"{'-' if sign else ''}{digits[0]}.{digits[1:]}E{exponent}"


def java_round(value: float) -> int:
    """Java's `Math.round(double)`: the nearest long, a half toward positive infinity; NaN is 0, past a long its end."""
    if math.isnan(value):
        return 0
    if value >= 2**63:
        return MAX_LONG
    if value <= MIN_LONG:
        return MIN_LONG
    floor = math.floor(value)
    return floor + 1 if value - floor >= 0.5 else floor


def _float(value: int) -> float:
    """A whole number as the nearest double, as Java's `BigInteger.doubleValue`: past a double's range, an infinity."""
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


as_double = _float
