"""
The warning of a fragment whose NodeId no index serves (#552, drivine4j#828): once per fragment and database, only on
the databases where it belongs (drivine4j#848), and never raised. drivine4j's NodeIdIndexCheckTest, ported, with the
cases Drivine for TypeScript's NodeIdIndexCheck.spec adds, and SchemaManager's part: what it gives the check, on the
async and the sync API. No database is spoken to but SQLite's file, and the planner's requests aren't run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest

import drivine.schema.schema_manager as async_schema_manager
import drivine.sync.schema.schema_manager as sync_schema_manager
from drivine import (
    DatabaseRegistry,
    GraphProperty,
    NodeFragment,
    NodeId,
    PersistenceManagerFactory,
    RangeIndex,
    SchemaCatalog,
    SchemaOptions,
    full_text_index,
    range_index,
    uniqueness_constraint,
)
from drivine import SchemaManager as AsyncSchemaManager
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.model.model_registry import ModelRegistry
from drivine.schema import node_id_index_check
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.index_manager import IndexManager
from drivine.schema.planned_managers import PlannedConstraintManager, PlannedIndexManager
from drivine.schema.schema_executor import SchemaExecutor
from drivine.schema.schema_grammar import NEO4J_SCHEMA
from drivine.schema.schema_item import SchemaItem
from drivine.sync import DatabaseRegistry as SyncDatabaseRegistry
from drivine.sync import PersistenceManagerFactory as SyncPersistenceManagerFactory
from drivine.sync import SchemaManager as SyncSchemaManager
from drivine.sync.manager.graph_object_manager_factory import (
    GraphObjectManagerFactory as SyncGraphObjectManagerFactory,
)
from drivine.sync.schema.planned_managers import PlannedConstraintManager as SyncPlannedConstraintManager
from drivine.sync.schema.planned_managers import PlannedIndexManager as SyncPlannedIndexManager


class OrderFixture(NodeFragment, labels=["Order", "Purchase"]):
    id: Annotated[str, NodeId()]


class InvoiceFixture(NodeFragment, labels=["Invoice"]):
    number: Annotated[str, NodeId(), GraphProperty("invoice_no")]


class NoteWithoutIdFixture(NodeFragment, labels=["Note"]):
    text: str


ORDER_WARNING = (
    "@NodeId 'id' of OrderFixture has no index under :Order, so each save scans every :Order node. Its default index "
    "is off (class OrderFixture(NodeFragment, node_id_index=False) / SchemaOptions(index_node_ids=False), or "
    "OrderFixture isn't in a SchemaCatalog): declare RangeIndex() or Unique() on it, or "
    'indexes.ensure(range_index("Order", "id")).'
)


def on(database: str) -> str:
    """The words a warning ends with where there is more than one database: the one it is said of (drivine4j#848)."""
    return f" On database '{database}'."


def use(type_: type) -> None:
    """Builds the class's model in a registry of its own, as each object manager's registry does at its first use."""
    ModelRegistry().fragment(type_)


def range_(label: str, *properties: str) -> SchemaItem:
    return SchemaItem(label, properties)


unique = range_


def created(spec: Any) -> EnsureResult[Any]:
    return EnsureResult("CREATED", spec)


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    node_id_index_check.reset()
    yield
    node_id_index_check.reset()


@pytest.fixture
def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The warnings of an unindexed NodeId given so far, in order: read anew each time it is iterated."""
    caplog.set_level(logging.WARNING, logger="drivine.schema")

    class Warnings(list[str]):
        def _now(self) -> list[str]:
            return [
                it.getMessage()
                for it in caplog.records
                if it.levelno == logging.WARNING and it.getMessage().startswith("@NodeId")
            ]

        def __iter__(self) -> Iterator[str]:
            return iter(self._now())

        def __len__(self) -> int:
            return len(self._now())

        def __getitem__(self, index: Any) -> Any:
            return self._now()[index]

        def __eq__(self, other: object) -> bool:
            return self._now() == other

        def clear(self) -> None:
            caplog.clear()

    return Warnings()


def test_a_fragment_some_catalog_names_is_checked_only_on_the_databases_whose_catalogs_name_it_naming_the_database(
    warnings: list[str],
):
    node_id_index_check.managed()
    use(OrderFixture)
    use(InvoiceFixture)
    # Order's catalog is for a alone, Invoice's for b alone; neither has its id indexed.
    node_id_index_check.catalogued({"a": {"Order"}, "b": {"Invoice"}})
    node_id_index_check.listed("a", [], [])
    node_id_index_check.listed("b", [], [])
    assert [it for it in warnings if "OrderFixture" in it] == [ORDER_WARNING + on("a")]
    assert len([it for it in warnings if "InvoiceFixture" in it and it.endswith(on("b"))]) == 1
    assert len(warnings) == 2, "neither is warned of on the other's database"


def test_a_fragment_first_used_after_the_enforce_is_checked_only_where_it_belongs_and_where_a_later_ensure_names_it(
    warnings: list[str],
):
    node_id_index_check.managed()
    node_id_index_check.catalogued({"a": {"Purchase"}, "b": {"Invoice"}})
    node_id_index_check.listed("a", [], [])
    node_id_index_check.listed("b", [], [])
    use(OrderFixture)
    assert warnings == [ORDER_WARNING + on("a")], "named on a under its other label, Purchase"
    # A catalog ensured on b at runtime names Order there, and b is listed again.
    node_id_index_check.catalogued_too("b", ["Order"])
    node_id_index_check.listed("b", [], [])
    assert warnings == [ORDER_WARNING + on("a"), ORDER_WARNING + on("b")]
    # What b's catalogs named before is kept beside it: Invoice is still checked on b alone.
    use(InvoiceFixture)
    assert len(warnings[2:]) == 1
    assert "InvoiceFixture" in warnings[2] and warnings[2].endswith(on("b"))


def test_a_fragment_no_catalog_names_is_checked_on_every_database_listed(warnings: list[str]):
    node_id_index_check.managed()
    use(OrderFixture)
    node_id_index_check.catalogued({"a": {"Customer"}, "b": set()})
    node_id_index_check.listed("a", [], [])
    node_id_index_check.listed("b", [], [])
    assert warnings == [ORDER_WARNING + on("a"), ORDER_WARNING + on("b")]


def test_the_database_is_not_named_where_one_alone_is_catalogued_and_listed(warnings: list[str]):
    node_id_index_check.managed()
    use(OrderFixture)
    node_id_index_check.catalogued({"a": {"Order"}})
    node_id_index_check.listed("a", [], [])
    assert warnings == [ORDER_WARNING]


def test_a_fragment_no_index_serves_is_warned_of_once_per_database_in_drivine4js_words(warnings: list[str]):
    node_id_index_check.managed()
    use(OrderFixture)
    node_id_index_check.listed("default", [], [])
    node_id_index_check.listed("default", [], [])
    use(OrderFixture)
    assert warnings == [ORDER_WARNING]
    node_id_index_check.listed("users", [range_("Customer", "id")], [])
    # Another database, whose index is under a label the fragment hasn't: named, as there are two.
    assert warnings == [ORDER_WARNING, ORDER_WARNING + on("users")]
    # An index under any of the fragment's labels serves it: a save matches all of them.
    node_id_index_check.listed("archive", [range_("Purchase", "id")], [])
    assert len(warnings) == 2


def test_a_range_index_or_a_uniqueness_constraint_over_the_id_alone_under_one_of_its_labels_serves_it(
    warnings: list[str],
):
    node_id_index_check.managed()
    use(OrderFixture)
    node_id_index_check.listed("a", [range_("Order", "id")], [])
    node_id_index_check.listed("b", [], [unique("Order", "id")])
    assert warnings == []
    node_id_index_check.listed("c", [range_("Order", "id", "placedAt")], [unique("Order", "id", "line")])
    assert warnings == [ORDER_WARNING + on("c")], "an item over more properties serves no lookup by the id alone"


def test_an_item_serves_the_id_where_the_store_takes_it_for_one_over_the_id_alone_as_falkordb_lists_its_ranges(
    warnings: list[str],
):
    node_id_index_check.managed()
    use(OrderFixture)
    # FalkorDB: one item of every range property of the label, each its own index.
    node_id_index_check.listed("falkor", [range_("Order", "placedAt", "id")], [], ranges_per_property=True)
    assert warnings == []
    node_id_index_check.listed("neo", [range_("Order", "placedAt", "id")], [])
    assert warnings == [ORDER_WARNING + on("neo")], "a composite index elsewhere"
    # A constraint over more properties is still one over them all.
    node_id_index_check.listed("falkor2", [], [unique("Order", "id", "line")], ranges_per_property=True)
    assert len(warnings) == 2


def test_a_fragment_first_used_after_an_enforce_listed_is_checked_against_it_by_the_property_its_id_is_stored_under(
    warnings: list[str],
):
    node_id_index_check.managed()
    node_id_index_check.listed("default", [range_("Invoice", "number")], [])
    use(InvoiceFixture)
    assert len(warnings) == 1
    assert warnings[0].startswith("@NodeId 'invoice_no' of InvoiceFixture has no index under :Invoice")
    node_id_index_check.reset()
    warnings.clear()
    node_id_index_check.managed()
    node_id_index_check.listed("default", [range_("Invoice", "invoice_no")], [])
    use(InvoiceFixture)
    assert warnings == []


def test_an_index_or_a_constraint_ensured_after_an_enforce_listed_serves_a_fragment_first_used_later(
    warnings: list[str],
):
    node_id_index_check.managed()
    node_id_index_check.listed("default", [], [])
    node_id_index_check.ensured("default", range_index("Order", "id"), created(range_index("Order", "id")))
    use(OrderFixture)
    invoice = uniqueness_constraint("Invoice", "invoice_no")
    node_id_index_check.ensured("default", invoice, EnsureResult("ALREADY_MATCHING", invoice))
    use(InvoiceFixture)
    assert warnings == []

    # A recreate that ends a drift, as the managers' recreate reports it.
    node_id_index_check.reset()
    node_id_index_check.managed()
    node_id_index_check.listed("default", [], [unique("Order", "id", "line")])
    order = uniqueness_constraint("Order", "id")
    node_id_index_check.ensured("default", order, EnsureResult("RECREATED", order))
    use(OrderFixture)
    assert warnings == [], "a recreate that ends a drift serves it"

    # One that ignores case, of another kind, over more properties, not made, or on a database no enforce listed.
    node_id_index_check.reset()
    node_id_index_check.managed()
    node_id_index_check.listed("default", [], [])
    for spec in (
        range_index("Order", "id", ignore_case=True),
        full_text_index("Order", "id"),
        range_index("Order", ["id", "placedAt"]),
    ):
        node_id_index_check.ensured("default", spec, created(spec))
    node_id_index_check.ensured("default", order, EnsureResult("DRIFT", order, found=unique("Order", "id", "line")))
    node_id_index_check.ensured("other", range_index("Order", "id"), created(range_index("Order", "id")))
    use(OrderFixture)
    assert warnings == [ORDER_WARNING]


@pytest.mark.parametrize("api", ["async", "sync"])
def test_an_index_ensured_through_the_default_manager_serves_a_fragment_on_the_database_it_is_registered_as(
    api: str, tmp_path: Path, warnings: list[str]
):
    """
    An object manager asked for `default` is of the first registered database, which SchemaManager lists by its
    registered name: an index ensured through it serves a fragment there (drivine4j#886).
    """
    env = {"NEO_DATABASE_TYPE": "SQLITE", "NEO_DATABASE_NAME": str(tmp_path / "neo.db")}
    registry: Any = DatabaseRegistry() if api == "async" else SyncDatabaseRegistry()
    registry.build_or_resolve_from_env("neo", env)
    factory: Any = (
        GraphObjectManagerFactory(PersistenceManagerFactory(registry))
        if api == "async"
        else SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(registry))
    )
    gom = factory.get("default")
    assert gom.database == "default"
    assert gom.indexes.database == "neo" and gom.constraints.database == "neo"
    assert IndexManager(SchemaExecutor(registry.providers[0]), NEO4J_SCHEMA).database is None, (
        "a manager made on its own is of no database"
    )

    node_id_index_check.managed()
    node_id_index_check.listed("neo", [], [])
    node_id_index_check.ensured(gom.indexes.database, range_index("Order", "id"), created(range_index("Order", "id")))
    use(OrderFixture)
    assert warnings == []


def test_with_no_schema_manager_an_index_or_a_constraint_ensured_by_hand_that_serves_the_id_silences_the_later_warning(
    warnings: list[str],
):
    """
    With no SchemaManager, the index or constraint the warning names, ensured by hand, serves the fragment, on whichever
    database: the warning 30 s after its first use isn't given (drivine4j#886). One that doesn't serve it leaves it.
    """
    node_id_index_check.ensured("neo", range_index("Order", "id"), created(range_index("Order", "id")))
    use(OrderFixture)
    node_id_index_check.unmanaged(OrderFixture)
    # Ensured after the fragment's first use, before the warning is due: the order an application's startup gives.
    use(InvoiceFixture)
    invoice = uniqueness_constraint("Invoice", "invoice_no")
    node_id_index_check.ensured("neo", invoice, EnsureResult("ALREADY_MATCHING", invoice))
    node_id_index_check.unmanaged(InvoiceFixture)
    assert warnings == []

    # Under another of the fragment's labels serves it too: a save matches every label.
    node_id_index_check.reset()
    node_id_index_check.ensured("neo", range_index("Purchase", "id"), created(range_index("Purchase", "id")))
    use(OrderFixture)
    node_id_index_check.unmanaged(OrderFixture)
    assert warnings == [], "an index under another of its labels"

    node_id_index_check.reset()
    for spec in (range_index("Order", "id", ignore_case=True), range_index("Order", ["id", "placedAt"])):
        node_id_index_check.ensured("neo", spec, created(spec))
    order = uniqueness_constraint("Order", "id")
    node_id_index_check.ensured("neo", order, EnsureResult("DRIFT", order, found=unique("Order", "id", "line")))
    node_id_index_check.ensured(None, range_index("Order", "id"), created(range_index("Order", "id")))
    use(OrderFixture)
    node_id_index_check.unmanaged(OrderFixture)
    assert warnings == [ORDER_WARNING], "one that ignores case, over more properties, not made, or of no database"

    # Forgotten by a reset, as everything else is.
    node_id_index_check.reset()
    warnings.clear()
    node_id_index_check.ensured("neo", range_index("Order", "id"), created(range_index("Order", "id")))
    node_id_index_check.reset()
    use(OrderFixture)
    node_id_index_check.unmanaged(OrderFixture)
    assert warnings == [ORDER_WARNING], "after a reset"


def test_an_index_ensured_by_hand_before_an_enforce_gives_way_to_what_the_enforce_lists(warnings: list[str]):
    """
    What was ensured by hand before any enforce is not taken into what an enforce lists of the database later: that
    listing, read of the store, is what serves an id there from then on (drivine4j#886).
    """
    node_id_index_check.ensured("neo", range_index("Order", "id"), created(range_index("Order", "id")))
    use(OrderFixture)
    node_id_index_check.managed()
    node_id_index_check.listed("neo", [], [])
    assert warnings == [ORDER_WARNING], "the store, as listed, has no index serving it"

    node_id_index_check.reset()
    warnings.clear()
    node_id_index_check.ensured("neo", range_index("Order", "id"), created(range_index("Order", "id")))
    use(OrderFixture)
    node_id_index_check.managed()
    node_id_index_check.listed("neo", [range_("Order", "id")], [])
    assert warnings == [], "listed by the enforce"


def test_with_no_schema_manager_a_fragment_is_warned_of_later_and_not_where_one_was_built_by_then(
    warnings: list[str],
):
    use(OrderFixture)
    assert warnings == [], "not at its first use: a manager may yet be built"
    node_id_index_check.unmanaged(OrderFixture)
    assert warnings == [ORDER_WARNING]

    node_id_index_check.reset()
    warnings.clear()
    use(OrderFixture)
    node_id_index_check.managed()
    node_id_index_check.unmanaged(OrderFixture)
    assert warnings == []


def eventually(condition: Any, within: float = 5.0) -> bool:
    """Whether `condition()` holds within `within` seconds, asked every 10 ms."""
    until = time.monotonic() + within
    while not condition():
        if time.monotonic() >= until:
            return False
        time.sleep(0.01)
    return True


def test_the_warning_with_no_schema_manager_is_given_on_a_timer_which_a_manager_or_a_reset_stops(
    warnings: list[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(node_id_index_check, "UNMANAGED_DELAY", 0.2)
    use(OrderFixture)
    assert warnings == []
    assert eventually(lambda: warnings == [ORDER_WARNING])

    # A SchemaManager built by then: none.
    node_id_index_check.reset()
    warnings.clear()
    use(OrderFixture)
    node_id_index_check.managed()
    time.sleep(0.4)
    assert warnings == []

    # A timer set before a reset does nothing after it, though the class is admitted again and no manager built; the
    # one set after it warns.
    node_id_index_check.reset()
    use(OrderFixture)
    node_id_index_check.reset()
    monkeypatch.setattr(node_id_index_check, "UNMANAGED_DELAY", 0.6)
    use(OrderFixture)
    time.sleep(0.4)
    assert warnings == []
    assert eventually(lambda: warnings == [ORDER_WARNING])


def test_the_one_timer_gives_each_pending_warning_when_it_is_due_and_is_set_again_for_the_next(
    warnings: list[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(node_id_index_check, "UNMANAGED_DELAY", 0.3)
    use(OrderFixture)
    time.sleep(0.15)
    # Pending behind Order's, on the timer already set: given when it is due, not with Order's.
    use(InvoiceFixture)
    assert eventually(lambda: len(warnings) == 1)
    assert warnings == [ORDER_WARNING]
    assert eventually(lambda: len(warnings) == 2)
    assert warnings[1].startswith("@NodeId 'invoice_no' of InvoiceFixture has no index under :Invoice")


def test_a_fragment_with_no_node_id_is_never_warned_of(warnings: list[str]):
    node_id_index_check.managed()
    use(NoteWithoutIdFixture)
    node_id_index_check.listed("default", [], [])
    assert warnings == []


def test_a_check_that_fails_is_passed_over_so_the_model_is_still_built(monkeypatch: pytest.MonkeyPatch):
    node_id_index_check.managed()
    node_id_index_check.listed("default", [], [])

    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("no log")

    monkeypatch.setattr(node_id_index_check.logger, "warning", broken)
    assert ModelRegistry().fragment(OrderFixture).node_id_property == "id"
    node_id_index_check.listed("other", [], [])
    node_id_index_check.unmanaged(OrderFixture)


# ----- What SchemaManager gives the check -----


class SessionFixture(NodeFragment, labels=["Session"]):
    id: Annotated[str, NodeId()]


class RangedSessionFixture(NodeFragment, labels=["Session"]):
    id: Annotated[str, NodeId(), RangeIndex()]


class UnindexedSessionFixture(NodeFragment, labels=["UnindexedSession"], node_id_index=False):
    id: Annotated[str, NodeId()]


Api = str
APIS: list[Api] = ["async", "sync"]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    """
    What SchemaManager tells the check, in order, each call as (name, *arguments), the check then told it all the same;
    the planner's requests aren't run, and the SQL store's managers list nothing.
    """
    seen: list[tuple[Any, ...]] = []
    for name in ("managed", "catalogued", "catalogued_too", "listed", "ensured"):
        real = getattr(node_id_index_check, name)

        def spy(*args: Any, _name: str = name, _real: Any = real) -> Any:
            seen.append((_name, *args))
            return _real(*args)

        monkeypatch.setattr(node_id_index_check, name, spy)

    async def planned(*_: Any) -> dict[str, Any]:
        return {"owners": []}

    def planned_sync(*_: Any) -> dict[str, Any]:
        return {"owners": []}

    monkeypatch.setattr(async_schema_manager, "run_plan", planned)
    monkeypatch.setattr(sync_schema_manager, "run_plan", planned_sync)

    async def nothing(*_: Any) -> list[SchemaItem]:
        return []

    for of in (PlannedIndexManager, PlannedConstraintManager):
        monkeypatch.setattr(of, "list", nothing)
    for of in (SyncPlannedIndexManager, SyncPlannedConstraintManager):
        monkeypatch.setattr(of, "list", lambda *_: [])
    yield seen


async def run(api: Api, tmp_path: Path, steps: Any, databases: tuple[str, ...] = ("graph",)) -> None:
    """`steps(manager_of)`, each step awaited on the async API, `manager_of(options)` a SchemaManager over SQLite."""
    env = {}
    for name in databases:
        env[f"{name.upper()}_DATABASE_TYPE"] = "SQLITE"
        env[f"{name.upper()}_DATABASE_NAME"] = str(tmp_path / f"{name}.db")
    if api == "async":
        registry = DatabaseRegistry()
        for name in databases:
            registry.build_or_resolve_from_env(name, env)
        factory = GraphObjectManagerFactory(PersistenceManagerFactory(registry))
        try:
            for step in steps(lambda options: AsyncSchemaManager(registry, factory, options)):
                await step
        finally:
            await registry.close()
    else:
        sync_registry = SyncDatabaseRegistry()
        for name in databases:
            sync_registry.build_or_resolve_from_env(name, env)
        sync_factory = SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(sync_registry))
        try:
            for _ in steps(lambda options: SyncSchemaManager(sync_registry, sync_factory, options)):
                pass
        finally:
            sync_registry.close()


def named(calls: list[tuple[Any, ...]], name: str) -> list[tuple[Any, ...]]:
    return [it[1:] for it in calls if it[0] == name]


@pytest.mark.parametrize("api", APIS)
async def test_the_check_is_given_each_databases_catalogued_labels_before_it_lists_and_an_ensured_catalogs_after(
    api: Api, tmp_path: Path, calls: list[tuple[Any, ...]]
):
    def steps(manager_of: Any) -> Iterator[Any]:
        manager = manager_of(
            SchemaOptions(
                [
                    # 'default' is the first registered, graph: its labels are graph's.
                    SchemaCatalog.of(range_index("Person", "name")).for_default_database(),
                    # A fragment read with its default off declares nothing: it belongs on 'other' all the same.
                    SchemaCatalog.from_fragments(UnindexedSessionFixture).for_database("other"),
                    SchemaCatalog.of(range_index("Customer", "id")),
                ]
            )
        )
        yield manager.enforce()
        yield manager.ensure_catalog(SchemaCatalog.of(range_index("Order", "at")).named("app").for_database("other"))
        # An ensured fragment catalog names its fragments' labels too, not only its items'.
        yield manager.ensure_catalog(SchemaCatalog.from_fragments(OrderFixture).named("orders").for_database("other"))
        # A later enforce names what was ensured among the rest, on the databases it was ensured on.
        yield manager.enforce()

    await run(api, tmp_path, steps, ("graph", "other"))
    # Each enforce lists each database it applies catalogs on once, by its registered name: 'default' is graph's.
    order = [
        it[:2] if it[0] == "listed" else it[:1] for it in calls if it[0] in ("catalogued", "catalogued_too", "listed")
    ]
    assert order == [
        ("catalogued",),
        *(("listed", it) for it in ("graph", "other")),
        ("catalogued_too",),
        ("listed", "other"),
        ("catalogued_too",),
        ("listed", "other"),
        ("catalogued",),
        *(("listed", it) for it in ("graph", "other")),
    ]
    first, again = (labels for (labels,) in named(calls, "catalogued"))
    assert {db: sorted(it) for db, it in first.items()} == {
        "graph": ["Customer", "Person"],
        "other": ["Customer", "UnindexedSession"],
    }
    assert [(db, sorted(labels)) for db, labels in named(calls, "catalogued_too")] == [
        ("other", ["Order"]),
        ("other", ["Order", "Order", "Purchase"]),
    ]
    assert {db: sorted(it) for db, it in again.items()} == {
        "graph": ["Customer", "Person"],
        "other": ["Customer", "Order", "UnindexedSession"],
    }


@pytest.mark.parametrize("api", APIS)
async def test_an_ensured_catalog_is_not_catalogued_on_a_database_no_enforce_listed(
    api: Api, tmp_path: Path, calls: list[tuple[Any, ...]]
):
    def steps(manager_of: Any) -> Iterator[Any]:
        manager = manager_of(SchemaOptions([SchemaCatalog.of(range_index("Person", "name")).for_database("graph")]))
        yield manager.enforce()
        # 'other' was never listed: naming Order there would stop it being checked on 'graph', where it is used.
        yield manager.ensure_catalog(SchemaCatalog.of(range_index("Order", "at")).named("app").for_database("other"))

    await run(api, tmp_path, steps, ("graph", "other"))
    assert named(calls, "catalogued_too") == []
    assert [it[0] for it in named(calls, "listed")] == ["graph"]


@pytest.mark.parametrize("api", APIS)
async def test_what_serves_an_id_is_listed_after_an_enforce_and_a_recreate_all_and_after_an_ensure_once_one_listed(
    api: Api, tmp_path: Path, calls: list[tuple[Any, ...]]
):
    def steps(manager_of: Any) -> Iterator[Any]:
        manager = manager_of(SchemaOptions([SchemaCatalog.from_fragments(SessionFixture)]))
        yield manager.ensure("app", range_index("Session", "name"))
        yield manager.enforce()
        yield manager.recreate_all()
        yield manager.ensure("app", range_index("Session", "at"))

    await run(api, tmp_path, steps)
    # No enforce had listed the database at the first ensure: it lists nothing.
    assert [(it[0], it[1], it[2], it[3]) for it in named(calls, "listed")] == [("graph", [], [], False)] * 3


@pytest.mark.parametrize("api", APIS)
async def test_a_listing_that_fails_is_passed_over_so_the_enforce_still_succeeds(
    api: Api, tmp_path: Path, calls: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
):
    async def failing(*_: Any) -> list[SchemaItem]:
        raise RuntimeError("no listing")

    def failing_sync(*_: Any) -> list[SchemaItem]:
        raise RuntimeError("no listing")

    monkeypatch.setattr(PlannedIndexManager, "list", failing)
    monkeypatch.setattr(SyncPlannedIndexManager, "list", failing_sync)
    reports: list[Any] = []

    def steps(manager_of: Any) -> Iterator[Any]:
        manager = manager_of(SchemaOptions([SchemaCatalog.from_fragments(SessionFixture)]))
        if api == "async":

            async def enforced() -> None:
                reports.append(await manager.enforce())

            yield enforced()
        else:
            reports.append(manager.enforce())
            yield None

    await run(api, tmp_path, steps)
    assert reports == [{"graph": []}]
    assert named(calls, "listed") == []


@pytest.mark.parametrize("api", APIS)
async def test_a_manager_counts_as_managed_only_with_an_item_to_apply(
    api: Api, tmp_path: Path, calls: list[tuple[Any, ...]]
):
    def steps(manager_of: Any) -> Iterator[Any]:
        manager_of(SchemaOptions([SchemaCatalog.of()]))
        manager_of(SchemaOptions([SchemaCatalog.from_fragments(SessionFixture)], index_node_ids=False))
        assert named(calls, "managed") == []
        manager_of(SchemaOptions([SchemaCatalog.from_fragments(SessionFixture, RangedSessionFixture)]))
        assert named(calls, "managed") == [()]
        yield from ()

    await run(api, tmp_path, steps)


@pytest.mark.parametrize("api", APIS)
async def test_an_index_or_a_constraint_ensured_or_recreated_by_hand_is_told_to_the_check_under_the_registered_name(
    api: Api, tmp_path: Path, calls: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
):
    order, invoice = range_index("Order", "id"), uniqueness_constraint("Invoice", "invoice_no")
    results = iter(["CREATED", "RECREATED", "ALREADY_MATCHING", "RECREATED"])

    async def result(self: Any, call: str, spec: Any, *_: Any) -> EnsureResult[Any]:
        return EnsureResult(next(results), spec)  # pyright: ignore[reportArgumentType]

    def result_sync(self: Any, call: str, spec: Any, *_: Any) -> EnsureResult[Any]:
        return EnsureResult(next(results), spec)  # pyright: ignore[reportArgumentType]

    for of in (PlannedIndexManager, PlannedConstraintManager):
        monkeypatch.setattr(of, "_result", result)
    for of in (SyncPlannedIndexManager, SyncPlannedConstraintManager):
        monkeypatch.setattr(of, "_result", result_sync)
    env = {"GRAPH_DATABASE_TYPE": "SQLITE", "GRAPH_DATABASE_NAME": str(tmp_path / "graph.db")}
    if api == "async":
        registry = DatabaseRegistry()
        registry.build_or_resolve_from_env("graph", env)
        gom = GraphObjectManagerFactory(PersistenceManagerFactory(registry)).get()
        try:
            await gom.indexes.ensure(order)
            await gom.indexes.recreate(order)
            await gom.constraints.ensure(invoice)
            await gom.constraints.recreate(invoice)
        finally:
            await registry.close()
    else:
        sync_registry = SyncDatabaseRegistry()
        sync_registry.build_or_resolve_from_env("graph", env)
        sync_gom = SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(sync_registry)).get()
        try:
            sync_gom.indexes.ensure(order)
            sync_gom.indexes.recreate(order)
            sync_gom.constraints.ensure(invoice)
            sync_gom.constraints.recreate(invoice)
        finally:
            sync_registry.close()
    assert [(database, spec, result.status) for database, spec, result in named(calls, "ensured")] == [
        ("graph", order, "CREATED"),
        ("graph", order, "RECREATED"),
        ("graph", invoice, "ALREADY_MATCHING"),
        ("graph", invoice, "RECREATED"),
    ]


class CustomerFixture(NodeFragment, labels=["Customer"]):
    id: Annotated[str, NodeId()]


@pytest.mark.parametrize("api", APIS)
async def test_a_database_applied_as_default_and_by_its_name_is_listed_once_all_its_catalogs_are_applied(
    api: Api, tmp_path: Path, warnings: list[str]
):
    """
    A catalog for `default` and one for every database are applied on the first registered database apart, as
    `default` and by its name: it is listed only once both are, so the index the second makes isn't taken for missing.
    """
    use(CustomerFixture)

    def steps(manager_of: Any) -> Iterator[Any]:
        manager = manager_of(
            SchemaOptions(
                [
                    SchemaCatalog.of(range_index("Person", "name")).for_default_database(),
                    SchemaCatalog.from_fragments(CustomerFixture),
                ]
            )
        )
        yield manager.enforce()

    await run(api, tmp_path, steps)
    assert warnings == []


@pytest.mark.parametrize("api", APIS)
async def test_a_graph_engines_managers_tell_the_check_what_they_ensure_or_recreate_under_the_database_they_are_of(
    api: Api, calls: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
):
    """
    On a graph engine the managers are drivine-python's own, not the planned ones: each ensure and recreate is told,
    under the `database` they were made with, and nothing of the store is read for it. The store's part is stubbed.
    """
    from drivine.schema.constraint_manager import ConstraintManager
    from drivine.sync.schema.constraint_manager import ConstraintManager as SyncConstraintManager
    from drivine.sync.schema.index_manager import IndexManager as SyncIndexManager

    indexes_of, constraints_of = (
        (IndexManager, ConstraintManager) if api == "async" else (SyncIndexManager, SyncConstraintManager)
    )

    def stub(of: type, name: str, value: Any = None) -> None:
        if api == "async":

            async def answer(*_: Any, **__: Any) -> Any:
                return value(*_) if callable(value) else value

            monkeypatch.setattr(of, name, answer)
        else:
            monkeypatch.setattr(of, name, lambda *_, **__: value(*_) if callable(value) else value)

    for name in ("_find", "_create", "_await_built", "_log_effective", "drop"):
        stub(indexes_of, name)
    stub(constraints_of, "_ensure_made", lambda _self, spec, *__: EnsureResult("CREATED", spec))
    stub(constraints_of, "list", [])
    stub(constraints_of, "drop")
    order, invoice = range_index("Order", "id"), uniqueness_constraint("Invoice", "invoice_no")
    indexes = indexes_of(SchemaExecutor(None), NEO4J_SCHEMA, database="neo")  # pyright: ignore[reportArgumentType]
    constraints = constraints_of(SchemaExecutor(None), NEO4J_SCHEMA, database="neo")  # pyright: ignore[reportArgumentType]
    for step in (
        indexes.ensure(order),
        indexes.recreate(order),
        constraints.ensure(invoice),
        constraints.recreate(invoice),
    ):
        if api == "async":
            await step
    assert [(database, spec, result.status) for database, spec, result in named(calls, "ensured")] == [
        ("neo", order, "CREATED"),
        ("neo", order, "RECREATED"),
        ("neo", invoice, "CREATED"),
        ("neo", invoice, "CREATED"),
    ], "a constraint's recreate is told once, as the ensure it makes it again by"


def test_a_catalogs_fragment_labels_are_kept_through_every_operation_on_it():
    """Where a fragment belongs is read from them, whatever its catalog is made into (drivine4j#848)."""
    catalog = SchemaCatalog.from_fragments(OrderFixture)
    labels = frozenset({"Order", "Purchase"})
    assert catalog.fragment_labels == labels
    for made in (
        catalog.named("app"),
        catalog.with_version("2"),
        catalog.for_database("other"),
        catalog.for_default_database(),
        catalog.without_node_id_indexes(),
        catalog.beside([uniqueness_constraint("Order", "id")]),
        catalog + [range_index("Order", "at")],
    ):
        assert made.fragment_labels == labels
    assert SchemaCatalog.of(range_index("Order", "id")).fragment_labels == frozenset()
