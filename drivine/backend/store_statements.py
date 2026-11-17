"""
The store's own statements, as the shared module writes them (its `storeStatements` request, drivine-cypher-spec#107,
#135): what makes a SQL store's tables, indexes and functions, the stamp each store carries (`DrivineStore`), and the
schema marker (`_DrivineSchema`). So drivine-python makes, stamps and marks a store with drivine4j's and Drivine for
TypeScript's statements, byte for byte, and keeps no copy of its own; store.yaml and marker.yaml pin every one.

The module has no clock and no random source: a stamp's id and moment, and a marker's `appliedAt`, are this process's,
handed to the module, which gives back the parameters each statement binds.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from drivine.backend.graph_statement import GraphStatement
from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification

ModuleStore = Literal["postgres", "oracle", "sqlite", "neo4j", "memgraph", "falkordb"]
"""A store as the shared module names one in a `storeStatements` request."""


@dataclass(frozen=True)
class StampStatements:
    """The statements of each store's stamp, `(:DrivineStore {key: 'singleton'})`, as the module writes them."""

    prepare: str | None
    """What makes the table the stamp is written to, on a SQL store; None on a graph engine."""
    assign_if_absent: str
    """Writes the stamp unless one is there: binds what `stamp_parameters` gives."""
    read_stamps: str
    """Each stamp, `{id, assignedAt, layout}`, oldest first."""
    foreign_nodes: str
    """One row where the graph holds a node other than Drivine's own, none where it doesn't."""
    restamp_layout: str | None
    """Gives the stamp this layout: binds what `restamp_parameters` gives. None where there is one layout."""
    layout_objects: str | None
    """The objects an earlier layout may have left, `{name, type}` each. None where there is one layout."""
    constraint: list[str]
    """What keeps the stamp's key unique, in order."""


@dataclass(frozen=True)
class MarkerStatements:
    """The statements of the schema marker, `(:_DrivineSchema {scope})`, as the module writes them."""

    prepare: str | None
    read: dict[str, str]
    """The newest marker of the scope `$scope`'s `version` or `items`, as text: one row, or none."""
    merge: str
    """Writes the scope's marker, made where there is none: binds what `marker_parameters` gives."""
    delete_all_but_newest: str
    """Removes each marker of `$scope` but the newest."""
    delete: str
    """Removes each marker of `$scope`."""
    scopes: str | None
    """The scope of every marker, once each; None where the store's Cypher reads them."""
    items_mark: str
    """What a stored inventory opens with, so an empty one reads back empty and not absent: `items:` on Oracle."""
    constraint: list[str]


@dataclass(frozen=True)
class StoreStatements:
    """The store's own statements, by store and graph: see the module."""

    engine: str
    """The store as a stamp names it: `POSTGRES`, `SQLITE`."""
    layout: int
    """The store layout a stamp of it records."""
    ddl: list[str] | None
    """On a SQL store, what makes the graph's tables, indexes and functions, each safe to run again."""
    ensuring: str | None
    """On Postgres and Oracle, the block that makes them where the store's version is not the module's."""
    functions: list[str] | None
    """On Oracle, what makes each of the graph's functions."""
    versions: dict[str, str] | None
    """On Oracle, each function's version, as its `-- drivine:<hash>` says, by name."""
    stored_functions: dict[str, Any] | None
    """On Oracle, the probe of each function a session may call, by compiler flag, and the errors that mean none."""
    stamp: StampStatements
    marker: MarkerStatements


_asked: dict[tuple[str, str | None], StoreStatements] = {}
_asking = threading.Lock()


def store_statements(store: ModuleStore, graph: str | None = None) -> StoreStatements:
    """The statements of `store`, over the graph `graph` on a SQL store: the module writes them alike each time."""
    key = (store, graph)
    found = _asked.get(key)
    if found is None:
        given = _answer(store, graph, {})
        stamp: dict[str, Any] = given["stamp"]
        marker: dict[str, Any] = given["marker"]
        found = StoreStatements(
            engine=given["engine"],
            layout=given["layout"],
            ddl=given.get("ddl"),
            ensuring=given.get("ensuring"),
            functions=given.get("functions"),
            versions=given.get("versions"),
            stored_functions=given.get("storedFunctions"),
            stamp=StampStatements(
                prepare=stamp["prepare"],
                assign_if_absent=stamp["assignIfAbsent"],
                read_stamps=stamp["readStamps"],
                foreign_nodes=stamp["foreignNodes"],
                restamp_layout=stamp["restampLayout"],
                layout_objects=stamp["layoutObjects"],
                constraint=stamp["constraint"],
            ),
            marker=MarkerStatements(
                prepare=marker["prepare"],
                read=marker["read"],
                merge=marker["merge"],
                delete_all_but_newest=marker["deleteAllButNewest"],
                delete=marker["delete"],
                scopes=marker["scopes"],
                items_mark=marker["itemsMark"],
                constraint=marker["constraint"],
            ),
        )
        with _asking:
            _asked[key] = found
    return found


