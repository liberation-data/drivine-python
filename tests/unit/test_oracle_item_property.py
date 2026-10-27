"""
On Oracle, a property that is an item of a `RETURN` or a `WITH` is read through its text, as extended JSON: Oracle
crashed the session where a statement took a text past 32 KB from the properties as JSON for each of two rows (#144).
So is a text a write takes from the row it is writing, to work out what it writes (#176), and an unwound item's value a
`RETURN` or a `WITH` gives (#186). Elsewhere a property is read as it was. drivine4j's OracleItemPropertyTest, ported.
"""

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler, _direct  # pyright: ignore[reportPrivateUsage]
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler

LONG = (
    "JSON(JSON_QUERY(JSON_SERIALIZE(JSON_TRANSFORM(n1.props, KEEP '$.a') RETURNING CLOB EXTENDED), "
    "'$.a' RETURNING CLOB) EXTENDED)"
)


def oracle(cypher: str) -> str:
    return OracleCypherCompiler("g").compile(cypher).sql


def test_a_property_an_item_returns_or_passes_on_is_read_through_its_text_and_one_sorted_tested_or_nested_as_it_was():
    returned = oracle("MATCH (n:Doc) WHERE n.b = 1 RETURN n.a ORDER BY n.id")
    assert f"{LONG} AS c1" in returned, returned
    assert "JSON_QUERY(n1.props, '$.id' RETURNING JSON) AS s1" in returned, returned
    assert "JSON_QUERY(n1.props, '$.b' RETURNING JSON)" in returned, returned
    passed = oracle("MATCH (n:Doc) WITH n.a AS a RETURN [a]")
    assert passed.count(LONG) == 1, passed
    nested = oracle("MATCH (n:Doc) RETURN [n.a, n {.a}]")
    assert "RETURNING CLOB" not in nested, nested
    sorted_ = oracle("MATCH (n:Doc) RETURN n.a ORDER BY n.a")
    assert f"{LONG} AS c1" in sorted_ and f"{LONG} AS s1" in sorted_, sorted_


def test_a_property_returned_beside_a_variable_length_walk_is_read_as_elsewhere():
    sql = oracle("MATCH (:Doc {id: 1})-[:R*]->(n) RETURN n.a")
    assert "RETURNING CLOB" not in sql and ".a' RETURNING JSON)" in sql, sql


def test_a_relationships_property_returned_is_read_through_its_text():
    sql = oracle("MATCH ()-[r:R]->() RETURN r.a")
    assert "KEEP '$.a') RETURNING CLOB EXTENDED), '$.a' RETURNING CLOB) EXTENDED)" in sql, sql


def test_a_maps_value_returned_is_read_as_elsewhere():
    sql = oracle("WITH {a: 1} AS m RETURN m.a")
    # Every result is kept whole (`KEEP '$'`); the workaround keeps the one key (`KEEP '$.a'`).
    assert "KEEP '$.a'" not in sql and "'$.a' RETURNING JSON)" in sql, sql


def test_a_nodes_stamp_returned_is_read_from_its_column():
    sql = oracle("MATCH (n) RETURN n.`__drivine.stamp`")
    assert "JSON_SCALAR(n1.stamp) AS c1" in sql and "RETURNING CLOB" not in sql, sql


def test_a_property_an_item_groups_by_or_makes_distinct_is_read_through_its_text():
    grouped = oracle("MATCH (n:Doc) RETURN n.a, count(*)")
    assert LONG in grouped, grouped
    distinct = oracle("MATCH (n:Doc) RETURN DISTINCT n.a")
    assert LONG in distinct, distinct


def test_a_property_a_write_returns_is_read_through_its_text():
    sql = oracle("MATCH (n:Doc) SET n.b = 1 RETURN n.a")
    assert "KEEP '$.a') RETURNING CLOB EXTENDED), '$.a' RETURNING CLOB) EXTENDED) AS c1" in sql, sql


