from __future__ import annotations

from dataclasses import dataclass, field

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
    incomplete: bool = False
    """
    Whether the engine holds part of what is made for the item and lacks the rest (Oracle: a range index whose index
    over the label's nodes is gone), so that `ensure` makes the rest. It is listed, found and dropped as it is.
    """
    aliases: tuple[str, ...] = field(default=(), repr=False)
    """
    The other names the item goes by, where the store keeps them (the SQL stores, as `aliases` among a catalog record's
    `details`): names Neo4j's schema DDL gave it where the schema API names it otherwise, so that a `DROP` by one finds
    it (see SchemaCommands). drivine4j's `RecordedItem.aliases`.
    """
