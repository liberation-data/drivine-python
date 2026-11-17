"""
The store layout's verdict on a SQL store's graph, asked on a provider's first connection before anything is made in
it (liberation-data/drivine-cypher-spec#161): drivine4j's `SqlGraph.layout`, Drivine for TypeScript's `judgeLayout`.

The verdict is the shared module's (its `layout` request), asked of the graph's stamps, whether it holds a node other
than Drivine's own, and the objects an earlier layout may have left. `judging` writes the statements that ask the store,
and what the verdict says to run, as a generator: each statement it yields is run on the connection, and its rows sent
back. So the async API and the sync one each drive it on their own connections (see GraphTables.judge), and neither
keeps a copy of the rules.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from drivine.backend.store_statements import ModuleStore, restamp_parameters, stamp_parameters, store_statements
from drivine.connection.store_layout_error import StoreLayoutError
from drivine.drivine_error import DrivineError


@dataclass(frozen=True)
class Ask:
    """A statement of the store's own SQL to run, binding `$name`s, whose rows are sent back."""

    statement: str
    parameters: Mapping[str, Any] = field(default_factory=lambda: {})


Rows = list[Any]
"""The rows a statement gave: each a mapping of its columns, or its one value."""


def layout_verdict(engine: str, graph: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """The module's verdict on the graph `graph` of the store whose stamp names it `engine`, by `evidence`."""
    from drivine.cypher.shared_compiler import module_answer

    answer = module_answer({"layout": {"engine": engine, "graph": graph, **evidence}}, engine)
    if "error" in answer:
        raise DrivineError(f"The shared module can't judge the layout of the graph '{graph}': {answer['error']}")
    return answer


def judging(store: ModuleStore, graph: str, tables_made: str) -> Generator[Ask, Rows, None]:
    """
    Judges the graph `graph`'s store layout, yielding each statement to run and taking its rows. A graph whose table of
    nodes isn't there yet (`tables_made`, a query of the store's catalog, counts none) is new, and nothing more is
    asked. A graph in this layout is used. One that holds none of the application's values and is in an earlier layout,
    or none, is made this one's: the earlier layout's objects dropped, its stamp given this layout, and asked again;
    nothing is migrated. One of this layout whose stamp a clear deleted is stamped again. Any other graph is refused
    with StoreLayoutError.
    """
    statements = store_statements(store, graph)
    stamp = statements.stamp
    for asked in (1, 2):
        # Asked of the store's catalog, which no statement fails: on Postgres a failed one ends its transaction.
        if asked == 1 and int(_single((yield Ask(tables_made))) or 0) == 0:
            return
        stamps = [_document(it) for it in (yield Ask(stamp.read_stamps))]
        foreign_nodes = len((yield Ask(stamp.foreign_nodes))) > 0
        objects = [_document(it) for it in (yield Ask(stamp.layout_objects))] if stamp.layout_objects else []
        verdict = layout_verdict(
            statements.engine,
            graph,
            {
                "stamps": [{"layout": it.get("layout")} for it in stamps],
                "foreignNodes": foreign_nodes,
                "objects": [{"name": str(it["name"]), "type": str(it["type"])} for it in objects],
            },
        )
        if verdict["verdict"] == "current":
            return
        if verdict["verdict"] == "refuse":
            raise StoreLayoutError(graph, verdict["message"])
        if asked == 2:
            raise StoreLayoutError(
                graph,
                f"The graph '{graph}' was given store layout {statements.layout}, and its stamps read back otherwise: "
                "another process may be writing it with an earlier Drivine.",
            )
        for drop in verdict["drops"]:
            yield Ask(drop)
        if verdict["assign"]:
            # A graph of this layout whose stamp a clear deleted: stamped again as a stamp is first made. Another
            # process may stamp it first; the insert is made only where there is none, and both read the one back.
            assigned_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            yield Ask(
                stamp.assign_if_absent,
                stamp_parameters(store, graph, str(uuid.uuid4()), assigned_at, statements.engine),
            )
        elif stamp.restamp_layout:
            yield Ask(stamp.restamp_layout, {**restamp_parameters(store, graph), **verdict["parameters"]})


def judge(store: ModuleStore, graph: str, tables_made: str, rows: Callable[[str, Mapping[str, Any]], Rows]) -> None:
    """Judges the graph's layout (see `judging`), each statement run by `rows`, which gives its rows: for a host whose
    connection runs a statement as it is asked, as SQLite's session does on its own thread."""
    asking = judging(store, graph, tables_made)
    try:
        ask = next(asking)
        while True:
            ask = asking.send(rows(ask.statement, ask.parameters))
    except StopIteration:
        return


def _single(rows: Rows) -> Any:
    """A row of one value: that value; None for no row."""
    if not rows:
        return None
    row: Any = rows[0]
    if isinstance(row, Mapping):
        values: list[Any] = list(cast(Mapping[str, Any], row).values())
        return values[0] if len(values) == 1 else cast(Any, row)
    if isinstance(row, (list, tuple)):
        items = cast(Sequence[Any], row)
        return items[0] if len(items) == 1 else cast(Any, row)
    return row


def _document(row: Any) -> dict[str, Any]:
    """A row of one JSON column as the object it holds, whether the store gives it as text or as an object."""
    value = _single([row])
    found: dict[str, Any] = json.loads(value) if isinstance(value, str) else value
    return found