def row_read(path: str) -> str:
    return (
        f"CASE WHEN JSON_VALUE(cy_row, '{path}.type()') = 'string' "
        f"THEN JSON(JSON_QUERY(JSON_SERIALIZE(cy_row RETURNING CLOB), '{path}' RETURNING CLOB)) "
        f"ELSE JSON_QUERY(cy_row, '{path}' RETURNING JSON) END"
    )


def test_a_text_a_write_takes_from_its_row_is_read_through_the_rows_text_and_a_pattern_it_matches_tests_it_as_it_was():
    set_ = (
        OracleCypherCompiler("g")
        .compile("UNWIND $ts AS t MATCH (n:Doc {id: t.id}) SET n.a = t.a, n.b = t, n.c = t.m.`k.j`", {"ts": []})
        .sql
    )
    dotted = row_read('$.v1.m."k.j"')
    assert f"SELECT {row_read('$.v1.a')} INTO cy_value" in set_, set_
    assert f"SELECT {row_read('$.v1')} INTO cy_value" in set_, set_
    assert f"SELECT {dotted} INTO cy_value" in set_, set_
    assert "JSON_QUERY(JSON_QUERY(cy_row" not in set_, set_
    merged = (
        OracleCypherCompiler("g")
        .compile("UNWIND $ts AS t MERGE (n:Doc {id: t.id}) ON CREATE SET n.a = t.a", {"ts": []})
        .sql
    )
    assert "= JSON_QUERY(JSON_QUERY(cy_row, '$.v1' RETURNING JSON), '$.id' RETURNING JSON)" in merged, merged
    assert f"JSON_OBJECT('id' VALUE {row_read('$.v1.id')} NULL ON NULL RETURNING JSON) INTO cy_value" in merged, merged
    assert f"SELECT {row_read('$.v1.a')} INTO cy_value" in merged, merged


def item_read(json: str) -> str:
    return (
        f"CASE WHEN JSON_VALUE({json}, '$.a.type()') = 'string' "
        f"THEN JSON(JSON_QUERY(JSON_SERIALIZE({json} RETURNING CLOB), '$.a' RETURNING CLOB)) "
        f"ELSE JSON_QUERY({json}, '$.a' RETURNING JSON) END"
    )


def test_an_unwound_items_value_an_item_returns_or_passes_on_is_read_through_the_items_text_where_it_is_a_text():
    # Oracle crashed the session where such a value, a text past 32 KB, was read as JSON for each of two rows (#186).
    def compiled(cypher: str) -> str:
        return OracleCypherCompiler("g").compile(cypher, {"ts": []}).sql

    returned = compiled("UNWIND $ts AS t RETURN t.a")
    assert f"{item_read('u1.item')} AS c1" in returned, returned
    passed = compiled("UNWIND $ts AS t WITH t.a AS a CREATE (:Doc {a: a})")
    assert f"{item_read('u1.item')} AS c1" in passed, passed
    carried = compiled("UNWIND $ts AS t WITH t RETURN t.a")
    assert f"{item_read('w3.c1')} AS c1" in carried, carried
    written = compiled("UNWIND $ts AS t MATCH (n:Doc {id: t.id}) SET n.b = 1 RETURN t.a")
    from_rows = item_read("JSON_QUERY(r4.item, '$.v1' RETURNING JSON)")
    assert f"{from_rows} AS c1" in written, written
    # Tested, sorted, nested in an item, or matched by a pattern, as it was.
    other = compiled("UNWIND $ts AS t WITH t WHERE t.b = 1 RETURN [t.a] ORDER BY t.c")
    assert "SERIALIZE" not in other and "JSON_QUERY(w3.c1, '$.a' RETURNING JSON)" in other, other
    merged = compiled("UNWIND $ts AS t MERGE (n:Doc {a: t.a})")
    assert "= JSON_QUERY(JSON_QUERY(cy_row, '$.v1' RETURNING JSON), '$.a' RETURNING JSON)" in merged, merged
    for cypher in (
        # A UNION whose every query returns an unwound item gives one, read so too.
        "CALL { UNWIND $ts AS m RETURN m UNION ALL UNWIND $ts AS m RETURN m } RETURN m.a",
        # A map made of a node's properties and carried past a write is read from the write's rows, so read so too.
        "MATCH (n:Doc) WITH n, n {.a} AS m SET n.b = 1 RETURN m.a",
        # So does a CALL that returns one, an OPTIONAL one among them.
        "CALL { UNWIND $ts AS m RETURN m } RETURN m.a",
        "OPTIONAL CALL { UNWIND $ts AS m RETURN m } RETURN m.a",
    ):
        sql = compiled(cypher)
        assert "JSON_SERIALIZE(" in sql and "'$.a.type()'" in sql, sql
    # A temporal value's field, or a key that may name one, is read as a temporal field is.
    for cypher in ("UNWIND $ts AS t RETURN t.year", "UNWIND [date('2020-01-02')] AS t RETURN t.year"):
        sql = compiled(cypher)
        assert "'$.year.type()'" not in sql, sql


