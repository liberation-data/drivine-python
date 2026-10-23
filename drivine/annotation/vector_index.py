from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VectorIndex:
    """
    A vector index over the field. Its dimensions come from the catalog's dimension provider when the catalog is built
    (they belong to the embedding model, known at run time), unless given here. Drives `load_nearest`, and is how
    Drivine knows the field is an embedding: a catalog's vector index over a fragment field without it is refused.
    """

    similarity: Literal["COSINE", "EUCLIDEAN"] = "COSINE"
    dimensions: int | None = None
    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
