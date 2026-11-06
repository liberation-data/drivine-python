"""
#429, liberation-data/drivine4j#617, liberation-data/drivine#636: a node a pattern finds by a property, the value an
unwound row's (`UNWIND $rows AS row MATCH (a:Person {id: row.s})`), was compared as JSON inside a `CASE` alone, which no
index answers, so a batch of relationships read every node of the label for each row. On Postgres a test the property's
index answers now stands beside it, of the texts the value can be stored as, where the rows are known to hold no list or
map at the key. On Oracle the same read goes through the labelled index's expression, for the properties
`OracleCypherCompiler`'s `indexed_properties` names, which nothing names yet: with none named, Oracle's SQL is as it
was.
These tests pin the SQL each store compiles; tests/integration/sql/test_property_key_index.py pins the rows, and the
plans. drivine4j's PropertyKeyIndexTest is the reference.
"""

import re
from typing import Any

from drivine.backend.oracle.oracle_graph import holds_label, labelled_value
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import TRAILING_ZEROS, PostgresCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler

BY_ROW = "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k"
INDEXED = {("Person", "id")}


def postgres(cypher: str, parameters: dict[str, Any] | None = None) -> str:
    return PostgresCypherCompiler("g").compile(cypher, parameters or {}).sql


def oracle(cypher: str, parameters: dict[str, Any] | None = None, indexed: set[tuple[str, str]] | None = None) -> str:
    return OracleCypherCompiler("g", indexed_properties=indexed or set()).compile(cypher, parameters or {}).sql


def any_of_texts(alias: str, key: str = "id") -> str:
    """The opening of the test Postgres writes beside the comparison of the node's property with a row's value."""
    return f"(({alias}.props ->> '{key}') = ANY(CASE WHEN jsonb_typeof("


def rows(*values: Any) -> dict[str, Any]:
    return {"rows": [{"s": it} for it in values]}


def test_a_rows_value_is_compared_through_the_propertys_index_on_postgres():
    sql = postgres(BY_ROW, rows("p1", 7, 7.5, True))
    assert any_of_texts("n2") in sql, sql
    # The exact comparison still stands beside it.
    assert "THEN (n2.props -> 'id') = (u1.item -> 's') END" in sql, sql
    # A number's text with no trailing zeros, and with each count of them up to TRAILING_ZEROS: `numeric` keeps scale.
    assert f"::text || '.{'0' * TRAILING_ZEROS}'" in sql and f"::text || '.{'0' * (TRAILING_ZEROS + 1)}'" not in sql
    assert f"::text || '{'0' * TRAILING_ZEROS}'" in sql


def test_the_relationships_of_a_batch_are_read_through_the_index_for_each_end():
    statement = (
        "UNWIND $rows AS row MATCH (a:Person {id: row.s}) MATCH (b:Person {id: row.d}) CREATE (a)-[:FOLLOWS]->(b)"
    )
    compiled = PostgresCypherCompiler("g").compile(statement, {"rows": [{"s": "p1", "d": "p2"}]})
    procedure = f"{compiled.prelude}\n{compiled.sql}"
    assert "(n2.props ->> 'id') = ANY(" in procedure and "(n3.props ->> 'id') = ANY(" in procedure, procedure


def test_an_item_unwound_a_where_and_a_key_of_a_map_a_row_holds_are_read_through_the_index():
    assert any_of_texts("n2") in postgres("UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k", {"ids": ["p1", 2]})
    assert any_of_texts("n2") in postgres(
        "UNWIND $rows AS row MATCH (a:Person) WHERE a.id = row.s RETURN a.k", rows("p1")
    )
    assert any_of_texts("n2") in postgres(
        "UNWIND $rows AS row MATCH (a:Person {id: row.a.b}) RETURN a.k", {"rows": [{"a": {"b": "p1"}}, {"a": {}}, {}]}
    )
    # A list written of literals, maps and parameters, as APOC's export writes its rows, on a key of spaces.
    apoc = (
        "UNWIND [{start: {_id: 1}, end: {_id: 2}, properties: {}}, {start: {_id: $a}, end: {_id: 3}, properties: {}}] "
        "AS row MATCH (start:`UNIQUE IMPORT LABEL` {`UNIQUE IMPORT ID`: row.start._id}) "
        "MATCH (finish:`UNIQUE IMPORT LABEL` {`UNIQUE IMPORT ID`: row.end._id}) RETURN start, finish"
    )
    sql = postgres(apoc, {"a": 5})
    assert any_of_texts("n2", "UNIQUE IMPORT ID") in sql and any_of_texts("n3", "UNIQUE IMPORT ID") in sql, sql


