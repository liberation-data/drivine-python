# Written by scripts/unasync.py from tests/integration/postgres/test_postgres_connection.py:
# change that, and run the script again.
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.sync import CursorSpecification, DatabaseRegistry, PersistenceManager, QuerySpecification
from tests.sync_integration.postgres.conftest import ON_POSTGRES

pytestmark = ON_POSTGRES


@pytest.fixture
def table(pm: PersistenceManager) -> Iterator[None]:
    pm.execute(QuerySpecification[Any]("DROP TABLE IF EXISTS drivine_conn_test"))
    pm.execute(QuerySpecification[Any]("CREATE TABLE drivine_conn_test (id text PRIMARY KEY, n int, doc jsonb)"))
    yield
    pm.execute(QuerySpecification[Any]("DROP TABLE drivine_conn_test"))


def test_binds_named_parameters_a_map_as_jsonb_and_reads_jsonb_back_as_a_dict(pm, table):
    pm.execute(
        QuerySpecification[Any]("INSERT INTO drivine_conn_test (id, n, doc) VALUES ($id, $n, $doc)").bind(
            {"id": "a", "n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}
        )
    )
    row = pm.get_one(QuerySpecification[Any]("SELECT n, doc FROM drivine_conn_test WHERE id = $id").bind({"id": "a"}))
    assert row == {"n": 1, "doc": {"tags": ["x", "y"], "nested": {"ok": True}}}


def test_a_row_is_a_dict_of_its_columns_as_plain_sql_reads_it(pm):
    assert pm.query(QuerySpecification[Any]("SELECT 1 AS one")) == [{"one": 1}]


def test_a_parameter_is_never_the_prefix_of_a_longer_one_and_unnamed_binds_are_left_out(pm):
    row = pm.get_one(
        QuerySpecification[Any]("SELECT CAST($id AS text) AS id, CAST($idList AS text) AS list").bind(
            {"id": "one", "idList": "many", "unused": 3}
        )
    )
    assert row == {"id": "one", "list": "many"}


def test_a_percent_sign_is_itself_with_parameters_and_without(pm):
    bound = pm.get_one(
        QuerySpecification[Any]("SELECT '100%' AS text, CAST($n AS int) AS n, format('%s!', 'yes') AS f").bind({"n": 2})
    )
    assert bound == {"text": "100%", "n": 2, "f": "yes!"}
    unbound = pm.get_one(QuerySpecification[Any]("SELECT format('%s!', 'yes') AS f"))
    assert unbound == {"f": "yes!"}


def test_runs_a_block_without_parameters_as_it_is(pm, table):
    pm.execute(
        QuerySpecification[Any](
            "DO $block$ BEGIN EXECUTE format('INSERT INTO %I (id, n) VALUES (%L, 3)', 'drivine_conn_test', 'b'); "
            "END $block$"
        )
    )
    assert pm.query(QuerySpecification[Any]("SELECT n FROM drivine_conn_test")) == [{"n": 3}]


def test_skips_and_limits_with_offset_and_limit(pm):
    rows = pm.query(QuerySpecification[Any]("SELECT g AS n FROM generate_series(1, 5) g ORDER BY g").skip(1).limit(2))
    assert rows == [{"n": 2}, {"n": 3}]


def test_commits_outside_a_transaction_and_keeps_one_until_commit_or_rollback(pm, table, registry: DatabaseRegistry):
    provider = registry.connection_provider("postgres")
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
    ids = pm.query(QuerySpecification[Any]("SELECT id FROM drivine_conn_test WHERE id IN ('rolled', 'kept')"))
    assert ids == [{"id": "kept"}]


def test_reads_a_cursor_in_batches_outside_a_transaction_and_in_one(registry: DatabaseRegistry):
    provider = registry.connection_provider("postgres")
    assert provider is not None
    connection = provider.connect()
    statement = "SELECT g AS n FROM generate_series(1, 7) g ORDER BY g"
    try:
        cursor = connection.open_cursor(CursorSpecification[Any](statement).batch_size(3))
        seen = [it["n"] for it in cursor]
        cursor.close()
        assert seen == [1, 2, 3, 4, 5, 6, 7]
        connection.start_transaction()
        cursor = connection.open_cursor(CursorSpecification[Any](statement).batch_size(4))
        seen = [it["n"] for it in cursor]
        cursor.close()
        connection.commit_transaction()
        assert seen == [1, 2, 3, 4, 5, 6, 7]
    finally:
        connection.release()
