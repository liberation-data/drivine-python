# apsw's stubs type a bytes value as collections.abc.Buffer, which Python 3.11 lacks: what they give is partly unknown.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""
The functions Drivine registers on each connection to a SQLite store, which its statements call: SQLite keeps no
function of its own a statement could, and runs one written in the application's language as readily as its own.
drivine4j's `SqliteFunctions`, function for function, so a statement either library writes reads alike.

- `drivine_write` carries out a plan of steps: see SqlitePlanWriter.
- `drivine_delete` deletes the nodes a statement selected, and counts them.
- `drivine_lower_copies` fills in the lowercased copies of a property, and counts the nodes it wrote.
- `drivine_similarity` scores an embedding against a vector.
- `drivine_regexp_count` counts the matches of a regular expression, and `regexp` is SQLite's `REGEXP` operator.
- `drivine_lower` lowers text as Unicode does.
- `drivine_epoch` reads a moment as nanoseconds since the epoch.

Compiled Cypher calls the rest, for what Cypher does with its values, JSON text each, that SQLite does not: see
sqlite_cypher_functions.

A function that reads only its arguments is registered as deterministic, so SQLite may index an expression over it.

A failure inside a function fails the statement as itself, as apsw raises it: so a uniqueness constraint that refused a
save, or a file another connection locked, is told as itself.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

import apsw

from drivine.backend.sqlite import sqlite_cypher_functions, sqlite_graph
from drivine.backend.sqlite.sqlite_arguments import text as text_of_argument
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, has_label, path, text_of
from drivine.backend.sqlite.sqlite_patterns import Patterns
from drivine.backend.sqlite.sqlite_plan_writer import SqlitePlanWriter, Stopped
from drivine.model import lower_copies

REGEXP = "regexp"
"""The name SQLite's `X REGEXP Y` calls, as `regexp(Y, X)`."""

_SAVEPOINT = "drivine_write"


def register(connection: apsw.Connection) -> None:
    """Registers every function on the connection, for the statements run on it."""
    patterns = _Patterns()
    similarity = _Similarity()
    # apsw's stubs give a function any SQLite value; each here reads what it is given (see sqlite_arguments).
    add: Any = connection.create_scalar_function
    add(sqlite_graph.WRITE, partial(_write, connection), 6)
    add(sqlite_graph.DELETE, partial(_delete, connection), 2)
    add(sqlite_graph.LOWER_COPIES, partial(_lower_copies, connection), 4)
    add(sqlite_graph.SIMILARITY, similarity.score, 3, deterministic=True)
    add(sqlite_graph.REGEXP_COUNT, patterns.count, 2, deterministic=True)
    add(REGEXP, patterns.matches, 2, deterministic=True)
    add(sqlite_graph.LOWER, _lower, 1, deterministic=True)
    add(sqlite_graph.EPOCH, epoch, 1, deterministic=True)
    sqlite_cypher_functions.register(connection, patterns)


def _write(
    connection: apsw.Connection,
    graph: str | None,
    plan: str | None,
    shapes: str | None,
    mark: str | None,
    returns: str | None,
    patched_if: str | None,
) -> str:
    """
    `drivine_write(graph, plan, shapes, mark, returns, patched_if)`: carries out a plan of steps on the graph, as
    `<graph>_write` does on Postgres, and gives back a JSON array of what `returns` asks: the stamps a save hands back,
    a stamp for each node of a batch, or the number of relationships the steps counted.

    The steps run on the connection the statement runs on, in its transaction, within a savepoint of their own: a plan
    that fails, or finds its root stale, is rolled back to where it began, so a save is applied whole or not at all,
    with or without a transaction around it. A stale root gives nothing back; a node a `ref` step needs that is not
    there counts nothing.

    SQLite has one writer at a time, so a node is not locked as it is found, as it is on Postgres. A statement outside
    a transaction reads before it writes, and if another connection wrote in between, SQLite refuses the write as busy:
    the save is then run again, as on any store that turns a write away as contended.
    """
    if graph is None or plan is None or mark is None or returns not in ("STAMPS", "EACH_STAMP", "COUNT"):
        raise ValueError("drivine_write names a graph, a plan, a mark and what it returns.")
    writer = SqlitePlanWriter(SqliteGraph(graph), connection, plan, shapes or "[]", mark, patched_if)
    connection.execute(f"SAVEPOINT {_SAVEPOINT}").close()
    try:
        given = writer.write(returns)
        connection.execute(f"RELEASE {_SAVEPOINT}").close()
    except Stopped as stopped:
        # Rolled back: nothing was written.
        connection.execute(f"ROLLBACK TO {_SAVEPOINT}").close()
        connection.execute(f"RELEASE {_SAVEPOINT}").close()
        given = [stopped.count] if returns == "COUNT" else []
    except BaseException:
        for it in (f"ROLLBACK TO {_SAVEPOINT}", f"RELEASE {_SAVEPOINT}"):
            try:
                connection.execute(it).close()
            except apsw.Error:
                pass
        raise
    return json.dumps(given, ensure_ascii=False, separators=(",", ":"))


