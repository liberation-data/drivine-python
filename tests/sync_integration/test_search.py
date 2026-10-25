# Written by scripts/unasync.py from tests/integration/test_search.py:
# change that, and run the script again.
from typing import Annotated, Any

import pytest

from drivine.backend.matching_search import FILTERED_OVER_FETCH
from drivine.sync import (
    DatabaseType,
    DrivineError,
    FullTextIndex,
    GraphView,
    NodeFragment,
    NodeId,
    NodeStamp,
    PersistenceManager,
    QuerySpecification,
    Relationship,
    Root,
    SchemaCatalog,
    StatelessGraphObjectManager,
    VectorIndex,
    refs,
    vector_index,
)
from tests.sync_integration.stores import equals


class Proposition(NodeFragment, labels=["NearestTest", "Proposition"]):
    id: Annotated[str, NodeId()]
    text: str | None = None
    context_id: str | None = None
    embedding: Annotated[list[float] | None, VectorIndex(dimensions=3)] = None
    stamp: Annotated[str | None, NodeStamp()] = None


class Point(NodeFragment, labels=["NearestTest", "Point"]):
    id: Annotated[str, NodeId()]
    position: Annotated[list[float] | None, VectorIndex("EUCLIDEAN", dimensions=2)] = None


class Entity(NodeFragment, labels=["NearestTest", "Entity"]):
    id: Annotated[str, NodeId()]


class PropositionView(GraphView):
    proposition: Annotated[Proposition, Root()]
    mentions: Annotated[list[Entity], Relationship("MENTIONS", required=True)]


class Chunk(NodeFragment, labels=["MatchTest", "Chunk"]):
    id: Annotated[str, NodeId()]
    body: Annotated[str | None, FullTextIndex()] = None
    section_id: str | None = None


class Article(NodeFragment, labels=["MatchArticle"], schema=[FullTextIndex(["title", "summary"])]):
    id: Annotated[str, NodeId()]
    title: str | None = None
    summary: str | None = None


class Section(NodeFragment, labels=["MatchTest", "Section"]):
    id: Annotated[str, NodeId()]


class ChunkInSection(GraphView):
    chunk: Annotated[Chunk, Root()]
    section: Annotated[Section, Relationship("IN")]


def proposition(id: str, embedding: list[float], context_id: str = "a") -> Proposition:
    return Proposition(id=id, text=id, context_id=context_id, embedding=embedding)


def chunk(id: str, body: str, section_id: str = "s1") -> Chunk:
    return Chunk(id=id, body=body, section_id=section_id)


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


def ensure_indexes(gom: StatelessGraphObjectManager, *types: type) -> None:
    """Makes the indexes the fragments declare, unless they are there: they outlive each test's data."""
    for spec in SchemaCatalog.from_fragments(*types).items:
        gom.indexes.ensure(spec)  # pyright: ignore[reportArgumentType]
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))


@pytest.fixture
def propositions(gom: StatelessGraphObjectManager) -> None:
    if gom.persistence_manager.type == DatabaseType.MEMGRAPH:
        # Memgraph's vector search returns nodes deleted since its last garbage collection, and fails on them.
        gom.persistence_manager.execute(QuerySpecification("FREE MEMORY"))
    ensure_indexes(gom, Proposition, Point)
    gom.save_all(
        [
            proposition("north", [1, 0, 0]),
            proposition("north-east", [0.8, 0.6, 0]),
            proposition("east", [0, 1, 0], "b"),
            proposition("up", [0, 0, 1], "b"),
            Point(id="origin", position=[0, 0]),
            Point(id="far", position=[3, 4]),
        ]
    )
    gom.save(PropositionView(proposition=proposition("east", [0, 1, 0], "b"), mentions=[Entity(id="e1")]))


@pytest.fixture
def chunks(gom: StatelessGraphObjectManager) -> None:
    ensure_indexes(gom, Chunk, Article)
    gom.save_all(
        [
            chunk("c1", "graph databases store relationships natively"),
            chunk("c2", "graph databases and graph queries", "s2"),
            chunk("c3", "relational tables"),
            Article(id="a1", title="Moon Girl", summary="a genius inventor"),
            Article(id="a2", title="Devil", summary="a dinosaur"),
        ]
    )
    gom.save(
        ChunkInSection(chunk=chunk("c1", "graph databases store relationships natively"), section=Section(id="s1"))
    )


