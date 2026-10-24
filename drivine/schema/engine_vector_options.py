from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class EngineVectorOptions:
    """
    One engine's own vector index options: typed data, never DDL. An `hnsw_m` or `hnsw_ef_construction` here wins over
    the portable one on that engine. `quantization_enabled` is Neo4j's, and `ef_runtime` FalkorDB's.

    `quantization_enabled` pins `vector.quantization.enabled`. Neo4j 2026.06 and later still take it, quantizing with
    their default kind when it is true, and report `vector.quantization.type` back instead, which reads as enabled
    unless it is `NONE`.
    """

    engine: Literal["Neo4j", "FalkorDB"]
    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
    quantization_enabled: bool | None = None
    ef_runtime: int | None = None
