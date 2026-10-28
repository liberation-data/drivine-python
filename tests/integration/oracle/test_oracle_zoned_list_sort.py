"""
The statements whose reads of a list stalled Oracle's parse (liberation-data/drivine4j#331) run in moments, and lists of
zoned values sort by their moments. drivine4j's OracleZonedListSortRunTest of liberation-data/drivine4j#199 and #331.
Each runs on a connection of its own, in a thread of its own, with a deadline: neither a query timeout nor a kill ends
Oracle's parse, so past the deadline the session is killed, which marks it, and the thread, a daemon, left to it, which
ends the wait here, and the test fails rather than holding the suite.
"""

import contextlib
import json
import threading
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import oracledb
import pytest

from drivine import ConnectionProperties, QuerySpecification
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.connection.oracle.oracle_spec_compiler import OracleSpecCompiler
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.testing.drivine_test_containers import TEST_CONTAINER_PASSWORD
from tests.integration.oracle.oracle_seed import ON_ORACLE
from tests.unit.test_cypher_zoned_list_sort import DEEP_REPROS, KEYED, MAY_HOLD_REPROS, UNWOUND_REPROS

pytestmark = ON_ORACLE

GRAPH = "drivine_unwound_temporals"
"""The graph the statements are compiled against: its tables are made for the one that writes, and dropped after."""

AT = "datetime(CASE k WHEN 0 THEN '2024-03-15T10:00:00+02:00' ELSE '2024-03-15T09:00:00Z' END)"
"""By its moment 10:00+02:00 (08:00 UTC) comes first, as every store sorts it; by its text 09:00Z would."""
REPRO = (
    "UNWIND [0, 1] AS k WITH k, "
    "[i IN range(1, 10) | localdatetime('2024-03-15T10:00:00') + duration({minutes: i})] AS l"
)
"""liberation-data/drivine4j#331's statement: a list worked out item by item with temporal arithmetic, by a
comprehension."""


