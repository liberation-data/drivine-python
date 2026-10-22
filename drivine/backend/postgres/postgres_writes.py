from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

from drivine.annotation.direction import Direction
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.backend.postgres.found_stamp import FoundStamp, stamps_by_place
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.backend.postgres.postgres_save_builder import absent, present, stored_key, without_key
from drivine.backend.postgres.postgres_write_statement import PostgresWriteStatement
from drivine.backend.save_statement_builder import id_of, object_props
from drivine.backend.sql.node_writes import NodeWrites
from drivine.backend.sql.sql_graph import sql_text
from drivine.backend.sql.sql_json import storable, to_json
from drivine.backend.sql.write_plan import WritePlan
from drivine.drivine_error import DrivineError
from drivine.manager.relate_mode import RelateMode
from drivine.manager.save_options import SaveOptions
from drivine.model.fragment_model import FragmentModel

CHANGED = "1"
"""In a patch step's entry, where a save's has the stamp found: a value written differed from the node's."""

UNMET = "-"
"""In a patch step's entry: the node is there and does not meet the patch's condition, so nothing was written."""

_PARAMETER = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class PostgresWrites:
    """
    The writes beside a save, each a plan for drivine4j's write function, so stamps are kept as a save keeps them:
    patch and patch_all, a batch of fragments, and relationships written on their own. A patch's values, and the
    condition it holds to, are bound by the manager when it runs the statement, so its plan is made then.
    """

    def __init__(self, graph: PostgresGraph) -> None:
        self.graph = graph

    def patch_one(self, model: FragmentModel, where: str | None = None) -> GraphStatement:
        """
        Writes `$set` (a merge patch, `$vectors` beside it) to the node with id `$id`, if it meets `where`, a condition
        over its row `n`: `{found, applied, changed}`.
        """
        key = _id_property(model, "it can't be patched by id")

        def planned(bound: Mapping[str, Any]) -> tuple[WritePlan, str | None]:
            plan = WritePlan()
            values: dict[str, Any] = {**(bound.get("set") or {}), **(bound.get("vectors") or {})}
            plan.patch(plan.shape(model.labels, key), stored_key(bound["id"]), "0", present(values), absent(values))
            return plan, self._meeting(where, bound) if where else None

        def reshaped(rows: list[str], bound: Mapping[str, Any]) -> list[Any]:
            outcome = FoundStamp.of(rows[0]).found if rows else None
            return [
                {
                    "found": outcome is not None,
                    "applied": outcome is not None and outcome != UNMET,
                    "changed": outcome == CHANGED,
                }
            ]

        return PostgresWriteStatement.of(self.graph, "EACH_STAMP", planned, reshaped)

    def patch_many(self, model: FragmentModel) -> GraphStatement:
        """Patches a node per row of `$rows`, `{id, props, vectors}`: `{id, found, changed}` for each row."""
        key = _id_property(model, "it can't be patched by id")

        def planned(bound: Mapping[str, Any]) -> tuple[WritePlan, str | None]:
            plan = WritePlan()
            shape = plan.shape(model.labels, key)
            for i, row in enumerate(bound["rows"]):
                values: dict[str, Any] = {**(row.get("props") or {}), **(row.get("vectors") or {})}
                plan.patch(shape, stored_key(row["id"]), str(i), present(values), absent(values))
            return plan, None

        def reshaped(rows: list[str], bound: Mapping[str, Any]) -> list[Any]:
            outcomes = {it.index: it.found for it in (FoundStamp.of(row) for row in rows)}
            return [
                {"id": row["id"], "found": str(i) in outcomes, "changed": outcomes.get(str(i)) == CHANGED}
                for i, row in enumerate(bound["rows"])
            ]

        return PostgresWriteStatement.of(self.graph, "EACH_STAMP", planned, reshaped)

    def fragment_batch(self, groups: Sequence[FragmentGroup], options: SaveOptions) -> GraphStatement:
        """
        Saves fragments of any number of classes, each a step of one plan, merged on its id. Stamps are not checked: a
        batch only adds. Returns `{groups: [{before, after}]}`, each a list of `{id, stamp}`, for the stamps to be
        handed back.
        """
        plan = WritePlan()
        handed: dict[str, tuple[int, Any]] = {}
        for g, group in enumerate(groups):
            key = _id_property(group.model, "it can't be saved: a save merges on the id")
            shape = plan.shape(group.model.labels, key)
            for obj in group.objects:
                id_ = id_of(group.model, obj)
                props = without_key(object_props(group.model, obj, options), key)
                index = str(len(handed))
                handed[index] = (g, id_)
                plan.node(
                    shape,
                    key,
                    id_,
                    stored_key(id_),
                    list(dict.fromkeys(group.model.labels)),
                    _writes(props),
                    index,
                )

        def reshaped(rows: list[str], bound: Mapping[str, Any]) -> list[Any]:
            return [{"groups": stamps_by_place(rows, handed, len(groups))}]

        return PostgresWriteStatement.of(self.graph, "EACH_STAMP", lambda bound: (plan, None), reshaped)

    def relate(
        self, from_: MatchedNode, to: MatchedNode, type: str, properties: Mapping[str, Any], mode: RelateMode
    ) -> GraphStatement:
        """Joins two stored nodes: 1, or 0 when either is not there."""
        return self._counting(
            type,
            lambda plan: plan.link(
                self._found(plan, from_),
                self._found(plan, to),
                type,
                Direction.OUTGOING,
                present(properties),
                [],
                mode is RelateMode.CREATE,
            ),
        )

    def unrelate(self, from_: MatchedNode, to: MatchedNode, type: str) -> GraphStatement:
        """Removes the relationships of the type from one stored node to another: how many."""
        return self._counting(
            type, lambda plan: plan.cut(self._found(plan, from_), self._found(plan, to), type, Direction.OUTGOING)
        )

    def unrelate_all(self, from_: MatchedNode, type: str, direction: Direction) -> GraphStatement:
        """Removes the relationships of the type from a stored node in the direction: how many."""
        return self._counting(type, lambda plan: plan.cut(self._found(plan, from_), None, type, direction))

    def _counting(self, type: str, steps: Callable[[WritePlan], None]) -> GraphStatement:
        """The plan `steps` writes, which counts the relationships it writes: one row, their number."""
        if not type or not type.strip():
            raise DrivineError("A relationship needs a type.")
        plan = WritePlan()
        steps(plan)
        return PostgresWriteStatement.of(
            self.graph, "COUNT", lambda bound: (plan, None), lambda rows, bound: [int(it) for it in rows]
        )

    def _found(self, plan: WritePlan, node: MatchedNode) -> int:
        """The step that finds the node `node` names, which must be there: by its class's labels and those it adds."""
        key = _id_property(node.model, "a node of it can't be referred to by id")
        labels = list(dict.fromkeys([*node.model.labels, *node.labels]))
        return plan.ref(plan.shape(labels, key), stored_key(node.id))

    def _meeting(self, where: str, bound: Mapping[str, Any]) -> str:
        """
        The query a patch step asks of the node it found, by its `nid`, the function's `$1`: whether its row `n` meets
        `where`. The function runs it on its own, where no value can be bound, so each value `where` names is written
        in.
        """
        written = _PARAMETER.sub(lambda it: _literal(bound[it[1]]) if it[1] in bound else it[0], where)
        return f"SELECT COUNT(*) FROM {self.graph.nodes} n WHERE n.nid = $1 AND ({written})"


def _writes(props: Mapping[str, Any]) -> NodeWrites:
    """A node step's writes of the properties: those that are not null written, and those that are removed."""
    return NodeWrites(set=present(props), unset=absent(props))


def _literal(value: Any) -> str:
    """
    A value written into a statement as a literal, as a driver would bind it: a number as itself, anything else as
    text, which Postgres reads as the type the statement wants there, as it reads a value bound without a type. A number
    is written in parentheses, so a negative one after a minus is not read as the start of a comment.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return sql_text("true" if value else "false")
    if isinstance(value, (int, float)):
        return f"({value})"
    if isinstance(value, (datetime, date, time)):
        return sql_text(storable(value))
    if isinstance(value, (dict, list, tuple)):
        return sql_text(to_json(value))
    return sql_text(str(value))


def _id_property(model: FragmentModel, because: str) -> str:
    if not model.node_id_property:
        raise DrivineError(f"{model.type.__name__} has no NodeId field, so {because}.")
    return model.node_id_property
