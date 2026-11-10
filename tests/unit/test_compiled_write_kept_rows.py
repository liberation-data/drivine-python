"""
A write's procedure on Postgres and Oracle declares each list it keeps rows in (procedure_step.Kept) as the store's own
list, which a row is added to in place, its OUT and the RETURNED of each depth of `CALL { … }` it writes and returns
for; a procedure that keeps no rows declares OUT as it did (#419, liberation-data/drivine4j#588).
"""

from collections.abc import Callable

import pytest

from drivine.cypher.sql import procedure_step as steps
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

NESTED = (
    "UNWIND [1, 2] AS k CALL (k) { UNWIND [k] AS i CALL (i) { CREATE (:A {i: i}) RETURN i AS j } RETURN j } RETURN j"
)
"""A write within two calls that return: its OUT, and the RETURNED of each call's depth."""


def postgres(cypher: str) -> str:
    return "\n".join(PostgresCypherCompiler("g").compile(cypher).setup)


def oracle(cypher: str) -> str:
    return OracleCypherCompiler("g").compile(cypher).sql


@pytest.mark.parametrize(("compiled", "kept"), [(postgres, "jsonb[]"), (oracle, "JSON_ARRAY_T")])
def test_each_list_a_write_keeps_rows_in_is_declared_as_the_stores_own_list(compiled: Callable[[str], str], kept: str):
    sql = compiled(NESTED)
    for it in ("cy_out", "cy_returned1", "cy_returned2"):
        assert f"\n    {it} {kept};\n" in sql, sql
    assert "cy_returned3" not in sql, sql


@pytest.mark.parametrize(("compiled", "json"), [(postgres, "jsonb"), (oracle, "JSON")])
def test_a_procedure_that_keeps_no_rows_declares_its_out_as_before(compiled: Callable[[str], str], json: str):
    sql = compiled("MATCH (n:A) CALL (n) { DETACH DELETE n }")
    assert f"\n    cy_out {json};\n" in sql, sql
    # The one array declared on Postgres holds the planner's settings around the loop, not rows (#420).
    arrays = [it for it in sql.split("BEGIN")[0].split("\n") if "[]" in it and it != "    cy_planner text[];"]
    assert "cy_returned" not in sql and not arrays, sql


def test_the_lists_kept_in_are_found_within_loops_and_conditions_once_each_in_the_order_first_made():
    # No statement compiles to a Kept within an If yet: a list one made would still be declared.
    loop = steps.Loop("cy_record", "SELECT 1 AS item", False, [steps.Kept("cy_returned2"), steps.Kept("cy_out")])
    program = [steps.If("1 = 1", [steps.Kept("cy_returned1"), loop]), steps.Kept("cy_out"), steps.Kept("cy_returned1")]
    assert PostgresCypherCompiler("g").kept_lists(program) == ["cy_returned1", "cy_returned2", "cy_out"]
