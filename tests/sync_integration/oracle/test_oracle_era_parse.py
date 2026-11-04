# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_era_parse.py:
# change that, and run the script again.
"""
How long Oracle takes to plan a statement of temporal arithmetic, so that a change to the SQL a year BC is read and
written by fails here, clearly, and not as a test that times out. Renumbered inline, with a test and a regular
expression that wrote each value three or four times, the min and max of a collected list of moments took 4.7 seconds
of Oracle's hard parse in drivine4j, a moment two `CALL`s deep 72 seconds, and a truncation four deep was not planned
in ten minutes (liberation-data/drivine#572). The store's era functions (see `OracleGraph.era`) took them to about 1,
27 and 16 seconds on a fast machine, and a CI runner takes about twice as long: the bounds leave room for that.
drivine4j's OracleEraParseTest of liberation-data/drivine4j#584.

The time is the session's `parse time cpu` in `v$sesstat`, which the test user may not read, so the system user reads
it; each statement is new to Oracle (a comment that no other has), so it is parsed hard. Each runs on a connection of
its own, in a thread of its own, with a deadline past its bound, as tests/integration/oracle/test_oracle_zoned_list_sort
runs its statements: neither a query timeout nor a kill ends Oracle's parse, so past the deadline the session is killed,
and the test fails rather than holding the suite.
"""

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import oracledb
import pytest

from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.connection.oracle.oracle_spec_compiler import OracleSpecCompiler
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.sync import ConnectionProperties, QuerySpecification
from drivine.testing.drivine_test_containers import TEST_CONTAINER_PASSWORD
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE
from tests.sync_integration.oracle.test_oracle_nulls_equal import dropping

pytestmark = ON_ORACLE

GRAPH = "cy_era_parse"
"""The graph the statements are compiled against: made first, with its functions, and dropped after."""

AT = "datetime(CASE k WHEN 0 THEN '2024-03-15T10:00:00+02:00' ELSE '2024-03-15T09:00:00Z' END)"

ZLS = (
    f"UNWIND [0, 1] AS k UNWIND range(1, 3) AS i WITH k, {AT} + duration({{minutes: i}}) AS t "
    "WITH k, collect(t) AS l RETURN [min(l)[0].hour, max(l)[0].hour]"
)
"""The min and max of a collected list of moments: zls5."""

CALL = (
    "MATCH (n:CyTemporal) CALL { WITH n CALL { WITH n RETURN n.day + duration('P1D') AS e } RETURN e AS d } "
    "RETURN (d + duration('P1D')).day"
)
"""A moment two `CALL`s deep: call5."""

TRUNCATED = (
    "RETURN "
    + "datetime.truncate('second', " * 4
    + "datetime('2017-10-11T12:31:14+01:00')"
    + ", {hour: 3})" * 4
    + ".hour"
)
"""A truncation with fields four deep: trunc4. Never compiled here without the era functions: Oracle's hard parse of
it ran past ten minutes."""


def number(value: Any) -> int | float:
    """A number Oracle's JSON gives as a `Decimal`, as JSON writes it: `1`, not `1.0`."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def dsn() -> str:
    properties = ConnectionProperties.from_env("oracle")
    return f"{properties.host}:{properties.port or 1521}/{properties.database_name}"


def connect() -> oracledb.Connection:
    """A connection of its own to the test Oracle, as the test user."""
    properties = ConnectionProperties.from_env("oracle")
    return oracledb.connect(user=properties.user_name, password=properties.password, dsn=dsn())


def admin() -> oracledb.Connection:
    """A connection as the system user, which may read `v$sesstat` and kill a session."""
    return oracledb.connect(user="system", password=TEST_CONTAINER_PASSWORD, dsn=dsn())


def sql_of(cypher: str, comment: str = "") -> tuple[str, dict[str, Any]]:
    """`cypher` compiled for the graph, its parameters bound as OracleConnection binds them, after `comment`."""
    compiled = OracleCypherCompiler(GRAPH).compile(cypher)
    spec = QuerySpecification[Any](f"{comment}{compiled.sql}").bind(dict(compiled.parameters)).finalized_copy("SQL")
    statement = OracleSpecCompiler(spec).compile()
    return statement.statement, statement.parameters


@pytest.fixture(scope="module", autouse=True)
def graph() -> Iterator[None]:
    """The graph's tables and functions, made first with one node of a date, and dropped after."""
    try:
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(OracleGraph(GRAPH).ensuring)
            cursor.execute(f"DELETE FROM {GRAPH}_node")
            statement, parameters = sql_of("CREATE (:CyTemporal {id: 'a', day: date('2024-03-15')})")
            cursor.execute(statement, parameters)
            connection.commit()
        yield
    finally:
        with connect() as connection, connection.cursor() as cursor:
            for statement in dropping(GRAPH):
                cursor.execute(statement)


