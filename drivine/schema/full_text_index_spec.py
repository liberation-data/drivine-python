from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FullTextIndexSpec:
    """A full-text index over one or more text properties of a label's nodes."""

    label: str
    properties: tuple[str, ...]
    kind: Literal["FULLTEXT"] = "FULLTEXT"
    analyzer: str | None = None
    """
    The analyzer Neo4j's schema DDL names, `fulltext.analyzer` (see schema_ddl), as drivine4j's spec holds it. This
    library reads it from DDL, and doesn't make an index with it, record it, or compare it yet (#442).
    """