def test_a_value_that_may_be_a_list_or_a_map_is_compared_as_json_alone_as_before():
    # A list's numbers may be stored at any scale, [1.0] equal to [1]: no list of texts holds that.
    for statement, parameters in [
        (BY_ROW, rows("p1", [1, 2])),
        (BY_ROW, rows({"x": 1})),
        ("UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k", {"ids": ["p1", []]}),
        ("UNWIND $rows AS row MATCH (a:Person {id: row.a.b}) RETURN a.k", {"rows": [{"a": {"b": [1]}}]}),
        # A key read of a list, which Neo4j refuses: Oracle's path reads the key of each of its items.
        ("UNWIND $rows AS row MATCH (a:Person {id: row.a.b}) RETURN a.k", {"rows": [{"a": [{"b": 1}]}]}),
        # A list written of a function's value, which may be a list, or a value worked out.
        ("UNWIND [{s: 'p5'}, {s: range(7, 8)}] AS row MATCH (a:Person {id: row.s}) RETURN a.k", {}),
        ("UNWIND $rows AS row MATCH (a:Person {id: toString(row.s)}) RETURN a.k", rows("p1")),
        ("UNWIND [{s: 'p5'}, {s: $l}] AS row MATCH (a:Person {id: row.s}) RETURN a.k", {"l": [1, 2]}),
    ]:
        sql = postgres(statement, parameters)
        assert "= ANY(" not in sql, f"{statement}\n{sql}"


def test_rows_that_hold_a_null_are_compared_as_json_alone_as_drivine4j_compares_them():
    # A null in a list or a map makes the comparison one of values that may hold a null (see `_plain`), which stands
    # alone, as in drivine4j.
    assert "= ANY(" not in postgres(BY_ROW, rows("p1", None))


def test_the_same_statement_given_rows_with_no_list_then_rows_with_one_is_compiled_for_each():
    # The compiled form depends on the rows: nothing keeps a statement compiled for one set of them by its text, so one
    # whose rows hold no list is no statement for rows that do. read_cypher keeps the statement's tree alone.
    first = postgres(BY_ROW, rows("p5"))
    second = postgres(BY_ROW, rows("p5", [1, 2]))
    assert any_of_texts("n2") in first and "= ANY(" not in second
    assert postgres(BY_ROW, rows("p5")) == first


def test_a_parameters_and_a_literals_text_are_compared_as_before_on_postgres():
    assert "((n1.props ->> 'id') = (CAST($_cy_s AS jsonb) #>> '{}') AND" in postgres(
        "MATCH (a:Person {id: $s}) RETURN a.k", {"s": "p1"}
    )
    assert "= ANY(" not in postgres("MATCH (a:Person {id: $s}) RETURN a.k", {"s": 7})


def test_sqlite_compiles_a_rows_value_as_before():
    sql = SqliteCypherCompiler("g").compile(BY_ROW, rows("p1")).sql
    assert "ANY(" not in sql and "props_text" not in sql.lower(), sql


def test_oracle_compiled_without_the_graphs_indexes_compares_the_property_as_json_alone():
    # A property with no index of its own: its labels tested by their text, the node table would be read whole
    # (liberation-data/drivine4j#638).
    for statement, parameters in [(BY_ROW, rows("p1")), ("MATCH (a:Person {id: $s}) RETURN a.k", {"s": "p1"})]:
        sql = oracle(statement, parameters)
        assert labelled_value("n2", "Person", "id") not in sql and labelled_value("n1", "Person", "id") not in sql
        assert "JSON_EXISTS(n" in sql and """.labels, '$[*]?(@ == "Person")')""" in sql, sql
        assert "CARDINALITY" not in sql, sql


