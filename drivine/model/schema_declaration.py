from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SchemaDeclaration:
    """A schema item a fragment declares, over the stored names of its fields."""

    kind: Literal["RANGE", "UNIQUE", "VECTOR", "FULLTEXT"]
    properties: tuple[str, ...]
    field: str = ""
    """The field a vector index is declared on, for messages."""
    similarity: Literal["COSINE", "EUCLIDEAN"] = "COSINE"
    dimensions: int | None = None
    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
    ignore_case: bool = False
    """Whether a range index or uniqueness constraint is over the lowercased copies of its properties."""
