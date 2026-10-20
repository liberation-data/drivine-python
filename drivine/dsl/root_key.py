from __future__ import annotations

from dataclasses import dataclass

from drivine.dsl.order_term import OrderTerm


@dataclass(frozen=True)
class RootKey:
    """A root term of an order: by an expression, or by the root property stored under `property`."""

    term: OrderTerm
    property: str | None = None
