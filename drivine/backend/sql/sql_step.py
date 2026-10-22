from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SqlStep:
    """
    One relationship from a node row: the clauses `FROM … WHERE …` (ending in an open WHERE to add to), the edge's and
    the far node's aliases, and the test of the far node's labels (`1 = 1` for none).
    """

    from_: str
    edge: str
    node: str
    x: str
