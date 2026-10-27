# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_nulls_equal.py:
# change that, and run the script again.
"""
The function an Oracle graph is made with, `<graph>_nulls_equal`, which a compiled Cypher statement compares two lists
or maps that may hold a null by, and what happens where a store has none (#188): drivine4j's OracleFreshGraphTest and
CypherWriteOracleTest of liberation-data/drivine4j#323, with #337's form of the concurrent test.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from drivine.backend.oracle import oracle_stored_functions
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.oracle.oracle_stored_functions import OracleStoredFunctions
from drivine.sync import ConnectionProperties, QuerySpecification, cypher_statement
from drivine.sync.connection.oracle.oracle_connection import OracleConnection
from drivine.sync.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

WRITE = (
    "CREATE (n:CyWrite) WITH n, [null] AS a, [1] AS b, [null] AS c "
    "RETURN [coalesce(a = b, 'null'), coalesce(a = c, 'null')]"
)
"""A statement that writes, comparing a pair that differs as JSON and a pair that is the same, both holding a null."""

READ = "WITH [null] AS a, [1] AS b, [null] AS c RETURN [coalesce(a = b, 'null'), coalesce(a = c, 'null')]"
"""The same comparisons in a read."""


def properties() -> ConnectionProperties:
    return ConnectionProperties.from_env("oracle")


def connect_string() -> str:
    it = properties()
    return f"{it.host}:{it.port or 1521}/{it.database_name}"


def provider_for(graph: str, user: str | None = None, password: str | None = None) -> OracleConnectionProvider:
    """A provider of its own pool for the graph `graph`, as the test user unless another is named."""
    it = properties()
    return OracleConnectionProvider(
        f"nulls-{graph}", connect_string(), user or it.user_name, password or it.password, graph=graph
    )


def run(statements: list[str], user: str | None = None, password: str | None = None) -> None:
    """Runs `statements` in a session of its own, outside any graph, as the test user unless another is named."""
    provider = provider_for("drivine", user, password)
    pool = provider._pool_of()
    connection = pool.acquire()
    try:
        cursor = connection.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
        finally:
            cursor.close()
    finally:
        pool.release(connection)
        provider.end()


def answer(sql: str) -> Any:
    """The one value the query `sql` gives, asked in a session of its own outside any graph."""
    provider = provider_for("drivine")
    pool = provider._pool_of()
    connection = pool.acquire()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            row = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        pool.release(connection)
        provider.end()
    return row[0] if row else None


def dropping(graph: str) -> list[str]:
    """What drops the graph `graph`'s store, its function among it."""
    return [
        f"DROP PROPERTY GRAPH IF EXISTS {graph}",
        f"DROP TABLE IF EXISTS {graph}_edge PURGE",
        f"DROP TABLE IF EXISTS {graph}_node PURGE",
        f"DROP TABLE IF EXISTS {graph}_schema PURGE",
        f"DROP FUNCTION IF EXISTS {graph}_nulls_equal",
    ]


def cypher(provider: OracleConnectionProvider, text: str) -> list[Any]:
    connection = provider.connect()
    try:
        return connection.query(QuerySpecification[Any](cypher_statement(text)))
    finally:
        connection.release()


