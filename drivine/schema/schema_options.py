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
    """FAIL_FAST raises on drift or a violation; WARN logs it and carries on."""
    recreate_on_drift: bool = False
    """Destructive: replace an item whose shape drifted, rather than reporting it."""
    recreate_on_startup: bool = False
    """Destructive: drop and make every declared item on every enforce."""
    violation_sample_size: int = VIOLATION_SAMPLE
    """How many values held more than once a constraint violation reports; 0 skips the check."""
