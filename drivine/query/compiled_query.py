from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompiledQuery:
    """
    A statement and its parameters, formatted for the target database.
    """

    statement: str
    parameters: dict[str, Any]
