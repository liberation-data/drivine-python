"""
How the rows of a statement given NaN or an infinity are read on Postgres. `jsonb` holds neither, so drivine4j's
compiler writes each as a number past any double's: NaN as `to_jsonb(2e309 + 0.5)`, the infinities as
`to_jsonb(1e309 + 0.5)` and `to_jsonb(-1e309 - 0.5)`. Read as `json.loads` reads them, NaN would be an infinity. Such a
statement says so (`CompiledCypher.non_finite`), and its rows alone are read so, comparing each number's decimal text
exactly, as drivine4j's `PostgresCypherCompiler.read` does (liberation-data/drivine#673).

Every compiled statement's rows are read here (see read_compiled), so a whole number past a long's range is the float
nearest it, as drivine4j reads one: `jsonb` drops a float's fraction where it has none, and writes 1e20 as
`100000000000000000000`. As the Cypher spec's host.yaml pins both.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from drivine.backend.sql import value_layout

NOT_A_NUMBER = Decimal("2" + "0" * 309 + ".5")
"""The number that stands for NaN, 2e309 + 0.5, as drivine4j's compiler writes it on Postgres."""

INFINITE = Decimal("1e309")
"""Where the numbers that stand for the infinities begin, past any double's: `±(1e309 + 0.5)` are written for them."""

_LONGEST_FINITE_WHOLE = 309
"""The most digits a whole number under INFINITE, 1 and 309 zeros, has: one with more may stand for an infinity."""

_LONGEST_LONG = 19
"""The most digits a long has: a whole number of more is past a long's range."""


def non_finite_of(text: str) -> float | None:
    """
    The double the JSON number written `text`, in the rows of a statement given NaN or an infinity, stands for: NaN
    where it is exactly the number that stands for NaN, an infinity where it is past any double's, else None, which is
    the number it is.
    """
    number = Decimal(text)
    if number == NOT_A_NUMBER:
        return math.nan
    if number >= INFINITE:
        return math.inf
    if number <= -INFINITE:
        return -math.inf
    return None


def _float(text: str) -> float:
    """A number with a fraction or an exponent: as `json.loads` reads it, unless it stands for NaN or an infinity."""
    number = float(text)
    # A number float reads as finite is under any double's greatest, 1.8e308, so stands for neither.
    if math.isfinite(number):
        return number
    try:
        found = non_finite_of(text)
    except InvalidOperation:
        # Its exponent is past even Decimal's (`1e99999999999999999999`, as a `json` value may hold it): far past any
        # double's, so the infinity float reads.
        return number
    return number if found is None else found


def _whole(text: str) -> int | float:
    """A whole number: an int where a long holds it, else the float nearest it, as drivine4j reads it."""
    if len(text.lstrip("-")) <= _LONGEST_LONG:
        number = int(text)
        if -(2**63) <= number < 2**63:
            return number
    return float(text)


def _int(text: str) -> int | float:
    """A whole number: as _whole reads it, unless it is past any double's, an infinity."""
    if len(text.lstrip("-")) <= _LONGEST_FINITE_WHOLE:
        return _whole(text)
    found = non_finite_of(text)
    return float(text) if found is None else found


def read_compiled_json(data: str | bytes) -> Any:
    """
    The JSON `data`, as Postgres writes a `json` or `jsonb` value, read as `json.loads` reads it, but for a whole number
    past a long's range, the float nearest it; in the store layout (see value_layout).
    """
    text = data.decode() if isinstance(data, bytes) else data
    return value_layout.read(text, lambda it: json.loads(it, parse_int=_whole))


def read_non_finite_json(data: str | bytes) -> Any:
    """
    The JSON `data` read as read_compiled_json reads it, with each number that stands for NaN or an infinity the double
    it is, then in the store layout. A string is never read as a number, whatever it holds.
    """
    text = data.decode() if isinstance(data, bytes) else data
    return value_layout.read_after(text, lambda it: it, lambda it: json.loads(it, parse_float=_float, parse_int=_int))


def read_layout_json(data: str | bytes) -> Any:
    """
    The JSON `data` read as `json.loads` reads it, each whole number exactly, in the store layout: the object manager's
    own statements' (see QuerySpecification.store_layout), which read values as they were saved.
    """
    text = data.decode() if isinstance(data, bytes) else data
    return value_layout.read(text)


def read_compiled(cursor: Any, non_finite: bool, compiled: bool = True) -> None:
    """
    Has `cursor` read `json` and `jsonb`, as text or binary, in the store layout: a compiled statement's with
    read_non_finite_json where the statement was given NaN or an infinity, else with read_compiled_json; any other of
    the object manager's own with read_layout_json. Set on that cursor alone, never for the connection or the process:
    it would reach every statement, a user's own SQL and other libraries' too.
    """
    from psycopg.types.json import set_json_loads

    loads = (read_non_finite_json if non_finite else read_compiled_json) if compiled else read_layout_json
    set_json_loads(loads, cursor)
