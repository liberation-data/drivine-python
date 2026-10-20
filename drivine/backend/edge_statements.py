from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_stamps import LOCK, relink
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.drivine_error import DrivineError
from drivine.manager.relate_mode import RelateMode
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel


class EdgeStatements:
    """
    Statements for a relationship between two stored nodes, matched by id and never created. Each end gets a new links
    token when a relationship is made, removed, or has its properties changed; neither node's own properties are
    touched.
    """

    @staticmethod
    def relate(
        origin: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement:
        """Joins `origin` to `to`. Returns how many such relationships there are after: 0 when either is absent."""
        _assert_type(type)
        written = [(key, value) for key, value in properties.items() if value is not None]
        bindings: dict[str, Any] = {"_fromId": origin.id, "_toId": to.id, "_stamp": stamps.fresh()}
        for i, (_, value) in enumerate(written):
            bindings[f"_rel{i}"] = value
        sets = [f"r.{quote_name(key)} = $_rel{i}" for i, (key, _) in enumerate(written)]
        merges = mode is RelateMode.MERGE
        changed = "_had = 0 OR _same < _had" if merges else "true"
        parts = [f"MATCH {_pattern('a', origin, '$_fromId')}", f"MATCH {_pattern('b', to, '$_toId')}"]
        if merges:
            # Both locks are taken before the relationships between the two are counted, so the count still holds when
            # the MERGE runs.
            same = [
                "x IS NOT NULL",
                *(f"coalesce(x.{quote_name(key)} = $_rel{i}, false)" for i, (key, _) in enumerate(written)),
            ]
            parts.extend(
                [
                    f"SET a.{LOCK} = true, b.{LOCK} = true REMOVE a.{LOCK}, b.{LOCK}",
                    "WITH a, b",
                    f"OPTIONAL MATCH (a)-[x:{quote_name(type)}]->(b)",
                    f"WITH a, b, count(x) AS _had, sum(CASE WHEN {' AND '.join(same)} THEN 1 ELSE 0 END) AS _same",
                ]
            )
        parts.extend(
            [
                f"{mode.value} (a)-[r:{quote_name(type)}]->(b)",
                f"SET {', '.join([*sets, relink('a', changed), relink('b', changed)])}",
                "RETURN count(r) AS related",
            ]
        )
        return GraphStatement("\n".join(parts), bindings)

    @staticmethod
    def unrelate(origin: MatchedNode, to: MatchedNode, type: str) -> GraphStatement:
        """Removes every relationship of the type from `origin` to `to`. Returns how many there were."""
        _assert_type(type)
        return GraphStatement(
            "\n".join(
                [
                    f"MATCH {_pattern('a', origin, '$_fromId')}-[r:{quote_name(type)}]->{_pattern('b', to, '$_toId')}",
                    "DELETE r",
                    f"SET {relink('a')}, {relink('b')}",
                    "RETURN count(*) AS removed",
                ]
            ),
            {"_fromId": origin.id, "_toId": to.id, "_stamp": stamps.fresh()},
        )

    @staticmethod
    def related(origin: MatchedNode, type: str, direction: Direction, target: FragmentModel) -> GraphStatement:
        """The nodes of `target` joined to `origin` by the type in the direction, each once, as fragment rows."""
        _assert_type(type)
        return GraphStatement(
            "\n".join(
                [
                    f"MATCH {_pattern('a', origin, '$_fromId')}{_arrow(f'[:{quote_name(type)}]', direction)}"
                    f"(n{label_expression(target.labels)})",
                    "WITH DISTINCT n",
                    "RETURN {props: properties(n), labels: labels(n)} AS result",
                ]
            ),
            {"_fromId": origin.id},
        )

    @staticmethod
    def unrelate_all(origin: MatchedNode, type: str, direction: Direction) -> GraphStatement:
        """Removes every relationship of the type `origin` has in the direction. Returns how many there were."""
        _assert_type(type)
        return GraphStatement(
            "\n".join(
                [
                    f"MATCH {_pattern('a', origin, '$_fromId')}{_arrow(f'[r:{quote_name(type)}]', direction)}(b)",
                    "DELETE r",
                    f"SET {relink('a')}, {relink('b')}",
                    "RETURN count(*) AS removed",
                ]
            ),
            {"_fromId": origin.id, "_stamp": stamps.fresh()},
        )


def _arrow(edge: str, direction: Direction) -> str:
    if direction is Direction.INCOMING:
        return f"<-{edge}-"
    if direction is Direction.UNDIRECTED:
        return f"-{edge}-"
    return f"-{edge}->"


def _pattern(alias: str, node: MatchedNode, parameter: str) -> str:
    if not node.model.node_id_property:
        raise DrivineError(
            f"{node.model.type.__name__} has no NodeId field, so a node of it can't be referred to by id."
        )
    labels = list(dict.fromkeys([*node.model.labels, *node.labels]))
    return f"({alias}{label_expression(labels)} {{{quote_name(node.model.node_id_property)}: {parameter}}})"


def _assert_type(type: str) -> None:
    if not type or not type.strip():
        raise DrivineError("A relationship needs a type.")
