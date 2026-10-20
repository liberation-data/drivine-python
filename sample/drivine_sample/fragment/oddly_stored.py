from __future__ import annotations

from typing import Annotated

from drivine import GraphProperty, NodeFragment, NodeId


class OddlyStored(NodeFragment, labels=["OddlyStored"]):
    """Stored names that aren't plain: a dollar, a percent, quotes and a backslash, each queried as it is declared."""

    id: Annotated[str, NodeId()]
    price: Annotated[float | None, GraphProperty("price$usd")] = None
    rate: Annotated[float | None, GraphProperty("rate%")] = None
    greeting: Annotated[str | None, GraphProperty('say "hi"')] = None
    path: Annotated[str | None, GraphProperty("back\\slash")] = None
