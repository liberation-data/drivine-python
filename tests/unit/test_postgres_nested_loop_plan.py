"""
On Postgres, a loop whose statements for each row read a table by its key where the loop adds to it, or join a table
to a JSON list of rows, is planned as nested loops over the indexes, and the planner's settings are put back after it
(#420, liberation-data/drivine4j#609): see `PostgresCypherCompiler._planned_as_nested_loops`. Their times are
tests/integration/postgres/test_compiled_write_keyed_read_time.py's. drivine4j's PostgresNestedLoopPlanTest, ported.
"""

import pytest

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler

OFF = (
    "PERFORM set_config('enable_seqscan', 'off', true), set_config('enable_hashjoin', 'off', true), "
    "set_config('enable_mergejoin', 'off', true), set_config('enable_material', 'off', true);"
)
SAVED = (
    "cy_planner := ARRAY[current_setting('enable_seqscan'), current_setting('enable_hashjoin'), "
    "current_setting('enable_mergejoin'), current_setting('enable_material')];"
)
RESTORED = (
    "PERFORM set_config('enable_seqscan', cy_planner[1], true), set_config('enable_hashjoin', cy_planner[2], true), "
    "set_config('enable_mergejoin', cy_planner[3], true), set_config('enable_material', cy_planner[4], true);"
)


def compiled(cypher: str) -> str:
    """The procedure the statement compiles to on Postgres, with the query that calls it."""
    it = PostgresCypherCompiler("g").compile(cypher)
    return f"{it.prelude or ''}\n{it.sql}"


def test_a_merges_loop_is_planned_with_seq_scans_and_hash_and_merge_joins_off_and_the_settings_are_put_back_after_it():
    sql = compiled("MATCH (n:CyLoop) MERGE (n)-[:CY_SELF]->(n)")
    lines = sql.split("\n")
    assert "    cy_planner text[];" in lines, sql
    loop = next(i for i, it in enumerate(lines) if it.startswith("    FOR cy_record IN "))
    end = lines.index("    END LOOP;")
    assert lines[loop - 2 : loop] == [f"    {SAVED}", f"    {OFF}"], sql
    assert lines[end + 1] == f"    {RESTORED}", sql
    # Once each: the MERGE's own read of its pattern, in the loop, is planned with it.
    assert sum(1 for it in lines if it.strip() == OFF) == 1, sql
    assert any(it.strip().startswith("FOR cy_matched IN (") for it in lines[loop:end]), sql


def test_each_of_two_loops_that_qualify_saves_the_settings_before_it_and_puts_them_back_after_it():
    sql = compiled("MATCH (n:CyLoop) MERGE (n)-[:CY_A]->(n) WITH n MERGE (n)-[:CY_B]->(n)")
    lines = [it.strip() for it in sql.split("\n")]
    loops = [i for i, it in enumerate(lines) if it.startswith("FOR cy_record IN ")]
    ends = [i for i, it in enumerate(lines) if it == RESTORED]
    assert len(loops) == 2, sql
    assert [lines[it - 2 : it] for it in loops] == [[SAVED, OFF]] * 2, sql
    assert len(ends) == 2, sql
    assert loops[0] < ends[0] < loops[1] - 2 < loops[1] < ends[1], sql
    assert sum(1 for it in lines if it == "cy_planner text[];") == 1, sql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n:CyLoop) MERGE (n)-[r:CY_SELF]->(n) ON CREATE SET r.w = n.i",
        "MATCH (n:CyLoop) CALL (n) { MERGE (n)-[:CY_SELF]->(n) } RETURN count(n)",
        "MATCH (n:CyLoop) CALL (n) { CREATE (n)-[r:CY_SELF]->(n) SET r.w = n.i } RETURN count(n)",
        "MATCH (n:CyLoop) FOREACH (x IN [1] | CREATE (n)-[r:CY_SELF]->(n) SET r.w = x)",
        "UNWIND range(1, 3) AS i CALL (i) { CREATE (a:CyLoop {i: i}) SET a.y = i } RETURN count(i)",
        "MATCH (n:CyLoop) CALL (n) { SET n.y = n.i RETURN n.i AS j } RETURN count(j)",
        "MATCH (n:CyLoop) CALL (n) { CREATE (m:CyMade) DETACH DELETE m } RETURN count(n)",
    ],
)
def test_a_loop_that_reads_by_key_what_it_makes_or_joins_a_table_to_a_rows_list_is_planned_so(cypher: str):
    sql = compiled(cypher)
    assert OFF in sql, sql


@pytest.mark.parametrize(
    "cypher",
    [
        "UNWIND range(1, 3) AS i CREATE (:CyLoop {i: i})",
        "MATCH (n:CyLoop) SET n.x = n.i",
        "MATCH (n:CyLoop) REMOVE n.x",
        "MATCH (n:CyLoop) CREATE (n)-[r:CY_SELF]->(n) SET r.w = n.i",
        "MATCH (n:CyLoop) DETACH DELETE n",
        "MATCH (n:CyLoop) RETURN n.i",
    ],
)
def test_a_loop_with_neither_read_and_a_statement_with_no_loop_are_compiled_as_they_were(cypher: str):
    sql = compiled(cypher)
    assert "enable_seqscan" not in sql and "cy_planner" not in sql, sql


def test_oracle_and_sqlite_plan_their_loops_as_they_did():
    cypher = "MATCH (n:CyLoop) MERGE (n)-[:CY_SELF]->(n)"
    for it in (OracleCypherCompiler("g").compile(cypher), SqliteCypherCompiler("g").compile(cypher, {})):
        assert "enable_seqscan" not in f"{it.prelude or ''}\n{it.sql}", it.sql
