from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import pytest

from drivine import CursorSpecification, DatabaseRegistry, PersistenceManager, QuerySpecification
from tests.integration.postgres.conftest import ON_POSTGRES

pytestmark = ON_POSTGRES


@pytest.fixture
async def table(pm: PersistenceManager) -> AsyncIterator[None]:
    await pm.execute(QuerySpecification[Any]("DROP TABLE IF EXISTS drivine_conn_test"))
    await pm.execute(QuerySpecification[Any]("CREATE TABLE drivine_conn_test (id text PRIMARY KEY, n int, doc jsonb)"))
    yield
    await pm.execute(QuerySpecification[Any]("DROP TABLE drivine_conn_test"))


async def test_binds_named_parameters_a_map_as_jsonb_and_reads_jsonb_back_as_a_dict(pm, table):
    await pm.execute(
        QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n, doc) VALUES ($id, $n, $doc)").bind(
            {"id": "a", "n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}
        )
    )
    row = await pm.get_one(
        QuerySpecification[Any]("SELECT n, doc FROM drivine_conn_test WHERE id = $id").bind({"id": "a"})
    )
    assert row == {"n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}


class Level(Enum):
    HIGH = 2


async def test_binds_a_map_holding_a_date_and_an_enum_as_a_save_writes_them(pm):
    doc = {"at": datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC), "level": Level.HIGH}
    row = await pm.get_one(QuerySpecification[Any]("SELECT CAST($doc AS jsonb) AS doc").bind({"doc": doc}))
    assert row == {"doc": {"at": "2024-01-02T03:04:05.000Z", "level": 2}}


async def test_binds_a_list_of_whole_and_fractional_numbers_as_one_of_floats(pm):
    row = await pm.get_one(QuerySpecification[Any]("SELECT CAST($v AS float8[]) AS v").bind({"v": [1, 0.5, 0]}))
    assert row == {"v": [1.0, 0.5, 0.0]}


async def test_a_row_is_a_dict_of_its_columns_as_plain_sql_reads_it(pm):
    assert await pm.query(QuerySpecification[Any]("SELECT 1 AS one")) == [{"one": 1}]


async def test_a_parameter_is_never_the_prefix_of_a_longer_one_and_unnamed_binds_are_left_out(pm):
    row = await pm.get_one(
        QuerySpecification[Any]("SELECT CAST($id AS text) AS id, CAST($idList AS text) AS list").bind(
            {"id": "one", "idList": "many", "unused": 3}
        )
    )
    assert row == {"id": "one", "list": "many"}


async def test_a_percent_sign_is_itself_with_parameters_and_without(pm):
    bound = await pm.get_one(
        QuerySpecification[Any]("SELECT '100%' AS text, CAST($n AS int) AS n, format('%s!', 'yes') AS f").bind({"n": 2})
    )
    assert bound == {"text": "100%", "n": 2, "f": "yes!"}
    unbound = await pm.get_one(QuerySpecification[Any]("SELECT format('%s!', 'yes') AS f"))
    assert unbound == {"f": "yes!"}


async def test_runs_a_block_without_parameters_as_it_is(pm, table):
    await pm.execute(
        QuerySpecification[Any](
            "DO $block$ BEGIN EXECUTE format('INSERT INTO %I (id, n) VALUES (%L, 3)', 'drivine_conn_test', 'b'); "
            "END $block$"
        )
    )
    assert await pm.query(QuerySpecification[Any]("SELECT n FROM drivine_conn_test")) == [{"n": 3}]


async def test_skips_and_limits_with_offset_and_limit(pm):
    rows = await pm.query(
        QuerySpecification[Any]("SELECT g AS n FROM generate_series(1, 5) g ORDER BY g").skip(1).limit(2)
    )
    assert rows == [{"n": 2}, {"n": 3}]


async def test_commits_outside_a_transaction_and_keeps_one_until_commit_or_rollback(
    pm, table, registry: DatabaseRegistry
):
    provider = registry.connection_provider("postgres")
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
    ids = await pm.query(QuerySpecification[Any]("SELECT id FROM drivine_conn_test WHERE id IN ('rolled', 'kept')"))
    assert ids == [{"id": "kept"}]


async def test_reads_a_cursor_in_batches_outside_a_transaction_and_in_one(registry: DatabaseRegistry):
    provider = registry.connection_provider("postgres")
    assert provider is not None
    connection = await provider.connect()
    statement = "SELECT g AS n FROM generate_series(1, 7) g ORDER BY g"
    try:
        cursor = await connection.open_cursor(CursorSpecification[Any](statement).batch_size(3))
        seen = [it["n"] async for it in cursor]
        await cursor.close()
        assert seen == [1, 2, 3, 4, 5, 6, 7]
        await connection.start_transaction()
        cursor = await connection.open_cursor(CursorSpecification[Any](statement).batch_size(4))
        seen = [it["n"] async for it in cursor]
        await cursor.close()
        await connection.commit_transaction()
        assert seen == [1, 2, 3, 4, 5, 6, 7]
    finally:
        await connection.release()


async def test_rolls_back_a_transaction_left_open_when_released(pm, table, registry: DatabaseRegistry):
    provider = registry.connection_provider("postgres")
    assert provider is not None
    connection = await provider.connect()
    try:
        await connection.start_transaction()
        await connection.query(QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n) VALUES ('open', 1)"))
    finally:
        await connection.release()
    assert await pm.query(QuerySpecification[Any]("SELECT id FROM drivine_conn_test")) == []