def test_a_value_of_a_map_made_of_a_nodes_properties_or_written_is_read_as_elsewhere():
    # Only a value read from an unwound item is read through its text: a CASE on a node's properties made Oracle drop a
    # label test.
    for cypher in (
        "MATCH (n:Doc) RETURN properties(n).a",
        "MATCH (n:Doc) WITH n {.a} AS m RETURN m.a",
        "MATCH (n:Doc) WITH {a: n.a} AS m RETURN m.a",
        "MATCH (n:Doc) CALL { WITH n RETURN n {.a} AS m UNION ALL UNWIND [{a: 1}] AS m RETURN m } RETURN m.a",
    ):
        sql = oracle(cypher)
        assert "SERIALIZE" not in sql, sql


def test_on_postgres_an_unwound_items_value_returned_is_read_as_elsewhere():
    def compiled(cypher: str) -> str:
        return PostgresCypherCompiler("g").compile(cypher, {"ts": []}).sql

    assert "SELECT (u1.item -> 'a') AS c1" in compiled("UNWIND $ts AS t RETURN t.a")
    assert "SELECT (w3.c1 -> 'a') AS c1" in compiled("UNWIND $ts AS t WITH t RETURN t.a")
    # A write on Postgres is a function, which `prelude` makes.
    prelude = (
        PostgresCypherCompiler("g")
        .compile("UNWIND $ts AS t MATCH (n:Doc {id: t.id}) SET n.b = 1 RETURN t.a", {"ts": []})
        .prelude
    )
    assert prelude is not None
    assert "SELECT ((r4.item -> 'v1') -> 'a') AS c1" in prelude, prelude


def test_a_value_a_union_in_a_writes_subquery_takes_from_its_row_in_each_query_is_read_from_what_the_union_gives():
    # x is t.m in one query and t.l in the other: read from the row at the first's keys, the other's rows read t.m.a.
    sql = (
        OracleCypherCompiler("g")
        .compile(
            "UNWIND $ts AS t MATCH (n {id: t.id}) SET n.a = COUNT { CALL { WITH t RETURN t.m AS x UNION ALL "
            "WITH t RETURN t.l AS x } MATCH (y) WHERE y.k = x.a RETURN y }",
            {"ts": []},
        )
        .sql
    )
    assert "'$.v1.m.a'" not in sql, sql
    assert "'$.v1.l.a'" not in sql, sql


def test_a_value_an_optional_call_in_a_writes_subquery_returns_from_its_row_is_read_from_what_the_call_gives():
    # u is null where the call finds nothing: read from the row, it would be t, and y.k compared with t.a.
    cypher = (
        "UNWIND $ts AS t MATCH (n {id: t.id}) SET n.a = COUNT { OPTIONAL CALL { WITH t MATCH (y:Nope) RETURN t AS u } "
        "MATCH (y) WHERE y.k = u.a RETURN y }"
    )
    assert "'$.v1.a'" not in OracleCypherCompiler("g").compile(cypher, {"ts": []}).sql
    prelude = PostgresCypherCompiler("g").compile(cypher, {"ts": []}).prelude
    assert prelude is not None
    assert "((cy_row -> 'v1') -> 'a')" not in prelude, prelude


