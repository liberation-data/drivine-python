# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_connection.py:
# change that, and run the script again.
import json
import re
import time
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.backend.oracle.oracle_graph import OracleGraph, has_labels
from drivine.query.statement import cypher_statement
from drivine.sync import Connection, CursorSpecification, DatabaseRegistry, PersistenceManager, QuerySpecification
from drivine.sync.connection.oracle.oracle_connection_provider import oracle_refusal
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE


@pytest.fixture
def table(pm: PersistenceManager) -> Iterator[None]:
    pm.execute(QuerySpecification[Any]("DROP TABLE IF EXISTS drivine_conn_test"))
    pm.execute(
        QuerySpecification[Any]("CREATE TABLE drivine_conn_test (id VARCHAR2(20) PRIMARY KEY, n NUMBER, doc JSON)")
    )
    yield
    pm.execute(QuerySpecification[Any]("DROP TABLE drivine_conn_test"))


def test_binds_named_parameters_a_map_as_json_and_reads_json_back_as_a_dict(pm, table):
    pm.execute(
        QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n, doc) VALUES ($id, $n, JSON($doc))").bind(
            {"id": "a", "n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}
        )
    )
    row = pm.get_one(
        QuerySpecification[Any]('SELECT n AS "n", doc AS "doc" FROM drivine_conn_test WHERE id = $id').bind({"id": "a"})
    )
    assert row == {"n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}


def test_a_parameter_is_never_the_prefix_of_a_longer_one_and_unnamed_binds_are_left_out(pm):
    row = pm.get_one(
        QuerySpecification[Any]('SELECT $id AS "id", $idList AS "list" FROM dual').bind(
            {"id": "one", "idList": "many", "unused": 3}
        )
    )
    assert row == {"id": "one", "list": "many"}


def test_skips_and_limits_with_offset_and_fetch(pm):
    rows = pm.query(
        QuerySpecification[Any]('SELECT LEVEL AS "n" FROM dual CONNECT BY LEVEL <= 5 ORDER BY LEVEL').skip(1).limit(2)
    )
    assert rows == [2, 3]


def test_hands_back_the_rows_a_plsql_block_returns(pm):
    rows = pm.query(
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


def test_commits_outside_a_transaction_and_keeps_one_until_commit_or_rollback(pm, table, registry: DatabaseRegistry):
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = provider.connect()
    try:
        connection.start_transaction()
        connection.query(QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n) VALUES ('rolled', 1)"))
        connection.rollback_transaction()
        connection.start_transaction()
        connection.query(QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n) VALUES ('kept', 2)"))
        connection.commit_transaction()
    finally:
        connection.release()
    ids = pm.query(QuerySpecification[Any]("SELECT id AS \"id\" FROM drivine_conn_test WHERE id IN ('rolled', 'kept')"))
    assert ids == ["kept"]


def _drop(pm: PersistenceManager, graph: OracleGraph) -> None:
    for statement in (
        f"DROP PROPERTY GRAPH IF EXISTS {graph.name}",
        f"DROP TABLE IF EXISTS {graph.edges} PURGE",
        f"DROP TABLE IF EXISTS {graph.nodes} PURGE",
    ):
        pm.execute(QuerySpecification[Any](statement))


def test_finds_a_node_of_several_labels_a_transaction_has_written_and_not_committed_by_a_test_of_them_all(
    pm, registry: DatabaseRegistry
):
    graph = OracleGraph("drivine_labels_test")
    _drop(pm, graph)
    pm.execute(QuerySpecification[Any](graph.ensuring))
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = provider.connect()
    try:
        connection.start_transaction()
        connection.query(
            QuerySpecification[Any](
                f"""INSERT INTO {graph.nodes} (labels, props) VALUES (JSON('["Dog", "Animal"]'), JSON('{{}}'))"""
            )
        )
        found = connection.query(
            QuerySpecification[Any](
                f'SELECT COUNT(*) AS "n" FROM {graph.nodes} n WHERE {has_labels("n", ["Dog", "Animal"])}'
            )
        )
        assert found == [1]
    finally:
        connection.rollback_transaction()
        connection.release()
        _drop(pm, graph)


def test_finds_a_label_after_the_first_by_its_whole_text_as_the_labels_json_writes_it(pm):
    graph = OracleGraph("drivine_labels_text_test")
    _drop(pm, graph)
    try:
        pm.execute(QuerySpecification[Any](graph.ensuring))
        for labels in (["Pet", "Dogs"], ["Pet", "Dog", 'it\'s "q"', "c\\d", "Ünïcødé"]):
            pm.execute(
                QuerySpecification[Any](
                    f"INSERT INTO {graph.nodes} (labels, props) VALUES (JSON($labels), JSON('{{}}'))"
                ).bind({"labels": json.dumps(labels, ensure_ascii=False)})
            )

        def count(labels: list[str]) -> int:
            rows = pm.query(
                QuerySpecification[Any](f'SELECT COUNT(*) AS "n" FROM {graph.nodes} n WHERE {has_labels("n", labels)}')
            )
            return rows[0]

        assert count(["Pet", "Dog"]) == 1
        assert count(["Pet", "og"]) == 0
        assert count(["Pet", 'it\'s "q"', "c\\d", "Ünïcødé"]) == 1
    finally:
        _drop(pm, graph)


def test_reads_a_cursor_in_batches(registry: DatabaseRegistry):
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = provider.connect()
    try:
        cursor = connection.open_cursor(
            CursorSpecification[Any]('SELECT LEVEL AS "n" FROM dual CONNECT BY LEVEL <= 7 ORDER BY LEVEL').batch_size(3)
        )
        seen = [it for it in cursor]
        cursor.close()
        assert seen == [1, 2, 3, 4, 5, 6, 7]
    finally:
        connection.release()


def test_the_test_image_is_oracle_23_9_or_later_by_the_drivers_version_and_runs_cyphers_random_uuid(
    registry: DatabaseRegistry,
):
    # python-oracledb reads the version as it connects, which needs no grant (#252, as liberation-data/drivine4j#401).
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection: Any = provider.connect()
    try:
        assert oracle_refusal(connection.connection.version) is None, connection.connection.version
        [uuid] = connection.query(QuerySpecification[Any](cypher_statement("RETURN randomUUID()")))
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", uuid), uuid
    finally:
        connection.release()


DDL_WAIT_TEST = OracleGraph("drivine_ddl_wait_test")
"""The graph the tests of the ensure's wait on another session's lock make and drop."""


def _drop_ddl_wait_test(pm: PersistenceManager) -> None:
    graph = DDL_WAIT_TEST
    pm.execute(QuerySpecification[Any](f"DROP PROPERTY GRAPH IF EXISTS {graph.name}"))
    for table in (graph.edges, graph.nodes, graph.catalog):
        pm.execute(QuerySpecification[Any](f"DROP TABLE IF EXISTS {table} PURGE"))


def _turned_away_after(holder: Connection, maker: Connection) -> float:
    """
    How long DDL `maker` runs on the test graph's edges, while `holder` writes them, takes to be turned away
    (ORA-00054): at once unless `maker`'s session waits on DDL locks. CREATE INDEX, as ALTER TABLE ... ADD doesn't wait.
    """
    holder.start_transaction()
    try:
        holder.query(QuerySpecification[Any](f"LOCK TABLE {DDL_WAIT_TEST.edges} IN ROW EXCLUSIVE MODE"))
        started = time.monotonic()
        with pytest.raises(Exception, match="ORA-00054"):
            maker.query(
                QuerySpecification[Any](f"CREATE INDEX {DDL_WAIT_TEST.name}_extra ON {DDL_WAIT_TEST.edges} (type)")
            )
        return time.monotonic() - started
    finally:
        holder.rollback_transaction()


def test_oracle_ensuring_leaves_the_sessions_ddl_not_waiting_after_made_or_failed(pm, registry: DatabaseRegistry):
    graph = DDL_WAIT_TEST
    provider = registry.connection_provider("oracle")
    assert provider is not None
    holder = provider.connect()
    maker = provider.connect()
    try:
        _drop_ddl_wait_test(pm)
        maker.query(QuerySpecification[Any](graph.ensuring))
        assert _turned_away_after(holder, maker) < 10

        # A node table without the key the edges reference: the block fails after the wait is set.
        _drop_ddl_wait_test(pm)
        pm.execute(QuerySpecification[Any](f"CREATE TABLE {graph.nodes} (x NUMBER)"))
        with pytest.raises(Exception, match="ORA-00904"):
            maker.query(QuerySpecification[Any](graph.ensuring))
        pm.execute(QuerySpecification[Any](f"CREATE TABLE {graph.edges} (type VARCHAR2(10))"))
        assert _turned_away_after(holder, maker) < 10

        # And the measure tells the wait: a session that waits three seconds is turned away after them.
        maker.query(QuerySpecification[Any]("ALTER SESSION SET DDL_LOCK_TIMEOUT = 3"))
        assert _turned_away_after(holder, maker) >= 2.5
        maker.query(QuerySpecification[Any]("ALTER SESSION SET DDL_LOCK_TIMEOUT = 0"))
    finally:
        holder.release()
        maker.release()
        _drop_ddl_wait_test(pm)
