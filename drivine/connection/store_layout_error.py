from __future__ import annotations

from drivine.drivine_error import DrivineError


class StoreLayoutError(DrivineError):
    """
    A graph whose store layout this version of Drivine does not read: written in an earlier layout and holding values,
    written by a later Drivine, or stamped by two at once (liberation-data/drivine-cypher-spec#161). The shared module
    decides, and the message says which, and what to do: migrate the graph, reload it with this version, or upgrade.
    Raised on every connection, as no statement may run over a store whose values are held otherwise. drivine4j's
    StoreLayoutException.
    """

    def __init__(self, graph: str, message: str) -> None:
        super().__init__(message)
        self.graph = graph
        """The graph refused, as it was named."""
