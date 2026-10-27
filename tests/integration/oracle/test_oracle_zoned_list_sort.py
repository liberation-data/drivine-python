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
from decimal import Decimal
from typing import Any

import oracledb
import pytest

from drivine import ConnectionProperties
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.testing.drivine_test_containers import TEST_CONTAINER_PASSWORD
from tests.integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

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


def rows(cypher: str, seconds: float = 10) -> list[str]:
    """The first column of each row the statement, compiled for Oracle, gives, as JSON text; failing where they take
    past `seconds`, which is short of the 21 seconds the parse of a `JSON_TABLE` over liberation-data/drivine4j#331's
    list took, and far past the 15 to 200 milliseconds each of these statements takes, so a loaded machine does not fail
    them."""
    compiled = OracleCypherCompiler("cypher").compile(cypher)
    properties = ConnectionProperties.from_env("oracle")
    dsn = f"{properties.host}:{properties.port or 1521}/{properties.database_name}"
    connection = oracledb.connect(user=properties.user_name, password=properties.password, dsn=dsn)
    with connection.cursor() as cursor:
        cursor.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual")
        (sid,) = cursor.fetchone() or (None,)
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute(compiled.sql, compiled.parameters, fetch_lobs=False)
                outcome["rows"] = [json.dumps(it[0], separators=(",", ":"), default=number) for it in cursor.fetchall()]
        except BaseException as e:  # noqa: BLE001 - raised again where the test waits
            outcome["error"] = e
        finally:
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