def number(value: Any) -> int | float:
    """A number Oracle's JSON gives as a `Decimal`, as JSON writes it: `1`, not `1.0`."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def connect() -> oracledb.Connection:
    """A connection of its own to the test Oracle."""
    properties = ConnectionProperties.from_env("oracle")
    dsn = f"{properties.host}:{properties.port or 1521}/{properties.database_name}"
    return oracledb.connect(user=properties.user_name, password=properties.password, dsn=dsn)


@pytest.fixture(scope="module", autouse=True)
def graph() -> Iterator[None]:
    """The tables of `GRAPH`, which the statement that writes writes to, made first and dropped after."""
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(OracleGraph(GRAPH).ensuring)
    yield
    with connect() as connection, connection.cursor() as cursor:
        for statement in (
            f"DROP PROPERTY GRAPH IF EXISTS {GRAPH}",
            f"DROP TABLE IF EXISTS {GRAPH}_edge PURGE",
            f"DROP TABLE IF EXISTS {GRAPH}_node PURGE",
            f"DROP TABLE IF EXISTS {GRAPH}_schema PURGE",
        ):
            cursor.execute(statement)


def rows(cypher: str, seconds: float = 10) -> list[str]:
    """The first column of each row the statement, compiled for Oracle, gives, as JSON text; failing where they take
    past `seconds`, which is short of the 21 seconds the parse of a `JSON_TABLE` over liberation-data/drivine4j#331's
    list took, and far past the 15 to 200 milliseconds each of these statements takes, so a loaded machine does not fail
    them. A statement that writes is a block that hands its rows back, and what it writes is rolled back."""
    sql = OracleCypherCompiler(GRAPH).compile(cypher)
    # Its parameters bound as OracleConnection binds them: a write reads them from one, `$_cy_params`.
    spec = QuerySpecification[Any](sql.sql).bind(dict(sql.parameters)).finalized_copy("SQL")
    compiled = OracleSpecCompiler(spec).compile()
    properties = ConnectionProperties.from_env("oracle")
    dsn = f"{properties.host}:{properties.port or 1521}/{properties.database_name}"
    connection = connect()
    with connection.cursor() as cursor:
        cursor.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual")
        (sid,) = cursor.fetchone() or (None,)
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute(compiled.statement, compiled.parameters, fetch_lobs=False)
                found = cursor.fetchall() if cursor.description else cursor.getimplicitresults()[0].fetchall()
                outcome["rows"] = [json.dumps(it[0], separators=(",", ":"), default=number) for it in found]
        except BaseException as e:  # noqa: BLE001 - raised again where the test waits
            outcome["error"] = e
        finally:
            with contextlib.suppress(oracledb.Error):
                connection.rollback()
            with contextlib.suppress(oracledb.Error):
                connection.close()

    running = threading.Thread(target=run, daemon=True)
    running.start()
    running.join(seconds)
    if not running.is_alive():
        if "error" in outcome:
            raise outcome["error"]
        return outcome["rows"]
    with (
        contextlib.suppress(oracledb.Error),
        oracledb.connect(user="system", password=TEST_CONTAINER_PASSWORD, dsn=dsn) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SELECT serial# FROM v$session WHERE sid = :sid", {"sid": sid})
        (serial,) = cursor.fetchone() or (None,)
        cursor.execute(f"ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE")
    pytest.fail(f"Oracle took more than {seconds} seconds to run: {cypher}")


def test_a_list_worked_out_item_by_item_with_temporal_arithmetic_is_filtered_sorted_reversed_and_sized_in_moments():
    assert rows(f"{REPRO} RETURN [k, size([x IN l WHERE x IS NOT NULL])] ORDER BY k") == ["[0,10]", "[1,10]"]
    assert sorted(rows(f"{REPRO} RETURN k ORDER BY size([x IN l WHERE x IS NOT NULL])")) == ["0", "1"]
    zoned = (
        "UNWIND [0, 1] AS k WITH k, "
        "[i IN range(1, 10) | datetime('2024-03-15T10:00:00Z') + duration({minutes: i})] AS l"
    )
    assert rows(f"{zoned} RETURN [k, size([x IN l WHERE x IS NOT NULL])] ORDER BY k") == ["[0,10]", "[1,10]"]
    assert rows(
        f"{zoned} WITH k, l WHERE k = 0 "
        "RETURN [k, all(x IN l WHERE x > datetime('2024-03-15T10:00:00Z')), size([x IN l WHERE x.minute = 1])]"
    ) == ["[0,true,1]"]
    assert rows(f"{REPRO} WITH k, l WHERE k = 0 UNWIND l AS t RETURN count(t)") == ["10"]
    assert rows(f"{REPRO} WITH k, l WHERE k = 0 RETURN [reverse(l)[0].minute, tail(l)[0].minute]") == ["[10,2]"]
    assert rows(f"{REPRO} WITH collect(DISTINCT l) AS ls RETURN [size(ls), size(ls[0])]") == ["[1,10]"]


def test_a_coalesce_of_such_a_list_and_a_list_of_lists_of_them_or_of_maps_of_them_are_read_in_moments():
    # 21 to 106 seconds each, before liberation-data/drivine4j#347.
    assert rows(f"{REPRO} RETURN [k, size([x IN coalesce(l, []) WHERE x IS NOT NULL])] ORDER BY k") == [
        "[0,10]",
        "[1,10]",
    ]
    assert sorted(rows(f"{REPRO} RETURN k ORDER BY size([x IN coalesce(l, []) WHERE x IS NOT NULL])")) == ["0", "1"]
    for deep in DEEP_REPROS:
        (row,) = rows(deep)
        assert row.startswith("[[") and "2024-03-15T10:10:00" in row, row
    assert rows(f"{REPRO} WITH collect(DISTINCT [l, l]) AS ls RETURN [size(ls), size(ls[0]), size(ls[0][0])]") == [
        "[1,2,10]"
    ]
    assert rows(f"{REPRO} WITH k, [x IN l | {{t: x}}] AS ms WHERE k = 0 UNWIND ms AS m RETURN count(m.t)") == ["10"]
    assert rows(f"{REPRO} WITH k, {{l: l}} AS m WHERE k = 0 UNWIND m.l AS t RETURN count(t)") == ["10"]
    assert rows(MAY_HOLD_REPROS[0]) == ["[0,true]", "[1,true]"]
    for cypher in MAY_HOLD_REPROS[1:]:
        assert rows(cypher) == ["10", "10"], cypher
    assert rows(KEYED) == ["1", "1"]


def test_a_list_of_temporal_values_a_with_takes_out_of_an_unwound_map_is_read_in_moments():
    # No answer within 30 seconds before liberation-data/drivine4j#369; the session had to be killed.
    for cypher in UNWOUND_REPROS[:-2]:
        assert rows(cypher) == ["10", "10"], cypher
    assert rows(UNWOUND_REPROS[3]) == ["10", "10", "10", "10"]
    # After a write, rolled back with the session's work.
    assert rows(UNWOUND_REPROS[4]) == ["10", "10"]


def test_a_list_worked_out_with_temporal_arithmetic_by_a_comprehension_or_collect_sorts_by_its_moments_in_moments():
    comprehension = f"UNWIND [0, 1] AS k WITH k, [i IN range(1, 10) | {AT} + duration({{minutes: i}})] AS l"
    assert rows(f"{comprehension} RETURN k ORDER BY l") == ["0", "1"]
    assert rows(f"{comprehension} RETURN k ORDER BY [l]") == ["0", "1"]
    assert rows(f"{comprehension} RETURN [min(l)[0].hour, max(l)[0].hour]") == ["[10,9]"]
    collected = (
        f"UNWIND [0, 1] AS k UNWIND range(1, 3) AS i WITH k, {AT} + duration({{minutes: i}}) AS t "
        "WITH k, collect(t) AS l"
    )
    assert rows(f"{collected} RETURN k ORDER BY l") == ["0", "1"]
    assert rows(f"{collected} RETURN [min(l)[0].hour, max(l)[0].hour]") == ["[10,9]"]
