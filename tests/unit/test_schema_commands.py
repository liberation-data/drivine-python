"""
What runs schema DDL on a SQL store, with no store spoken to: one SchemaCommands for each database, however its managers
are asked for, and the aliases written byte for byte as drivine4j writes them.
"""

from __future__ import annotations

from typing import Any

import pytest

from drivine import (
    DatabaseRegistry,
    DatabaseType,
    DrivineError,
    PersistenceManagerFactory,
    QuerySpecification,
    transaction,
)
from drivine.backend.sql.sql_catalog import aliases_in, aliases_json
from drivine.manager.non_transactional_persistence_manager import NonTransactionalPersistenceManager
from drivine.query.statement import cypher_statement
from drivine.sync import PersistenceManagerFactory as SyncPersistenceManagerFactory
from drivine.sync.manager.non_transactional_persistence_manager import (
    NonTransactionalPersistenceManager as SyncNonTransactionalPersistenceManager,
)
from tests.unit.fakes import FakeProvider


def registry_of(*providers: FakeProvider) -> DatabaseRegistry:
    registry = DatabaseRegistry()
    for provider in providers:
        registry.register(provider)
    return registry


def postgres(name: str) -> FakeProvider:
    provider = FakeProvider(name)
    provider.type = DatabaseType.POSTGRES
    return provider


def test_the_managers_of_a_sql_database_asked_for_by_default_and_by_its_name_share_its_commands() -> None:
    registry = registry_of(postgres("store"), postgres("other"), FakeProvider("graph"))
    for factory in (PersistenceManagerFactory(registry), SyncPersistenceManagerFactory(registry)):
        by_default = factory.get("default", "NON_TRANSACTIONAL")
        by_name = factory.get("store", "NON_TRANSACTIONAL")
        other = factory.get("other", "NON_TRANSACTIONAL")
        graph = factory.get("graph", "NON_TRANSACTIONAL")
        assert isinstance(by_default, (NonTransactionalPersistenceManager, SyncNonTransactionalPersistenceManager))
        assert isinstance(by_name, (NonTransactionalPersistenceManager, SyncNonTransactionalPersistenceManager))
        assert isinstance(other, (NonTransactionalPersistenceManager, SyncNonTransactionalPersistenceManager))
        assert isinstance(graph, (NonTransactionalPersistenceManager, SyncNonTransactionalPersistenceManager))
        assert by_default is not by_name
        assert by_default.schema_commands is not None
        assert by_default.schema_commands is by_name.schema_commands
        assert by_name.schema_commands.database == "store"
        assert other.schema_commands is not None
        assert other.schema_commands is not by_name.schema_commands
        # A graph engine is sent the statement as it is, and has none.
        assert graph.schema_commands is None


def test_aliases_are_written_as_drivine4j_writes_them_and_read_back_as_written() -> None:
    aliases = ["by_name", 'a "quoted" name', "back\\slash", "tab\there", "ünï 🙂"]

    written = aliases_json(aliases)

    assert written == '["by_name","a \\"quoted\\" name","back\\\\slash","tab\\u0009here","ünï 🙂"]'
    assert aliases_in({"labelled": "x", "aliases": aliases}) == tuple(aliases)
    assert aliases_in({"labelled": "x"}) == ()
    assert aliases_in(None) == ()


async def test_each_way_into_a_sql_stores_managers_runs_db_await_indexes_as_nothing_and_refuses_ddl_in_a_transaction():
    provider = postgres("store")
    factory = PersistenceManagerFactory(registry_of(provider))
    delegating = factory.get("store")
    awaits = QuerySpecification[Any](cypher_statement("CALL db.awaitIndexes(300)"))

    # Outside a transaction it is a schema command, run as nothing, with no connection to the store.
    assert await delegating.query(awaits) == []
    await delegating.execute(awaits)
    assert await delegating.maybe_get_one(awaits) is None
    assert provider.events == []

    async with transaction(rollback=True):
        assert await delegating.query(awaits) == []
        with pytest.raises(DrivineError, match="not run inside a transaction on Postgres"):
            await delegating.execute(QuerySpecification[Any](cypher_statement("DROP INDEX x IF EXISTS")))
        # One the store can't honour is refused as the translator refuses it: YIELD, which Neo4j's gives nothing to.
        with pytest.raises(DrivineError, match="runs on its own"):
            await delegating.query(QuerySpecification[Any](cypher_statement("CALL db.awaitIndexes(300) YIELD x")))
    assert not any(":start:" in it for it in provider.events)

    # DDL sent as SQL is SQL, and a graph engine is sent the Cypher, both as they are.
    assert await delegating.query(QuerySpecification[Any]("CREATE INDEX x ON t (a)")) == ["CREATE INDEX x ON t (a)"]
    graph = PersistenceManagerFactory(registry_of(FakeProvider("graph"))).get("graph")
    assert await graph.query(awaits) == ["CALL db.awaitIndexes(300)"]
