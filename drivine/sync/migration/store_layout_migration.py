# Written by scripts/unasync.py from drivine/migration/store_layout_migration.py:
# change that, and run the script again.
"""
A graph on Postgres, Oracle or SQLite moved from store layout 1, which every Drivine for Python before its layout-2
release wrote, to layout 2, in place of reloading it (drivine-cypher-spec#256). The shared module decides each round, by
its JSON entry's `migrate` request, as every library migrates alike; this runs what it decides on the provider's store,
and gives back the module's report. drivine4j's `StoreLayoutMigration`, and Drivine for TypeScript's.

Run it while nothing else uses the graph, with a backup taken first: a migration stopped at any moment is resumed by
running it again with the same declarations. A dry run writes nothing, and reports what would change, the values
that are no value of their declared kind, the candidates to declare, and the uniqueness constraints' keys that may
collide. A migration makes no index or constraint: the application's schema plan makes its declared ones at its
next start.

Statements run on a connection of their own that neither makes the graph's tables nor judges its layout, as `connect`
would, and refuse the graph; the lock on another, in a transaction left open until the migration ends.

Not yet proven on every store (drivine-python#514): `migrate` refuses to run unless told `unproven=True`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from drivine.drivine_error import DrivineError
from drivine.query.query_specification import QuerySpecification
from drivine.sync.connection.connection import Connection

DeclaredKind = Literal["date", "localtime", "time", "localdatetime", "datetime", "duration", "float", "bytes"]
"""What a property is declared to hold, which store layout 1 can't tell (the module's `LayoutMigration.Kind`)."""


@dataclass(frozen=True)
class MigrationDeclaration:
    """That the property `property` of the nodes labelled `label`, or the relationships of that type, holds `kind`."""

    label: str
    property: str
    kind: DeclaredKind


class MigratableProvider(Protocol):
    """A provider of a SQL store a migration runs on: Postgres's, Oracle's or SQLite's."""

    @property
    def type(self) -> Any: ...

    @property
    def graph_name(self) -> str: ...

    def connect_unjudged(self) -> Connection:
        """A connection that neither makes the graph's tables nor judges its layout."""
        ...

    def prepare_unjudged(self, connection: Connection) -> None:
        """Makes the graph's tables on `connection` as a first connection makes them, without the layout's verdict."""
        ...

    def stamp_statement(self) -> tuple[str, dict[str, Any]]:
        """
        The stamp's `assignIfAbsent` and what it binds, this process's id and moment, of layout 2: see the module's
        `StoreStamp`.
        """
        ...


Compile = Callable[[str], str]
"""The shared module's JSON entry: a request's text in, its answer's text out."""

STORES = ("postgres", "oracle", "sqlite")


class StoreLayoutMigration:
    """See the module."""

    def __init__(self, provider: MigratableProvider, compile_json: Compile | None = None) -> None:
        store = str(getattr(provider.type, "value", provider.type)).lower()
        if store not in STORES:
            raise DrivineError(
                "A store layout migration runs on Postgres, Oracle or SQLite, which keep a graph in tables, "
                f"not on {provider.type}."
            )
        self._store = store
        self._provider = provider
        self._compile = compile_json

    def migrate(
        self,
        declarations: Sequence[MigrationDeclaration],
        *,
        batch_size: int = 1000,
        dry_run: bool = False,
        keep_invalid: bool = False,
        unproven: bool = False,
    ) -> dict[str, Any]:
        """
        Migrates the graph by `declarations`, or with `dry_run` reports what a migration would do and writes nothing,
        reading and writing `batch_size` rows at a time. Where `keep_invalid`, a value that is no value of its declared
        kind is kept as it is, and named in the report, where it would otherwise refuse the graph. Gives the module's
        report.
        """
        if not unproven:
            raise DrivineError(
                "The store layout migration is not yet proven on every store (drivine-python#514): back the graph up "
                "first, and pass unproven=True to run it."
            )
        work = self._provider.connect_unjudged()
        held: dict[str, Connection] = {}
        try:
            answer = self._ask(
                {
                    "start": {
                        "declarations": [
                            {"label": d.label, "property": d.property, "kind": d.kind} for d in declarations
                        ],
                        "batchSize": batch_size,
                        "dryRun": dry_run,
                        "keepInvalid": keep_invalid,
                    }
                }
            )
            while "run" in answer:
                outcomes = self._round(work, held, answer["run"])
                answer = self._ask({"resume": answer["state"], "outcomes": outcomes})
            return dict(answer["done"])
        finally:
            # Cleanup after the migration's own outcome, which a failure here must not hide.
            lock = held.get("lock")
            if lock is not None:
                with suppress(Exception):
                    lock.rollback_transaction()
                with suppress(Exception):
                    lock.release()
            with suppress(Exception):
                work.release()

    def _round(
        self, work: Connection, held: dict[str, Connection], actions: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Runs a round's actions in order, up to and with the first that fails; the lock taken once, on a connection of
        its own.
        """
        outcomes: list[dict[str, Any]] = []
        for action in actions:
            try:
                if action["type"] == "lock":
                    if "lock" not in held:
                        lock = self._provider.connect_unjudged()
                        held["lock"] = lock
                        lock.start_transaction()
                        lock.query(QuerySpecification(action["statement"]))
                    outcome: dict[str, Any] = {"type": "ran"}
                else:
                    outcome = self._run(work, action)
            except Exception as error:  # noqa: BLE001 - every store error is the module's to read
                outcome = {"type": "failed", "message": str(error)}
            outcomes.append(outcome)
            if outcome["type"] == "failed":
                break
        return outcomes

    def _run(self, work: Connection, action: Mapping[str, Any]) -> dict[str, Any]:
        kind = action["type"]
        if kind == "query":
            return {"type": "rows", "rows": self._rows(work, action["statement"], action.get("parameters") or {})}
        if kind == "execute":
            self._rows(work, action["statement"], action.get("parameters") or {})
            return {"type": "ran"}
        if kind == "transaction":
            work.start_transaction()
            try:
                for statement in action["statements"]:
                    self._rows(work, statement["statement"], statement.get("parameters") or {})
                work.commit_transaction()
            except BaseException:
                with suppress(Exception):
                    work.rollback_transaction()
                raise
            return {"type": "ran"}
        if kind == "prepare":
            self._provider.prepare_unjudged(work)
            return {"type": "ran"}
        if kind == "assignStamp":
            statement, parameters = self._provider.stamp_statement()
            self._rows(work, statement, parameters)
            return {"type": "ran"}
        raise DrivineError(f"No migration action {kind}.")

    def _rows(self, work: Connection, statement: str, parameters: Mapping[str, Any]) -> list[Any]:
        rows = work.query(QuerySpecification(statement).bind(dict(parameters)))
        return [_document(row) for row in rows]

    def _ask(self, request: Mapping[str, Any]) -> dict[str, Any]:
        compile_json = self._compile or _shared_compile()
        answer = json.loads(
            compile_json(json.dumps({"migrate": {"store": self._store, "graph": self._provider.graph_name, **request}}))
        )
        if "error" in answer:
            raise DrivineError(
                f"The shared module refused the migration of '{self._provider.graph_name}': {answer['error']}"
            )
        return dict(answer)


def _shared_compile() -> Compile:
    from drivine.cypher.shared_compiler import _compiler, not_installed

    return _compiler(lambda: not_installed("SQLITE"))


def _document(row: Any) -> Any:
    """
    A row of one JSON column as the value it holds, whether the store gives it as text or as an object; any other row
    as it is.
    """
    if isinstance(row, Mapping) and len(row) == 1:
        value = next(iter(row.values()))
        if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value
    return row
