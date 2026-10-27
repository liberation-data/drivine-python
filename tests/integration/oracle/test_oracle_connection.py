import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine import Connection, CursorSpecification, DatabaseRegistry, PersistenceManager, QuerySpecification
from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels
from tests.integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE


@pytest.fixture
async def table(pm: PersistenceManager) -> AsyncIterator[None]:
    await pm.execute(QuerySpecification[Any]("DROP TABLE IF EXISTS drivine_conn_test"))
    await pm.execute(
        QuerySpecification[Any]("CREATE TABLE drivine_conn_test (id VARCHAR2(20) PRIMARY KEY, n NUMBER, doc JSON)")
    )
    yield
    await pm.execute(QuerySpecification[Any]("DROP TABLE drivine_conn_test"))


async def test_binds_named_parameters_a_map_as_json_and_reads_json_back_as_a_dict(pm, table):
    await pm.execute(
        QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n, doc) VALUES ($id, $n, JSON($doc))").bind(
            {"id": "a", "n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}
        )
    )
    row = await pm.get_one(
        QuerySpecification[Any]('SELECT n AS "n", doc AS "doc" FROM drivine_conn_test WHERE id = $id').bind({"id": "a"})
    )
    assert row == {"n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}


async def test_a_parameter_is_never_the_prefix_of_a_longer_one_and_unnamed_binds_are_left_out(pm):
    row = await pm.get_one(
        QuerySpecification[Any]('SELECT $id AS "id", $idList AS "list" FROM dual').bind(
            {"id": "one", "idList": "many", "unused": 3}
        )
    )
    assert row == {"id": "one", "list": "many"}


async def test_skips_and_limits_with_offset_and_fetch(pm):
    rows = await pm.query(
        QuerySpecification[Any]('SELECT LEVEL AS "n" FROM dual CONNECT BY LEVEL <= 5 ORDER BY LEVEL').skip(1).limit(2)
    )
    assert rows == [2, 3]


async def test_hands_back_the_rows_a_plsql_block_returns(pm):
    rows = await pm.query(
        QuerySpecification[Any](
            """
            DECLARE c SYS_REFCURSOR;
            BEGIN
                OPEN c FOR SELECT 'hello' AS "greeting", $who AS "who" FROM dual;
                DBMS_SQL.RETURN_RESULT(c);
            END;"""
        ).bind({"who": "world"})
    )
    assert rows == [{"greeting": "hello", "who": "world"}]


async def test_commits_outside_a_transaction_and_keeps_one_until_commit_or_rollback(
    pm, table, registry: DatabaseRegistry
):
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = await provider.connect()
    try:
        await connection.start_transaction()
        await connection.query(QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n) VALUES ('rolled', 1)"))
        await connection.rollback_transaction()
        await connection.start_transaction()
        await connection.query(QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n) VALUES ('kept', 2)"))
        await connection.commit_transaction()
    finally:
        await connection.release()
    ids = await pm.query(
        QuerySpecification[Any]("SELECT id AS \"id\" FROM drivine_conn_test WHERE id IN ('rolled', 'kept')")
    )
    assert ids == ["kept"]


async def _drop(pm: PersistenceManager, graph: OracleGraph) -> None:
    for statement in (
        f"DROP PROPERTY GRAPH IF EXISTS {graph.name}",
        f"DROP TABLE IF EXISTS {graph.edges} PURGE",
        f"DROP TABLE IF EXISTS {graph.nodes} PURGE",
    ):
        await pm.execute(QuerySpecification[Any](statement))


async def test_finds_a_node_of_several_labels_a_transaction_has_written_and_not_committed_by_a_test_of_them_all(
    pm, registry: DatabaseRegistry
):
    graph = OracleGraph("drivine_labels_test")
    await _drop(pm, graph)
    await pm.execute(QuerySpecification[Any](graph.ensuring))
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = await provider.connect()
    try:
        await connection.start_transaction()
        await connection.query(
            QuerySpecification[Any](
                f"""INSERT INTO {graph.nodes} (labels, props) VALUES (JSON('["Dog", "Animal"]'), JSON('{{}}'))"""
            )
        )
        found = await connection.query(
            QuerySpecification[Any](
                f'SELECT COUNT(*) AS "n" FROM {graph.nodes} n WHERE {has_labels("n", ["Dog", "Animal"])}'
            )
        )
        assert found == [1]
    finally:
        await connection.rollback_transaction()
        await connection.release()
        await _drop(pm, graph)


async def test_finds_a_label_after_the_first_by_its_whole_text_as_the_labels_json_writes_it(pm):
    graph = OracleGraph("drivine_labels_text_test")
    await _drop(pm, graph)
    try:
        await pm.execute(QuerySpecification[Any](graph.ensuring))
        for labels in (["Pet", "Dogs"], ["Pet", "Dog", 'it\'s "q"', "c\\d", "Ünïcødé"]):
            await pm.execute(
                QuerySpecification[Any](
                    f"INSERT INTO {graph.nodes} (labels, props) VALUES (JSON($labels), JSON('{{}}'))"
                ).bind({"labels": json.dumps(labels, ensure_ascii=False)})
            )

        async def count(labels: list[str]) -> int:
            rows = await pm.query(
                QuerySpecification[Any](f'SELECT COUNT(*) AS "n" FROM {graph.nodes} n WHERE {has_labels("n", labels)}')
            )
            return rows[0]

        assert await count(["Pet", "Dog"]) == 1
        assert await count(["Pet", "og"]) == 0
        assert await count(["Pet", 'it\'s "q"', "c\\d", "Ünïcødé"]) == 1
    finally:
        await _drop(pm, graph)


async def test_reads_a_cursor_in_batches(registry: DatabaseRegistry):
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = await provider.connect()
    try:
        cursor = await connection.open_cursor(
            CursorSpecification[Any]('SELECT LEVEL AS "n" FROM dual CONNECT BY LEVEL <= 7 ORDER BY LEVEL').batch_size(3)
        )
        seen = [it async for it in cursor]
        await cursor.close()
        assert seen == [1, 2, 3, 4, 5, 6, 7]
    finally:
        await connection.release()


DDL_WAIT_TEST = OracleGraph("drivine_ddl_wait_test")
"""The graph the tests of the ensure's wait on another session's lock make and drop."""


async def _drop_ddl_wait_test(pm: PersistenceManager) -> None:
    graph = DDL_WAIT_TEST
    await pm.execute(QuerySpecification[Any](f"DROP PROPERTY GRAPH IF EXISTS {graph.name}"))
    for table in (graph.edges, graph.nodes, graph.catalog):
        await pm.execute(QuerySpecification[Any](f"DROP TABLE IF EXISTS {table} PURGE"))


async def _turned_away_after(holder: Connection, maker: Connection) -> float:
    """
    How long DDL `maker` runs on the test graph's edges, while `holder` writes them, takes to be turned away
    (ORA-00054): at once unless `maker`'s session waits on DDL locks. CREATE INDEX, as ALTER TABLE ... ADD doesn't wait.
    """
    await holder.start_transaction()
    try:
        await holder.query(QuerySpecification[Any](f"LOCK TABLE {DDL_WAIT_TEST.edges} IN ROW EXCLUSIVE MODE"))
        started = time.monotonic()
        with pytest.raises(Exception, match="ORA-00054"):
            await maker.query(
                QuerySpecification[Any](f"CREATE INDEX {DDL_WAIT_TEST.name}_extra ON {DDL_WAIT_TEST.edges} (type)")
            )
        return time.monotonic() - started
    finally:
        await holder.rollback_transaction()


@pytest.mark.async_only
async def test_oracle_ensuring_makes_the_store_once_another_sessions_lock_goes(pm, registry: DatabaseRegistry):
    graph = DDL_WAIT_TEST
    provider = registry.connection_provider("oracle")
    assert provider is not None
    holder = await provider.connect()
    maker = await provider.connect()
    try:
        await _drop_ddl_wait_test(pm)
        await pm.execute(QuerySpecification[Any](graph.ensuring))
        # Made but for the property graph and an index on the edges, which the ensure makes again.
        await pm.execute(QuerySpecification[Any](f"DROP PROPERTY GRAPH {graph.name}"))
        await pm.execute(QuerySpecification[Any](f"DROP INDEX {graph.name}_edge_src"))

        await holder.start_transaction()
        await holder.query(QuerySpecification[Any](f"LOCK TABLE {graph.edges} IN ROW EXCLUSIVE MODE"))
        making = asyncio.create_task(maker.query(QuerySpecification[Any](graph.ensuring)))
        await asyncio.sleep(2)
        # Waiting on the lock, not turned away.
        assert not making.done()
        await holder.rollback_transaction()
        await making
        made = await pm.query(
            QuerySpecification[Any](
                f"SELECT COUNT(*) AS \"n\" FROM user_property_graphs WHERE graph_name = '{graph.name.upper()}'"
            )
        )
        assert made == [1]
    finally:
        await holder.release()
        await maker.release()
        await _drop_ddl_wait_test(pm)


async def test_oracle_ensuring_leaves_the_sessions_ddl_not_waiting_after_made_or_failed(pm, registry: DatabaseRegistry):
    graph = DDL_WAIT_TEST
    provider = registry.connection_provider("oracle")
    assert provider is not None
    holder = await provider.connect()
    maker = await provider.connect()
    try:
        await _drop_ddl_wait_test(pm)
        await maker.query(QuerySpecification[Any](graph.ensuring))
        assert await _turned_away_after(holder, maker) < 10

        # A node table without the key the edges reference: the block fails after the wait is set.
        await _drop_ddl_wait_test(pm)
        await pm.execute(QuerySpecification[Any](f"CREATE TABLE {graph.nodes} (x NUMBER)"))
        with pytest.raises(Exception, match="ORA-00904"):
            await maker.query(QuerySpecification[Any](graph.ensuring))
        await pm.execute(QuerySpecification[Any](f"CREATE TABLE {graph.edges} (type VARCHAR2(10))"))
        assert await _turned_away_after(holder, maker) < 10

        # And the measure tells the wait: a session that waits three seconds is turned away after them.
        await maker.query(QuerySpecification[Any]("ALTER SESSION SET DDL_LOCK_TIMEOUT = 3"))
        assert await _turned_away_after(holder, maker) >= 2.5
        await maker.query(QuerySpecification[Any]("ALTER SESSION SET DDL_LOCK_TIMEOUT = 0"))
    finally:
        await holder.release()
        await maker.release()
        await _drop_ddl_wait_test(pm)
