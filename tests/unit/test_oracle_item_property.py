"""
On Oracle, a property that is an item of a `RETURN` or a `WITH` is read through its text, as extended JSON: Oracle
crashed the session where a statement took a text past 32 KB from the properties as JSON for each of two rows (#144).
So is a text a write takes from the row it is writing, to work out what it writes (#176). Elsewhere a property is read
as it was. drivine4j's OracleItemPropertyTest, ported.
"""

from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
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
