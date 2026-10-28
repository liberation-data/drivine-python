"""
A list of zoned values, and a list of lists of them, compiled: every store sorts it, and takes its `min` and `max`, by
their moments (#181). Oracle reads the items of a list of temporal values without `JSON_TABLE`, whose parse of one
worked out with temporal arithmetic stalled the session (liberation-data/drivine4j#331). drivine4j's
OracleZonedListSortTest of liberation-data/drivine4j#199 and #331. Beside the spec's case since v0.6.17 (#199).
"""

import re

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
DEEP_REPROS = (
    f"{REPRO} RETURN collect(DISTINCT [l, l])",
    f"{REPRO} RETURN collect(DISTINCT [x IN l | [x]])",
    f"{REPRO} RETURN collect(DISTINCT [x IN l | [x, null]])",
    f"{REPRO} WITH k, [l, l] AS ll RETURN collect(DISTINCT ll)",
    f"{REPRO} RETURN collect(DISTINCT [x IN l | {{t: x}}])",
)
"""Statements that collect, once each, lists that hold #331's temporal values two levels down, or in maps: see
liberation-data/drivine4j#347."""
MAY_HOLD_REPROS = (
    f"{REPRO} WITH k, [l, l] AS ll RETURN [k, any(x IN ll WHERE size([y IN x WHERE y IS NOT NULL]) > 9)] ORDER BY k",
    f"{REPRO} WITH k, [l, l] AS ll RETURN reduce(s = 0, x IN ll | s + size([y IN x WHERE y IS NOT NULL])) / 2",
    "UNWIND [0, 1] AS k WITH k, [i IN range(1, 10) | coalesce(CASE WHEN k > 5 THEN k END, "
    "localdatetime('2024-03-15T10:00:00') + duration({minutes: i}))] AS l RETURN size([x IN l WHERE x IS NOT NULL])",
    "UNWIND [0, 1] AS k WITH k, [i IN range(1, 10) | CASE WHEN k > 5 THEN k ELSE "
    "localdatetime('2024-03-15T10:00:00') + duration({minutes: i}) END] AS l RETURN size([x IN l WHERE x IS NOT NULL])",
    f"{REPRO} WITH k, reduce(acc = [], x IN l | acc + [x]) AS m RETURN size([y IN m WHERE y IS NOT NULL])",
    f"{REPRO} WITH k, {{minute: l}} AS m RETURN size([y IN m.minute WHERE y IS NOT NULL])",
    f"{REPRO} WITH k, [{{minute: l}}] AS ms RETURN size([y IN ms[0].minute WHERE y IS NOT NULL])",
)
"""
Statements whose lists may hold #331's temporal values: a list of lists of them, an item of which a quantifier or reduce
takes, a list worked out item by item with a coalesce or a CASE that is a temporal value or a number, a list a reduce
gives of them, and a list of them at a map's key that a temporal value's field is named for.
"""
KEYED = f"{REPRO} WITH k, {{l: l}} AS m RETURN size(keys(m))"
"""#331's list in a map whose keys are taken: Oracle reads them from the map's data guide."""


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