@pytest.mark.usefixtures("propositions")
class TestLoadNearest:
    def test_stores_embeddings_the_index_finds_and_loads_them_back(self, gom):
        loaded = gom.load(Proposition, "north-east")
        assert [round(it, 1) for it in loaded.embedding] == [0.8, 0.6, 0]

    def test_ranks_the_nearest_first_scored_as_one_similarity_on_every_engine(self, gom):
        hits = gom.load_nearest(Proposition, [1, 0, 0], top_k=2)
        assert [it.value.id for it in hits] == ["north", "north-east"]
        assert isinstance(hits[0].value, Proposition)
        assert hits[0].score == pytest.approx(1, abs=0.01)
        assert hits[1].score == pytest.approx((1 + 0.8) / 2, abs=0.01)

    def test_scores_euclidean_similarity_as_one_over_one_plus_d_squared(self, gom):
        hits = gom.load_nearest(Point, [0, 0], top_k=2)
        assert [it.value.id for it in hits] == ["origin", "far"]
        assert hits[0].score == pytest.approx(1, abs=0.01)
        assert hits[1].score == pytest.approx(1 / 26, abs=0.01)

    def test_filters_by_threshold_and_by_where_with_a_wider_search(self, pm, gom):
        close = gom.load_nearest(Proposition, [1, 0, 0], top_k=4, threshold=0.85)
        assert [it.value.id for it in close] == ["north", "north-east"]
        # east and up are both in context b; the query leans towards east, so they don't tie.
        scoped = gom.load_nearest(
            Proposition,
            [1, 0.1, 0],
            top_k=1,
            search_k=4,
            where=equals(pm, "contextId", "context"),
            bindings={"context": "b"},
        )
        assert [it.value.id for it in scoped] == ["east"]
        typed = gom.load_nearest(
            Proposition, [1, 0.1, 0], top_k=1, search_k=4, where=refs(Proposition).context_id.eq("b")
        )
        assert [it.value.id for it in typed] == ["east"]
        with pytest.raises(DrivineError, match="search_k must be"):
            gom.load_nearest(Proposition, [1, 0, 0], top_k=3, search_k=2)

    def test_searches_a_view_by_its_root_keeping_roots_with_its_required_relationships(self, gom):
        hits = gom.load_nearest(PropositionView, [1, 0, 0], top_k=4)
        assert [it.value.proposition.id for it in hits] == ["east"]
        assert [it.id for it in hits[0].value.mentions] == ["e1"]

    def test_keeps_the_stamp_when_a_vector_is_saved_unchanged(self, gom):
        loaded = gom.load(Proposition, "north")
        gom.save(loaded)
        assert (gom.load(Proposition, "north")).stamp == loaded.stamp

    def test_names_the_field_to_search(self, gom):
        with pytest.raises(DrivineError, match="declares no VectorIndex field missing"):
            gom.load_nearest(Proposition, [1, 0, 0], top_k=1, property="missing")
        with pytest.raises(DrivineError, match="top_k must be"):
            gom.load_nearest(Proposition, [1, 0, 0], top_k=0)


@pytest.mark.usefixtures("chunks")
class TestLoadMatching:
    def test_finds_matches_by_a_fields_index_scaled_so_the_best_hit_is_1(self, gom):
        hits = gom.load_matching(Chunk, "graph", top_k=10)
        assert sorted(it.value.id for it in hits) == ["c1", "c2"]
        assert hits[0].score == pytest.approx(1)
        assert all(0 < it.score <= 1 for it in hits)
        assert isinstance(hits[0].value, Chunk)
        assert [it.score for it in hits] == sorted((it.score for it in hits), reverse=True)

    def test_searches_a_class_level_index_over_several_properties(self, gom):
        hits = gom.load_matching(Article, "inventor", top_k=5)
        assert [it.value.id for it in hits] == ["a1"]

    def test_limits_to_top_k_and_filters_by_threshold_and_where(self, pm, gom):
        assert len(gom.load_matching(Chunk, "graph", top_k=1)) == 1
        scoped = gom.load_matching(Chunk, "graph", top_k=10, where=equals(pm, "sectionId", "s"), bindings={"s": "s2"})
        assert [it.value.id for it in scoped] == ["c2"]
        typed = gom.load_matching(Chunk, "graph", top_k=10, where=refs(Chunk).section_id.eq("s2"))
        assert [it.value.id for it in typed] == ["c2"]
        assert gom.load_matching(Chunk, "graph", top_k=10, threshold=1.01) == []

    def test_searches_a_view_by_its_root_keeping_roots_with_its_required_relationships(self, gom):
        hits = gom.load_matching(ChunkInSection, "graph", top_k=10)
        assert [it.value.chunk.id for it in hits] == ["c1"]
        assert hits[0].value.section.id == "s1"

    def test_needs_an_index_to_search(self, gom):
        with pytest.raises(DrivineError, match="declares no FullTextIndex"):
            gom.load_matching(Section, "graph", top_k=1)


