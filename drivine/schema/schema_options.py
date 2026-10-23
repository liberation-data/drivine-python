from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from drivine.schema.constraint_manager import VIOLATION_SAMPLE
from drivine.schema.schema_catalog import SchemaCatalog


@dataclass(frozen=True)
class SchemaOptions:
    catalogs: Sequence[SchemaCatalog] = field(default_factory=lambda: [])
    mode: Literal["FAIL_FAST", "WARN"] = "FAIL_FAST"
    """FAIL_FAST raises on drift, a violation or an index in the way of a constraint; WARN logs it and carries on."""
    recreate_on_drift: bool = False
    """Destructive: replace an item whose shape drifted, rather than reporting it."""
    recreate_on_startup: bool = False
    """Destructive: drop and make every declared item on every enforce."""
    violation_sample_size: int = VIOLATION_SAMPLE
    """How many values held more than once a constraint violation reports; 0 skips the check."""
    replace_blocking_index: bool = False
    """
    Drop a plain range index in the way of a uniqueness constraint and make the constraint, whose own index takes its
    place. Only Neo4j reports one in the way: there the constraint brings its own index, while on FalkorDB the plain
    index is what the constraint needs.
    """
