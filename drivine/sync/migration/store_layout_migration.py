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
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol, TypedDict, cast

from pydantic import AwareDatetime, BaseModel

from drivine.cypher.shared_compiler import module_text
from drivine.drivine_error import DrivineError
from drivine.model.field_mapping import FieldMapping
from drivine.model.field_shape import FieldShape
from drivine.model.model_registry import ModelRegistry
from drivine.query.query_specification import QuerySpecification
from drivine.sync.connection.connection import Connection

logger = logging.getLogger("drivine.store")

Kind = Literal["date", "localtime", "time", "localdatetime", "datetime", "duration", "float", "bytes"]
"""
What a property is declared to hold, which store layout 1 can't tell: which texts are dates or moments, which whole
numbers are floats, and which lists of numbers are bytes (the shared module's kinds, by their words, as drivine4j's
`Kind` and Drivine for TypeScript's give them).
"""


@dataclass(frozen=True)
class Declaration:
    """That the property `property` of the nodes labelled `label`, or the relationships of that type, holds `kind`."""

    label: str
    property: str
    kind: Kind


MigrationStatus = Literal[
    "current", "empty", "refused", "inProgress", "invalid", "dryRun", "collisions", "migrated", "failed"
]
"""
How a migration ended, as drivine4j's `MigrationReport.Status` and Drivine for TypeScript's name it: `migrated`, the
graph is in layout 2; `current` or `empty`, nothing to do; `dryRun`, a dry run's, nothing written; `inProgress`, a dry
run of a graph part way through a migration, which only resuming it goes on with; `invalid`, values that are no value of
their declared kind (declare them otherwise, or pass `keep_invalid`); `collisions`, a uniqueness constraint's keys that
collide in layout 2 (correct them with plain SQL, and run it again); `refused`, a graph of another layout, or one
changed since its migration began, or a migration resumed, before every row is written, with other declarations or
another `keep_invalid`; `failed`, a statement failed, the message the store's (run it again to resume).
"""


class MigrationReport(TypedDict):
    """
    The report a migration, or its dry run, ends with: the shared module's, as its JSON entry writes it. `tables`, for
    `nodes` and `edges`, the rows read and changed; `properties`, each label's property a rewrite changes or finds a
    value of not of its declared kind; `observed`, the classes of the values of the properties no declaration names;
    `candidates`, those whose values all read as one kind, to declare; `kept`, the values kept as they were;
    `collisions`, the uniqueness constraints' keys that collide; `notes`, what a migration leaves to others: the
    indexes, which the application's schema plan makes at its next start, and the count store, left unseeded.
    """

    status: MigrationStatus
    message: str
    tables: dict[str, dict[str, int]]
    properties: list[dict[str, Any]]
    observed: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    kept: list[dict[str, Any]]
    collisions: list[dict[str, Any]]
    notes: list[str]


_KINDS: dict[Any, Kind] = {
    date: "date",
    datetime: "datetime",
    AwareDatetime: "datetime",
    timedelta: "duration",
    float: "float",
}
"""
The kind a field of each type is declared: those whose values drivine-python 0.8.0, the last release of layout 1, saved
in the form the migration reads that kind from. A `date` as its ISO text; a `datetime`, naive or aware, as the UTC
moment's ISO text with a `Z`; a `timedelta` as its ISO duration; a float as a number.

A field layout 1 saved in a form other than its kind's is given no declaration, as the migration would read its values
as no value of the kind and refuse the graph: it is kept as it is. So a `NaiveDatetime` is not declared a
localdatetime, as 0.8.0 saved it with a `Z`, as a moment; a `time` not a localtime, as 0.8.0 saved an aware one with its
offset, which its type can't tell; and bytes not at all, as 0.8.0 saved them as their UTF-8 text, not the list of
numbers or base64 text the migration reads bytes from. Each such property is a candidate in a dry run's report where
its values all read as one kind, to declare by hand (see `with_declared`).

A field's own pydantic serializer, alias or `json_encoders` changes nothing here: 0.8.0 saved the field's value, not its
serialized form, under the name the registry gives the field, as this version does.
"""


