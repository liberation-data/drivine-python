# apsw's stubs type a bytes value as collections.abc.Buffer, which Python 3.11 lacks: what they give is partly unknown.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""
The functions compiled Cypher calls on SQLite, for what Cypher does with its values, JSON text each, that SQLite does
not: drivine4j's, registered on each connection by `register` (see sqlite_functions.register), by the names in
sqlite_graph, of the same arity and as deterministic as drivine4j's.

- `drivine_equal`, `drivine_contains`, `drivine_canonical` and `drivine_add` compare, group and add Cypher's values
  (see sqlite_cypher_values);
- `drivine_upper`, `drivine_split`, `drivine_reverse` and `drivine_uuid` are Cypher's `toUpper`, `split`, `reverse`
  and `randomUUID`;
- `drivine_math` and `drivine_round` work out Cypher's functions of numbers, `exp`, `log`, `sin` …, and `round` by a
  mode, as Neo4j does;
- `drivine_regex_group` and `drivine_regexp_replace` read and replace what a regular expression matches;
- `drivine_float_text` writes a float as Java does, for `toString`, `drivine_sort_key` orders a list as Cypher does,
  and `drivine_list_order` compares two lists as Cypher does;
- `drivine_trunc`, `drivine_to_char` and `drivine_temporal` work out temporal values (see sqlite_temporal_functions);
- `drivine_cypher` carries out the procedure a statement that writes compiles to (see sqlite_cypher_procedure);
- `drivine_fail` fails the statement with its text, where Neo4j fails on a value it reads.

A function reads its arguments as drivine4j's reads them, as SQLite gives them as text or as a number (see
sqlite_arguments), and a failure in one fails the statement as itself.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    localcontext,
)
from functools import partial
from typing import Any

import apsw

from drivine.backend.sqlite import sqlite_arguments as arguments
from drivine.backend.sqlite import sqlite_cypher_procedure, sqlite_graph
from drivine.backend.sqlite import sqlite_cypher_values as values
from drivine.backend.sqlite.sqlite_patterns import Patterns
from drivine.backend.sqlite.sqlite_temporal_functions import temporal, to_char
from drivine.drivine_error import DrivineError


def register(connection: apsw.Connection, patterns: Patterns) -> None:
    """Registers on the connection the functions compiled Cypher calls."""
    g = sqlite_graph

    def add(name: str, arity: int, function: Any, deterministic: bool = True) -> None:
        connection.create_scalar_function(name, function, arity, deterministic=deterministic)

    add(g.CYPHER, 2, partial(sqlite_cypher_procedure.run, connection), False)
    add(g.EQUAL, 2, equal)
    add(g.CONTAINS, 2, contains)
    add(g.ADD, 2, add_values)
    add(g.CANONICAL, 1, canonical)
    add(g.UPPER, 1, upper)
    add(g.SPLIT, 2, split)
    add(g.REVERSE, 1, reverse)
    add(g.UUID, 0, random_uuid, False)
    add(g.REGEX_GROUP, 3, partial(regex_group, patterns))
    add(g.REGEX_REPLACE, 3, partial(regex_replace, patterns))
    add(g.TRUNC, 1, trunc)
    add(g.TO_CHAR, 2, to_char)
    add(g.FLOAT_TEXT, 1, float_text)
    add(g.SORT_KEY, 1, sort_key)
    add(g.LIST_ORDER, 2, list_order)
    add(g.ROUND, 3, round_by)
    # Not deterministic: SQLite must not work it out before a row that needs it.
    add(g.FAIL, 1, fail, False)
    # Of as many arguments as its operation takes.
    add(g.TEMPORAL, -1, temporal)
    add(g.MATH, -1, math_of)


def _read(value: Any) -> Any:
    return values.read(arguments.text(value))


def _truth(value: bool | None) -> int | None:
    return None if value is None else int(value)


def equal(a: Any, b: Any) -> int | None:
    """
    `drivine_equal(a, b)`: whether the JSON values are equal as Cypher compares them (see sqlite_cypher_values.compare),
    1 or 0; null where either is no value, as a comparison with null is, or where a null they hold makes it null.
    """
    x, y = _read(a), _read(b)
    return None if x is None or y is None else _truth(values.compare(x, y))