def test_on_oracle_a_coalesce_of_a_list_of_temporal_values_and_a_list_that_holds_them_deeper_is_read_item_by_item():
    # coalesce(l, []) is of l's kind (liberation-data/drivine4j#347).
    coalesced = (
        OracleCypherCompiler("g")
        .compile(f"{REPRO} RETURN k ORDER BY size([x IN coalesce(l, []) WHERE x IS NOT NULL])")
        .sql
    )
    assert "PASSING x6_r.n - 1" in coalesced, coalesced
    assert "JSON_TABLE(w4.c2" not in coalesced, coalesced
    # A list of lists of lists, or of maps, of temporal values, as collect(DISTINCT …) gathers it.
    for deep in DEEP_REPROS:
        sql = OracleCypherCompiler("g").compile(deep).sql
        assert "PASSING e_r.n - 1" in sql, sql
        assert not re.search(r"JSON_TABLE\(w\d+\.", sql), sql
    # A list of maps of them unwound, and such a list taken back out of a map.
    for unwound in (
        f"{REPRO} UNWIND [x IN l | {{t: x}}] AS m RETURN count(m.t)",
        f"{REPRO} WITH k, {{l: l}} AS m UNWIND m.l AS t RETURN count(t)",
    ):
        sql = OracleCypherCompiler("g").compile(unwound).sql
        assert re.search(r"PASSING u\d+_r\.n - 1", sql), sql
    # Not a list of lists of lists of other values, a coalesce, a collect, a UNION or a map of them.
    numbers = "UNWIND [0, 1] AS k WITH k, [i IN range(1, 10) | i + k] AS l"
    for other in (
        f"{numbers} RETURN collect(DISTINCT [l, l])",
        f"{numbers} RETURN size([x IN coalesce(l, []) WHERE x > 1])",
        f"{numbers} RETURN size([x IN coalesce(l, [1, 2]) WHERE x > 1])",
        f"{numbers} UNWIND l AS x WITH k, collect(x) AS c RETURN size([y IN c WHERE y > 1])",
        f"{numbers} WITH k, [x IN l | {{a: x, b: 'z'}}] AS ms RETURN size([m IN ms WHERE m.a > 1])",
        f"{numbers} WITH k, {{l: l, n: 1}} AS m RETURN size([x IN m.l WHERE x > 1])",
        "CALL { RETURN [1, 2] AS l UNION RETURN [3, 4] AS l } RETURN size([x IN l WHERE x > 1])",
    ):
        sql = OracleCypherCompiler("g").compile(other).sql
        assert "PASSING" not in sql, sql


def test_on_oracle_a_value_that_may_hold_temporal_values_and_a_map_of_them_and_its_keys_are_read_item_by_item():
    # An item a quantifier takes, a coalesce, CASE or reduce that may hold a temporal value, and a map of them.
    for cypher in MAY_HOLD_REPROS:
        sql = OracleCypherCompiler("g").compile(cypher).sql
        assert "PASSING" in sql, sql
        assert not re.search(r"JSON_TABLE\((w|q)\d+\.(c|item)", sql), sql
    keys = OracleCypherCompiler("g").compile(KEYED).sql
    assert "PASSING dg_r.n - 1" in keys, keys
    assert "JSON_TABLE((SELECT JSON_DATAGUIDE" not in keys, keys


def test_on_oracle_a_list_of_temporal_values_is_read_by_its_places_wherever_it_is_read_in_each_shape_of_in_too():
    # Each reader of the list `l`, which may hold a null: every JSON_TABLE that reads a list of numbers made so reads
    # the list of temporal values by its places instead, and only the UNWIND's and range's JSON_TABLEs are left.
    def listed(item: str) -> str:
        return f"UNWIND [0, 1, 2] AS k WITH k, [i IN range(1, k) | CASE WHEN i > 1 THEN {item} END] AS l, $v AS v"

    readers = (
        "RETURN v IN l",
        "WITH k, l, v WHERE v IN l RETURN k",
        "WITH k, l WHERE $v IN l RETURN k",
        "WITH k, l WHERE l[0] IN l RETURN k",
        "RETURN l[0] IN l",
        "RETURN [x IN l WHERE x IN l]",
        "RETURN l[1..]",
        "RETURN reverse(l)",
        "RETURN tail(l)",
        "UNWIND l AS t RETURN t",
        "RETURN any(x IN l WHERE x IS NULL)",
        "RETURN all(x IN l WHERE x IS NULL)",
        "RETURN none(x IN l WHERE x IS NULL)",
        "RETURN single(x IN l WHERE x IS NULL)",
        "RETURN [x IN l | x]",
        "RETURN collect(DISTINCT l)",
        "UNWIND l AS t RETURN collect(DISTINCT t)",
    )
    for reader in readers:
        temporal, other = (
            OracleCypherCompiler("g").compile(f"{listed(item)} {reader}", {"v": 1}).sql
            for item in ("localdatetime('2024-03-15T10:00:00') + duration({minutes: i})", "i + 1")
        )
        assert temporal.count("JSON_TABLE") == 2, reader
        assert temporal.count("PASSING") - other.count("PASSING") == other.count("JSON_TABLE") - 2, reader
