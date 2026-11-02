from __future__ import annotations

from typing import Protocol

from drivine.backend.sql.write_plan import WriteReturns


class PlanGraph(Protocol):
    """
    A graph whose writes are a plan of steps (see WritePlan), carried out by the store: on Postgres by the graph's write
    function, on SQLite by the function Drivine registers on each connection. Each is drivine4j's, the plan the same.
    """

    name: str
    nodes: str
    booleans: tuple[str, str]
    """True and false as literals, as the store's driver binds them: on SQLite the integers 1 and 0."""

    def write_call(self, returns: WriteReturns) -> str:
        """The statement that carries out the plan bound as `$_plan`, giving back what `returns` asks."""
        ...
