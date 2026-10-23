"""
With nothing to apply, the schema manager says so, and says which: no catalog at all, or catalogs that are all empty,
named. drivine4j's SchemaManagerNothingToApplyTest. No database is spoken to, so none is registered.
"""

import logging

import pytest

from drivine import DatabaseRegistry, PersistenceManagerFactory, SchemaCatalog, SchemaOptions, range_index
from drivine import SchemaManager as AsyncSchemaManager
from drivine.drivine_assembly import DrivineAssembly
from drivine.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.sync import PersistenceManagerFactory as SyncPersistenceManagerFactory
from drivine.sync import SchemaManager as SyncSchemaManager
from drivine.sync.drivine_assembly import DrivineAssembly as SyncDrivineAssembly
from drivine.sync.manager.graph_object_manager_factory import (
    GraphObjectManagerFactory as SyncGraphObjectManagerFactory,
)


def manager(*catalogs: SchemaCatalog) -> AsyncSchemaManager:
    registry = DatabaseRegistry()
    return AsyncSchemaManager(
        registry, GraphObjectManagerFactory(PersistenceManagerFactory(registry)), SchemaOptions(list(catalogs))
    )


def sync_manager(*catalogs: SchemaCatalog) -> SyncSchemaManager:
    registry = DatabaseRegistry()
    return SyncSchemaManager(
        registry, SyncGraphObjectManagerFactory(SyncPersistenceManagerFactory(registry)), SchemaOptions(list(catalogs))
    )


def lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [it for it in caplog.records if it.name == "drivine.schema"]


def only_line(caplog: pytest.LogCaptureFixture) -> str:
    records = lines(caplog)
    assert len(records) == 1, [it.getMessage() for it in records]
    assert records[0].levelno == logging.INFO
    return records[0].getMessage()


async def test_no_catalog_at_all_is_said_at_info_with_the_catalogs_read_once(caplog):
    with caplog.at_level(logging.INFO, "drivine.schema"):
        assert await manager().enforce() == {}

    line = only_line(caplog)
    assert "found no SchemaCatalog; nothing to apply" in line
    assert line.endswith(
        "Catalogs are read once, when the SchemaManager is built; schema known later is given to SchemaManager.ensure."
    )


async def test_catalogs_that_are_all_empty_are_named_an_unnamed_one_as_unnamed(caplog):
    catalogs = (
        SchemaCatalog.of().named("dice"),
        SchemaCatalog.of(),
        SchemaCatalog.of().named("rag").for_database("neo"),
        SchemaCatalog.of(),
    )

    with caplog.at_level(logging.INFO, "drivine.schema"):
        assert await manager(*catalogs).enforce() == {}

    line = only_line(caplog)
    assert "found 4 SchemaCatalog(s), all empty: 'dice', unnamed (2), 'rag'; nothing to apply" in line
    assert line.endswith(
        "Catalogs are read once, when the SchemaManager is built; schema known later is given to SchemaManager.ensure."
    )


async def test_every_way_in_says_it(caplog):
    schema = manager(SchemaCatalog.of().named("dice"))

    with caplog.at_level(logging.INFO, "drivine.schema"):
        await schema.enforce()
        await schema.recreate_all()
        sync_manager(SchemaCatalog.of().named("dice")).enforce()
        sync_manager(SchemaCatalog.of().named("dice")).recreate_all()

    assert len(lines(caplog)) == 4
    assert all("all empty: 'dice'" in it.getMessage() for it in lines(caplog))


async def test_items_ensured_at_runtime_are_something_to_apply(caplog):
    # With no catalog, and with only an empty one, items given to `ensure` are applied by every way in, never reported
    # as nothing to apply. No database is registered, so they apply to none.
    for schema in (manager(), manager(SchemaCatalog.of().named("dice"))):
        await schema.ensure("dice", range_index("Proposition", ["text"]))
        with caplog.at_level(logging.INFO, "drivine.schema"):
            assert await schema.enforce() == {}
            assert await schema.recreate_all() == {}
    for sync_schema in (sync_manager(), sync_manager(SchemaCatalog.of().named("dice"))):
        sync_schema.ensure("dice", range_index("Proposition", ["text"]))
        with caplog.at_level(logging.INFO, "drivine.schema"):
            assert sync_schema.enforce() == {}
            assert sync_schema.recreate_all() == {}

    assert [it.getMessage() for it in lines(caplog) if "nothing to apply" in it.getMessage()] == []


async def test_the_sync_manager_says_it_in_the_same_words(caplog):
    with caplog.at_level(logging.INFO, "drivine.schema"):
        await manager().enforce()
        sync_manager().enforce()
        await manager(SchemaCatalog.of().named("dice"), SchemaCatalog.of()).enforce()
        sync_manager(SchemaCatalog.of().named("dice"), SchemaCatalog.of()).enforce()

    said = [it.getMessage() for it in lines(caplog)]
    assert len(said) == 4
    assert said[0] == said[1]
    assert said[2] == said[3]


class _NoDatabases(DrivineAssembly):
    def register_databases(self, registry: DatabaseRegistry) -> None:
        pass


class _NoSyncDatabases(SyncDrivineAssembly):
    def register_databases(self, registry: DatabaseRegistry) -> None:
        pass


async def test_the_start_of_an_assembly_with_no_catalog_says_it(caplog):
    with caplog.at_level(logging.INFO, "drivine.schema"):
        await _NoDatabases().start()
        _NoSyncDatabases().start()

    assert len(lines(caplog)) == 2
    assert all("found no SchemaCatalog" in it.getMessage() for it in lines(caplog))
