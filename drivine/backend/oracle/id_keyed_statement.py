from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from drivine.backend.graph_statement import GraphStatement
from drivine.backend.oracle.oracle_graph import carries_labels
from drivine.backend.sql.node_ids import has_id
from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import write
from drivine.query.query_specification import QuerySpecification

NO_JSON_TABLE_TRANSFORM = "/*+ NO_JSON_TABLE_TRANSFORM */"
"""
The hint each query that compares a node's id carries: a comparison reads several JSON values of the node's
properties, which Oracle 23's JSON table transformation reads by one `JSON_TABLE`, and so rewritten, a node's table
beside another table, or beside a subquery of one, lost the tests of its labels and properties
(drivine-cypher-spec#415). The shared module's statements carry it on Oracle for the same reason.
"""


def held_id(id: Any) -> str | None:
    """
    An id as the JSON text Oracle's layout holds it as (see value_layout.write), as a save writes it: a float extended
    JSON's double (`{"$numberDouble": "1.0E-7"}`), which a statement reads `JSON(… EXTENDED)` so that it compares equal
    to the binary double the node holds, a text its JSON (`"a"`), and a whole number its digits. A null id is bound as
    it is, None, and finds no node: not as the text `null`, which is a JSON value (Drivine for TypeScript's `oracleId`,
    liberation-data/drivine#777).
    """
    return None if id is None else to_json(write(id, "oracle"))


def bound_id_row(sql: str) -> str:
    """
    The one row `i.v` of the id bound as `sql`, the JSON text Oracle's layout holds it as (see held_id), read
    `EXTENDED` so a float is the binary double the node holds. The module's comparison reads the id more than once, and
    a dynamic statement's binds are by position, so the id is bound once, here, and the comparison reads the row's `v`.
    """
    return f"(SELECT JSON({sql} EXTENDED) v FROM dual)"


def bound_id_rows(sql: str) -> str:
    """
    A row `i.v` for each id of the JSON array bound as `sql`, each the JSON Oracle's layout holds it as, read
    `EXTENDED` as bound_id_row reads one. Not the module's `hasIdIn`: that reads each id's JSON text without
    `EXTENDED`, so a float, held as a binary double, would be no id of the list.
    """
    return f"JSON_TABLE(JSON({sql} EXTENDED), '$[*]' COLUMNS (v JSON PATH '$'))"


def found_by_ids(nodes: str, alias: str, property: str, labels: Sequence[str], rows: str) -> str:
    """
    Whether the node row `alias` is a node of `labels` whose id, the property `property`, is one of the ids `rows`
    holds (bound_id_row, bound_id_rows): each id finds its node `f` through the index over the id, compared as the
    shared module writes the comparison (see node_ids.has_id), and `alias` is the node found, by its `nid`. The `nid`
    is handed back as `f.nid + 0`, the shared module's form of a node equality: Oracle 23.26 eliminates a join of the
    node table to a view of it on a bare `nid`, the filter on the view with it, and then ends the server process as it
    parses a statement given a bound value, or gives every row for a literal one (drivine-cypher-spec#160). The labels
    are told by their text (carries_labels), so Oracle reads the index over the id, and not every node of the first
    label. Drivine for TypeScript's `foundByIds` writes the same.
    """
    tests = [it for it in (has_id("oracle", "f", property, "i.v"), carries_labels("f", labels)) if it]
    return (
        f"{alias}.nid IN (SELECT {NO_JSON_TABLE_TRANSFORM} f.nid + 0 FROM {rows} i CROSS JOIN {nodes} f "
        f"WHERE {' AND '.join(tests)})"
    )


def holds_id_among(alias: str, property: str, rows: str) -> str:
    """
    Whether the id, the property `property`, of the node row `alias`, found some other way, is one of the ids `rows`
    holds (bound_id_rows), compared as the shared module writes the comparison (see node_ids.has_id). The subquery
    carries NO_JSON_TABLE_TRANSFORM, and so does the query that finds the node, beside which it reads the node's
    properties.
    """
    return f"EXISTS (SELECT {NO_JSON_TABLE_TRANSFORM} 1 FROM {rows} i WHERE {has_id('oracle', alias, property, 'i.v')})"


def id_match(nodes: str, property: str, labels: Sequence[str]) -> str:
    """
    The match `find_node` and `write_node` run, `SELECT nid FROM nodes n WHERE <match>`, for the node of `labels` whose
    id, the property `property`, is the dynamic statement's one bind, `:1`: the id bound once, in a row of its own
    (bound_id_row), since the shared module's comparison reads it more than once (see found_by_ids).
    """
    return found_by_ids(nodes, "n", property, labels, bound_id_row(":1"))


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
        if "id" in given:
            given["id"] = held_id(given["id"])
        return super().spec(given)
