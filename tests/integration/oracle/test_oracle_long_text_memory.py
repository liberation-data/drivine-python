"""
Compiled Cypher statements that read a text past 32 KB, many of them on one session, as an application runs them: each
statement's memory goes back to the instance when it ends. drivine4j's OracleLongTextMemoryTest of
liberation-data/drivine4j#313 (#218).
"""

from typing import Any

import oracledb

from drivine import ConnectionProperties, QuerySpecification, cypher_statement
from drivine.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from drivine.testing.drivine_test_containers import TEST_CONTAINER_PASSWORD
from tests.integration.oracle.oracle_seed import ON_ORACLE

pytestmark = ON_ORACLE

GRAPH = "drivine_long_text_memory"


def session_memory(dsn: str, sid: str) -> int:
    """The memory, in bytes, the session `sid` holds now, as the container's administrator sees it."""
    with oracledb.connect(user="system", password=TEST_CONTAINER_PASSWORD, dsn=dsn) as admin, admin.cursor() as cursor:
        cursor.execute(
            "SELECT st.value FROM v$sesstat st JOIN v$statname sn ON sn.statistic# = st.statistic# "
            "WHERE sn.name = 'session pga memory' AND st.sid = :sid",
            {"sid": sid},
        )
        (value,) = cursor.fetchone() or (0,)
        return int(value)


async def test_compiled_statements_that_read_a_text_past_32_kb_give_back_their_memory_however_many_run_on_one_session():
    # Each of these held 2 to 24 MB of the instance's memory for as long as the driver kept its statement in its cache,
    # 20 statements a session, and a few dozen ran the instance out of PGA (ORA-04036, liberation-data/drivine4j#313).
    # Given, stored and made by the statement, each a statement of its own.
    properties = ConnectionProperties.from_env("oracle")
    dsn = f"{properties.host}:{properties.port or 1521}/{properties.database_name}"
    provider = OracleConnectionProvider("longtextmemory", dsn, properties.user_name, properties.password, graph=GRAPH)
    connection = await provider.connect()
    try:

        async def query(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
            return await connection.query(QuerySpecification[Any](cypher_statement(cypher)).bind(parameters or {}))

        t = "aB" * 20_000
        await query("MATCH (n:LongText) DETACH DELETE n")
        await query("CREATE (n:LongText {t: $t})", {"t": t})
        (sid,) = await connection.query(QuerySpecification[Any]("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual"))
        before = session_memory(dsn, str(sid))
        parts = {"a": t[:20_000], "b": t[20_000:]}
        for i in range(1, 31):
            function = f"left(t, size(t) - {i})"
            expected = [t[:-i]]
            assert await query(f"WITH $t AS t RETURN {function}", {"t": t}) == expected, f"{function} given"
            assert await query(f"MATCH (n:LongText) WITH n.t AS t RETURN {function}") == expected, f"{function} stored"
            assert await query(f"WITH $a + $b AS t RETURN {function}", parts) == expected, f"{function} made"
        grown = (session_memory(dsn, str(sid)) - before) // 1_000_000
        # Kept, the 90 statements held 400 MB (20 a session, where drivine4j keeps 64 and held 1,134 MB); closed, 37 MB.
        assert grown < 200, f"The session holds {grown} MB more after 90 statements."
        for statement in [
            f"DROP PROPERTY GRAPH IF EXISTS {GRAPH}",
            f"DROP TABLE IF EXISTS {GRAPH}_edge PURGE",
            f"DROP TABLE IF EXISTS {GRAPH}_node PURGE",
            f"DROP TABLE IF EXISTS {GRAPH}_schema PURGE",
        ]:
            await connection.query(QuerySpecification[Any](statement))
    finally:
        await connection.release()
        await provider.end()
