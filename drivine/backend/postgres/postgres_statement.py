from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from drivine.backend.graph_statement import GraphStatement
from drivine.query.query_specification import QuerySpecification


@dataclass(frozen=True)
class PostgresStatement(GraphStatement):
    """
    A statement of the Postgres backend. Postgres gives a row as a record of its columns, which the plain SQL a Postgres
    persistence manager runs reads as it is; the object manager reads a row of one column as that column's value, as a
    record of one column is on the graph engines and on Oracle. So this statement's rows are read so.
    """

    @staticmethod
    def of(statement: GraphStatement) -> PostgresStatement:
        """The statement, its rows read as a Postgres backend's are."""
        return PostgresStatement(statement.text, statement.bindings)

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        return super().spec(more).map(value_of)


def value_of(row: Any) -> Any:
    """A row of one column as that column's value; any other row as it is."""
    if not isinstance(row, Mapping):
        return row
    columns = cast(Mapping[Any, Any], row)
    return next(iter(columns.values())) if len(columns) == 1 else columns
