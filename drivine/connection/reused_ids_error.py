from __future__ import annotations

from drivine.drivine_error import DrivineError

IN_MEMORY = "an in-memory database"
"""What the message names in place of a file for a database SQLite keeps in memory or in a temporary file."""


class ReusedIdsError(DrivineError):
    """
    A SQLite graph whose tables an earlier Drivine made: their `INTEGER PRIMARY KEY`, without `AUTOINCREMENT`, gives a
    new row the id of a deleted one, so an element id could name another node or relationship than the one it was read
    from (liberation-data/drivine4j#713). Raised as the graph's tables are looked for, on a provider's first connection
    and on each one after it, rather than logged as a failure to make the tables is: every statement over the graph
    could give a wrong answer. There is no migration; the message says what to do instead. drivine4j's
    ReusedIdsException.
    """

    def __init__(self, graph: str, path: str | None) -> None:
        super().__init__(
            f"The SQLite graph '{graph}' in {path or IN_MEMORY} was made by an earlier Drivine, whose node and "
            "relationship ids could be reused after a delete. Make a new file and save the graph into it again."
        )
        self.graph = graph
        """The graph whose tables were made so, as it was named."""
        self.path = path
        """The database's file; None for one in memory or in a temporary file."""
