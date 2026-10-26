"""A SQL store's graph tables, made at its provider's first connection: once, for threads that ask at once."""

import threading
from typing import Any

import pytest

from drivine import QuerySpecification, cypher_statement
from drivine.sync import PersistenceManagerFactory
from tests.sync_integration.conftest import ORACLE, POSTGRES
from tests.sync_integration.sql.new_graph import drop_graph, new_graph_registry

SQL_STORES = [*(["postgres"] if POSTGRES else []), *(["oracle"] if ORACLE else [])]


@pytest.mark.parametrize("store", SQL_STORES)
def test_makes_a_new_graphs_tables_for_threads_that_send_cypher_at_once(store: str, factory: PersistenceManagerFactory):
    registry, name = new_graph_registry(store)
    pm = PersistenceManagerFactory(registry).get(store)
    started = threading.Barrier(6)
    failures: list[BaseException] = []

    def create(n: int) -> None:
        started.wait()
        try:
            pm.execute(QuerySpecification[Any](cypher_statement("CREATE (:Racer {n: $n})")).bind({"n": n}))
        except BaseException as failure:
            failures.append(failure)

    threads = [threading.Thread(target=create, args=(it,)) for it in range(6)]
    try:
        for it in threads:
            it.start()
        for it in threads:
            it.join()
        assert failures == []
        assert pm.query(QuerySpecification[Any](cypher_statement("MATCH (r:Racer) RETURN count(r)"))) == [6]
    finally:
        registry.close()
        drop_graph(factory.get(store), name)
