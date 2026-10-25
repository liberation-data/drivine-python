# Written by scripts/unasync.py from tests/integration/test_vector_scores.py:
# change that, and run the script again.
"""
The score of a known pair of vectors, the same on every store: (1 + cos) / 2 for cosine and 1 / (1 + d²) for euclidean.

Each node is measured from the query, at [1, 0]:

| node | cosine embedding | cos  | score | euclidean embedding | d²  | score |
|------|------------------|------|-------|---------------------|-----|-------|
| Q    | [1, 0]           | 1    | 1     | [1, 0]              | 0   | 1     |
| P    | [0.6, 0.8]       | 0.6  | 0.8   | [0.6, 0.8]          | 0.8 | 1/1.8 |
| N    | [-0.6, 0.8]      | -0.6 | 0.2   | [3, 4]              | 20  | 1/21  |
| O    | [-1, 0]          | -1   | 0     | [1, 2]              | 4   | 0.2   |

The negative cosines are there for Memgraph, whose `similarity` is the absolute value of 1 - distance: were it read, N
and O would score as P and Q do (memgraph/memgraph#5199).
"""

from typing import Annotated

import pytest

from drivine.sync import (
    DatabaseType,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    SchemaCatalog,
    StatelessGraphObjectManager,
    VectorIndex,
)


class ScoredPair(NodeFragment, labels=["ScoredPair"]):
    id: Annotated[str, NodeId()]
    cosine: Annotated[list[float] | None, VectorIndex("COSINE", dimensions=2)] = None
    euclidean: Annotated[list[float] | None, VectorIndex("EUCLIDEAN", dimensions=2)] = None


QUERY = [1.0, 0.0]

COSINE = [("Q", 1.0), ("P", 0.8), ("N", 0.2), ("O", 0.0)]
EUCLIDEAN = [("Q", 1.0), ("P", 1 / 1.8), ("O", 0.2), ("N", 1 / 21)]

WIDE = 10
"""Wide enough that every store, approximate or not, sees every node."""

TOLERANCE = 1e-3
"""A quantizing Neo4j index reports 0.7998811 for an exact 0.8."""


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    gom = StatelessGraphObjectManager(pm)
    if pm.type == DatabaseType.MEMGRAPH:
        # Memgraph's vector search returns nodes deleted since its last garbage collection, and fails on them.
        pm.execute(QuerySpecification("FREE MEMORY"))
    for spec in SchemaCatalog.from_fragments(ScoredPair).items:
        gom.indexes.ensure(spec)  # pyright: ignore[reportArgumentType]
    gom.save_all(
        [
            ScoredPair(id="Q", cosine=[1.0, 0.0], euclidean=[1.0, 0.0]),
            ScoredPair(id="P", cosine=[0.6, 0.8], euclidean=[0.6, 0.8]),
            ScoredPair(id="N", cosine=[-0.6, 0.8], euclidean=[3.0, 4.0]),
            ScoredPair(id="O", cosine=[-1.0, 0.0], euclidean=[1.0, 2.0]),
        ]
    )
    if pm.type == DatabaseType.NEO4J:
        # A vector index is filled in the background: it must hold the nodes before it is searched.
        pm.execute(QuerySpecification("CALL db.awaitIndexes(30)"))
    return gom


def nearest(
    gom: StatelessGraphObjectManager,
    property: str,
    *,
    top_k: int = WIDE,
    threshold: float | None = None,
) -> list[tuple[str, float]]:
    hits = gom.load_nearest(ScoredPair, QUERY, top_k=top_k, search_k=WIDE, threshold=threshold, property=property)
    return [(hit.value.id, hit.score) for hit in hits]


def assert_scored(expected: list[tuple[str, float]], actual: list[tuple[str, float]]) -> None:
    assert [id for id, _ in actual] == [id for id, _ in expected], f"scored {actual}"
    for (id, score), (_, got) in zip(expected, actual, strict=True):
        assert got == pytest.approx(score, abs=TOLERANCE), f"{id} in {actual}"


class TestVectorScores:
    def test_scores_a_cosine_pair_as_one_plus_cos_over_two(self, gom):
        assert_scored(COSINE, nearest(gom, "cosine"))

    def test_scores_a_euclidean_pair_as_one_over_one_plus_d_squared(self, gom):
        assert_scored(EUCLIDEAN, nearest(gom, "euclidean"))

    def test_gives_the_same_scores_when_fewer_are_asked_for_than_searched(self, gom):
        assert_scored(COSINE[:2], nearest(gom, "cosine", top_k=2))
        assert_scored(EUCLIDEAN[:2], nearest(gom, "euclidean", top_k=2))

    def test_compares_a_threshold_with_that_score(self, gom):
        assert_scored(COSINE[:2], nearest(gom, "cosine", threshold=0.5))
        assert_scored(EUCLIDEAN[:3], nearest(gom, "euclidean", threshold=0.1))
