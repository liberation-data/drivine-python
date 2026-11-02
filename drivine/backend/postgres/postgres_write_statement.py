from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.postgres.postgres_statement import value_of
from drivine.backend.sql.plan_graph import PlanGraph
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.write_plan import WritePlan, WriteReturns
from drivine.query.query_specification import QuerySpecification

Planned = Callable[[Mapping[str, Any]], tuple[WritePlan, str | None]]
"""The plan for the values the manager binds, and the query a `patch` step asks, where there is one."""

Reshaped = Callable[[list[str], Mapping[str, Any]], list[Any]]
"""The rows the function gives back, each a text, as the manager reads the statement's result."""


@dataclass(frozen=True, kw_only=True)
class PostgresWriteStatement(GraphStatement):
    """
    A statement that carries out a plan through the store's write function, drivine4j's (see PlanGraph), and reads what
    it gives back as the object manager reads a write's result: on Postgres and on SQLite alike. The plan is made when
    the statement is bound, from the values the manager binds then: a patch's, or a relationship's ends.
    """

    planned: Planned = field(repr=False)
    reshaped: Reshaped = field(repr=False)

    @staticmethod
    def of(graph: PlanGraph, returns: WriteReturns, planned: Planned, reshaped: Reshaped) -> PostgresWriteStatement:
        return PostgresWriteStatement(graph.write_call(returns), planned=planned, reshaped=reshaped)

    @staticmethod
    def plan_bindings(plan: WritePlan, patched_if: str | None = None) -> dict[str, Any]:
        """The values a plan is bound with."""
        return {
            WritePlan.PLAN: to_json(plan.plan),
            WritePlan.SHAPES: to_json(plan.shapes_bound),
            WritePlan.MARK: plan.mark,
            WritePlan.PATCHED_IF: patched_if,
        }

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        bound = {**self.bindings, **(more or {})}
        plan, patched_if = self.planned(bound)
        return (
            QuerySpecification[Any](self.text)
            .bind(PostgresWriteStatement.plan_bindings(plan, patched_if))
            .add_post_processors(_Reshaping(self.reshaped, bound))
        )


class _Reshaping:
    """Reads the rows the write function gave back, each a text, as the manager reads the statement's result."""

    def __init__(self, reshaped: Reshaped, bound: Mapping[str, Any]) -> None:
        self.reshaped = reshaped
        self.bound = bound

    def apply(self, results: list[Any]) -> list[Any]:
        return self.reshaped([value_of(it) for it in results], self.bound)
