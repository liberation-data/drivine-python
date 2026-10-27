"""
The statements whose key of moments stalled Oracle's parse (liberation-data/drivine4j#331) run in moments, and sort as
JSON, by the text of each item: Postgres sorts them by moments (#181). drivine4j's OracleZonedListSortRunTest of
liberation-data/drivine4j#199. Each runs on a connection of its own, in a thread of its own, with a deadline: neither a
query timeout nor a kill ends Oracle's parse, so past the deadline the session is killed, which marks it, and the
thread, a daemon, left to it, which ends the wait here, and the test fails rather than holding the suite.
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
"""By its moment 10:00+02:00 (08:00 UTC) comes first, and Postgres sorts it first; by its text 09:00Z does."""


def number(value: Any) -> int | float:
    """A number Oracle's JSON gives as a `Decimal`, as JSON writes it: `1`, not `1.0`."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def rows(cypher: str, seconds: float = 30) -> list[str]:
    """The first column of each row the statement, compiled for Oracle, gives, as JSON text; failing where they take
    past `seconds`."""
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


def test_a_list_worked_out_with_temporal_arithmetic_by_a_comprehension_or_collect_sorts_as_its_text_in_moments():
    comprehension = f"UNWIND [0, 1] AS k WITH k, [i IN range(1, 10) | {AT} + duration({{minutes: i}})] AS l"
    assert rows(f"{comprehension} RETURN k ORDER BY l") == ["1", "0"]
    # A list of lists in Oracle's own order of JSON, not item by item: here, that it runs.
    assert sorted(rows(f"{comprehension} RETURN k ORDER BY [l]")) == ["0", "1"]
    assert rows(f"{comprehension} RETURN [min(l)[0].hour, max(l)[0].hour]") == ["[9,10]"]
    collected = (
        f"UNWIND [0, 1] AS k UNWIND range(1, 3) AS i WITH k, {AT} + duration({{minutes: i}}) AS t "
        "WITH k, collect(t) AS l"
    )
    assert rows(f"{collected} RETURN k ORDER BY l") == ["1", "0"]
    assert rows(f"{collected} RETURN [min(l)[0].hour, max(l)[0].hour]") == ["[9,10]"]
