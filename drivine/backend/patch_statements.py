from __future__ import annotations

from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.cypher_stamps import LOCK, OFFERED, STAMP, changes, restamp
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.vector_writes import vector_clauses
from drivine.drivine_error import DrivineError
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel


class PatchStatements:
    """
    Statements that write named properties of nodes found by id, making none. Each node found is locked, then found
    again with the condition, so the condition reads what the last writer committed; it sits in a WHERE, the only
    place Memgraph takes an existence check. A node whose values were already as set keeps its stamp; one that changed
    gets a new data token; one that never had a stamp is given one.
    """

    @staticmethod
    def one(model: FragmentModel, grammar: CypherGrammar, where: str | None = None) -> GraphStatement:
        """Patches one node, the one with id `$id`, setting `$set`. Returns `{found, applied, changed}`."""
        labels = label_expression(model.labels)
        id_ = f"{{{quote_name(_id_property(model))}: $id}}"
        return GraphStatement(
            "\n".join(
                [
                    f"OPTIONAL MATCH (f{labels} {id_})",
                    f"SET f.{LOCK} = true REMOVE f.{LOCK}",
                    "WITH f",
                    f"OPTIONAL MATCH (n{labels} {id_}){f' WHERE {where}' if where else ''}",
                    *PatchStatements._write(model, grammar, "$set", "$vectors"),
                    "RETURN {found: f IS NOT NULL, applied: n IS NOT NULL, changed: changed} AS result",
                ]
            ),
            {"_stamp": stamps.fresh()},
        )

    @staticmethod
    def many(model: FragmentModel, grammar: CypherGrammar) -> GraphStatement:
        """Patches a node for each row of `$rows`, `{id, props}`. Returns `{id, found, changed}` for each row."""
        labels = label_expression(model.labels)
        return GraphStatement(
            "\n".join(
                [
                    "UNWIND $rows AS row",
                    f"OPTIONAL MATCH (f{labels} {{{quote_name(_id_property(model))}: row.id}})",
                    f"SET f.{LOCK} = true REMOVE f.{LOCK}",
                    "WITH row, f, f AS n",
                    *PatchStatements._write(model, grammar, "row.props", "row.vectors", "row"),
                    "RETURN {id: row.id, found: f IS NOT NULL, changed: changed} AS result",
                ]
            ),
            {"_stamp": stamps.fresh()},
        )

    @staticmethod
    def _write(
        model: FragmentModel, grammar: CypherGrammar, props: str, vectors: str, carry: str | None = None
    ) -> list[str]:
        """Writes `props`, and the vectors `vectors` holds in the engine's vector form, where a value differs."""
        kept = f"{carry}, " if carry else ""
        changed = " OR ".join([changes("n", props), *vector_clauses(model, grammar, "n", vectors)[1]])
        sets = " ".join(vector_clauses(model, grammar, "x", vectors)[0])
        return [
            f"WITH {kept}f, n, CASE WHEN n IS NULL THEN false ELSE {changed} END AS changed",
            f"FOREACH (x IN CASE WHEN changed THEN [n] ELSE [] END | "
            f"SET {restamp('x', 'true')} SET x += {props} {sets})",
            f"FOREACH (x IN CASE WHEN f IS NOT NULL AND {stamps.two_tokens_of('f')} IS NULL THEN [f] ELSE [] END | "
            f"SET x.{STAMP} = {OFFERED})",
        ]


def _id_property(model: FragmentModel) -> str:
    if not model.node_id_property:
        raise DrivineError(f"{model.type.__name__} has no NodeId field, so it can't be patched by id.")
    return model.node_id_property