def contains(props: Any, map: Any) -> int | None:
    """
    `drivine_contains(props, map)`: whether the JSON map `props` holds each entry of the JSON map `map`, as a pattern's
    map of properties is met, 1 or 0; null where either is no value.
    """
    x, y = _read(props), _read(map)
    return None if x is None or y is None else int(values.contains(x, y))


def add_values(a: Any, b: Any) -> str | None:
    """
    `drivine_add(a, b)`: Cypher's `a + b` of the JSON values, as JSON text (see sqlite_cypher_values.add). A list takes
    a null as an item; any other value added to null is null, as are two values Cypher does not add.
    """
    total = values.add(_read(a), _read(b))
    return None if total is values.NOTHING else values.write(total)


def canonical(json: Any) -> str | None:
    """
    `drivine_canonical(json)`: the JSON text of the value, each map in it with its keys in order, so two maps of the
    same entries are the same text, and group, and are kept once, as Postgres's `jsonb` keeps them. Null where it is
    null; the text itself where it is JSON's null.
    """
    text = arguments.text(json)
    value = values.read(text)
    return text if value is None else values.write(values.canonical(value))


def upper(text: Any) -> str | None:
    """
    `drivine_upper(text)`: the text in upper case, as Unicode raises it, as Java's `toUpperCase(Locale.ROOT)` does
    (`ß` is `SS`): SQLite's own `upper` raises only ASCII.
    """
    written = arguments.text(text)
    return None if written is None else written.upper()


def split(text: Any, separator: Any) -> str | None:
    """
    `drivine_split(text, separator)`: Cypher's `split`, the text cut at each separator, as the JSON text of a list of
    texts: an empty one at each end the text starts or ends with a separator, each character where the separator is
    empty, and the empty text a list of itself. Null where either is null.
    """
    written, by = arguments.text(text), arguments.text(separator)
    if written is None or by is None:
        return None
    parts = [written] if written == "" else list(written) if by == "" else written.split(by)
    return values.write(parts)


def reverse(text: Any) -> str | None:
    """`drivine_reverse(text)`: the text's characters from the last, a character outside the basic plane kept whole."""
    written = arguments.text(text)
    return None if written is None else written[::-1]


def random_uuid() -> str:
    """`drivine_uuid()`: a new random UUID, a version 4 one, as text: Cypher's `randomUUID()`."""
    return str(uuid.uuid4())


def regex_group(patterns: Patterns, text: Any, expression: Any, group: Any) -> str | None:
    """
    `drivine_regex_group(text, expression, group)`: the text the group of that number matches, where the regular
    expression first matches in the text, as Postgres's `regexp_match` gives it; null where it matches nowhere, or the
    group takes no part, or the text or the expression is null. A group of null is the whole match, as the number
    SQLite reads of a null is 0.
    """
    written, java = arguments.text(text), arguments.text(expression)
    if written is None or java is None:
        return None
    found = patterns.of(java).search(written)
    number = arguments.int32(arguments.integer(group))
    if found is None or number > (found.re.groups):
        return None
    if number < 0:
        raise DrivineError(f"No group {number}")
    return found.group(number)


def regex_replace(patterns: Patterns, text: Any, expression: Any, replacement: Any) -> str | None:
    """
    `drivine_regexp_replace(text, expression, replacement)`: the text with each match of the regular expression
    replaced, the replacement naming a group as Postgres's and Oracle's do, `\\1`, and as drivine4j hands it to Java's
    `replaceAll`: a backslash and a digit name a group, and the digits after it a group of more digits where there is
    one; a group that takes no part is nothing; anything else is itself. Null where any is null.
    """
    written, java, by = arguments.text(text), arguments.text(expression), arguments.text(replacement)
    if written is None or java is None or by is None:
        return None
    compiled = patterns.of(java)
    return compiled.sub(lambda found: _replaced(by, found), written)


