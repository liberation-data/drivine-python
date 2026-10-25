from typing import Annotated

import pytest

from drivine import (
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
)
from drivine.backend.matching_search import FILTERED_OVER_FETCH
from tests.integration.stores import equals


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


async def ensure_indexes(gom: StatelessGraphObjectManager, *types: type) -> None:
    """Makes the indexes the fragments declare, unless they are there: they outlive each test's data."""
    for spec in SchemaCatalog.from_fragments(*types).items:
        await gom.indexes.ensure(spec)  # pyright: ignore[reportArgumentType]
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        await gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))


@pytest.fixture
async def propositions(gom: StatelessGraphObjectManager) -> None:
    if gom.persistence_manager.type == DatabaseType.MEMGRAPH:
        # Memgraph's vector search returns nodes deleted since its last garbage collection, and fails on them.
        await gom.persistence_manager.execute(QuerySpecification("FREE MEMORY"))
    await ensure_indexes(gom, Proposition, Point)
    await gom.save_all(
        [
            proposition("north", [1, 0, 0]),
            proposition("north-east", [0.8, 0.6, 0]),
            proposition("east", [0, 1, 0], "b"),
            proposition("up", [0, 0, 1], "b"),
            Point(id="origin", position=[0, 0]),
            Point(id="far", position=[3, 4]),
        ]
    )
    await gom.save(PropositionView(proposition=proposition("east", [0, 1, 0], "b"), mentions=[Entity(id="e1")]))


@pytest.fixture
async def chunks(gom: StatelessGraphObjectManager) -> None:
    await ensure_indexes(gom, Chunk, Article)
    await gom.save_all(
        [
            chunk("c1", "graph databases store relationships natively"),
            chunk("c2", "graph databases and graph queries", "s2"),
            chunk("c3", "relational tables"),
            Article(id="a1", title="Moon Girl", summary="a genius inventor"),
            Article(id="a2", title="Devil", summary="a dinosaur"),
        ]
    )
    await gom.save(
        ChunkInSection(chunk=chunk("c1", "graph databases store relationships natively"), section=Section(id="s1"))
    )


@pytest.mark.usefixtures("propositions")
class TestLoadNearest:
    async def test_stores_embeddings_the_index_finds_and_loads_them_back(self, gom):
        loaded = await gom.load(Proposition, "north-east")
        assert [round(it, 1) for it in loaded.embedding] == [0.8, 0.6, 0]

    async def test_ranks_the_nearest_first_scored_as_one_similarity_on_every_engine(self, gom):
        hits = await gom.load_nearest(Proposition, [1, 0, 0], top_k=2)
        assert [it.value.id for it in hits] == ["north", "north-east"]
        assert isinstance(hits[0].value, Proposition)
        assert hits[0].score == pytest.approx(1, abs=0.01)
        assert hits[1].score == pytest.approx((1 + 0.8) / 2, abs=0.01)

    async def test_scores_euclidean_similarity_as_one_over_one_plus_d_squared(self, gom):
        hits = await gom.load_nearest(Point, [0, 0], top_k=2)
        assert [it.value.id for it in hits] == ["origin", "far"]
        assert hits[0].score == pytest.approx(1, abs=0.01)
        assert hits[1].score == pytest.approx(1 / 26, abs=0.01)

    async def test_filters_by_threshold_and_by_where_with_a_wider_search(self, pm, gom):
        close = await gom.load_nearest(Proposition, [1, 0, 0], top_k=4, threshold=0.85)
        assert [it.value.id for it in close] == ["north", "north-east"]
        # east and up are both in context b; the query leans towards east, so they don't tie.
        scoped = await gom.load_nearest(
            Proposition,
            [1, 0.1, 0],
            top_k=1,
            search_k=4,
            where=equals(pm, "contextId", "context"),
            bindings={"context": "b"},
        )
        assert [it.value.id for it in scoped] == ["east"]
        typed = await gom.load_nearest(
            Proposition, [1, 0.1, 0], top_k=1, search_k=4, where=refs(Proposition).context_id.eq("b")
        )
        assert [it.value.id for it in typed] == ["east"]
        with pytest.raises(DrivineError, match="search_k must be"):
            await gom.load_nearest(Proposition, [1, 0, 0], top_k=3, search_k=2)

    async def test_searches_a_view_by_its_root_keeping_roots_with_its_required_relationships(self, gom):
        hits = await gom.load_nearest(PropositionView, [1, 0, 0], top_k=4)
        assert [it.value.proposition.id for it in hits] == ["east"]
        assert [it.id for it in hits[0].value.mentions] == ["e1"]

    async def test_keeps_the_stamp_when_a_vector_is_saved_unchanged(self, gom):
        loaded = await gom.load(Proposition, "north")
        await gom.save(loaded)
        assert (await gom.load(Proposition, "north")).stamp == loaded.stamp

    async def test_names_the_field_to_search(self, gom):
        with pytest.raises(DrivineError, match="declares no VectorIndex field missing"):
            await gom.load_nearest(Proposition, [1, 0, 0], top_k=1, property="missing")
        with pytest.raises(DrivineError, match="top_k must be"):
            await gom.load_nearest(Proposition, [1, 0, 0], top_k=0)