class Story(NodeFragment, labels=["MatchStory"]):
    """One label, the index's own, so nothing but a caller's where removes a hit after the index."""

    id: Annotated[str, NodeId()]
    title: str | None = None
    body: Annotated[str | None, FullTextIndex()] = None


class ReviewedStory(NodeFragment, labels=["MatchStory", "MatchReviewed"]):
    """Searched by Story's index, over MatchStory, so a hit without the second label is dropped after it."""

    id: Annotated[str, NodeId()]
    title: str | None = None
    body: Annotated[str | None, FullTextIndex()] = None


def ensure_ranked_index(gom: StatelessGraphObjectManager) -> None:
    """
    Story's full-text index, as ensure_indexes makes it. On FalkorDB it is made again: once nodes it matched were
    deleted, as each test's data is, every score it gives is 0 and its hits come in no order (FalkorDB/FalkorDB#3489).
    """
    if gom.persistence_manager.type != DatabaseType.FALKORDB:
        ensure_indexes(gom, Story)
        return
    for spec in SchemaCatalog.from_fragments(Story).items:
        gom.indexes.recreate(spec)  # pyright: ignore[reportArgumentType]


LARGEST_TOP_K = 2**63 - 1
"""The largest top_k an engine takes: a 64-bit integer."""


@pytest.fixture
def ranked_stories(gom: StatelessGraphObjectManager) -> None:
    """
    Twelve stories match "zebra", each longer than the last so each scores below the last; the even ones are titled
    "keep". Saved worst first, so the order they are stored in is not the order they rank in.
    """
    ensure_ranked_index(gom)
    gom.save_all(
        [
            Story(id=f"z{i}", title="keep" if i % 2 == 0 else "drop", body="zebra" + " grass" * i)
            for i in range(12, 0, -1)
        ]
    )
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))


@pytest.mark.usefixtures("ranked_stories")
class TestLoadMatchingBound:
    def test_a_filter_keeps_the_best_hits_it_passes_not_the_first_it_is_given(self, gom):
        # The limit after the filter takes no sort of its own, so the head must hand it the hits in score order.
        ranked = gom.load_matching(Story, "zebra", top_k=20)
        assert len(ranked) == 12
        best_kept = [it.score for it in ranked if it.value.title == "keep"][:2]
        kept = gom.load_matching(Story, "zebra", top_k=2, where=refs(Story).title.eq("keep"))
        assert [it.score for it in kept] == pytest.approx(best_kept)

    def test_a_cut_that_bites_keeps_the_best_hits_not_the_first_the_index_yields(self, gom):
        # FalkorDB's call takes no limit, and the stories were stored worst first.
        ranked = gom.load_matching(Story, "zebra", top_k=20)
        top = gom.load_matching(Story, "zebra", top_k=3)
        assert [it.value.id for it in top] == [it.value.id for it in ranked[:3]]

    def test_bounds_the_index_by_top_k_at_its_edges(self, gom):
        with pytest.raises(DrivineError, match="top_k must be a whole number of at least 1"):
            gom.load_matching(Story, "zebra", top_k=0)
        assert len(gom.load_matching(Story, "zebra", top_k=LARGEST_TOP_K)) == 12
        kept = gom.load_matching(Story, "zebra", top_k=LARGEST_TOP_K, where=refs(Story).title.eq("keep"))
        assert len(kept) == 6

    @pytest.mark.graph_engine_only("the second label is set by a Cypher statement")
    def test_reaches_past_the_best_hit_for_a_fragment_whose_second_label_it_lacks(self, gom):
        # z1 ranks best and isn't MatchReviewed: an index asked for top_k alone yields it, and the label check drops it.
        gom.persistence_manager.execute(QuerySpecification("MATCH (z:MatchStory {id: 'z2'}) SET z:MatchReviewed"))
        reviewed = gom.load_matching(ReviewedStory, "zebra", top_k=1)
        assert [it.value.id for it in reviewed] == ["z2"]
        # z1 is dropped before the hits are scaled, so z2 is the best of the scale, as on a store that narrows by labels
        # first, and a threshold just under 1 keeps it.
        assert reviewed[0].score == pytest.approx(1.0)
        kept = gom.load_matching(ReviewedStory, "zebra", top_k=5, threshold=0.99)
        assert [it.value.id for it in kept] == ["z2"]


