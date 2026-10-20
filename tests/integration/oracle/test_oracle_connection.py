from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine import CursorSpecification, DatabaseRegistry, PersistenceManager, QuerySpecification
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