def _delete(connection: apsw.Connection, graph: str | None, nids: str | None) -> int:
    """
    `drivine_delete(graph, nids)`: deletes the nodes of the graph whose ids the JSON array `nids` holds, and gives how
    many it deleted. Their relationships go with them: the relationship table cascades.

    SQLite has no delete in a common table expression, so a statement that deletes and counts selects the nodes first,
    every one of them before any is deleted, and hands them here: a cascade reads what it keeps before it takes
    anything. The delete runs on the connection the statement runs on, in its transaction.
    """
    if graph is None:
        raise ValueError("drivine_delete names no graph.")
    if nids is None:
        return 0
    connection.execute(
        f"DELETE FROM {SqliteGraph(graph).nodes} WHERE nid IN (SELECT value FROM json_each(?))", (nids,)
    ).close()
    return connection.changes()


def _lower_copies(
    connection: apsw.Connection, graph: str | None, label: str | None, property: str | None, batch: int | None
) -> int:
    """
    `drivine_lower_copies(graph, label, property, batch)`: writes the lowercased copy of `property` (see lower_copies)
    on at most `batch` nodes of `label` whose copy is not the property lowercased, removes it from those whose property
    has no value, and gives how many nodes it wrote. The copy is lowercased by `drivine_lower`, as a lookup lowercases
    what it compares the copy with.
    """
    if graph is None or label is None or property is None:
        raise ValueError("drivine_lower_copies names a graph, a label and a property.")
    nodes = SqliteGraph(graph).nodes
    copy = lower_copies.of(property)

    def lowered(alias: str) -> str:
        return f"{sqlite_graph.LOWER}({text_of(alias, property)})"

    connection.execute(
        f"UPDATE {nodes} AS n\n"
        f"SET props = CASE WHEN {text_of('n', property)} IS NULL THEN json_remove(n.props, {path(copy)}) "
        f"ELSE json_set(n.props, {path(copy)}, {lowered('n')}) END\n"
        "WHERE n.nid IN (\n"
        f"    SELECT m.nid FROM {nodes} m\n"
        f"    WHERE {has_label('m', label)} AND {text_of('m', copy)} IS NOT {lowered('m')}\n"
        "    LIMIT ?)",
        (batch if batch is not None else 0,),
    ).close()
    return connection.changes()


def _lower(text: str | None) -> str | None:
    """
    `drivine_lower(text)`: the text in lower case, as Unicode lowers it. SQLite's own `lower` lowers only ASCII, and a
    search or a comparison that ignores case lowers what it is given as the application does.
    """
    return None if text is None else str(text).lower()


_INSTANT = re.compile(
    r"^(?P<date>[+-]?\d{4,}-\d{2}-\d{2})[Tt](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:\.(?P<fraction>\d{0,9}))?)?"
    r"(?P<zone>[Zz]|[+-]\d{2}(?::\d{2}(?::\d{2})?)?)?$",
    re.ASCII,
)
"""
A moment as Java's ISO parsers read one: `T` and `Z` in either case, a fraction after a point alone, and an offset of
hours alone or with minutes and seconds.
"""

MAX_OFFSET_SECONDS = 18 * 3600
"""The largest offset Java takes, ±18:00."""


