"""
#418, liberation-data/drivine4j#601, liberation-data/drivine#620: on Oracle, a node a `MATCH` finds after a `WITH` that
groups its rows is tested for its labels by their text, which the index over labels can't answer (`grouped_labels`).
Answered by that index, Oracle sometimes crashed the session building the cursor of the Cypher spec's "Maps and lists
holding relationships compare by identity" (ORA-00600 `[QJSNMV_GET_MVI_MD:2]`). Every other test of a node's labels is
as it was, so the index still finds the nodes of a label. The spec case pins the rows. drivine4j's GroupedLabelsTest is
the reference.
"""

import re

import pytest

from drivine.backend.oracle.oracle_graph import holds_label
from drivine.cypher.sql.oracle_cypher_compiler import OracleCypherCompiler
from drivine.cypher.sql.postgres_cypher_compiler import PostgresCypherCompiler
from drivine.cypher.sql.sql_cypher_compiler import SqlCypherCompiler
from drivine.cypher.sql.sqlite_cypher_compiler import SqliteCypherCompiler

SPEC_CASE = (
    "MATCH (p:Person)-[r:ACTED_IN]->() WITH p, collect(DISTINCT [r]) AS l, count(DISTINCT [r]) AS n "
    "MATCH (p)-[r2:ACTED_IN]->(:Movie {title: 'The Matrix'}) RETURN p.name, size(l), n, [r2] IN l ORDER BY p.name"
)


def oracle(cypher: str) -> str:
    return OracleCypherCompiler("g").compile(cypher, {}).sql


def indexed(alias: str, label: str) -> str:
    return f"JSON_EXISTS({alias}.labels, '$[*]?(@ == \"{label}\")')"


def label_tests(sql: str, label: str) -> tuple[list[str], list[str]]:
    """The aliases of the nodes `sql` tests for `label` in the index's form, and those it tests by the labels' text."""
    index = re.findall(rf"""JSON_EXISTS\((n\d+)\.labels, '\$\[\*]\?\(@ == "{re.escape(label)}"\)'\)""", sql)
    serialized = dict.fromkeys(re.findall(r"JSON_SERIALIZE\((n\d+)\.labels\)", sql))
    return index, [it for it in serialized if holds_label(f"{it}.labels", label) in sql]


def test_tests_the_labels_of_the_later_node_by_their_text_and_the_grouped_node_through_the_index_in_the_spec_case() -> (
    None
):
    sql = oracle(SPEC_CASE)
    movie_index, movie_text = label_tests(sql, "Movie")
    assert movie_index == [], sql
    assert len(movie_text) == 1, sql
    person_index, person_text = label_tests(sql, "Person")
    assert len(person_index) == 1, sql
    assert person_text == [], sql
    assert indexed(person_index[0], "Person") in sql, sql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:Person) WITH count(p) AS c MATCH (m:Movie) RETURN c, m.title",
        "MATCH (p:Person) WITH DISTINCT p.name AS name MATCH (m:Movie) RETURN name, m.title",
        "MATCH (p:Person) WITH p, count(*) AS c WITH p, c MATCH (p)-[:ACTED_IN]->(m:Movie) RETURN m.title",
        "MATCH (p:Person) WITH p, count(*) AS c WHERE c > 0 MATCH (p)-[:ACTED_IN]->(m:Movie) RETURN m.title",
        "MATCH (p:Person) WITH p, count(*) AS c MATCH (p)-[:ACTED_IN]->(m:Movie:Film) RETURN m.title",
    ],
)
def test_tests_by_their_text_after_a_with_that_aggregates_is_distinct_or_reads_one_that_does_each_label(
    cypher: str,
) -> None:
    sql = oracle(cypher)
    index, text = label_tests(sql, "Movie")
    assert index == [], sql
    assert len(text) == 1, sql
    if ":Film" in cypher:
        assert len(label_tests(sql, "Film")[1]) == 1, sql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:Person) WITH p, count(*) AS c OPTIONAL MATCH (p)-[:ACTED_IN]->(m:Movie) RETURN c, m.title",
        "MATCH (p:Person) WITH p, count(*) AS c OPTIONAL MATCH (p)-[:ACTED_IN]->(m) WHERE m:Movie RETURN c, m.title",
        "MATCH (p:Person) WITH p, count(*) AS c MATCH s = shortestPath((p)-[:ACTED_IN*]->(m:Movie)) RETURN c, m.title",
    ],
)
def test_tests_by_their_text_a_node_an_optional_match_or_a_shortest_path_finds_after_such_a_with_in_its_lateral(
    cypher: str,
) -> None:
    sql = oracle(cypher)
    index, text = label_tests(sql, "Movie")
    assert index == [], sql
    assert len(text) == 1, sql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:Person) WITH p, count(*) AS c MATCH (m) WHERE m:Movie RETURN c, m.title",
        "MATCH (p:Person) WITH p, count(*) AS c MATCH (p)-[:ACTED_IN]->(m) WHERE m:Movie RETURN c, m.title",
    ],
)
def test_tests_by_their_text_a_label_the_where_of_such_a_node_tests_as_one_its_pattern_names(cypher: str) -> None:
    sql = oracle(cypher)
    index, text = label_tests(sql, "Movie")
    assert index == [], sql
    assert len(text) == 1, sql