def test_oracle_told_of_the_index_reads_a_rows_and_a_parameters_value_through_its_expression():
    sql = oracle(BY_ROW, rows("p1", 7), INDEXED)
    assert (
        f"({labelled_value('n2', 'Person', 'id')} = JSON_VALUE(JSON_QUERY(u1.item, '$.s' RETURNING JSON), '$') AND"
        in sql
    )
    # The labels tested by their text, as a node read by its key is: the index over labels would read every node.
    assert holds_label("n2.labels", "Person") in sql and """JSON_EXISTS(n2.labels, '$[*]""" not in sql, sql
    # The unwound rows planned as a few, in their own query's hint: Oracle takes a JSON_TABLE to give 8,168 rows, and
    # hashed every node to them.
    assert "SELECT /*+ CARDINALITY(u1 10) */ " in sql, sql
    parameter = oracle("MATCH (a:Person {id: $s}) RETURN a.k", {"s": "p1"}, INDEXED)
    assert f"({labelled_value('n1', 'Person', 'id')} = JSON_VALUE(JSON($_cy_s), '$') AND" in parameter, parameter


def test_oracle_reads_a_parameters_text_through_the_labelled_index_where_a_walk_sets_out_or_a_quantified_path_finds():
    # The walk's seed, a variable-length relationship's end, and the same statement compiled bare, which writes none.
    statement = "MATCH (a:Person)-[:FOLLOWS*1..2]->(b:Person {id: $s}) RETURN a.k"
    walk = oracle(statement, {"s": "p1"}, INDEXED)
    seed = re.search(
        r"WITH w\d+ \(src, nid, depth, nids, eids\) AS \(\s*SELECT [^\n]*\n\s*FROM g_node (p\d+_n) WHERE [^\n]*", walk
    )
    assert seed is not None, walk
    assert f"{labelled_value(seed.group(1), 'Person', 'id')} = JSON_VALUE(JSON($_cy_s), '$')" in seed.group(0), walk
    assert "NULL ON ERROR) END =" not in oracle(statement, {"s": "p1"})
    # A node of a quantified path, tested on each step of the walk.
    quantified = oracle(
        "MATCH p = ((x:Person {id: $s})-[:FOLLOWS]->(:Person)){1,2} RETURN length(p)", {"s": "p1"}, INDEXED
    )
    step = re.search(r"JOIN g_node (n\d+) ON n\d+\.nid = w\d+\.nid", quantified)
    assert step is not None, quantified
    assert f"{labelled_value(step.group(1), 'Person', 'id')} = JSON_VALUE(JSON($_cy_s), '$')" in quantified, quantified


def test_oracle_lets_a_value_that_may_be_a_list_or_a_text_its_index_holds_none_of_through_to_the_json_comparison():
    long = "x" * 5000
    for given in [rows("p1", [1, 2]), rows("p1", ""), rows("p1", long)]:
        sql = oracle(BY_ROW, given, INDEXED)
        value = "JSON_VALUE(JSON_QUERY(u1.item, '$.s' RETURNING JSON), '$')"
        assert f"(({labelled_value('n2', 'Person', 'id')} = {value} OR {value} IS NULL) AND" in sql, sql
    # A parameter's empty or long text, which the index holds no text for, is compared as JSON alone.
    for text in ["", long]:
        sql = oracle("MATCH (a:Person {id: $s}) RETURN a.k", {"s": text}, INDEXED)
        assert labelled_value("n1", "Person", "id") not in sql, sql


def test_oracle_writes_no_index_expression_for_a_where_another_label_or_after_a_with_that_groups():
    # A WHERE names no label to write the labelled index's expression for.
    where = oracle("UNWIND $rows AS row MATCH (a:Person) WHERE a.id = row.s RETURN a.k", rows("p1"), INDEXED)
    assert "THEN JSON_VALUE(n2.props, '$.\"id\"' RETURNING VARCHAR2(4000)" not in where, where
    assert labelled_value("n2", "Movie", "id") not in oracle(
        "UNWIND $rows AS row MATCH (a:Movie {id: row.s}) RETURN a.k", rows("p1"), INDEXED
    )
    # After a WITH that groups, the index's form of the labels crashed the session (#418, drivine4j#601).
    grouped = oracle(
        "MATCH (n:L) WITH n.k AS k, count(*) AS c UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k",
        rows("p1"),
        INDEXED,
    )
    assert "RETURNING VARCHAR2(4000) NULL ON ERROR) END =" not in grouped, grouped
    assert "CARDINALITY" not in where and "CARDINALITY" not in grouped


