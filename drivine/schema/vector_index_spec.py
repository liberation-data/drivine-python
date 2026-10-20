from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from drivine.schema.engine_vector_options import EngineVectorOptions
from drivine.schema.vector_tuning import VectorTuning

SimilarityFunction = Literal["COSINE", "EUCLIDEAN"]


@dataclass(frozen=True)
class VectorIndexSpec:
    """
    A vector index over a property of a label's nodes, holding embeddings of a fixed number of dimensions. The HNSW
    pins are portable: HNSW is the same algorithm on Neo4j and FalkorDB. Unpinned, the engine chooses, and Drivine
    never substitutes a default of its own. What only one engine has goes in `engine_options`.
    """

    label: str
    properties: tuple[str]
    dimensions: int
    similarity: SimilarityFunction = "COSINE"
    capacity: int | None = None
    """Memgraph only: how many vectors the index is first made to hold."""
    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
    engine_options: tuple[EngineVectorOptions, ...] = ()
    kind: Literal["VECTOR"] = "VECTOR"

    def tuning(self, engine: str) -> VectorTuning:
        """What the index pins on one engine: the portable pins, overridden by that engine's own options."""
        own = next((it for it in self.engine_options if it.engine == engine), None)
        return VectorTuning(
            hnsw_m=own.hnsw_m if own is not None and own.hnsw_m is not None else self.hnsw_m,
            hnsw_ef_construction=(
                own.hnsw_ef_construction
                if own is not None and own.hnsw_ef_construction is not None
                else self.hnsw_ef_construction
            ),
            quantization_enabled=own.quantization_enabled if own is not None else None,
            ef_runtime=own.ef_runtime if own is not None else None,
        )
