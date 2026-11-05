"""The BM25 statement the SQL stores score a full-text search by (drivine4j#604), as Drivine for TypeScript tests it."""

import math
import re
from typing import Annotated

import pytest

from drivine import FullTextIndex, ModelRegistry, NodeFragment, NodeId
from drivine.backend.matching_search import MatchingSearch
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.sql.bm25 import Bm25Dialect, bm25_ranked, bm25_score
from drivine.backend.sql.text_query import TextQuery, is_prefix


class Shelf(NodeFragment, labels=["Shelf"]):
    id: Annotated[str, NodeId()]
    title: Annotated[str | None, FullTextIndex()] = None


# Writes each call as itself, so the statement shows what was asked of the store.
dialect = Bm25Dialect(
    count=lambda text, term: f"COUNT({text}, {term})",
    holds=lambda text, term: f"HOLDS({text}, {term})",
    ln=lambda value: f"LN({value})",
    real=lambda value: f"REAL({value})",
)


def squashed(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def test_counts_each_term_holds_the_candidates_to_the_scoring_terms_and_filters_required_and_excluded_after():
    query = TextQuery.parse("graph +data -noise")
    graph, data = query.scored
    (noise,) = query.excluded
    sql = bm25_ranked("CANDIDATES", "LABEL", query, dialect)
    assert (
        f"SELECT s.nid, s.len, COUNT(s.text, {graph}) AS tf1, COUNT(s.text, {data}) AS tf2, "
        f"COUNT(s.text, {data}) AS r1, COUNT(s.text, {noise}) AS x1 FROM (CANDIDATES) s "
        f"WHERE (HOLDS(s.text, {graph}) OR HOLDS(s.text, {data}))"
    ) in sql
    # The label's figures are read with no term matched, and each term's rarity is a window over the candidates.
    assert (
        "SELECT h.*, (SELECT REAL(COUNT(*)) FROM (LABEL) l) AS n, (SELECT REAL(AVG(l.len)) FROM (LABEL) l) AS avglen, "
        "SUM(CASE WHEN h.tf1 > 0 THEN 1 ELSE 0 END) OVER () AS df1, SUM(CASE WHEN h.tf2 > 0 THEN 1 ELSE 0 END) OVER () "
        "AS df2"
    ) in sql
    assert (
        "LN(1 + (w.n - w.df1 + 0.5) / (w.df1 + 0.5)) * w.tf1 * 2.2 / (w.tf1 + 1.2 * (0.25 + 0.75 * w.len / "
        "NULLIF(w.avglen, 0)))"
    ) in sql
    assert ") w WHERE w.r1 > 0 AND w.x1 = 0) b WHERE b.score > 0" in sql
    assert sql.startswith("SELECT b.nid, b.score / MAX(b.score) OVER () AS score FROM (")


def test_filters_nothing_after_the_window_when_no_term_is_required_or_excluded():
    sql = bm25_ranked("CANDIDATES", "LABEL", TextQuery.parse("graph"), dialect)
    assert " w WHERE" not in sql
    assert ") w) b WHERE b.score > 0" in sql


def test_scores_a_prefix_term_by_k1_plus_1_for_each_node_that_holds_it_as_lucenes_prefix_query_the_rest_by_bm25():
    query = TextQuery.parse("kiw* melon")
    assert [is_prefix(it) for it in query.scored] == [True, False]
    sql = bm25_ranked("CANDIDATES", "LABEL", query, dialect)
    assert (
        "REAL(CASE WHEN w.tf1 > 0 THEN 2.2 ELSE 0 END + LN(1 + (w.n - w.df2 + 0.5) / (w.df2 + 0.5)) * w.tf2 * 2.2 / "
        "(w.tf2 + 1.2 * (0.25 + 0.75 * w.len / NULLIF(w.avglen, 0)))) AS score"
    ) in sql
    assert "w.df1 + 0.5" not in sql


def test_scores_as_lucene_does_with_k1_1_2_and_b_0_75_drivine4js_figures():
    def score(count: int, holding: int, nodes: int, length: int, average: float) -> float:
        # The score, as SQL, worked out in Python: its arithmetic is the same in both.
        sql = bm25_score([(f"{count}", f"{holding}")], f"{nodes}", f"{length}", f"{average}", lambda it: f"log({it})")
        return eval(sql, {"__builtins__": {}, "log": math.log})

    # a1 holds the word twice in 32 characters, a2 once in 23, the average 67 / 3; two of the three hold it.
    assert score(1, 2, 3, 23, 67 / 3) / score(2, 2, 3, 32, 67 / 3) == pytest.approx(0.8059651474530831, abs=1e-12)
    # A term every node holds weighs ln(1 + 0.5 / (N + 0.5)): more than nothing, never less.
    assert score(1, 3, 3, 10, 10) > 0


def test_binds_each_term_on_its_own_on_oracle_once_for_its_count_and_once_more_for_whether_a_node_holds_it():
    statement = OracleBackend(OracleGraph()).matching(
        ModelRegistry().fragment(Shelf), MatchingSearch("Shelf", ("title",), 10, text="graph +data -noise")
    )
    sql = squashed(statement.text)
    query = TextQuery.parse("graph +data -noise")
    assert list(statement.bindings) == ["_term0", "_term1", "_term2", "_term3", "_term4", "_term5"]
    assert list(statement.bindings.values()) == [*query.scored, *query.required, *query.excluded, *query.scored]
    # The text is lowered to match a term in, and measured as it is.
    assert (
        "SELECT c.nid, LOWER(NVL(JSON_VALUE(c.props, '$.\"title\"' RETURNING CLOB), '')) AS text, "
        "NVL(LENGTH(NVL(JSON_VALUE(c.props, '$.\"title\"' RETURNING CLOB), '')), 0) AS len"
    ) in sql
    assert (
        "REGEXP_COUNT(s.text, $_term0) AS tf1, REGEXP_COUNT(s.text, $_term1) AS tf2, "
        "REGEXP_COUNT(s.text, $_term2) AS r1, REGEXP_COUNT(s.text, $_term3) AS x1"
    ) in sql
    assert "WHERE (REGEXP_LIKE(s.text, $_term4) OR REGEXP_LIKE(s.text, $_term5))" in sql
