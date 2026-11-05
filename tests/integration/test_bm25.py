"""
Full-text search scores by BM25 on every store (drivine4j#604): Lucene's, on Neo4j, and bm25_ranked on the SQL stores
without a text index of their own. A rare term outweighs a common one, a short text outranks a long one that holds a
term as often, and a term's count adds less each time it is counted again. Memgraph and FalkorDB score by their own
measures, and are left out.
"""

import json
import math
from pathlib import Path
from typing import Annotated

import pytest

from drivine import (
    DatabaseType,
    FullTextIndex,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    full_text_index,
)
from drivine.backend.sql.bm25 import K1, PREFIX, B


class Bm25Article(NodeFragment, labels=["Bm25Article"]):
    id: Annotated[str, NodeId()]
    body: Annotated[str | None, FullTextIndex()] = None


class Bm25Paper(NodeFragment, labels=["Bm25Paper"], schema=[FullTextIndex(["title", "body"])]):
    """A text searched over two properties, either of which a node may lack."""

    id: Annotated[str, NodeId()]
    title: str | None = None
    body: str | None = None


class RankArticle(NodeFragment, labels=["RankArticle"]):
    id: Annotated[str, NodeId()]
    title: str | None = None
    body: Annotated[str | None, FullTextIndex()] = None


# With the twenty common ones, "graph" is in most texts, so it says little of any; "soup" is in one.
ARTICLES = [
    *(Bm25Article(id=f"common{i + 1}", body="a note on a graph and its nodes") for i in range(20)),
    Bm25Article(id="many", body="graph graph graph graph and more of the graph"),
    Bm25Article(id="rare", body="a graph of soup"),
    Bm25Article(id="short", body="zebra crossing"),
    Bm25Article(
        id="long", body="zebra and a great many other words that make this text far longer than the one before it is"
    ),
    Bm25Article(id="once", body="kiwi bird"),
    Bm25Article(id="often", body="kiwi kiwi kiwi kiwi kiwi kiwi kiwi kiwi kiwi kiwi"),
    Bm25Article(id="none", body="nothing to see"),
]

# A missing property is no text, and its length none.
PAPERS = [
    Bm25Paper(id="untitled", body="kiwi pie"),
    Bm25Paper(id="unbodied", title="kiwi"),
    Bm25Paper(id="both", title="kiwi", body="kiwi kiwi"),
    Bm25Paper(id="other", title="nothing", body="to see"),
]


class Bm25Fruit(NodeFragment, labels=["Bm25Fruit"]):
    """drivine4j#612's texts, measured on Neo4j: a prefix that "rare" alone holds beside "melon"."""

    id: Annotated[str, NodeId()]
    body: Annotated[str | None, FullTextIndex()] = None


FRUITS = {
    "one": "kiwi",
    "three": "kiwi kiwis kiwifruit",
    "long": "a kiwi among a great many other words that make this text far longer than any of the others",
    "rare": "kiwano melon",
    "other": "nothing here",
}

SPECS = [
    full_text_index("Bm25Article", "body"),
    full_text_index("Bm25Paper", ["title", "body"]),
    full_text_index("Bm25Fruit", "body"),
]


async def clear(pm: PersistenceManager, *labels: str) -> None:
    if pm.type in (DatabaseType.ORACLE, DatabaseType.POSTGRES, DatabaseType.SQLITE):
        await pm.execute(QuerySpecification("DELETE FROM drivine_node"))
    else:
        await pm.execute(
            QuerySpecification(f"MATCH (n) WHERE {' OR '.join(f'n:{it}' for it in labels)} DETACH DELETE n")
        )


async def await_indexes(pm: PersistenceManager) -> None:
    if pm.type == DatabaseType.NEO4J:
        await pm.execute(QuerySpecification("CALL db.awaitIndexes(60)"))


@pytest.fixture
async def gom(cypher_pm: PersistenceManager) -> StatelessGraphObjectManager:
    """Over Neo4j through its index, and each SQL store without one: Postgres compares the two below."""
    gom = StatelessGraphObjectManager(cypher_pm)
    for spec in SPECS:
        await gom.indexes.drop(spec)
    await clear(cypher_pm, "Bm25Article", "Bm25Paper", "Bm25Fruit")
    await gom.save_all([*ARTICLES, *PAPERS, *(Bm25Fruit(id=id_, body=body) for id_, body in FRUITS.items())])
    if cypher_pm.type == DatabaseType.NEO4J:
        for spec in SPECS:
            await gom.indexes.ensure(spec)
        await await_indexes(cypher_pm)
    return gom


