from __future__ import annotations

from dataclasses import dataclass

from drivine.schema.vector_index_spec import SimilarityFunction


@dataclass(frozen=True)
class SchemaItem:
    """What an engine reports of an item: its label and properties, its name where it names items, and its shape."""

    label: str
    properties: tuple[str, ...]
    name: str | None = None
    dimensions: int | None = None
    similarity: SimilarityFunction | None = None
    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
    quantization_enabled: bool | None = None
    ef_runtime: int | None = None
