from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FullTextIndexSpec:
    """A full-text index over one or more text properties of a label's nodes."""

    label: str
    properties: tuple[str, ...]
    kind: Literal["FULLTEXT"] = "FULLTEXT"