def parse_cpu(cursor: Any, sid: int) -> int:
    """The `parse time cpu` of the session `sid` so far, in centiseconds."""
    cursor.execute(
        "SELECT s.value FROM v$sesstat s JOIN v$statname n ON n.statistic# = s.statistic# "
        "WHERE s.sid = :sid AND n.name = 'parse time cpu'",
        {"sid": sid},
    )
    (value,) = cursor.fetchone() or (0,)
    return int(value)


def parsed(cypher: str, bound_ms: int) -> tuple[int, list[str]]:
    """
    The CPU, in milliseconds, Oracle took to parse `cypher`, compiled for a store with the era functions, and its rows
    as JSON text; failing where it runs past twice `bound_ms`.
    """
    assert OracleCypherCompiler(GRAPH).compile(cypher).calls_stored_era, cypher
    statement, parameters = sql_of(cypher, f"/* era parse {time.time_ns()} */ ")
    connection = connect()
    with connection.cursor() as cursor:
        cursor.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual")
        (sid,) = cursor.fetchone() or (None,)
    sid = int(sid)
    outcome: dict[str, Any] = {}
    with admin() as system, system.cursor() as reading:
        before = parse_cpu(reading, sid)

        def run() -> None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(statement, parameters, fetch_lobs=False)
                    rows = cursor.fetchall()
                    outcome["rows"] = [json.dumps(it[0], separators=(",", ":"), default=number) for it in rows]
            except BaseException as e:  # noqa: BLE001 - raised again where the test waits
                outcome["error"] = e

        running = threading.Thread(target=run, daemon=True)
        running.start()
        running.join(2 * bound_ms / 1000)
        if running.is_alive():
            with contextlib.suppress(oracledb.Error):
                reading.execute("SELECT serial# FROM v$session WHERE sid = :sid", {"sid": sid})
                (serial,) = reading.fetchone() or (None,)
                reading.execute(f"ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE")
            pytest.fail(f"Oracle took more than {2 * bound_ms} ms to run: {cypher}")
        try:
            if "error" in outcome:
                raise outcome["error"]
            return (parse_cpu(reading, sid) - before) * 10, outcome["rows"]
        finally:
            with contextlib.suppress(oracledb.Error):
                connection.close()


ZLS_BOUND = 3_500
CALL_BOUND = 90_000
TRUNC_BOUND = 90_000


def test_the_min_and_max_of_a_collected_list_of_moments_are_planned_in_seconds():
    # About 1 second in drivine4j; 4.7 renumbered inline.
    ms, rows = parsed(ZLS, ZLS_BOUND)
    assert rows == ["[10,9]"]
    assert ms < ZLS_BOUND, f"hard parse took {ms} ms, over {ZLS_BOUND} ms"


def test_a_moment_two_calls_deep_is_planned_in_a_minute_and_a_half():
    # 27 seconds on a fast machine, 51 on a CI runner; 72 renumbered inline on a fast machine. A fallback to inline SQL
    # is caught by `parsed`'s check that the statement calls the era functions, and by ZLS_BOUND.
    ms, rows = parsed(CALL, CALL_BOUND)
    assert rows == ["17"]
    assert ms < CALL_BOUND, f"hard parse took {ms} ms, over {CALL_BOUND} ms"


def test_a_truncation_with_fields_four_deep_is_planned_in_a_minute_and_a_half():
    # 16 seconds in drivine4j; renumbered inline it was still being planned after ten minutes.
    ms, rows = parsed(TRUNCATED, TRUNC_BOUND)
    assert rows == ["3"]
    assert ms < TRUNC_BOUND, f"hard parse took {ms} ms, over {TRUNC_BOUND} ms"
