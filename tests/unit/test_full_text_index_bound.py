"""
A full-text search is bounded where the index yields, not only where the rows are returned.

The head collects every hit the index call yields to scale the scores, and every row after it carries that collection,
so the number of hits, not `top_k`, is what a search costs. An ordinary English query matches most of a corpus:
unbounded, one `k = 8` search over ~8,500 chapters allocated 1.17 GB on Neo4j, and two at once exhausted its
transaction memory pool. As drivine4j's FullTextIndexBoundTest.
"""

import re
from typing import Annotated, Any

import pytest

from drivine import (
    Direction,
    FullTextIndex,
    GraphPath,
    GraphView,
    ModelRegistry,
    NodeFragment,
    NodeId,
    Relationship,
    Root,
    hop,
)
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from drivine.backend.matching_search import FILTERED_OVER_FETCH, INDEX_LIMIT_CEILING, MatchingSearch, index_limit
from drivine.model.graph_view_model import GraphViewModel


class Article(NodeFragment, labels=["Article"]):
    id: Annotated[str, NodeId()]
    body: Annotated[str | None, FullTextIndex()] = None


class ReviewedArticle(NodeFragment, labels=["Article", "Reviewed"]):
    """A fragment of two labels: the index is over the first, so a hit without the second is dropped after it."""

    id: Annotated[str, NodeId()]
    body: Annotated[str | None, FullTextIndex()] = None


class Author(NodeFragment, labels=["Author"]):
    id: Annotated[str, NodeId()]


class ArticleView(GraphView):
    """A view whose relationships are optional, so they remove no hit."""

    article: Annotated[Article, Root()]
    authors: Annotated[list[Author], Relationship("WROTE", Direction.INCOMING)] = []
    editor: Annotated[Author | None, Relationship("EDITED", Direction.INCOMING)] = None


class AuthoredArticleView(GraphView):
    """A view with a required relationship: its check removes the hits that have no author."""

    article: Annotated[Article, Root()]
    author: Annotated[Author, Relationship("WROTE", Direction.INCOMING)]


ENGINES = [NEO4J_GRAMMAR, MEMGRAPH_GRAMMAR, FALKORDB_GRAMMAR]


def search(
    type_: type[Any],
    limit: int = 8,
    where: str | None = None,
    grammar: CypherGrammar = NEO4J_GRAMMAR,
    threshold: bool = False,
) -> str:
    registry = ModelRegistry()
    target = registry.view(type_) if issubclass(type_, GraphView) else registry.fragment(type_)
    root = target.root if isinstance(target, GraphViewModel) else target
    return (
        CypherBackend(grammar).matching(target, MatchingSearch(root.labels[0], ("body",), limit, where, threshold)).text
    )


def cut(text: str) -> int:
    """How many hits the head keeps: the limit right after its sort."""
    found = re.search(r"\nWITH node, score\nORDER BY score DESC\nLIMIT (\d+)\n", text)
    assert found, text
    return int(found.group(1))


def test_the_neo4j_index_call_is_asked_for_a_limit():
    assert (
        "CALL db.index.fulltext.queryNodes('Article_body_fulltext', $_text, {limit: 80}) YIELD node, score"
        in search(Article, where="n.id = $id")
    )


def test_the_memgraph_index_call_is_asked_for_a_limit_no_larger_than_it_survives():
    # Without a limit text_search yields 1,000 hits; with one near 500,000,000 Memgraph aborts (memgraph/memgraph#5200).
    assert "CALL text_search.search_all('Article_body_fulltext', $_text, {limit: 1100}) YIELD node, score" in search(
        Article, 1100, grammar=MEMGRAPH_GRAMMAR
    )
    capped = search(Article, 2_000_000, grammar=MEMGRAPH_GRAMMAR)
    assert "{limit: 1000000}) YIELD node, score" in capped
    assert cut(capped) == 2_000_000


def test_the_falkordb_index_call_takes_no_limit():
    assert "CALL db.idx.fulltext.queryNodes('Article', $_text) YIELD node, score" in search(
        Article, grammar=FALKORDB_GRAMMAR
    )


@pytest.mark.parametrize("grammar", ENGINES, ids=lambda it: it.engine)
def test_every_engine_cuts_the_hits_best_first_before_collecting_them(grammar: CypherGrammar):
    text = search(Article, grammar=grammar)
    head = text.find("YIELD node, score\nWITH node, score\nORDER BY score DESC\nLIMIT 8\n")

    assert 0 <= head < text.index("collect("), text


def test_an_unfiltered_fragment_search_asks_the_index_for_exactly_its_limit():
    # Nothing removes a hit after the index, and a threshold keeps a prefix of the ranking: the best 8 are the answer.
    assert cut(search(Article, threshold=True)) == 8


def test_a_filtered_search_over_fetches_by_the_stated_factor():
    text = search(Article, where="n.id = $id")

    assert cut(text) == 8 * FILTERED_OVER_FETCH
    assert "\nLIMIT 8\n" in text


def test_a_view_without_a_required_relationship_asks_the_index_for_exactly_its_limit():
    assert cut(search(ArticleView)) == 8


def test_a_view_over_fetches_for_its_required_relationship_alone():
    assert cut(search(AuthoredArticleView)) == 8 * FILTERED_OVER_FETCH


def test_the_index_limit_is_capped_however_large_the_limit():
    assert index_limit(INDEX_LIMIT_CEILING, True) == INDEX_LIMIT_CEILING
    assert index_limit(2**63 - 1, False) == INDEX_LIMIT_CEILING
    assert index_limit(1000, True) == 1000 * FILTERED_OVER_FETCH


@pytest.mark.parametrize("grammar", ENGINES, ids=lambda it: it.engine)
def test_a_fragment_is_limited_before_it_is_projected_without_a_second_sort(grammar: CypherGrammar):
    # A node's properties, a chunk's text and its embedding among them, are read only for the rows returned. A sort
    # between the filter and the limit would hold every row that passes, each carrying the head's collected hits.
    text = search(Article, where="n.id = $id", grammar=grammar)
    after_head = text.split("UNWIND _hits AS _hit", 1)[1]

    assert "\nWITH n, _score\nLIMIT 8\n" in after_head, text
    assert after_head.index("LIMIT 8") < after_head.index("properties(n)"), text
    assert after_head.count("ORDER BY") == 1, text
    assert after_head.rstrip().endswith("ORDER BY result.score DESC"), text


def test_a_view_is_limited_after_it_is_projected():
    text = search(AuthoredArticleView)

    assert "WITH n, _score" not in text
    assert text.endswith("ORDER BY result.score DESC\nLIMIT 8")


class EditedArticleView(GraphView):
    """A view with a required path: its check removes the hits that have no editor's author at its far end."""

    article: Annotated[Article, Root()]
    editor: Annotated[Author, GraphPath([hop("EDITED_BY", Direction.OUTGOING, "Author"), hop("WROTE")])]


def test_a_view_over_fetches_for_its_required_path_alone():
    assert cut(search(EditedArticleView)) == 8 * FILTERED_OVER_FETCH


def test_a_view_without_a_required_relationship_over_fetches_for_a_where():
    assert cut(search(ArticleView, where="n.id = $id")) == 8 * FILTERED_OVER_FETCH


def test_a_fragment_over_fetches_for_its_labels_beyond_the_first():
    # The index is over Article; the label check after it drops the hits that aren't Reviewed.
    text = search(ReviewedArticle)

    assert cut(text) == 8 * FILTERED_OVER_FETCH
    assert "MATCH (n) WHERE n:Article:Reviewed" in text, text