def stamp_parameters(
    store: ModuleStore, graph: str | None, store_id: str, assigned_at: str, engine: str
) -> dict[str, Any]:
    """
    What the stamp's `assign_if_absent` binds: its key, `storeId`, `assignedAt` and layout, as the module gives them,
    and `engine`, the manager's own type, which names a store the module writes no statements of too, as drivine4j
    binds it.
    """
    given = _parameters(store, graph, {"stampParameters": {"storeId": store_id, "assignedAt": assigned_at}})
    return {**given, "engine": engine}


def restamp_parameters(store: ModuleStore, graph: str | None) -> dict[str, Any]:
    """What the stamp's `restamp_layout` binds."""
    return _parameters(store, graph, {"restamp": True})


def marker_parameters(
    store: ModuleStore, graph: str | None, scope: str, version: str | None, items: str, applied_at: int
) -> dict[str, Any]:
    """
    What the marker's `merge` binds: the scope, its version, its inventory `items` (newline-joined keys) as the store
    holds it, and the moment `applied_at`, in milliseconds.
    """
    asked = {"scope": scope, "version": version, "items": items, "appliedAt": applied_at}
    return _parameters(store, graph, {"markerParameters": asked})


def marker_items(statements: StoreStatements, stored: str | None) -> list[str] | None:
    """The inventory keys a marker's stored `items` holds, read past its mark; None where none was recorded."""
    if stored is None:
        return None
    mark = statements.marker.items_mark
    items = stored[len(mark) :] if mark and stored.startswith(mark) else stored
    return [] if items == "" else items.split("\n")


def _parameters(store: ModuleStore, graph: str | None, asking: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _answer(store, graph, asking)["parameters"])


def _answer(store: ModuleStore, graph: str | None, asking: dict[str, Any]) -> dict[str, Any]:
    """The module's answer to a `storeStatements` request; its error raised."""
    from drivine.cypher.shared_compiler import module_answer

    request = {"storeStatements": {"store": store, **({"graph": graph} if graph is not None else {}), **asking}}
    given = module_answer(request, store)
    if "error" in given:
        raise DrivineError(f"The shared module wrote no statements for the {store} store: {given['error']}")
    return given


@dataclass(frozen=True)
class MarkerMerge(GraphStatement):
    """
    The marker's `merge`: bound with what the module makes of the caller's `scope`, `version` and `items` and this
    process's clock (see marker_parameters), so the inventory is kept as the store keeps it (behind `items:` on
    Oracle) and `appliedAt` is the moment it is written.
    """

    store: ModuleStore = "postgres"
    graph: str | None = None

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        given = {**self.bindings, **(more or {})}
        bound = marker_parameters(
            self.store, self.graph, given["scope"], given.get("version"), given["items"], int(time.time() * 1000)
        )
        return QuerySpecification[Any](self.text).bind(bound)


class StampAndMarker:
    """
    A SQL backend's statements of the store's stamp and its schema marker: the module's for its store and graph (see
    store_statements), each made a statement of the backend's own by `statement`, so its rows are read as the backend
    reads any. drivine4j's and Drivine for TypeScript's, byte for byte.
    """

    def __init__(
        self, store: ModuleStore, graph: str, statement: Callable[[GraphStatement], GraphStatement] = lambda it: it
    ) -> None:
        self.store: ModuleStore = store
        self.graph = graph
        self._statement = statement

    @property
    def statements(self) -> StoreStatements:
        return store_statements(self.store, self.graph)

    def store_stamps(self) -> GraphStatement:
        """The store's identity stamps, `{id, assignedAt, layout}`, oldest first."""
        return self._statement(GraphStatement(self.statements.stamp.read_stamps))

    def assign_store_stamp(self) -> GraphStatement:
        """
        Mints the identity stamp keyed `$key` unless it is there, `$storeId`, `$assignedAt` and `$engine` bound by the
        caller, and the layout its values are held in, this store's.
        """
        statements = self.statements
        return self._statement(GraphStatement(statements.stamp.assign_if_absent, {"layout": statements.layout}))

    def schema_marker(self, property: str) -> GraphStatement:
        """The newest marker of the scope `$scope`'s `version` or `items`, as text: one row, or none."""
        return self._statement(GraphStatement(self.statements.marker.read[property]))

    def marker_items(self, stored: str | None) -> list[str] | None:
        """See marker_items."""
        return marker_items(self.statements, stored)

    def record_schema_marker(self) -> GraphStatement:
        """Writes the scope `$scope`'s marker, made where there is none, with `$version` and `$items`, applied now."""
        return MarkerMerge(self.statements.marker.merge, store=self.store, graph=self.graph)

    def prune_schema_markers(self) -> GraphStatement:
        """Removes every marker of the scope `$scope` but the newest, as a race before its constraint may leave."""
        return self._statement(GraphStatement(self.statements.marker.delete_all_but_newest))
