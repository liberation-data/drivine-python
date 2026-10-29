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
LIST = "[i IN range(1, 10) | localdatetime('2024-03-15T10:00:00') + duration({minutes: i})]"
"""liberation-data/drivine4j#331's list: worked out item by item with temporal arithmetic, by a comprehension."""
REPRO = f"UNWIND [0, 1] AS k WITH k, {LIST} AS l"
"""#331's statement: its list, on each of two rows."""
_SIZE = "RETURN size([x IN l WHERE x IS NOT NULL])"
UNWOUND_REPROS = (
    f"UNWIND [0, 1] AS k WITH k, [{{l: {LIST}}}] AS ms UNWIND ms AS t WITH k, t.l AS l {_SIZE}",
    f"{REPRO} UNWIND [{{l: l}}] AS t WITH k, t.l AS l {_SIZE}",
    f"UNWIND [0, 1] AS k WITH k, [{{m: {{l: {LIST}}}}}] AS ms UNWIND ms AS t WITH k, t.m AS m WITH k, m.l AS l {_SIZE}",
    f"UNWIND [0, 1] AS k WITH k, [{{l: {LIST}}}, {{l: {LIST}}}] AS ms UNWIND ms AS t WITH k, t.l AS l {_SIZE}",
    f"UNWIND [0, 1] AS k WITH k, [{{l: {LIST}}}] AS ms UNWIND ms AS t CREATE (:CyUnwoundTemporals) "
    f"WITH k, t.l AS l {_SIZE}",
)
"""
#331's list in a map, unwound, and taken out by a `WITH` (liberation-data/drivine4j#369): from a map, one the list was
put in after it was worked out, a map two levels down, a list of two maps, and after a write.
"""
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
    # Worked out by the lateral view itself: a subquery in its select list is worked out once, for every row
    # (liberation-data/drivine4j#414, here #257).
    assert "LATERAL (SELECT /*+ NO_MERGE */ JSON_DATAGUIDE(" in keys, keys
    assert "(SELECT JSON_DATAGUIDE" not in keys, keys


def test_on_oracle_a_list_of_temporal_values_a_with_takes_out_of_an_unwound_map_is_read_item_by_item_too():
    # A property of an unwound item held no temporal values deeper, and such a list was read with JSON_TABLE again
    # (liberation-data/drivine4j#369). Zoned values and durations, and a list with nulls among them, as well.
    others = tuple(
        f"UNWIND [0, 1] AS k WITH k, [{{l: [i IN range(1, 10) | {it}]}}] AS ms UNWIND ms AS t WITH k, t.l AS l {_SIZE}"
        for it in (
            "datetime('2024-03-15T10:00:00+02:00') + duration({minutes: i})",
            "duration({minutes: i})",
            "CASE i WHEN 1 THEN null ELSE date() END",
        )
    )
    for cypher in UNWOUND_REPROS + others:
        sql = OracleCypherCompiler("g").compile(cypher).sql
        assert re.search(r"PASSING x\d+_r\.n - 1", sql), sql
        assert not re.search(r"JSON_TABLE\((w|q|u)\d+\.(c|item)", sql), sql
    # A list of other values out of an unwound map is still read with JSON_TABLE, and the value is read as
    # liberation-data/drivine4j#344 reads it.
    numbers = (
        OracleCypherCompiler("g")
        .compile(
            "UNWIND [0, 1] AS k WITH k, [{l: range(1, 10)}] AS ms UNWIND ms AS t WITH k, t.l AS l "
            "RETURN size([x IN l WHERE x > 1])"
        )
        .sql
    )
    assert "PASSING" not in numbers, numbers
    for cypher in (UNWOUND_REPROS[0], "UNWIND [{a: 'x', l: [date()]}] AS t WITH t.a AS a RETURN a"):
        sql = OracleCypherCompiler("g").compile(cypher).sql
        assert "JSON(JSON_QUERY(JSON_SERIALIZE(" in sql, sql


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


def test_on_oracle_a_list_of_maps_of_temporal_values_is_read_by_its_places_wherever_the_statement_passes_it_on():
    # Each step that passes on what a list holds deeper than its items (liberation-data/drivine4j#347): every JSON_TABLE
    # that reads a list of maps of numbers made so reads the list of maps of temporal values by its places instead, and
    # only the UNWIND's and range's JSON_TABLEs are left.
    def listed(item: str) -> str:
        return f"UNWIND [0, 1, 2] AS k WITH k, [i IN range(1, k) | {{t: {item}}}] AS ms"

    where = "WHERE m.t IS NOT NULL"
    readers = (
        f"RETURN [m IN tail(ms) {where}]",
        f"RETURN [m IN reverse(ms) {where}]",
        f"RETURN [m IN ms[1..] {where}]",
        f"RETURN [m IN ms + ms {where}]",
        f"RETURN [m IN CASE WHEN k > 0 THEN ms END {where}]",
        f"WITH k, [ms, ms] AS mss RETURN [m IN head(mss) {where}]",
        f"WITH k, [ms, ms] AS mss RETURN [m IN last(mss) {where}]",
        f"WITH k, [ms, ms] AS mss RETURN [m IN mss[1] {where}]",
        f"WITH min(ms) AS mn RETURN [m IN mn {where}]",
        f"WITH max(ms) AS mx RETURN [m IN mx {where}]",
        "WITH collect(ms) AS c RETURN [x IN c WHERE size(x) > 0]",
        f"WITH k, {{s: ms}} AS z RETURN [m IN z {{.s}}.s {where}]",
        f"WITH k, {{s: ms}} AS z RETURN [m IN z {{.*, n: 1}}.s {where}]",
        f"UNWIND [ms] AS u RETURN [m IN u {where}]",
        f"CALL {{ WITH ms RETURN [{{t: 1}}] AS u UNION WITH ms RETURN ms AS u }} RETURN [m IN u {where}]",
    )
    for reader in readers:
        temporal, other = (
            OracleCypherCompiler("g").compile(f"{listed(item)} {reader}").sql
            for item in ("localdatetime('2024-03-15T10:00:00') + duration({minutes: i})", "i + 1")
        )
        assert temporal.count("JSON_TABLE") == 2, reader
        assert temporal.count("PASSING") - other.count("PASSING") == other.count("JSON_TABLE") - 2, reader
