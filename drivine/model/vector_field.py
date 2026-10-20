from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VectorField:
    """A fragment's field with a vector index: the property it is stored as, and the index's similarity."""

    property: str
    similarity: Literal["COSINE", "EUCLIDEAN"] = "COSINE"
