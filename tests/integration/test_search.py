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