def test_on_postgres_a_value_a_write_takes_from_its_row_is_read_as_elsewhere():
    # A write on Postgres is a function, which `prelude` makes: its body reads the row's keys one by one, as before.
    compiled = PostgresCypherCompiler("g").compile("UNWIND $ts AS t MATCH (n:Doc {id: t.id}) SET n.a = t.a", {"ts": []})
    assert compiled.prelude is not None
    assert "SELECT ((cy_row -> 'v1') -> 'a') INTO cy_value" in compiled.prelude, compiled.prelude


def test_on_postgres_a_property_returned_is_read_as_elsewhere():
    sql = PostgresCypherCompiler("g").compile("MATCH (n:Doc) RETURN n.a").sql
    assert "(n1.props -> 'a')" in sql, sql


def test_a_property_a_text_function_or_a_sum_reads_is_read_from_the_properties_by_its_own_path_not_taken_out_first():
    # Taken out with JSON_QUERY and read as a CLOB or summed, a text past 32 KB closed the session, one row a statement,
    # and a later statement lost its rows (#168).
    sql = oracle(
        "MATCH (n:Doc) RETURN [toUpper(n.a), size(n.a), substring(n.a, 1), n.a STARTS WITH 'x', n.a + 'x', "
        "toString(n.a)]"
    )
    assert "JSON_VALUE(JSON_QUERY(n1.props, '$.a' RETURNING JSON)" not in sql, sql
    assert "JSON_VALUE(n1.props, '$.a' RETURNING CLOB)" in sql and "JSON_VALUE(n1.props, '$.a.type()')" in sql, sql
    assert "JSON_VALUE(n1.props, '$.a.size()' RETURNING NUMBER)" in sql, sql


def test_a_property_whose_key_is_quoted_in_its_path_or_is_in_a_map_is_read_by_its_own_path_too():
    def read(cypher: str) -> str:
        sql = oracle(cypher)
        assert "JSON_VALUE(JSON_QUERY(n1.props, '$.\"" not in sql, sql
        return sql

    quoted = read("MATCH (n:Doc) RETURN [size(n.`it's`), toUpper(n.`a b`), size(n.`x' RETURNING JSON) y`)]")
    assert "JSON_VALUE(n1.props, '$.\"it''s\".size()' RETURNING NUMBER)" in quoted, quoted
    assert "JSON_VALUE(n1.props, '$.\"a b\"' RETURNING CLOB)" in quoted, quoted
    assert "JSON_VALUE(n1.props, '$.\"x'' RETURNING JSON) y\".type()')" in quoted, quoted
    # A key in a map is read by its path in the map, the map taken out of the properties as it was.
    nested = read("MATCH (n:Doc) RETURN size(n.m.a)")
    assert "JSON_VALUE(JSON_QUERY(n1.props, '$.m' RETURNING JSON), '$.a.size()' RETURNING NUMBER)" in nested, nested


def test_a_map_whose_key_holds_a_parenthesis_is_read_by_its_path_and_two_values_read_together_are_left_as_they_are():
    # The parenthesis is in a literal, so the map taken out of the properties is still the one argument.
    sql = oracle("MATCH (n:Doc) RETURN size(n.`a)`.b)")
    assert "JSON_VALUE(JSON_QUERY(n1.props, '$.\"a)\"' RETURNING JSON), '$.b.size()' RETURNING NUMBER)" in sql, sql
    # Two values that start and end as a property does are no property: what reads them is left as it is.
    two = "JSON_QUERY(a, '$.x' RETURNING JSON) || JSON_QUERY(b, '$.y' RETURNING JSON)"
    read = f"JSON_VALUE({two}, '$.type()')"
    assert _direct(read, two) == read
