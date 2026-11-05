"""
The planner's settings, after a procedure that set them off for a loop, are those the session or its transaction had
before it, and where the loop fails, those it had before the procedure: Postgres undoes `set_config(…, true)` with the
transaction or savepoint the failure rolls back (#420, liberation-data/drivine4j#609). drivine4j's
PostgresNestedLoopSettingsTest, ported.
"""

import re

from drivine import DatabaseRegistry
from drivine.connection.postgres.postgres_connection import PostgresConnection
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from tests.integration.postgres.conftest import ON_POSTGRES

pytestmark = ON_POSTGRES

GRAPH = "drivine"

MERGE = "MATCH (n:CyLoop) MERGE (n)-[:CY_SELF]->(n)"


async def settings_after(
    registry: DatabaseRegistry, setup: str, cypher: str, nodes: bool = False, fails: str | None = None
) -> list[str]:
    """The settings after `cypher`, compiled, ran in a transaction that ran `setup` first, then rolled back.

    `fails` is a part of the error the call must raise, so that a failure before the loop turns the settings off
    doesn't pass for one inside it.
    """
    compiled = PostgresCypherCompiler(GRAPH).compile(cypher)
    prelude = compiled.prelude or ""
    assert "set_config('enable_seqscan', 'off', true)" in prelude, prelude
    function = re.search(r"pg_temp\.drivine_cypher_[0-9a-f]+", prelude)
    assert function is not None, prelude
    provider = registry.connection_provider("postgres")
    assert provider is not None
    connection = await provider.connect()
    assert isinstance(connection, PostgresConnection)
    raw = connection.connection
    try:
        await raw.execute("BEGIN")
        await raw.execute(setup)
        if nodes:
            await raw.execute(f"""INSERT INTO {GRAPH}_node (labels, props) VALUES ('["CyLoop"]', '{{"i": 1}}')""")
        await raw.execute(prelude)
        await raw.execute("SAVEPOINT cy_planner_test")
        error: str | None = None
        try:
            await raw.execute(f"SELECT count(*) FROM {function.group()}('{{}}'::jsonb)")
        except Exception as e:
            error = str(e)
        if fails is None:
            assert error is None, f"{cypher} failed: {error}"
        else:
            assert error is not None and fails in error, f"{cypher} failed with: {error}"
        if error is not None:
            await raw.execute("ROLLBACK TO SAVEPOINT cy_planner_test")
        cursor = await raw.execute(
            "SELECT current_setting('enable_seqscan') AS a, current_setting('enable_hashjoin') AS b, "
            "current_setting('enable_mergejoin') AS c"
        )
        row = await cursor.fetchone()
        assert row is not None
        return [row["a"], row["b"], row["c"]]
    finally:
        await raw.execute("ROLLBACK")
        await connection.release()


async def test_a_procedure_puts_the_settings_back_as_the_session_had_them_in_its_transaction(
    registry: DatabaseRegistry,
):
    assert await settings_after(registry, "SET enable_hashjoin = off", MERGE) == ["on", "off", "on"]


async def test_a_procedure_puts_the_settings_back_as_its_transaction_had_them(registry: DatabaseRegistry):
    settings = await settings_after(registry, "SET LOCAL enable_hashjoin = off", MERGE, nodes=True)
    assert settings == ["on", "off", "on"]


async def test_a_procedure_that_fails_in_its_loop_leaves_the_settings_as_they_were_once_its_savepoint_is_rolled_back(
    registry: DatabaseRegistry,
):
    # Divided by zero in the loop, for its one row. drivine4j's test merges a null property, `{w: n.missing}`, which
    # this compiler writes as no property rather than refusing.
    failing = "MATCH (n:CyLoop) MERGE (n)-[:CY_SELF {w: n.i / 0}]->(n)"
    setup = "SET LOCAL enable_hashjoin = off"
    settings = await settings_after(registry, setup, failing, nodes=True, fails="division by zero")
    assert settings == ["on", "off", "on"]
