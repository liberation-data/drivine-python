from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NativeOperation:
    """A command for the engine's driver, rather than a Cypher statement: FalkorDB's constraints."""

    native: Literal["CONSTRAINT_CREATE", "CONSTRAINT_DROP"]
    label: str
    properties: tuple[str, ...]