async def search(gom: StatelessGraphObjectManager, text: str) -> list[tuple[str, float]]:
    return [(it.value.id, it.score) for it in await gom.load_matching(Bm25Article, text, top_k=50)]


class TestBm25:
    async def test_a_rare_term_outweighs_a_common_one_counted_more_often(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        hits = await search(gom, "graph soup")
        assert hits[0][0] == "rare"
        assert hits[0][1] == pytest.approx(1, abs=1e-9)
        assert all(0 < score <= 1 for _, score in hits)
        if gom.persistence_manager.type != DatabaseType.NEO4J:
            # bm25_ranked's own figure, lengths in characters (Lucene's are in words): "graph" counted 3 times in 45
            # characters (back to back, the pattern counts every other one: drivine4j#607 moves this), against "graph"
            # and "soup" once each in 15, over 27 texts of 857 characters, "graph" in 22 and "soup" in 1.
            assert dict(hits)["many"] == pytest.approx(0.07864229315202613, abs=1e-9)

    async def test_a_short_text_outranks_a_long_one_that_holds_the_term_as_often(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        hits = await search(gom, "zebra")
        assert [id_ for id_, _ in hits] == ["short", "long"]
        # Counted alone, the two tied.
        assert hits[0][1] > hits[1][1]

    async def test_a_term_counted_again_adds_less_each_time(self, gom: StatelessGraphObjectManager) -> None:
        hits = dict(await search(gom, "kiwi"))
        assert hits["often"] == pytest.approx(1, abs=1e-9)
        # Ten times the count, as long again as the other's text: not ten times the score.
        assert hits["once"] > 0.4

    async def test_a_prefix_term_scores_every_node_that_holds_it_alike_as_lucenes_prefix_query_does(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        # Lucene scores a prefix query by a constant, not by how often or how rare (drivine4j#612).
        hits = await search(gom, "kiw*")
        assert {id_ for id_, _ in hits} == {"once", "often"}
        assert all(score == pytest.approx(1, abs=1e-9) for _, score in hits)

    async def test_a_prefix_term_beside_a_word_adds_the_same_to_each_node_that_holds_it(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        hits = await search(gom, "kiw* bird")
        assert [id_ for id_, _ in hits] == ["once", "often"]
        assert hits[1][1] < hits[0][1]

    async def test_a_prefix_term_beside_a_rare_word_weighs_as_lucenes_constant_beside_its_bm25_as_neo4j_measured(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        async def fruit(query: str) -> dict[str, float]:
            return {it.value.id: it.score for it in await gom.load_matching(Bm25Fruit, query, top_k=50)}

        # On Neo4j, every hit of a prefix alone scored 1 (drivine4j#612).
        prefix, whole = await fruit("kiw*"), await fruit("kiwi*")
        assert set(prefix) == {"one", "three", "long", "rare"}
        assert set(whole) == {"one", "three", "long"}
        assert all(score == pytest.approx(1, abs=1e-6) for score in [*prefix.values(), *whole.values()])
        # "melon" is in "rare" alone: its BM25 beside the prefix's constant, with the lengths the store measures: in
        # tokens on Lucene, in characters on the SQL stores.
        neo4j = gom.persistence_manager.type == DatabaseType.NEO4J
        lengths = {id_: len(body.split(" ")) if neo4j else len(body) for id_, body in FRUITS.items()}
        average = sum(lengths.values()) / len(lengths)
        idf = math.log(1 + (len(FRUITS) - 1 + 0.5) / (1 + 0.5))
        melon = idf * (K1 + 1) / (1 + K1 * (1 - B + B * lengths["rare"] / average))
        expected = PREFIX / (PREFIX + melon)
        for query in ["kiw* melon", "+kiw* melon"]:
            hits = await fruit(query)
            assert set(hits) == {"one", "three", "long", "rare"}, query
            assert hits["rare"] == pytest.approx(1, abs=1e-6), query
            assert all(hits[it] == pytest.approx(expected, abs=1e-4) for it in ("one", "three", "long")), query
        # Neo4j gave 0.5409; the SQL stores, measuring length in characters, about 0.549.
        assert expected == pytest.approx(0.5409, abs=0.01)

    async def test_a_common_term_still_finds_its_nodes_and_required_and_excluded_terms_keep_their_meaning(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        assert len(await search(gom, "graph")) == 22
        assert [id_ for id_, _ in await search(gom, "+soup graph")] == ["rare"]
        assert [id_ for id_, _ in await search(gom, "zebra -longer")] == ["short"]
        assert [id_ for id_, _ in await search(gom, "+zebra")] == ["short", "long"]

    async def test_a_node_that_lacks_one_of_several_searched_properties_is_measured_by_the_rest(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        hits = await gom.load_matching(Bm25Paper, "kiwi", top_k=50)
        assert {it.value.id for it in hits} == {"untitled", "unbodied", "both"}
        assert all(it.score > 0 for it in hits)

    async def test_narrowed_by_the_index_each_node_scores_as_it_does_without_one(
        self, gom: StatelessGraphObjectManager
    ) -> None:
        if gom.persistence_manager.type != DatabaseType.POSTGRES:
            pytest.skip("Only Postgres narrows a search by its index and scores by BM25 through it")
        queries = [
            "graph soup",
            "zebra",
            "kiwi",
            "graph",
            "+soup graph",
            '"zebra crossing" kiwi',
            "zebra -longer",
            '+"of soup" graph',
        ]
        bare = [dict(await search(gom, it)) for it in queries]
        await gom.indexes.ensure(SPECS[0])
        try:
            narrowed = [dict(await search(gom, it)) for it in queries]
        finally:
            await gom.indexes.drop(SPECS[0])
        for query, before, after in zip(queries, bare, narrowed, strict=True):
            assert sorted(after) == sorted(before), query
            for id_, score in before.items():
                assert after[id_] == pytest.approx(score, abs=1e-9), f"{query}: {id_}"


TOP = 5

CORPUS = json.loads(
    (Path(__file__).parents[1] / "integration" / "fulltext" / "docs-corpus.json").read_text(encoding="utf-8")
)
"""The sections of drivine4j's docs at 3a8b0b12, a section to a node, as its FullTextRankingAgreementTest reads them."""

QUERIES = [
    "GraphObjectManager",
    "agent process persistence",
    "Jinja",
    "full-text search",
    "vector index",
    "transaction rollback",
    "optimistic locking stamp",
    "schema marker",
    "cascade delete",
    "Cypher on a relational database",
    "polymorphic subtype",
    "connection provider",
    "Spring Boot configuration",
    "query DSL",
    "Memgraph",
    "pgvector",
    "keyset pagination",
    "property bag",
    "relationship direction",
    "Oracle Text",
    "test containers",
    "save all atomically",
]
"""Queries a reader of the docs might ask, from a rare exact term to a few common words; and #604's own."""

RANK_SPEC = full_text_index("RankArticle", "body")


async def ranked(pm: PersistenceManager, indexed: bool) -> dict[str, list[str]]:
    """Each query's top hits on the store, by id, best first, over the corpus written to it."""
    gom = StatelessGraphObjectManager(pm)
    await gom.indexes.drop(RANK_SPEC)
    await clear(pm, "RankArticle")
    await gom.save_all([RankArticle(**it) for it in CORPUS])
    if indexed:
        await gom.indexes.ensure(RANK_SPEC)
        await await_indexes(pm)
    try:
        return {
            query: [it.value.id for it in await gom.load_matching(RankArticle, query, top_k=TOP)] for query in QUERIES
        }
    finally:
        await gom.indexes.drop(RANK_SPEC)
        await clear(pm, "RankArticle")


@pytest.mark.parametrize("indexed", [False, True], ids=["unindexed", "indexed"])
async def test_a_sql_store_gives_the_top_hits_neo4j_gives_as_bm25_ranks_them(
    cypher_pm: PersistenceManager, factory: PersistenceManagerFactory, indexed: bool
) -> None:
    """
    How closely a SQL store's ranking agrees with Neo4j's, whose full-text index is Lucene's and scores by BM25. Scored
    by raw term counts, a store let common words outweigh the word a query is about, and long texts win by holding more
    of everything: 6 of 21 top hits were Neo4j's, and are 17 now.
    """
    if cypher_pm.type == DatabaseType.NEO4J:
        pytest.skip("Neo4j is the ranking the stores are held to")
    if indexed and cypher_pm.type != DatabaseType.POSTGRES:
        pytest.skip("Only Postgres searches through its index by BM25: Oracle Text scores by its own measure")
    reference = await ranked(factory.get("default"), indexed=True)
    hits = await ranked(cypher_pm, indexed)
    asked = [it for it in QUERIES if reference[it]]
    same_top = sum(1 for it in asked if hits[it][:1] == reference[it][:1])
    overlap = sum(len(set(hits[it]) & set(reference[it])) for it in asked) / len(asked)
    print(
        f"{cypher_pm.type}{' indexed' if indexed else ''}: same top hit {same_top}/{len(asked)}, "
        f"mean overlap of the top {TOP} {overlap:.2f}"
    )
    assert same_top >= len(asked) * 3 // 4
    assert overlap >= 3.5