@pytest.mark.usefixtures("chunks")
class TestLoadMatching:
    async def test_finds_matches_by_a_fields_index_scaled_so_the_best_hit_is_1(self, gom):
        hits = await gom.load_matching(Chunk, "graph", top_k=10)
        assert sorted(it.value.id for it in hits) == ["c1", "c2"]
        assert hits[0].score == pytest.approx(1)
        assert all(0 < it.score <= 1 for it in hits)
        assert isinstance(hits[0].value, Chunk)
        assert [it.score for it in hits] == sorted((it.score for it in hits), reverse=True)

    async def test_searches_a_class_level_index_over_several_properties(self, gom):
        hits = await gom.load_matching(Article, "inventor", top_k=5)
        assert [it.value.id for it in hits] == ["a1"]

    async def test_limits_to_top_k_and_filters_by_threshold_and_where(self, pm, gom):
        assert len(await gom.load_matching(Chunk, "graph", top_k=1)) == 1
        scoped = await gom.load_matching(
            Chunk, "graph", top_k=10, where=equals(pm, "sectionId", "s"), bindings={"s": "s2"}
        )
        assert [it.value.id for it in scoped] == ["c2"]
        typed = await gom.load_matching(Chunk, "graph", top_k=10, where=refs(Chunk).section_id.eq("s2"))
        assert [it.value.id for it in typed] == ["c2"]
        assert await gom.load_matching(Chunk, "graph", top_k=10, threshold=1.01) == []

    async def test_searches_a_view_by_its_root_keeping_roots_with_its_required_relationships(self, gom):
        hits = await gom.load_matching(ChunkInSection, "graph", top_k=10)
        assert [it.value.chunk.id for it in hits] == ["c1"]
        assert hits[0].value.section.id == "s1"

    async def test_needs_an_index_to_search(self, gom):
        with pytest.raises(DrivineError, match="declares no FullTextIndex"):
            await gom.load_matching(Section, "graph", top_k=1)


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


async def ensure_ranked_index(gom: StatelessGraphObjectManager) -> None:
    """
    Story's full-text index, as ensure_indexes makes it. On FalkorDB it is made again: once nodes it matched were
    deleted, as each test's data is, every score it gives is 0 and its hits come in no order (FalkorDB/FalkorDB#3489).
    """
    if gom.persistence_manager.type != DatabaseType.FALKORDB:
        await ensure_indexes(gom, Story)
        return
    for spec in SchemaCatalog.from_fragments(Story).items:
        await gom.indexes.recreate(spec)  # pyright: ignore[reportArgumentType]


LARGEST_TOP_K = 2**63 - 1
"""The largest top_k an engine takes: a 64-bit integer."""


@pytest.fixture
async def ranked_stories(gom: StatelessGraphObjectManager) -> None:
    """
    Twelve stories match "zebra", each longer than the last so each scores below the last; the even ones are titled
    "keep". Saved worst first, so the order they are stored in is not the order they rank in.
    """
    await ensure_ranked_index(gom)
    await gom.save_all(
        [
            Story(id=f"z{i}", title="keep" if i % 2 == 0 else "drop", body="zebra" + " grass" * i)
            for i in range(12, 0, -1)
        ]
    )
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        await gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))