@pytest.fixture
def reviewed_and_draft(gom: StatelessGraphObjectManager) -> None:
    """
    A draft that is only a Story outranks a ReviewedStory on every scorer: it holds the word three times to the
    other's once. Both are in Story's index, over MatchStory.
    """
    ensure_ranked_index(gom)
    gom.save_all([Story(id="draft", body="zebra zebra zebra")])
    gom.save_all([ReviewedStory(id="reviewed", body="zebra")])
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))


@pytest.mark.usefixtures("reviewed_and_draft")
class TestLoadMatchingScale:
    def test_scales_by_the_best_hit_with_every_root_label_on_every_store(self, gom):
        # The draft is no ReviewedStory, so it is dropped before the hits are scaled: the reviewed story scores 1, as
        # the best hit the search can return, on the graph engines and the SQL stores alike.
        reviewed = gom.load_matching(ReviewedStory, "zebra", top_k=5)
        assert [it.value.id for it in reviewed] == ["reviewed"]
        assert reviewed[0].score == pytest.approx(1.0)
        kept = gom.load_matching(ReviewedStory, "zebra", top_k=5, threshold=0.99)
        assert [it.value.id for it in kept] == ["reviewed"]


LAST = "z-last"
"""A story far longer than the others, so the index ranks it last of them."""


@pytest.fixture
def one_long_story(gom: StatelessGraphObjectManager) -> None:
    """Thirty stories match "zebra": twenty-nine short ones, and LAST."""
    ensure_ranked_index(gom)
    gom.save_all(
        [
            *(Story(id=f"z{i}", title="short", body="zebra " + "grass " * i) for i in range(1, 30)),
            Story(id=LAST, title="long", body="zebra " + "savannah " * 400),
        ]
    )
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))


@pytest.mark.usefixtures("one_long_story")
@pytest.mark.graph_engine_only("a SQL store has no index call to bound")
class TestLoadMatchingIndexLimit:
    """
    The full-text index yields no more hits than the search's index limit: observed through the rows, not read off the
    statement.
    """

    def only_the_last(self, gom: StatelessGraphObjectManager, top_k: int) -> list[str]:
        hits = gom.load_matching(Story, "zebra", top_k=top_k, where=refs(Story).id.eq(LAST))
        return [it.value.id for it in hits]

    def test_a_filtered_search_sees_only_the_best_top_k_times_the_over_fetch(self, gom):
        assert FILTERED_OVER_FETCH < 29
        assert self.only_the_last(gom, 1) == []

    def test_a_filtered_search_whose_over_fetch_reaches_the_hit_finds_it(self, gom):
        assert self.only_the_last(gom, 3) == [LAST]

    def test_an_unfiltered_search_returns_the_best_top_k_in_score_order(self, gom):
        found = gom.load_matching(Story, "zebra", top_k=5)
        assert len(found) == 5
        assert found[0].score == pytest.approx(1)
        assert [it.score for it in found] == sorted((it.score for it in found), reverse=True)
        assert LAST not in [it.value.id for it in found]


MANY = 1_100
"""More hits than Memgraph's text_search yields when its call is given no limit."""


@pytest.mark.graph_engine_only("a SQL store has no index call to bound")
def test_a_search_asking_for_more_than_a_thousand_hits_gets_them(gom: StatelessGraphObjectManager):
    ensure_indexes(gom, Story)
    gom.save_all([Story(id=f"z{i}", title="t", body=f"zebra {i}") for i in range(MANY)])
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))
    assert len(gom.load_matching(Story, "zebra", top_k=MANY)) == MANY


class SizedDoc(NodeFragment, labels=["SizedTest"]):
    """A document whose embedding has 4 dimensions, as the index made for it has, but for W's 3."""

    id: Annotated[str, NodeId()]
    title: str | None = None
    embedding: Annotated[list[float] | None, VectorIndex(dimensions=4)] = None


class PartitionedDoc(SizedDoc, labels=["SizedCorpus"]):
    """A document of a partition no index is made for, so a search partitioned by it measures every node, on SQL."""


class SizedAuthor(NodeFragment, labels=["SizedTest", "SizedAuthor"]):
    id: Annotated[str, NodeId()]


class SizedDocView(GraphView):
    doc: Annotated[SizedDoc, Root()]
    authors: Annotated[list[SizedAuthor], Relationship("WRITTEN_BY", required=True)]


SIZED_QUERY = [1.0, 0.0, 0.0, 0.0]
"""The vector searched by: A's own embedding, of the index's 4 dimensions."""

