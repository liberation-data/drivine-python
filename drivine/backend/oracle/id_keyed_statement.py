from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import write
from drivine.query.query_specification import QuerySpecification


def held_id(id: Any) -> str:
    """
    An id as the JSON text Oracle's layout holds it as (see value_layout.write), as a save writes it: a float extended
    JSON's double (`{"$numberDouble": "1.0E-7"}`), which a statement reads `JSON(… EXTENDED)` so that it compares equal
    to the binary double the node holds, a text its JSON (`"a"`), and a whole number its digits.
    """
    return to_json(write(id, "oracle"))


@dataclass(frozen=True)
class IdKeyedStatement(GraphStatement):
    """
    A statement that finds a node by `$id`, read `JSON($id EXTENDED)`, binding the id as the JSON text Oracle's layout
    holds it as (see held_id), as a save writes it and finds the node: a float is extended JSON's double, as the node
    holds a binary double, which a NUMBER bound as it is (`1e-7`) is not always equal to. drivine4j's NodeIds keys an id
    so. A null id is bound as it is, and finds no node.
    """

    @staticmethod
    def of(statement: GraphStatement) -> IdKeyedStatement:
        return IdKeyedStatement(statement.text, statement.bindings)

    def spec(self, more: Mapping[str, Any] | None = None) -> QuerySpecification[Any]:
        given = dict(more or {})
        if given.get("id") is not None:
            given["id"] = held_id(given["id"])
        return super().spec(given)
