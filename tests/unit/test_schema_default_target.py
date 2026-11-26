"""
A catalog for `default`, the first registered database, is planned there together with one for every database: each
database is planned once, by the name it is registered under, as drivine4j's SchemaTargets resolves a catalog's target
(drivine4j#854, #595). Planned apart, each warned the other's items were orphans of their owner on every enforce, and
the owner's inventory recorded one half or the other. SQLite in process, on the async and sync API.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

from drivine import DatabaseRegistry, PersistenceManagerFactory, SchemaCatalog, SchemaOptions, range_index
from drivine import SchemaManager as AsyncSchemaManager
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.schema.recorded_inventory import RecordedInventory, read_inventory
from drivine.schema.schema_manager import inventory_key
from drivine.sync import DatabaseRegistry as SyncDatabaseRegistry
from drivine.sync import PersistenceManagerFactory as SyncPersistenceManagerFactory
from drivine.sync import SchemaManager as SyncSchemaManager
from drivine.sync.manager.graph_object_manager_factory import (
    GraphObjectManagerFactory as SyncGraphObjectManagerFactory,
)

Api = Literal["async", "sync"]
APIS: list[Api] = ["async", "sync"]

PEOPLE = range_index("Person", "name")
CUSTOMERS = range_index("Customer", "id")


def catalogs() -> list[SchemaCatalog]:
    """One owner's items, some for the default database and some for every database."""
    return [SchemaCatalog.of(PEOPLE).for_default_database(), SchemaCatalog.of(CUSTOMERS)]


class Store:
    """
    `steps(store)` run on a SQLite database registered as `graph`, alone or after others: each step a call on the API,
    awaited on the async one. `store.manager(options)` is a SchemaManager over it, and `store.answers` what each step
    gave.
    """

    def __init__(self, api: Api, tmp_path: Path, databases: tuple[str, ...]) -> None:
        self.api = api
        env: dict[str, str] = {}
        for name in databases:
            env[f"{name.upper()}_DATABASE_TYPE"] = "SQLITE"
            env[f"{name.upper()}_DATABASE_NAME"] = str(tmp_path / f"{name}.db")
        self.registry: Any = DatabaseRegistry() if api == "async" else SyncDatabaseRegistry()
        for name in databases:
            self.registry.build_or_resolve_from_env(name, env)
        if api == "async":
            self.pms: Any = PersistenceManagerFactory(self.registry)
            self.goms: Any = GraphObjectManagerFactory(self.pms)
        else:
            self.pms = SyncPersistenceManagerFactory(self.registry)
            self.goms = SyncGraphObjectManagerFactory(self.pms)

    def manager(self, *catalogs: SchemaCatalog) -> Any:
        of = AsyncSchemaManager if self.api == "async" else SyncSchemaManager
        return of(self.registry, self.goms, SchemaOptions(list(catalogs)))

    def inventory(self, database: str, owner: str) -> Any:
        """The step that reads what the store records `owner` applied on `database`."""
        gom = self.goms.get(database)
        read = gom.persistence_manager.maybe_get_one(gom.backend.schema_marker("items").spec({"scope": owner}))

        def recorded(row: Any) -> RecordedInventory | None:
            return read_inventory(gom.backend.marker_items(row))

        if self.api == "sync":
            return recorded(read)

        async def answer() -> RecordedInventory | None:
            return recorded(await read)

        return answer()

    def ranges(self, database: str) -> Any:
        return self.goms.get(database).indexes.list("RANGE")


async def run(api: Api, tmp_path: Path, steps: Callable[[Store], Iterator[Any]], *databases: str) -> list[Any]:
    """What each step of `steps` gave, run on the async or the sync API."""
    store = Store(api, tmp_path, databases or ("graph",))
    answers: list[Any] = []
    try:
        for step in steps(store):
            answers.append(await step if api == "async" else step)
    finally:
        if api == "async":
            await store.registry.close()
        else:
            store.registry.close()
    return answers


def orphan_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [it.getMessage() for it in caplog.records if "no longer declares" in it.getMessage()]