def _replaced(replacement: str, found: re.Match[str]) -> str:
    """The replacement with each group it names, `\\1`, as the match holds it, as Java's `appendReplacement` does."""
    count = found.re.groups
    written: list[str] = []
    at = 0
    while at < len(replacement):
        c = replacement[at]
        if c != "\\" or at + 1 >= len(replacement) or not replacement[at + 1].isdigit():
            written.append(c)
            at += 1
            continue
        group = int(replacement[at + 1])
        at += 2
        while at < len(replacement) and replacement[at].isdigit() and group * 10 + int(replacement[at]) <= count:
            group = group * 10 + int(replacement[at])
            at += 1
        if group > count:
            raise DrivineError(f"No group {group}")
        written.append(found.group(group) or "")
    return "".join(written)


_SLACK = 2
"""How many units of its last place a real number may miss a whole number by and be it: see trunc."""


def trunc(number: Any) -> int | float | None:
    """
    `drivine_trunc(number)`: the number with its fraction cut off, as a whole number where it fits one, as Postgres's
    and Oracle's `TRUNC` give a number of no fraction: SQLite's own `trunc` gives a real number, which JSON writes
    `2.0`.

    A real number within two units of its last place of a whole number is that whole number. Postgres and Oracle work
    out a fraction of a second exactly, and SQLite in binary, which misses it by that much: 33.001 seconds times a
    million is 33000999.999999996, and those microseconds are 33001000.
    """
    if number is None:
        return None
    if arguments.is_integer(number):
        return int(number)
    real = arguments.real(number)
    if not math.isfinite(real):
        return real
    whole = float(round(real))
    cut = whole if abs(real - whole) <= _SLACK * math.ulp(real) else float(math.trunc(real))
    return int(cut) if -(2**63) <= cut < 2**63 else cut


def float_text(number: Any) -> str | None:
    """
    `drivine_float_text(real)`: the number as Java writes a double, as `toString` gives it on a graph engine: `2.0`,
    `1.0E7`, `1.2E-4`, `0.3333333333333333`. SQLite's own text of a real has 15 digits, or 17.
    """
    return None if number is None else arguments.java_double(arguments.real(number))


def sort_key(json: Any) -> str | None:
    """
    `drivine_sort_key(json)`: a list as text that orders, as SQLite orders text, as Cypher orders lists (see
    sqlite_cypher_values.sort_key). Null where the value is null; a value that is no list, its JSON text.
    """
    text = arguments.text(json)
    return None if text is None else values.sort_key(text)


def list_order(a: Any, b: Any) -> int | None:
    """
    `drivine_list_order(a, b)`: where the JSON list `a` comes beside the JSON list `b` as Cypher orders them in a
    comparison (see sqlite_cypher_values.order), -1, 0 or 1; null where either is no list, or where the pair that
    decides is not ordered.
    """
    x, y = _read(a), _read(b)
    if not isinstance(x, list) or not isinstance(y, list):
        return None
    return values.order(x, y)


_MODES = {
    "UP": ROUND_UP,
    "DOWN": ROUND_DOWN,
    "CEILING": ROUND_CEILING,
    "FLOOR": ROUND_FLOOR,
    "HALF_UP": ROUND_HALF_UP,
    "HALF_DOWN": ROUND_HALF_DOWN,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "UNNECESSARY": "UNNECESSARY",
}
"""Java's `RoundingMode`s, by their names, as Python's decimal rounds."""


def round_by(number: Any, precision: Any, mode: Any) -> float | None:
    """
    `drivine_round(number, precision, mode)`: the number rounded to as many decimal places by the mode, a name of Java's
    `RoundingMode`, as Java's `BigDecimal` rounds it, as Neo4j's `round(value, precision, mode)` does: in decimal, so
    2.345 is halfway between 2.34 and 2.35, where SQLite's binary arithmetic finds it just below. A real number, as
    `BigDecimal.valueOf` reads a double, of the digits Java writes it with. An infinity, which SQLite stores, is itself.
    Null where the number or the precision is.
    """
    if number is None or precision is None:
        return None
    if isinstance(number, float) and math.isinf(number):
        return number
    exact = (
        Decimal(int(number)) if arguments.is_integer(number) else Decimal(arguments.java_double(arguments.real(number)))
    )
    exponent = exact.as_tuple().exponent
    assert isinstance(exponent, int)
    places = arguments.integer(precision)
    # Places at or past the number's own change nothing, and would write each out: round(2.5, 1000000000).
    if places >= -exponent:
        return float(exact)
    places = arguments.int32(places)
    if places >= -exponent:
        return float(exact)
    name = arguments.text(mode)
    if name is None:
        raise DrivineError("Required value was null.")
    if name not in _MODES:
        raise DrivineError(f"No enum constant java.math.RoundingMode.{name}")
    with localcontext() as context:
        context.prec = 100_000
        quantum = Decimal(1).scaleb(-places)
        if _MODES[name] == "UNNECESSARY":
            rounded = exact.quantize(quantum, ROUND_DOWN)
            if rounded != exact:
                raise DrivineError("Rounding necessary")
        else:
            rounded = exact.quantize(quantum, _MODES[name])
        return float(rounded)