@pytest.mark.usefixtures("ranked_stories")
class TestLoadMatchingBound:
    async def test_a_filter_keeps_the_best_hits_it_passes_not_the_first_it_is_given(self, gom):
        # The limit after the filter takes no sort of its own, so the head must hand it the hits in score order.
        ranked = await gom.load_matching(Story, "zebra", top_k=20)
        assert len(ranked) == 12
        best_kept = [it.score for it in ranked if it.value.title == "keep"][:2]
        kept = await gom.load_matching(Story, "zebra", top_k=2, where=refs(Story).title.eq("keep"))
        assert [it.score for it in kept] == pytest.approx(best_kept)

    async def test_a_cut_that_bites_keeps_the_best_hits_not_the_first_the_index_yields(self, gom):
        # FalkorDB's call takes no limit, and the stories were stored worst first.
        ranked = await gom.load_matching(Story, "zebra", top_k=20)
        top = await gom.load_matching(Story, "zebra", top_k=3)
        assert [it.value.id for it in top] == [it.value.id for it in ranked[:3]]

    async def test_bounds_the_index_by_top_k_at_its_edges(self, gom):
        with pytest.raises(DrivineError, match="top_k must be a whole number of at least 1"):
            await gom.load_matching(Story, "zebra", top_k=0)
        assert len(await gom.load_matching(Story, "zebra", top_k=LARGEST_TOP_K)) == 12
        kept = await gom.load_matching(Story, "zebra", top_k=LARGEST_TOP_K, where=refs(Story).title.eq("keep"))
        assert len(kept) == 6

    @pytest.mark.graph_engine_only("the second label is set by a Cypher statement")
    async def test_reaches_past_the_best_hit_for_a_fragment_whose_second_label_it_lacks(self, gom):
        # z1 ranks best and isn't MatchReviewed: an index asked for top_k alone yields it, and the label check drops it.
        await gom.persistence_manager.execute(QuerySpecification("MATCH (z:MatchStory {id: 'z2'}) SET z:MatchReviewed"))
        reviewed = await gom.load_matching(ReviewedStory, "zebra", top_k=1)
        assert [it.value.id for it in reviewed] == ["z2"]
        # z1 is dropped before the hits are scaled, so z2 is the best of the scale, as on a store that narrows by labels
        # first, and a threshold just under 1 keeps it.
        assert reviewed[0].score == pytest.approx(1.0)
        kept = await gom.load_matching(ReviewedStory, "zebra", top_k=5, threshold=0.99)
        assert [it.value.id for it in kept] == ["z2"]


LAST = "z-last"
"""A story far longer than the others, so the index ranks it last of them."""


@pytest.fixture
async def one_long_story(gom: StatelessGraphObjectManager) -> None:
    """Thirty stories match "zebra": twenty-nine short ones, and LAST."""
    await ensure_ranked_index(gom)
    await gom.save_all(
        [
            *(Story(id=f"z{i}", title="short", body="zebra " + "grass " * i) for i in range(1, 30)),
            Story(id=LAST, title="long", body="zebra " + "savannah " * 400),
        ]
    )
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        await gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))


@pytest.mark.usefixtures("one_long_story")
@pytest.mark.graph_engine_only("a SQL store has no index call to bound")
class TestLoadMatchingIndexLimit:
    """
    The full-text index yields no more hits than the search's index limit: observed through the rows, not read off the
    statement.
    """

    async def only_the_last(self, gom: StatelessGraphObjectManager, top_k: int) -> list[str]:
        hits = await gom.load_matching(Story, "zebra", top_k=top_k, where=refs(Story).id.eq(LAST))
        return [it.value.id for it in hits]

    async def test_a_filtered_search_sees_only_the_best_top_k_times_the_over_fetch(self, gom):
        assert FILTERED_OVER_FETCH < 29
        assert await self.only_the_last(gom, 1) == []

    async def test_a_filtered_search_whose_over_fetch_reaches_the_hit_finds_it(self, gom):
        assert await self.only_the_last(gom, 3) == [LAST]

    async def test_an_unfiltered_search_returns_the_best_top_k_in_score_order(self, gom):
        found = await gom.load_matching(Story, "zebra", top_k=5)
        assert len(found) == 5
        assert found[0].score == pytest.approx(1)
        assert [it.score for it in found] == sorted((it.score for it in found), reverse=True)
        assert LAST not in [it.value.id for it in found]


MANY = 1_100
"""More hits than Memgraph's text_search yields when its call is given no limit."""


@pytest.mark.graph_engine_only("a SQL store has no index call to bound")
async def test_a_search_asking_for_more_than_a_thousand_hits_gets_them(gom: StatelessGraphObjectManager):
    await ensure_indexes(gom, Story)
    await gom.save_all([Story(id=f"z{i}", title="t", body=f"zebra {i}") for i in range(MANY)])
    if gom.persistence_manager.type == DatabaseType.NEO4J:
        await gom.persistence_manager.execute(QuerySpecification("CALL db.awaitIndexes(30)"))
    assert len(await gom.load_matching(Story, "zebra", top_k=MANY)) == MANY
