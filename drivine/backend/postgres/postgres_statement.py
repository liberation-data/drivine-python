from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
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

    id_key: Callable[[Any], str] | None = field(default=None, compare=False)
    """What the id given as `$id` is bound as: see keying_id. Unset, it is bound as it is given."""

    @staticmethod
    def of(statement: GraphStatement) -> PostgresStatement:
        """The statement, its rows read as a Postgres backend's are."""
        return PostgresStatement(statement.text, statement.bindings)

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        given = dict(more or {})
        if self.id_key is not None and given.get("id") is not None:
            given["id"] = self.id_key(given["id"])
        return super().spec(given).map(value_of)

    def keying_id(self, key: Callable[[Any], str]) -> PostgresStatement:
        """
        This statement, which finds a node by the id bound as `$id`, binding the id it is given as `key` writes it: the
        text the store reads the node's id back as (see stored_key), so a float id is found by the text it is held as,
        not by the text the driver writes the number as (`1e-07`). A null id is bound as it is, and finds no node.
        """
        return replace(self, id_key=key)


def value_of(row: Any) -> Any:
    """A row of one column as that column's value; any other row as it is."""
    if not isinstance(row, Mapping):
        return row
    columns = cast(Mapping[Any, Any], row)
    return next(iter(columns.values())) if len(columns) == 1 else columns
