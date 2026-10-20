from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VectorTuning:
    """What a vector index pins on one engine: the portable pins, overridden by that engine's own options."""

    hnsw_m: int | None = None
    hnsw_ef_construction: int | None = None
    quantization_enabled: bool | None = None
    ef_runtime: int | None = None