@pytest.fixture
def forgetting(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Nothing remembered of any session: a session found to call a function is remembered for the process, and these
    tests take the function away.
    """
    monkeypatch.setattr(OracleStoredFunctions, "_callable", set())
    monkeypatch.setattr(OracleStoredFunctions, "_absent", {})
    monkeypatch.setattr(OracleStoredFunctions, "_warned", set())
    yield


@pytest.fixture
def graph(request: pytest.FixtureRequest, forgetting: None) -> Iterator[str]:
    """A graph of the test's own, dropped before and after it."""
    name = f"nulls_{request.node.name.removeprefix('test_')[:24]}"
    run(dropping(name))
    yield name
    run(dropping(name))


def test_the_graph_is_made_with_the_function_which_answers_as_cypher_would(graph: str):
    run([OracleGraph(graph).ensuring])
    f = OracleGraph(graph).nulls_equal
    compared = (
        f"NVL(TO_CHAR({f}(JSON('[null]'), JSON('[1]'))), 'null') || ',' || "
        f"TO_CHAR({f}(JSON('[1, null]'), JSON('[2, null]'))) || ',' || "
        f"TO_CHAR({f}(JSON('{{\"x\": [1]}}'), JSON('{{\"x\": [1]}}')))"
    )
    assert answer(f"SELECT {compared} FROM DUAL") == "null,0,1"


def test_a_function_of_the_graphs_name_that_is_not_valid_is_made_again(graph: str):
    f = OracleGraph(graph).nulls_equal
    run(
        [
            f"BEGIN EXECUTE IMMEDIATE 'CREATE OR REPLACE FUNCTION {f} RETURN NUMBER IS BEGIN RETURN nope; END;'; "
            "EXCEPTION WHEN OTHERS THEN NULL; END;"
        ]
    )
    run([OracleGraph(graph).ensuring])
    assert answer(f"SELECT TO_CHAR({f}(JSON('[1, null]'), JSON('[2, null]'))) FROM DUAL") == "0"


def test_a_user_who_may_not_make_a_function_has_the_graph_made_without_it_and_a_statement_compiled_to_call_none(
    graph: str,
):
    # Making a function takes CREATE PROCEDURE, which this user lacks (ORA-01031). The test container's system user
    # has the test user's password.
    user = "drivine_no_procedure"
    password = properties().password or ""
    run(
        [
            f"DROP USER IF EXISTS {user} CASCADE",
            f'CREATE USER {user} IDENTIFIED BY "{password}" DEFAULT TABLESPACE users QUOTA UNLIMITED ON users',
            f"GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE PROPERTY GRAPH TO {user}",
        ],
        "system",
        password,
    )
    restricted = provider_for(graph, user, password)
    try:
        assert cypher(restricted, WRITE) == [[False, "null"]]
        assert cypher(restricted, READ) == [["null", "null"]]
        connection = restricted.connect()
        try:
            made = connection.query(
                QuerySpecification[Any](
                    'SELECT object_name AS "name" FROM user_objects '
                    f"WHERE object_name IN ('{graph.upper()}_NODE', '{graph.upper()}_NULLS_EQUAL')"
                )
            )
        finally:
            connection.release()
        assert made == [f"{graph.upper()}_NODE"]
    finally:
        restricted.end()
        run([f"DROP USER IF EXISTS {user} CASCADE"], "system", password)


def test_a_store_without_the_function_compares_as_before_until_the_graph_is_ensured_again(graph: str):
    # A store an earlier version made: the first connection makes the graph, the function with it, which is then taken
    # away.
    provider = provider_for(graph)
    try:
        cypher(provider, "RETURN 1")
        run([f"DROP FUNCTION {OracleGraph(graph).nulls_equal}"])
        assert cypher(provider, WRITE) == [[False, "null"]]
        # A read declares the function itself.
        assert cypher(provider, READ) == [["null", "null"]]
        # A pool made again ensures the graph again, and the session is asked again at once.
        provider.end()
        assert cypher(provider, WRITE) == [["null", "null"]]
    finally:
        provider.end()


# Whether a session may call the function, asked of a real session of a store whose function each test makes or takes
# away.


def session_of(provider: OracleConnectionProvider) -> OracleConnection:
    connection = provider.connect()
    assert isinstance(connection, OracleConnection)
    return connection


def test_a_session_found_to_call_no_function_is_not_asked_again_until_a_minute_passes(
    graph: str, monkeypatch: pytest.MonkeyPatch
):
    # A store without the function would otherwise pay a query and a second compile on every statement.
    provider = provider_for(graph)
    connection = session_of(provider)
    try:
        run([f"DROP FUNCTION {OracleGraph(graph).nulls_equal}"])
        assert not connection._calls_nulls_equal()
        run(OracleGraph(graph).functions)
        assert not connection._calls_nulls_equal(), "a session found to call none is remembered"
        later = oracle_stored_functions.clock() + OracleStoredFunctions.ABSENT_FOR + 1
        monkeypatch.setattr(oracle_stored_functions, "clock", lambda: later)
        assert connection._calls_nulls_equal(), "and asked again once the minute has passed"
    finally:
        connection.release()
        provider.end()


def test_a_function_that_is_not_valid_is_taken_as_missing(graph: str):
    # Calling it gives ORA-06575.
    provider = provider_for(graph)
    connection = session_of(provider)
    try:
        run(
            [
                f"CREATE OR REPLACE FUNCTION {OracleGraph(graph).nulls_equal}(a JSON, b JSON) RETURN NUMBER IS "
                "BEGIN RETURN nope; END;"
            ]
        )
        assert not connection._calls_nulls_equal()
    finally:
        connection.release()
        provider.end()


def test_a_function_that_fails_for_another_reason_than_not_being_there_is_not_taken_as_missing(graph: str):
    # Only the errors of a function there is none of (ORA-00904, ORA-06575…) mean none: this one, like a connection
    # lost, is reported.
    import oracledb

    provider = provider_for(graph)
    connection = session_of(provider)
    try:
        run(
            [
                f"CREATE OR REPLACE FUNCTION {OracleGraph(graph).nulls_equal}(a JSON, b JSON) RETURN NUMBER IS "
                "BEGIN RAISE_APPLICATION_ERROR(-20001, 'failed'); END;"
            ]
        )
        with pytest.raises(oracledb.DatabaseError) as raised:
            connection._calls_nulls_equal()
        assert raised.value.args[0].code == 20001
    finally:
        connection.release()
        provider.end()
