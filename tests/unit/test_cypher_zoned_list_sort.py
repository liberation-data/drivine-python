"""
A list of zoned values, and a list of lists of them, compiled: every store sorts it, and takes its `min` and `max`, by
their moments (#181). Oracle reads the items of a list of temporal values without `JSON_TABLE`, whose parse of one
worked out with temporal arithmetic stalled the session (liberation-data/drivine4j#331). drivine4j's
OracleZonedListSortTest of liberation-data/drivine4j#199 and #331. Not in the spec yet (#199).
"""

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

UNWOUND = "UNWIND ['2024-03-15T10:00:00+02:00', '2024-03-15T09:00:00Z'] AS at"
SORTED = f"{UNWOUND} WITH [datetime(at)] AS l RETURN 1 ORDER BY l"
EXTREME = f"{UNWOUND} WITH [[datetime(at)]] AS l RETURN min(l), max(l)"
REPRO = (
    "UNWIND [0, 1] AS k WITH k, "
    "[i IN range(1, 10) | localdatetime('2024-03-15T10:00:00') + duration({minutes: i})] AS l"
)
"""liberation-data/drivine4j#331's statement: a list worked out item by item with temporal arithmetic, by a
comprehension."""


def test_every_store_sorts_a_list_of_zoned_values_or_of_lists_of_them_and_takes_its_extremes_by_their_moments():
    for cypher in (SORTED, EXTREME):
        oracle = OracleCypherCompiler("g").compile(cypher).sql
        assert "LISTAGG" in oracle and " mk1" in oracle, oracle
        postgres = PostgresCypherCompiler("g").compile(cypher).sql
        assert "string_agg" in postgres and " mk1" in postgres, postgres


def test_on_oracle_a_list_of_temporal_values_is_read_item_by_item_with_no_json_table_and_a_list_of_others_with_one():
    temporal = OracleCypherCompiler("g").compile(f"{REPRO} RETURN k ORDER BY size([x IN l WHERE x IS NOT NULL])").sql
    assert "PASSING x6_r.n - 1" in temporal, temporal
    assert "JSON_TABLE(w4.c2" not in temporal, temporal
    other = (
        OracleCypherCompiler("g")
        .compile("UNWIND [0, 1] AS k WITH k, [i IN range(1, 10) | i + k] AS l RETURN size([x IN l WHERE x > 1])")
        .sql
    )
    assert "PASSING" not in other, other
    # collect(DISTINCT …) keeps each list, or each value, once, reading the collected list by its places too.
    for distinct in (f"{REPRO} RETURN collect(DISTINCT l)", f"{REPRO} UNWIND l AS t RETURN collect(DISTINCT t)"):
        sql = OracleCypherCompiler("g").compile(distinct).sql
        assert "PASSING e_r.n - 1" in sql, sql
    assert "PASSING" not in OracleCypherCompiler("g").compile("UNWIND [1, 1] AS k RETURN collect(DISTINCT k)").sql
