# apsw's stubs type a bytes value as collections.abc.Buffer, which Python 3.11 lacks: what they give is partly unknown.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""
The functions compiled Cypher calls on SQLite that the shared module leaves to each library, registered on each
connection by `register` (see sqlite_functions.register), by the names in sqlite_graph, of the same arity and as
deterministic as drivine4j's. The module's own, which compare, add, order, round, match and work out temporal values
(`drivine_equal`, `drivine_add`, `drivine_canonical`, `drivine_sort_key`, `drivine_list_order`, `drivine_round`,
`regexp`, `drivine_regex_group`, `drivine_regexp_replace`, `drivine_temporal`, `drivine_trunc`, `drivine_to_char` …),
come from its SQLite extension (see sqlite_extension).

- `drivine_upper`, `drivine_split`, `drivine_reverse` and `drivine_uuid` are Cypher's `toUpper`, `split`, `reverse`
  and `randomUUID`;
- `drivine_math` works out Cypher's functions of numbers, `exp`, `log`, `sin` …, as Neo4j does;
- `drivine_cypher` carries out the procedure a statement that writes compiles to (see sqlite_cypher_procedure);
- `drivine_fail` fails the statement with its text, where Neo4j fails on a value it reads.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from functools import partial
from typing import Any

import apsw

from drivine.backend.sqlite import sqlite_arguments as arguments
from drivine.backend.sqlite import sqlite_cypher_procedure, sqlite_graph
from drivine.backend.sqlite import sqlite_cypher_values as values
from drivine.backend.sqlite.sqlite_patterns import Patterns
from drivine.drivine_error import DrivineError


def register(connection: apsw.Connection, patterns: Patterns) -> None:
    """Registers on the connection the functions compiled Cypher calls."""
    g = sqlite_graph

    def add(name: str, arity: int, function: Any, deterministic: bool = True) -> None:
        connection.create_scalar_function(name, function, arity, deterministic=deterministic)

    add(g.CYPHER, 2, partial(sqlite_cypher_procedure.run, connection), False)
    add(g.UPPER, 1, upper)
    add(g.SPLIT, 2, split)
    add(g.REVERSE, 1, reverse)
    add(g.UUID, 0, random_uuid, False)
    # Not deterministic: SQLite must not work it out before a row that needs it.
    add(g.FAIL, 1, fail, False)
    # Of as many arguments as its operation takes.
    add(g.MATH, -1, math_of)


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


"""How many units of its last place a real number may miss a whole number by and be it: see trunc."""


"""Java's `RoundingMode`s, by their names, as Python's decimal rounds."""


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