def declarations_of(
    fragment: type, relationship_type: str | None = None, registry: ModelRegistry | None = None
) -> list[Declaration]:
    """
    What the node fragment `fragment` says of its properties that layout 1 can't tell: a declaration under each of its
    labels, its own and those it inherits, for each of its fields of a type `_KINDS` declares (a `date`, a `datetime`,
    an `AwareDatetime`, a `timedelta` or a float), or a list, a tuple or a set of one, by the name the property is
    stored under (a `GraphProperty`'s, or the registry's naming of the field), as the user names it: a name that
    begins with `$` is declared as it is. For the relationship fragment `fragment`, the same of each of its fields but
    the node it leads to, under `relationship_type`. A field that is transient, JSON packed, a map, a list of lists, the
    node's stamp, its labels or a property bag is not declared, nor is any other type (see `_KINDS`). drivine4j's
    `declarationsOf`. `registry` is the one the application's managers build models with, for its naming of fields: a
    new one, which names them in camel case, by default.
    """
    models = registry or ModelRegistry()
    mapping: FieldMapping
    if relationship_type is not None:
        mapping, labels = models.relationship_fragment(fragment), [relationship_type]
    else:
        model = models.fragment(fragment)
        mapping, labels = model, model.labels
    pydantic_model: type[BaseModel] = fragment  # pyright: ignore[reportAssignmentType]
    fields = pydantic_model.model_fields
    declared: list[Declaration] = []
    for label in labels:
        for field, stored in mapping.graph_properties.items():
            kind = None if field in mapping.json_packed else _kind_of(fields[field].annotation)
            if kind is not None:
                declared.append(Declaration(label, stored, kind))
    return declared


def with_declared(derived: Iterable[Declaration], by_hand: Iterable[Declaration]) -> list[Declaration]:
    """
    The `derived` declarations (see `declarations_of`), each of a label's property that one of `by_hand` declares
    replaced by it, then the rest of `by_hand`: each label's property declared once.
    """
    hand = {(it.label, it.property): it for it in by_hand}
    found: dict[tuple[str, str], Declaration] = {}
    for it in derived:
        found.setdefault((it.label, it.property), hand.get((it.label, it.property), it))
    for key, it in hand.items():
        found.setdefault(key, it)
    return list(found.values())


def _kind_of(annotation: Any) -> Kind | None:
    """The kind a field of `annotation` is declared: its own, or a list's, a tuple's or a set's items'; else None."""
    shape = FieldShape.of(annotation)
    if shape.collection == "dict":
        return None
    if shape.collection is not None:
        shape = FieldShape.of(shape.item)
        if shape.collection is not None:
            return None
    return _KINDS.get(shape.item)


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
        declarations: Sequence[Declaration],
        *,
        batch_size: int = 1000,
        dry_run: bool = False,
        keep_invalid: bool = False,
        unproven: bool = False,
    ) -> MigrationReport:
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
        # Asked first, so declarations or a batch size the module refuses are refused before the store is reached.
        answer = self._ask(
            {
                "start": {
                    "declarations": [{"label": d.label, "property": d.property, "kind": d.kind} for d in declarations],
                    "batchSize": batch_size,
                    "dryRun": dry_run,
                    "keepInvalid": keep_invalid,
                }
            }
        )
        work = self._provider.connect_unjudged()
        held: dict[str, Connection] = {}
        try:
            while "run" in answer:
                outcomes = self._round(work, held, answer["run"])
                answer = self._ask({"resume": answer["state"], "outcomes": outcomes})
            report = cast(MigrationReport, dict(answer["done"]))
            graph = self._provider.graph_name
            logger.info("Store layout migration of '%s': %s: %s", graph, report["status"], report["message"])
            for note in report.get("notes", []):
                logger.warning("Store layout migration of '%s': %s", graph, note)
            return report
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
                        lock.query(QuerySpecification[Any](action["statement"]))
                    outcome: dict[str, Any] = {"type": "ran"}
                else:
                    outcome = self._run(work, action)
            except Exception as error:  # noqa: BLE001 - every store error is the module's to read
                logger.warning(
                    "Store layout migration of '%s': %s failed: %s", self._provider.graph_name, action["type"], error
                )
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
        rows = work.query(QuerySpecification[Any](statement).bind(dict(parameters)))
        return [_document(row) for row in rows]

    def _ask(self, request: Mapping[str, Any]) -> dict[str, Any]:
        asked = {"migrate": {"store": self._store, "graph": self._provider.graph_name, **request}}
        # Where the compiler isn't installed, module_text says how to install it for this store.
        answer = json.loads(self._compile(json.dumps(asked)) if self._compile else module_text(asked, self._store))
        if "error" in answer:
            raise DrivineError(
                f"The shared module refused the migration of '{self._provider.graph_name}': {answer['error']}"
            )
        return dict(answer)


def _document(row: Any) -> Any:
    """
    A row of one JSON column as the value it holds, whether the store gives it as text or as an object; any other row
    as it is.
    """
    if isinstance(row, Mapping) and len(cast(Mapping[str, Any], row)) == 1:
        value: Any = next(iter(cast(Mapping[str, Any], row).values()))
        if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value
    return cast(Any, row)
