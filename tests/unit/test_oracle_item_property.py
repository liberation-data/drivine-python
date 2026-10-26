"""
On Oracle, a property that is an item of a `RETURN` or a `WITH` is read through its text, as extended JSON: Oracle
crashed the session where a statement took a text past 32 KB from the properties as JSON for each of two rows (#144).
Elsewhere a property is read as it was. drivine4j's OracleItemPropertyTest, ported.
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


def test_on_postgres_a_property_returned_is_read_as_elsewhere():
    sql = PostgresCypherCompiler("g").compile("MATCH (n:Doc) RETURN n.a").sql
    assert "(n1.props -> 'a')" in sql, sql
