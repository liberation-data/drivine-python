"""
The plans Postgres makes for a write that reads its rows' nodes or relationships back by id, where the table's
statistics say it is empty while it still has pages: as an ANALYZE (or an autoanalyze) just after a `DELETE` leaves it,
before the rows are written again (#446, liberation-data/drivine4j#644). A plain join was planned to compare each row
with every row of a scan materialized, and a `FOREACH` over 20,000 nodes took 19 s where it takes under one. The probe
is planned as a scan of the key's index: see `PostgresCypherCompiler.key_join`. drivine4j's PostgresKeyProbePlanTest,
ported.

Each write runs on a connection of its own, as PL/pgSQL keeps a plan for its session: a pooled connection that planned
the statement before, with the statistics right, keeps that plan. The plans are logged by auto_explain, as notices.
"""

import dataclasses
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from drivine import ConnectionProperties, DatabaseRegistry, PersistenceManager, PersistenceManagerFactory
from drivine.connection.postgres.postgres_connection import PostgresConnection
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.query.query_specification import QuerySpecification
from drivine.query.statement import cypher_statement
from tests.integration.postgres.conftest import ON_POSTGRES

pytestmark = ON_POSTGRES

GRAPH = "cy_probe"
"""The graph the plans are made on: tables of their own, apart from those the other tests read."""

NAME = f"postgres_{GRAPH}"

ROWS = 2000

CHECK = "MATCH (:CyLoop)-[r:CY_SELF]->(:CyLoop) WHERE r.w = 1 RETURN count(r)"


@pytest.fixture
async def probe(registry: DatabaseRegistry, factory: PersistenceManagerFactory) -> AsyncIterator[PersistenceManager]:
    """A manager of GRAPH, its tables emptied and autovacuumed again and its pool closed after the test."""
    if registry.registered(NAME) is None:
        properties = dataclasses.replace(ConnectionProperties.from_env("postgres"), default_graph_path=GRAPH)
        registry.builder().with_properties(properties).register(NAME)
    pm = factory.get(NAME)
    try:
        yield pm
    finally:
        try:
            await pm.execute(QuerySpecification(f"ALTER TABLE {GRAPH}_node RESET (autovacuum_enabled)"))
            await pm.execute(QuerySpecification(f"ALTER TABLE {GRAPH}_edge RESET (autovacuum_enabled)"))
            await pm.execute(QuerySpecification(f"TRUNCATE {GRAPH}_edge, {GRAPH}_node"))
        finally:
            provider = registry.registered(NAME)
            assert provider is not None
            await provider.end()


async def cypher(pm: PersistenceManager, text: str) -> list[Any]:
    return await pm.query(QuerySpecification[Any](cypher_statement(text)).bind({"n": ROWS}))


async def stale(pm: PersistenceManager, relationships: bool) -> None:
    """
    ROWS nodes, each with a relationship to itself where `relationships`, made, deleted and made again, with the tables
    analyzed while they were empty: their statistics say they hold no rows, over the pages the first ones left.
    """
    await pm.execute(QuerySpecification(f"TRUNCATE {GRAPH}_edge, {GRAPH}_node"))
    await pm.execute(QuerySpecification(f"ALTER TABLE {GRAPH}_node SET (autovacuum_enabled = off)"))
    await pm.execute(QuerySpecification(f"ALTER TABLE {GRAPH}_edge SET (autovacuum_enabled = off)"))
    made = (
        "UNWIND range(1, $n) AS i CREATE (a:CyLoop {i: i})-[:CY_SELF]->(a)"
        if relationships
        else "UNWIND range(1, $n) AS i CREATE (:CyLoop {i: i})"
    )
    await cypher(pm, made)
    # A node's relationships go with it: the relationship table cascades.
    await pm.execute(QuerySpecification(f"DELETE FROM {GRAPH}_node"))
    await pm.execute(QuerySpecification(f"ANALYZE {GRAPH}_node"))
    await pm.execute(QuerySpecification(f"ANALYZE {GRAPH}_edge"))
    await cypher(pm, made)