@pytest.mark.parametrize("api", APIS)
async def test_a_catalog_for_default_and_one_for_every_database_are_planned_once_together_under_its_name(
    api: Api, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    def steps(store: Store) -> Iterator[Any]:
        manager = store.manager(*catalogs())
        yield manager.enforce()
        yield manager.enforce()
        yield store.manager(*catalogs()).recreate_all()
        yield store.inventory("graph", "schema")
        yield store.ranges("graph")

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        first, again, recreated, inventory, ranges = await run(api, tmp_path, steps)

    assert list(first) == ["graph"] and list(again) == ["graph"] and list(recreated) == ["graph"], (
        "one plan, under the database's registered name"
    )
    assert sorted(it.status for it in first["graph"]) == ["CREATED", "CREATED"]
    assert sorted(it.status for it in again["graph"]) == ["ALREADY_MATCHING", "ALREADY_MATCHING"]
    assert orphan_warnings(caplog) == [], "neither half is taken for an orphan of the other"
    assert inventory is not None and set(inventory.keys) == {inventory_key(PEOPLE), inventory_key(CUSTOMERS)}
    assert {(it.label, tuple(it.properties)) for it in ranges} >= {("Person", ("name",)), ("Customer", ("id",))}


@pytest.mark.parametrize("api", APIS)
async def test_items_ensured_at_runtime_for_default_are_those_of_the_database_by_its_name(
    api: Api, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """
    Runtime items are kept by the databases their catalog names, each by its registered name: ensured for `default`,
    they are removed by a catalog that names the database, and an enforce applies them there with the rest.
    """

    def steps(store: Store) -> Iterator[Any]:
        manager = store.manager(SchemaCatalog.of(CUSTOMERS))
        yield manager.enforce()
        yield manager.ensure_catalog(SchemaCatalog.of(PEOPLE).for_default_database())
        yield manager.enforce()
        yield manager.remove_catalog(SchemaCatalog.of(PEOPLE).for_database("graph"))
        yield manager.enforce()

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        _, ensured, with_it, _, without_it = await run(api, tmp_path, steps)

    assert list(ensured) == ["graph"]
    assert list(with_it) == ["graph"] and len(with_it["graph"]) == 2, "the runtime item is applied with the catalog's"
    assert list(without_it) == ["graph"] and len(without_it["graph"]) == 1, "removed by the database's name"
    assert orphan_warnings(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_default_is_the_first_registered_database_alone(
    api: Api, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    def steps(store: Store) -> Iterator[Any]:
        yield store.manager(*catalogs()).enforce()
        yield store.ranges("other")

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        report, other = await run(api, tmp_path, steps, "graph", "other")

    assert sorted(report) == ["graph", "other"]
    assert sorted(it.spec.label for it in report["graph"]) == ["Customer", "Person"]
    assert [it.spec.label for it in report["other"]] == ["Customer"], "the default catalog is graph's alone"
    assert ("Person", ("name",)) not in {(it.label, tuple(it.properties)) for it in other}
    assert orphan_warnings(caplog) == []


@pytest.mark.parametrize("api", APIS)
async def test_a_catalog_naming_default_and_the_database_by_name_targets_it_once(
    api: Api, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """
    `default` and the name it resolves to are one database: planned once, and the runtime items kept by it alone, so a
    catalog that names it by name removes them.
    """

    def steps(store: Store) -> Iterator[Any]:
        manager = store.manager(SchemaCatalog.of(CUSTOMERS))
        yield manager.ensure_catalog(SchemaCatalog.of(PEOPLE).for_databases("default", "graph"))
        yield manager.remove_catalog(SchemaCatalog.of(PEOPLE).for_database("graph"))
        yield manager.enforce()
        yield store.ranges("graph")

    with caplog.at_level(logging.WARNING, "drivine.schema"):
        ensured, _, enforced, ranges = await run(api, tmp_path, steps, "graph", "other")

    assert list(ensured) == ["graph"] and len(ensured["graph"]) == 1
    assert sorted(enforced) == ["graph", "other"] and [it.spec.label for it in enforced["graph"]] == ["Customer"]
    assert ("Person", ("name",)) not in {(it.label, tuple(it.properties)) for it in ranges}
    assert orphan_warnings(caplog) == []
