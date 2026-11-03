from __future__ import annotations

from typing import Any

from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.cypher_stamps import LOCK, STAMP, changes, restamp
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.save_statement_builder import id_of, object_props
from drivine.backend.vector_writes import split_vectors, vector_clauses
from drivine.drivine_error import DrivineError
from drivine.manager.save_options import SaveOptions
from drivine.model import lower_copies, stamps
from drivine.model.fragment_model import FragmentModel


class FragmentBatchStatement:
    """
    Saves fragments of any number of classes in one statement: a FOREACH for each class over its rows, each node merged
    on its id and given its properties, stamped as a save stamps it. Stamps aren't checked: a batch only adds. The
    stamps of each class's nodes are read before and after, and the statement returns `{groups: [{before, after}]}`,
    each a list of `{id, stamp}`, for the stamps to be handed back.
    """

    @staticmethod
    def build(groups: list[FragmentGroup], options: SaveOptions, grammar: CypherGrammar) -> GraphStatement:
        bindings: dict[str, Any] = {"_stamp": stamps.fresh()}
        carried = ["_"]
        parts = ["WITH 1 AS _"]
        for i, group in enumerate(groups):
            rows: list[dict[str, Any]] = []
            for it in group.objects:
                props, vectors = split_vectors(group.model, object_props(group.model, it, options))
                rows.append({"id": id_of(group.model, it), "props": props, "vectors": vectors})
            bindings[f"rows{i}"] = rows
            bindings[f"ids{i}"] = [it["id"] for it in rows]
            parts.extend(FragmentBatchStatement._read(group.model, i, "before", carried))
            carried.append(f"before{i}")
        for i, group in enumerate(groups):
            id_ = quote_name(_id_property(group.model))
            vector_sets, vector_changes = vector_clauses(group.model, grammar, "n", "row.vectors")
            changed = " OR ".join([changes("n", "row.props"), *vector_changes])
            # The lowercased copies after the properties, from what the node then holds: see lower_copies.
            writes = [*vector_sets, *lower_copies.clauses("n", group.model.lower_copied)]
            parts.append(
                f"FOREACH (row IN $rows{i} | MERGE (n{label_expression(group.model.labels)} {{{id_}: row.id}}) "
                f"SET n.{LOCK} = true REMOVE n.{LOCK} SET {restamp('n', changed)} SET n += row.props "
                f"{' '.join(writes)})"
            )
        for i, group in enumerate(groups):
            parts.extend(FragmentBatchStatement._read(group.model, i, "after", carried))
            carried.append(f"after{i}")
        result = ", ".join(f"{{before: before{i}, after: after{i}}}" for i in range(len(groups)))
        parts.append(f"RETURN {{groups: [{result}]}} AS result")
        return GraphStatement("\n".join(parts), bindings)

    @staticmethod
    def _read(model: FragmentModel, i: int, when: str, carried: list[str]) -> list[str]:
        a = f"{when[0]}{i}"
        id_ = quote_name(_id_property(model))
        return [
            f"WITH {', '.join(carried)}",
            f"OPTIONAL MATCH ({a}{label_expression(model.labels)}) WHERE {a}.{id_} IN $ids{i}",
            f"WITH {', '.join(carried)}, collect(CASE WHEN {a} IS NOT NULL THEN {{id: {a}.{id_}, "
            f"stamp: coalesce({a}.{STAMP}, '{stamps.NEVER_STAMPED}')}} END) AS {when}{i}",
        ]


def _id_property(model: FragmentModel) -> str:
    if not model.node_id_property:
        raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be saved: a save merges on the id.")
    return model.node_id_property
