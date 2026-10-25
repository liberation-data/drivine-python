"""What Oracle's vector search measures by a vector of a given size: Oracle refuses to measure two sizes (ORA-51808)."""

from typing import Annotated

from drivine import ModelRegistry, NodeFragment, NodeId, VectorIndex
from drivine.backend.nearest_search import NearestSearch
from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.backend.oracle.oracle_graph import OracleGraph


class SizedNote(NodeFragment, labels=["SizedNote"]):
    id: Annotated[str, NodeId()]
    embedding: Annotated[list[float] | None, VectorIndex(dimensions=3)] = None


EMBEDDING = "TO_VECTOR(JSON_SERIALIZE(JSON_QUERY(c.props, '$.\"embedding\"')))"

backend = OracleBackend(OracleGraph())
note = ModelRegistry().fragment(SizedNote)


def test_measures_only_the_embeddings_of_the_vectors_size_which_oracle_refuses_to_measure_against_another():
    sql = backend.nearest(note, NearestSearch("SizedNote", "embedding", "COSINE", 5, dimensions=3)).text
    assert f"AND JSON_EXISTS(c.props, '$.\"embedding\"') AND VECTOR_DIMENSION_COUNT({EMBEDDING}) = 3 ORDER BY" in sql
    assert (
        "VECTOR_DIMENSION_COUNT" not in backend.nearest(note, NearestSearch("SizedNote", "embedding", "COSINE", 5)).text
    )


def test_looks_for_a_vector_index_made_for_embeddings_of_the_vectors_size_only():
    sized = backend.search_index_table("VECTOR", "SizedNote", ["embedding"], 3)
    assert sized.text.endswith(" AND JSON_VALUE(details, '$.dimensions' RETURNING NUMBER) = $dimensions")
    assert sized.bindings["dimensions"] == 3
    # A full-text index has no size, and a vector index looked for without one is any made.
    for statement in (
        backend.search_index_table("FULLTEXT", "SizedNote", ["text"], 3),
        backend.search_index_table("VECTOR", "SizedNote", ["embedding"]),
    ):
        assert "dimensions" not in statement.text
        assert "dimensions" not in statement.bindings
