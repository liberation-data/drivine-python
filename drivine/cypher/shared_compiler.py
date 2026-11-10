"""
Cypher compiled to SQL by the shared compiler: drivine-cypher-spec's Kotlin Multiplatform module, the compiler drivine4j
and Drivine for TypeScript run, here as the native library the `drivine-cypher-compiler` wheel holds. A statement gives
the same SQL in all three libraries, and a compiler fix lands once, in the module.

The library is loaded on the first compile for a SQL store, not on `import drivine`, so an application on the graph
engines alone never loads it, and runs where it has no wheel.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import platform
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from drivine.cypher.sql.compiled_cypher import CompiledCypher
from drivine.drivine_error import DrivineError

SharedStore = Literal["POSTGRES", "ORACLE", "SQLITE"]

PACKAGE = "drivine_cypher_compiler"

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_compile: Callable[[str], str] | None = None


def shared_compiler_loaded() -> bool:
    """Whether the shared compiler has been loaded in this process: a graph engine's application never loads it."""
    return _compile is not None


def _compiler(store: SharedStore) -> Callable[[str], str]:
    global _compile
    if _compile is None:
        with _lock:
            if _compile is None:
                try:
                    import drivine_cypher_compiler
                except ModuleNotFoundError as error:
                    if error.name != PACKAGE:
                        raise
                    raise DrivineError(not_installed(store)) from error
                _compile = drivine_cypher_compiler.compile
    return _compile


def has_wheel() -> bool:
    """
    Whether drivine-cypher-compiler has a wheel for this platform: macOS on Apple Silicon, Linux with glibc on x86_64 or
    aarch64, or Windows x64. Kotlin/Native, which builds it, has no target for Intel Macs or for musl Linux. The machine
    is the system's, so a 32-bit Python on a 64-bit system, which takes no 64-bit wheel, has none.
    """
    machine = platform.machine().lower()
    if sys.maxsize <= 2**32:
        return False
    if sys.platform == "darwin":
        return machine == "arm64"
    if sys.platform.startswith("linux"):
        return machine in ("x86_64", "aarch64") and bool(platform.libc_ver()[0])
    return sys.platform == "win32" and machine in ("amd64", "x86_64")


def not_installed(store: SharedStore) -> str:
    """
    Why Cypher can't be compiled to SQL here: drivine-cypher-compiler isn't installed. Where it has a wheel, the store's
    extra installs it; where it has none, it can't be, and the graph engines are what work here.
    """
    if has_wheel():
        extra = store.lower()
        return (
            f"Cypher on {store.capitalize() if store != 'SQLITE' else 'SQLite'} is compiled to SQL by "
            f'drivine-cypher-compiler, which drivine[{extra}] installs: pip install "drivine[{extra}]".'
        )
    return unsupported_platform()


def unsupported_platform() -> str:
    """Why Cypher can't be compiled to SQL here: drivine-cypher-compiler has no wheel for this platform."""
    machine = f"{platform.system()} {platform.machine()}".strip()
    if sys.platform.startswith("linux") and not platform.libc_ver()[0]:
        machine += " (musl, as Alpine has)"
    return (
        f"Cypher is compiled to SQL for Postgres, Oracle and SQLite by drivine-cypher-compiler, which has no wheel "
        f"for {machine}, as Kotlin/Native, which builds it, has no target for Intel Macs or for musl Linux. Neo4j, "
        "Memgraph and FalkorDB work here as they are. For the SQL stores, run on Linux with glibc (a Debian image such "
        "as python:3.x-slim), macOS on Apple Silicon or Windows x64, and install the store's extra, "
        'pip install "drivine[postgres]" for one. If you need this platform, open an issue or a pull request at '
        "https://github.com/liberation-data/drivine-cypher-spec."
    )