def test_a_where_label_test_of_a_node_the_with_passed_on_or_before_the_with_keeps_the_index() -> None:
    # A node the WITH passed on is no node found after it: its WHERE tests the WITH's column, in the index's form.
    passed = oracle("MATCH (m) WITH m, count(*) AS c MATCH (m)-->() WHERE m:Movie RETURN m.title")
    assert re.search(r"""JSON_EXISTS\(w\d+\.c\d+_labels, '\$\[\*]\?\(@ == "Movie"\)'\)""", passed), passed
    assert label_tests(passed, "Movie")[1] == [], passed
    before = oracle("MATCH (m) WHERE m:Movie WITH m, count(*) AS c RETURN m.title, c")
    index, text = label_tests(before, "Movie")
    assert len(index) == 1, before
    assert text == [], before


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (n:Movie) RETURN count(n)",
        "MATCH (p:Person) WITH p MATCH (p)-[:ACTED_IN]->(m:Movie) RETURN m.title",
        "MATCH (p:Person) WITH p ORDER BY p.name LIMIT 3 MATCH (p)-[:ACTED_IN]->(m:Movie) RETURN m.title",
        "MATCH (m:Movie) WITH m, count(*) AS c RETURN m.title, c",
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WITH m, count(p) AS c RETURN m.title, c",
    ],
)
def test_tests_through_the_index_where_no_with_groups_or_before_the_with_that_does(cypher: str) -> None:
    sql = oracle(cypher)
    index, text = label_tests(sql, "Movie")
    assert len(index) == 1, sql
    assert text == [], sql


def test_another_union_branch_tests_through_the_index() -> None:
    sql = oracle(
        "MATCH (p:Person) WITH count(p) AS c MATCH (m:Movie) RETURN m.title AS t "
        "UNION MATCH (m:Movie) RETURN m.title AS t"
    )
    index, text = label_tests(sql, "Movie")
    assert len(index) == 1, sql
    assert len(text) == 1, sql


GROUPED = "MATCH (p:Person) WITH p, count(*) AS c MATCH (p)-[:ACTED_IN]->(m:Movie) RETURN m.title"


@pytest.mark.parametrize("compiler", [PostgresCypherCompiler, SqliteCypherCompiler], ids=lambda it: it.__name__)
def test_leaves_the_test_as_it_was_on_postgres_and_sqlite(compiler: type[SqlCypherCompiler]) -> None:
    store = compiler("g")
    sql = store.compile(GROUPED, {}).sql
    aliases = dict.fromkeys(re.findall(r"\b(n\d+)\.labels", sql))
    assert [it for it in aliases if store.has_labels(f"{it}.labels", ["Movie"]) in sql], sql
    if compiler is PostgresCypherCompiler:
        assert re.search(r"""n\d+\.labels @> '\["Movie"]'""", sql), sql