MIS_SIZED_QUERY = [1.0, 0.0, 0.0]
"""W's own embedding, of 3 dimensions."""

SIZED_WIDE = 10
"""Wide enough that every store, approximate or not, sees every node."""


@pytest.fixture(params=[True, False], ids=["indexed", "unindexed"])
def sized_docs(request: pytest.FixtureRequest, gom: StatelessGraphObjectManager) -> bool:
    """
    The documents, with the index made for them, and on a SQL store also without it, where it measures every node.
    Memgraph refuses to write an embedding of another size than its index's, so it has no such node to leave out.
    """
    indexed: bool = request.param
    engine = gom.persistence_manager.type
    if engine == DatabaseType.MEMGRAPH:
        pytest.skip("Memgraph refuses to store an embedding of another size than its index's.")
    if not indexed and engine not in (DatabaseType.ORACLE, DatabaseType.POSTGRES):
        pytest.skip("A graph engine searches only through an index.")
    spec = vector_index("SizedTest", "embedding", 4)
    gom.indexes.drop(spec)
    if indexed:
        gom.indexes.ensure(spec)
    author = SizedAuthor(id="au1")

    def written(doc: SizedDoc) -> SizedDocView:
        return SizedDocView(doc=doc, authors=[author])

    gom.save_all(
        [
            written(PartitionedDoc(id="A", title="Alpha", embedding=SIZED_QUERY)),
            written(SizedDoc(id="B", title="Beta", embedding=[0.8, 0.6, 0, 0])),
            written(SizedDoc(id="C", title="Gamma", embedding=[0.6, 0, 0.8, 0])),
            written(PartitionedDoc(id="W", title="Wrong size", embedding=MIS_SIZED_QUERY)),
        ]
    )
    gom.save(SizedDoc(id="D", title="Delta", embedding=[0, 0, 0, 1]))
    if engine == DatabaseType.NEO4J:
        gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))
    return indexed


def _ids(hits: list[Any]) -> list[str]:
    return [it.value.id for it in hits]


class TestLoadNearestOfAnotherSize:
    """
    W, whose embedding has 3 dimensions where the others and the index have 4, is left out of a search by a vector of
    4 on every store rather than failing it: Oracle refuses to measure two sizes against each other (ORA-51808). W has
    an author, so that a view leaves it out for its size and not for a missing relationship. W and A are in a
    partition, so that a search partitioned by it has one node of each size to measure. Ported from drivine4j's
    NearestAmongCrossEngineTest.
    """

    def test_leaves_out_a_node_whose_embedding_is_of_another_size_plain_filtered_or_as_a_view(self, gom, sized_docs):
        wide = {"top_k": SIZED_WIDE, "search_k": SIZED_WIDE}
        assert _ids(gom.load_nearest(SizedDoc, SIZED_QUERY, **wide)) == ["A", "B", "C", "D"]
        filtered = gom.load_nearest(SizedDoc, SIZED_QUERY, **wide, where=refs(SizedDoc).title.neq("Alpha"))
        assert _ids(filtered) == ["B", "C", "D"]
        # W has an author, as A, B and C do; D has none, so a view leaves it out.
        viewed = gom.load_nearest(SizedDocView, SIZED_QUERY, **wide)
        assert [it.value.doc.id for it in viewed] == ["A", "B", "C"]

    def test_finds_only_the_nodes_of_a_vectors_size_when_the_index_is_of_another_or_refuses_it(self, gom, sized_docs):
        wide = {"top_k": SIZED_WIDE, "search_k": SIZED_WIDE}
        if gom.persistence_manager.type not in (DatabaseType.ORACLE, DatabaseType.POSTGRES):
            # A graph engine searches only through its index, which refuses a vector of another size.
            with pytest.raises(Exception):  # noqa: B017
                gom.load_nearest(SizedDoc, MIS_SIZED_QUERY, **wide)
            return
        # A SQL store with an index of another size measures every node instead, as one with none does.
        assert _ids(gom.load_nearest(SizedDoc, MIS_SIZED_QUERY, **wide)) == ["W"]

    def test_leaves_out_a_node_whose_embedding_is_of_another_size_from_a_search_partitioned_with_no_index(
        self, gom, sized_docs
    ):
        if gom.persistence_manager.type not in (DatabaseType.ORACLE, DatabaseType.POSTGRES):
            pytest.skip("A graph engine searches a partition only through an index made for it.")
        found = gom.load_nearest(SizedDoc, SIZED_QUERY, top_k=SIZED_WIDE, partition_label="SizedCorpus")
        assert _ids(found) == ["A"]
