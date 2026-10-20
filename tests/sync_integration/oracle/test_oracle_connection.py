# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_connection.py:
# change that, and run the script again.
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.sync import CursorSpecification, DatabaseRegistry, PersistenceManager, QuerySpecification
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
