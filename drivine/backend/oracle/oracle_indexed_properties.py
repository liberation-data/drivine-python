"""
The leading property of each labelled index a graph's store has, by label, as a Cypher statement run through a
connection is compiled with: the compiler writes a key equality on such a property as the index's expression, so Oracle
reads the node through the index. Compiled without them, a keyed `MATCH` read every node of the label, for each row of
an `UNWIND`: a batch of 20 relationships as `apoc.export.cypher` writes it took 137 s, and 0.16 s with them
(liberation-data/drivine4j#805, #508).

The connection reads them with the shared module's query of the graph's catalog (`query`), on itself, and they are kept
for the database, its user and the graph: read again when `MAX_AGE` has passed or an index of the graph has been made
or dropped in this process (`changed`). What is kept only decides how a statement is written, never what it means: a
statement written for an index that has since gone is read without it. A catalog that can't be read leaves what was
read before, or none. drivine4j's OracleIndexedProperties is the reference.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

from drivine.backend.store_statements import store_statements

logger = logging.getLogger("drivine.connection")

MAX_AGE = 60.0
"""How long, in seconds, what was read is kept, as drivine4j keeps it."""

clock: Callable[[], float] = time.monotonic
"""Seconds, as `time.monotonic` counts them: replaced by a test that lets `MAX_AGE` pass."""

IndexedProperties = frozenset[tuple[str, str]]
"""Each label and the property an index on it begins with."""

NONE: IndexedProperties = frozenset()


@dataclass(frozen=True)
class _Read:
    leading: IndexedProperties
    at: float
    changes: int


_reads: dict[str, _Read] = {}
"""What was last read for each database, user and graph."""

_warned: set[str] = set()
"""The database, user and graph each said once to have a catalog that could not be read."""

_changes: dict[str, int] = {}
"""How many times this process has made or dropped an index or a constraint of each graph, by its name lowercased."""

_lock = threading.Lock()


def query(graph: str) -> str | None:
    """The module's query of the labelled indexes of `graph`, as the catalog records them; None from an older module."""
    return store_statements("oracle", graph).labelled


def key(database: str, graph: str) -> str | None:
    """
    What is kept for `graph` on the database and user `database` names is kept under. None where it names none, as
    the connection a provider judges a graph's layout on: its statements are compiled knowing no index, and none is
    read.
    """
    return f"{database}\0{graph.lower()}" if database else None


def changes(graph: str) -> int:
    """How many times this process has made or dropped an index or a constraint of the graph `graph`."""
    return _changes.get(graph.lower(), 0)


def changed(graph: str) -> None:
    """Says an index or a constraint of the graph `graph` has been made or dropped, so its catalog is read again."""
    with _lock:
        _changes[graph.lower()] = changes(graph) + 1


def known(key: str, graph: str) -> IndexedProperties | None:
    """What was read under `key`, while it is less than `MAX_AGE` old and no index of `graph` has changed since."""
    last = _reads.get(key)
    if last is not None and last.changes == changes(graph) and clock() - last.at < MAX_AGE:
        return last.leading
    return None


def found(key: str, graph: str, rows: Iterable[Any], since: int) -> IndexedProperties:
    """
    Keeps, and gives, the leading properties the rows of `query` list, each the JSON object of one index or its text.
    `since` is `changes` as it was before the read: an index made or dropped while it ran has it read again.
    """
    leading: set[tuple[str, str]] = set()
    for row in rows:
        value: Any = json.loads(row) if isinstance(row, (str, bytes)) else row
        if not isinstance(value, dict):
            continue
        index = cast(dict[str, Any], value)
        properties = cast(list[Any], index.get("properties") or [])
        if properties:
            leading.add((str(index["label"]), str(properties[0])))
    return _kept(key, frozenset(leading), since)


def failed(key: str, graph: str, error: BaseException, since: int) -> IndexedProperties:
    """
    Gives what was read before, or none, where the catalog could not be read: said once at WARNING for each database,
    user and graph, and after that at DEBUG. It is read again only after `MAX_AGE`, not on every statement.
    """
    if key not in _warned:
        _warned.add(key)
        logger.warning(
            "The labelled indexes of %s could not be read, so a Cypher statement is compiled without them until they "
            "can be: %s",
            graph,
            error,
        )
    else:
        logger.debug(
            "The labelled indexes of %s could not be read again, so what was read before is kept: %s", graph, error
        )
    last = _reads.get(key)
    return _kept(key, last.leading if last is not None else NONE, since)


def _kept(key: str, leading: IndexedProperties, since: int) -> IndexedProperties:
    _reads[key] = _Read(leading, clock(), since)
    return leading
