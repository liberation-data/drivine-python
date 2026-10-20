from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    """How a load's roots are ordered, an order written over the root, and how many are skipped and returned."""

    order_by: str | None = None
    skip: int | None = None
    limit: int | None = None