def test_oracle_plans_the_rows_of_a_batch_of_relationships_as_a_few_in_the_procedure():
    statement = "UNWIND $rows AS row MATCH (a:Person {id: row.s}) MATCH (b:Person {id: row.d}) CREATE (a)-[:F]->(b)"
    compiled = OracleCypherCompiler("g", indexed_properties=INDEXED).compile(statement, {"rows": [{"s": 1, "d": 2}]})
    assert "CARDINALITY(u1 10)" in f"{compiled.prelude}\n{compiled.sql}"
    plain = OracleCypherCompiler("g").compile(statement, {"rows": [{"s": 1, "d": 2}]})
    assert "CARDINALITY(u1 10)" not in f"{plain.prelude}\n{plain.sql}"


def test_oracle_lets_bytes_whose_base64_text_its_index_holds_none_of_through_to_the_json_comparison():
    # Bytes are bound as their base64 text, which the rows are read as (`_given_tree` reads the bound value): an empty
    # one, and one past 3,000 bytes (a text past 4,000), is a text the labelled index holds nothing for.
    value = "JSON_VALUE(JSON_QUERY(u1.item, '$.s' RETURNING JSON), '$')"
    fallback = f"OR {value} IS NULL) AND"
    for given in [b"", bytes(3001), bytearray(3001)]:
        assert fallback in oracle(BY_ROW, rows(given), INDEXED), given
    assert fallback not in oracle(BY_ROW, rows(bytes(3)), INDEXED)
    assert f"{labelled_value('n2', 'Person', 'id')} = {value} AND" in oracle(BY_ROW, rows(bytes(3)), INDEXED)


def test_oracle_plans_each_unwound_list_as_a_few_rows_in_the_query_that_reads_it():
    # Each hinted alias, and the alias of the first list the query its hint opens reads: they are to be the same.
    placed = re.compile(
        r"SELECT /\*\+ CARDINALITY\((u\d+) 10\) \*/ (?:(?!SELECT )[\s\S])*?'\$\[\*\]' COLUMNS "
        r"\(item_no FOR ORDINALITY, item JSON PATH '\$'\)\) (u\d+)\b"
    )
    union = oracle(
        "UNWIND $rows AS row MATCH (a:Person {id: row.s}) RETURN a.k UNION "
        "UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k",
        {**rows("p1"), "ids": ["p2"]},
        INDEXED,
    )
    assert [it.groups() for it in placed.finditer(union)] == [("u1", "u1"), ("u4", "u4")], union
    # The outermost query reads no list: a hint there is one Oracle doesn't apply.
    assert not re.match(r"^SELECT /\*\+[^*]*CARDINALITY", union), union
    # A second UNWIND: only the list whose item finds the node.
    second = oracle(
        "UNWIND $a AS x UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k", {"a": [1], "ids": ["p2"]}, INDEXED
    )
    assert "CARDINALITY(u2 10)" in second and "CARDINALITY(u1 " not in second, second
    # A subquery read before the list: the hint is the query's that reads both.
    after = oracle(
        "MATCH (b:Person {id: $s}) WITH b UNWIND $ids AS id MATCH (a:Person {id: id}) RETURN a.k",
        {"s": "p1", "ids": ["p2"]},
        INDEXED,
    )
    hinted = re.search(r"CARDINALITY\((u\d+) 10\)", after)
    assert hinted is not None and [hinted.group(1)] == re.findall(r"\) (u\d+)\n", after), after
    assert re.search(
        r"SELECT /\*\+ CARDINALITY\(u\d+ 10\) \*/ JSON\(JSON_QUERY\(JSON_SERIALIZE\(JSON_TRANSFORM\(n\d+\.props, "
        r"KEEP '\$\.k'\)",
        after,
    ), after
    # A text of the list that holds a bracket: the brackets read back over are the query's own.
    bracket = oracle("UNWIND [{s: 'a)('}, {s: 'p2'}] AS row MATCH (a:Person {id: row.s}) RETURN a.k", {}, INDEXED)
    assert [it.groups() for it in placed.finditer(bracket)] == [("u1", "u1")], bracket