def compile_cypher(
    cypher: str,
    parameters: Mapping[str, Any],
    store: SharedStore,
    graph: str,
    nulls_equal_stored: bool = True,
    era_stored: bool = True,
) -> CompiledCypher:
    """
    The statement compiled to the store's SQL over the graph's tables, with the values its parameters are bound to. On
    Oracle `nulls_equal_stored` and `era_stored` say whether it may call the functions the graph is made with. A
    statement the compiler refuses is a DrivineError with the compiler's message, as the spec's refusals pin it.
    """
    request = json.dumps(
        {
            "store": store.lower(),
            "graph": _graph(store, graph, nulls_equal_stored, era_stored),
            "cypher": cypher,
            "parameters": written(parameters),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    answer: dict[str, Any] = json.loads(_compiler(store)(request))
    if "refusal" in answer:
        raise DrivineError(answer["refusal"])
    if "error" in answer:
        raise DrivineError(f"The Cypher compiler can't read the request: {answer['error']}")
    # What it warns of, such as a RETURN of several items, logged as drivine4j logs it.
    warnings: list[str] = answer.get("warnings") or []
    for warning in warnings:
        logger.warning(warning)
    stored: dict[str, Any] = answer.get("storedFunctions") or {}
    return CompiledCypher(
        sql=answer["sql"],
        parameters=decoded(answer["parameters"]),
        setup=tuple(answer.get("setup") or ()),
        calls_stored_nulls_equal=bool(stored.get("nullsEqual", False)),
        calls_stored_era=bool(stored.get("era", False)),
        non_finite=bool(answer.get("nonFinite", False)),
    )


def _graph(store: SharedStore, graph: str, nulls_equal_stored: bool, era_stored: bool) -> dict[str, Any]:
    """The graph as the request has it: Oracle's options only on Oracle, where they mean something."""
    if store != "ORACLE":
        return {"name": graph}
    # A write's rows as a row of JSON each (oracleRowsAsText left false), as python-oracledb reads an implicit result's
    # JSON rows whole, as the compiler Drivine had before did (liberation-data/drivine-python#468). Each value returned is
    # encoded again (oracleReencodeJson): a document updated in place comes back as OSON version 2, which python-oracledb's
    # thin mode can't read (oracle/python-oracledb#622, liberation-data/drivine-cypher-spec#165), as the compiler Drivine had
    # before encoded it again too.
    return {"name": graph, "nullsEqualStored": nulls_equal_stored, "eraStored": era_stored, "oracleReencodeJson": True}


def decoded(value: Any) -> Any:
    """
    The values the answer binds, with its tags read: a float, `{"$float": "2.0"}`, the float; a decimal,
    `{"$decimal": "2.5"}`, the Decimal. Most are JSON text already, bound as such.
    """
    if isinstance(value, list):
        return [decoded(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        if len(value) == 1 and "$float" in value:  # pyright: ignore[reportUnknownArgumentType]
            return float(value["$float"])  # pyright: ignore[reportUnknownArgumentType]
        if len(value) == 1 and "$decimal" in value:  # pyright: ignore[reportUnknownArgumentType]
            return Decimal(str(value["$decimal"]))  # pyright: ignore[reportUnknownArgumentType]
        return {key: decoded(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def written(value: Any) -> Any:
    """
    A statement's parameters as the tagged JSON the shared compiler reads (its README, "drivine4j's own calls"), keeping
    what plain JSON loses of the kinds the compiled SQL depends on:

    - an int is an integer, however large, and a bool a boolean; a float with a fraction or an exponent is a JSON
      number, as Python writes every finite float (`2.0`, `1e-07`), and NaN and the infinities are
      `{"$float": "NaN"}`, `{"$float": "Infinity"}`, `{"$float": "-Infinity"}`;
    - a Decimal is an integer where it is whole and finite, otherwise the float nearest it, as Drivine bound one before;
    - a datetime is `{"$temporal": "DATE_TIME"}` with its ISO 8601 text where it has an offset, `LOCAL_DATE_TIME` where
      it has none; a date `DATE`; a time `TIME` or `LOCAL_TIME` alike; a timedelta `DURATION`, as the Neo4j driver
      sends each;
    - bytes, a bytearray or a memoryview are `{"$bytes": "<base64>"}`;
    - an Enum is its value;
    - a list, tuple, set, frozenset or iterator is the list of its items; a map is its entries, wrapped as
      `{"$map": {...}}` where a key starts with `$`, so no map a caller binds is read as a tag.

    Any other value is refused, naming its type.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, Enum):
        return written(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, Decimal):
        if value.is_nan():  # A signalling NaN too, which float() refuses.
            return {"$float": "NaN"}
        if value.is_finite() and value == value.to_integral_value():
            return int(value)
        return _float(float(value))
    if isinstance(value, datetime):
        kind = "DATE_TIME" if value.utcoffset() is not None else "LOCAL_DATE_TIME"
        return {"$temporal": kind, "text": value.isoformat()}
    if isinstance(value, date):
        return {"$temporal": "DATE", "text": value.isoformat()}
    if isinstance(value, time):
        kind = "TIME" if value.utcoffset() is not None else "LOCAL_TIME"
        return {"$temporal": kind, "text": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$temporal": "DURATION", "text": duration(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, Mapping):
        entries = {str(key): written(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return {"$map": entries} if any(key.startswith("$") for key in entries) else entries
    if isinstance(value, (list, tuple, set, frozenset, Iterator)):
        return [written(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    raise DrivineError(f"A parameter of type {type(value).__name__} can't be bound to a Cypher statement.")


def _float(value: float) -> Any:
    if math.isnan(value):
        return {"$float": "NaN"}
    if math.isinf(value):
        return {"$float": "Infinity" if value > 0 else "-Infinity"}
    return value


def duration(value: timedelta) -> str:
    """
    A timedelta as an ISO 8601 duration of its days and its time, as the Neo4j driver sends one, its days apart:
    `P1DT2H`, `P-1DT23H` for an hour less, `PT0S` for none.
    """
    if not value:
        return "PT0S"
    hours, rest = divmod(value.seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    fraction = Decimal(seconds) + Decimal(value.microseconds) / 1_000_000
    clock = "".join(
        f"{amount}{unit}"
        for amount, unit in ((hours, "H"), (minutes, "M"), (format(fraction.normalize(), "f"), "S"))
        if amount and amount != "0"
    )
    return f"P{f'{value.days}D' if value.days else ''}{f'T{clock}' if clock else ''}"


def drained(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """
    The parameters, with each iterator in them, at any depth, the list of its items: an iterator gives its items only
    once, and a statement may be compiled more than once (on Oracle, again to call none of the graph's stored functions
    the session may not), so a later compile would bind it as empty.
    """
    return {key: _drained(it) for key, it in parameters.items()}


def _drained(value: Any) -> Any:
    if isinstance(value, Iterator):
        return [_drained(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, Mapping):
        return {key: _drained(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, (list, tuple)) and not isinstance(value, str):
        items = [_drained(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
        return items if any(a is not b for a, b in zip(items, value, strict=True)) else value  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, memoryview, set, frozenset)):
        return [_drained(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    return value  # pyright: ignore[reportUnknownVariableType]