def fail(message: Any) -> None:
    """
    `drivine_fail(text)`: fails the statement with the text, where a compiled statement fails as Neo4j does on a value
    it reads, such as a precision below 0 for `round()`. SQLite has no expression that fails of its own.
    """
    raise DrivineError(arguments.text(message))


_TO_DEGREES = 57.29577951308232
_TO_RADIANS = 0.017453292519943295
"""Java's `Math.toDegrees` and `Math.toRadians` multiply by these, where Python's divide by one of them."""


def _java(function: Any, x: float, y: float = 0.0) -> float:
    """What Java's `Math` gives of the function, where Python's raises in its place: NaN, or an infinity."""
    try:
        return float(function(x, y))
    except OverflowError:
        return math.inf
    except ZeroDivisionError:
        return math.copysign(math.inf, x) if function is _cot else math.inf
    except ValueError:
        if function in (_log, _log10) and x == 0:
            return -math.inf
        return math.nan


def _log(x: float, _: float) -> float:
    return math.log(x)


def _log10(x: float, _: float) -> float:
    return math.log10(x)


def _cot(x: float, _: float) -> float:
    tangent = math.tan(x)
    if tangent == 0:
        return math.copysign(math.inf, tangent)
    return 1.0 / tangent


_MATH: dict[str, Callable[[float, float], float]] = {
    "exp": lambda x, _: math.exp(x),
    "log": _log,
    "log10": _log10,
    "sqrt": lambda x, _: math.sqrt(x),
    "sin": lambda x, _: math.sin(x),
    "cos": lambda x, _: math.cos(x),
    "tan": lambda x, _: math.tan(x),
    "cot": _cot,
    "asin": lambda x, _: math.asin(x),
    "acos": lambda x, _: math.acos(x),
    "atan": lambda x, _: math.atan(x),
    "atan2": lambda x, y: math.atan2(x, y),
    "degrees": lambda x, _: x * _TO_DEGREES,
    "radians": lambda x, _: x * _TO_RADIANS,
    "haversin": lambda x, _: (1.0 - math.cos(x)) / 2.0,
}


def math_of(*given: Any) -> float | None:
    """
    `drivine_math(name, number[, number])`: openCypher's function of numbers `name` (`exp`, `log`, `sin`, `atan2` …),
    worked out as Java's `Math` works it out, as Neo4j does. Null where a number is null. Where Java gives NaN or an
    infinity (`log(0)`, `sqrt(-1)`, `exp(1000)`), it fails: JSON holds neither, and Postgres and Oracle fail there too.
    """
    name = arguments.text(given[0]) if given else None
    if name is None:
        raise DrivineError("Required value was null.")
    if any(it is None for it in given[1:]):
        return None
    numbers = [arguments.real(it) for it in given[1:]]
    if not numbers:
        raise DrivineError(f"{sqlite_graph.MATH} is given no number for {name}.")
    function = _MATH.get(name)
    if function is None:
        raise DrivineError(f"{sqlite_graph.MATH} has no function {name}.")
    if name == "atan2" and len(numbers) < 2:
        raise DrivineError(f"{sqlite_graph.MATH} is given one number for atan2.")
    value = _java(function, numbers[0], numbers[1] if len(numbers) > 1 else 0.0)
    if not math.isfinite(value):
        raise DrivineError(
            f"{name}() of {', '.join(arguments.java_double(it) for it in numbers)} is {arguments.java_double(value)}, "
            "which JSON holds no value for: a SQL store does not give it."
        )
    return value