def epoch(text: Any) -> int | None:
    """
    `drivine_epoch(text)`: the moment the text names, as nanoseconds since the epoch, an integer; null for text that
    names no moment. A moment with an offset or `Z` is the instant it is; a date-time with none is read as if at UTC,
    so two of them compare as their clocks do. As drivine4j reads one: ISO 8601, to the nanosecond.

    SQLite's own `unixepoch(…, 'subsec')` keeps milliseconds, and a moment that moves forward by less than one would
    compare as unmoved. An integer of nanoseconds holds every moment from 1677 to 2262 exactly, and compares and orders
    as the moments do.
    """
    if not isinstance(text, str):
        return None
    found = _INSTANT.match(text.strip())
    if found is None:
        return None
    try:
        moment = datetime.fromisoformat(
            f"{found['date']}T{found['hour']}:{found['minute']}:{found['second'] or '00'}"
        ).replace(tzinfo=UTC)
    except ValueError:
        return None
    zone = found["zone"]
    offset = 0
    if zone and zone not in ("Z", "z"):
        sign = -1 if zone[0] == "-" else 1
        hours, minutes, seconds_ = ([int(it) for it in zone[1:].split(":")] + [0, 0])[:3]
        if minutes > 59 or seconds_ > 59 or hours * 3600 + minutes * 60 + seconds_ > MAX_OFFSET_SECONDS:
            return None
        offset = sign * (hours * 3600 + minutes * 60 + seconds_)
    seconds = int((moment - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()) - offset
    nanos = int((found["fraction"] or "").ljust(9, "0"))
    # As Java's multiplyExact then addExact: whole seconds that overflow are null, even where the fraction brings the
    # sum back in range.
    whole = seconds * 1_000_000_000
    value = whole + nanos
    return value if -(2**63) <= whole and value < 2**63 else None


class _Patterns(Patterns):
    """
    The regular expressions the functions of one connection have read, compiled once each, as Java reads them: see
    sqlite_patterns.
    """

    def matches(self, expression: str | None, text: Any) -> int | None:
        """
        SQLite's `REGEXP` operator, which it leaves to the application: `text REGEXP expression` calls
        `regexp(expression, text)`, true where the expression matches somewhere in the text. Null where either is null.
        """
        if expression is None or text is None:
            return None
        return 1 if self.of(str(expression)).search(text_of_argument(text) or "") else 0

    def count(self, text: Any, expression: str | None) -> int:
        """
        `drivine_regexp_count(text, expression)`: how many times the expression matches in the text, each match after
        the last one ends, as Postgres's `regexp_matches(text, expression, 'g')` counts them. Nothing in a null text.
        """
        if text is None or expression is None:
            return 0
        return sum(1 for _ in self.of(str(expression)).finditer(text_of_argument(text) or ""))


class _Similarity:
    """
    `drivine_similarity(embedding, vector, similarity)`: the score of the embedding against the vector, each a JSON
    array of numbers, by `COSINE` or `EUCLIDEAN`, as the graph engines score it: in `[0, 1]`, higher the nearer. The
    cosine score is `(1 + cos) / 2`, and the Euclidean `1 / (1 + d²)`, as on Postgres and Oracle. Null where either is
    not an array of numbers, where they differ in size, or where a cosine is asked of a vector with no length.

    Every row of a search is measured against the same vector, so the last one read is kept.
    """

    def __init__(self) -> None:
        self._last_text: str | None = None
        self._last: list[float] | None = None

    def score(self, embedding: str | None, vector: str | None, similarity: str | None) -> float | None:
        stored = _vector(embedding)
        if vector != self._last_text:
            self._last = _vector(vector)
            self._last_text = vector
        given = self._last
        if stored is None or given is None or len(stored) != len(given):
            return None
        if similarity == "EUCLIDEAN":
            return 1 / (1 + sum((a - b) * (a - b) for a, b in zip(stored, given, strict=True)))
        dot = sum(a * b for a, b in zip(stored, given, strict=True))
        norms = math.sqrt(sum(a * a for a in stored)) * math.sqrt(sum(b * b for b in given))
        return None if norms == 0 else (1 + dot / norms) / 2


def _vector(text: str | None) -> list[float] | None:
    """The JSON text as numbers, when it is an array of numbers and nothing else."""
    if text is None:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list):
        return None
    items = cast(list[Any], value)
    if any(isinstance(it, bool) or not isinstance(it, (int, float)) for it in items):
        return None
    return [float(it) for it in items]