async def plans(prelude: str | None, sql: str, parameters: dict[str, Any]) -> list[str]:
    """The plans of the statements `sql`, readied by `prelude`, ran, on a connection of its own."""
    properties = ConnectionProperties.from_env("postgres")
    conninfo = make_conninfo(
        host=properties.host,
        port=properties.port,
        dbname=properties.database_name,
        user=properties.user_name,
        password=properties.password,
    )
    notices: list[str] = []
    raw = await psycopg.AsyncConnection.connect(conninfo, autocommit=True, row_factory=dict_row)
    try:
        raw.add_notice_handler(lambda it: notices.append(it.message_primary or ""))
        for setting in (
            "LOAD 'auto_explain'",
            "SET auto_explain.log_min_duration = 0",
            "SET auto_explain.log_nested_statements = on",
            "SET auto_explain.log_level = notice",
            "SET client_min_messages = notice",
        ):
            await raw.execute(setting)
        if prelude is not None:
            await raw.execute(prelude)
        connection = PostgresConnection(raw, None, "key-probe-plan", GRAPH)
        await connection.query(QuerySpecification[Any](sql).bind(parameters))
    finally:
        await raw.close()
    return notices


async def join_plans(
    pm: PersistenceManager, text: str, table: str, relationships: bool, compiled: Callable[[str], str] = lambda it: it
) -> list[str]:
    """The plans of those of the statement's statements that join its rows to the table, with the statistics stale."""
    await stale(pm, relationships)
    statement = PostgresCypherCompiler(GRAPH).compile(text)
    prelude = compiled(statement.prelude) if statement.prelude is not None else None
    logged = await plans(prelude, compiled(statement.sql), statement.parameters)
    joins = [it for it in logged if "jsonb_array_elements" in it and f" {table} " in it]
    assert joins, f"{text} logged no join of its rows to {table}"
    return joins


def materialized(plan: str, table: str) -> bool:
    """Whether the plan materializes a scan of the table, of the whole table or of its key's whole index."""
    pattern = rf"Materialize\s*\(.*\n\s*->\s+(Seq Scan|Index (Only )?Scan using \S+) on {table} "
    return re.search(pattern, plan) is not None


def probed(plan: str, table: str) -> bool:
    """Whether the plan reads the table by its key's index for the row's id: a scan of the index with the id as its
    condition."""
    pattern = rf"Index (Only )?Scan using {table}_pkey on {table} \w+.*\n\s*Index Cond: \((\w+\.)?[ne]id = "
    return re.search(pattern, plan) is not None


_PROBE = re.compile(r"LEFT JOIN LATERAL \(\s*SELECT \* FROM (\S+) (\w+) WHERE (\2\.\w+ = .*?) OFFSET 0\s*\) \2 ON TRUE")


def plain(sql: str) -> str:
    """The SQL with each probe of a key put back as the plain join it was, as Oracle's and SQLite's compilers still
    join."""
    return _PROBE.sub(lambda m: f"LEFT JOIN {m[1]} {m[2]} ON {m[3]}", sql)


async def assert_probed(pm: PersistenceManager, text: str, table: str, relationships: bool) -> None:
    # The state is the one the probe is for: joined plainly, the rows are compared with a scan of every row
    # materialized.
    unprobed = await join_plans(pm, text, table, relationships, plain)
    assert any(materialized(it, table) for it in unprobed), "the plain join was not materialized:\n" + "\n---\n".join(
        unprobed
    )
    for plan in await join_plans(pm, text, table, relationships):
        assert not materialized(plan, table), plan
        assert probed(plan, table), plan
    assert await cypher(pm, CHECK) == [ROWS]


async def test_a_write_reads_its_rows_nodes_back_by_the_keys_index_where_the_node_tables_statistics_say_empty(
    probe: PersistenceManager,
):
    text = "MATCH (n:CyLoop) FOREACH (x IN [1] | CREATE (n)-[r:CY_SELF]->(n) SET r.w = x)"
    await assert_probed(probe, text, f"{GRAPH}_node", relationships=False)


async def test_a_write_reads_its_rows_relationships_back_by_the_keys_index_where_the_edge_tables_statistics_say_empty(
    probe: PersistenceManager,
):
    text = "MATCH ()-[r:CY_SELF]->() FOREACH (x IN [1] | SET r.w = x)"
    await assert_probed(probe, text, f"{GRAPH}_edge", relationships=True)
