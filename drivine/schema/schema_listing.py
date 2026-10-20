from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from drivine.schema.schema_item import SchemaItem


@dataclass(frozen=True)
class SchemaListing:
    """A statement that lists an engine's items of one kind, and how to read its rows."""

    statement: str
    read: Callable[[list[Any]], list[SchemaItem]]
