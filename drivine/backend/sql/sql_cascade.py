from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from drivine.annotation.direction import Direction
from drivine.backend.cascade_beyond import CascadeBeyond
from drivine.backend.sql.sql_graph import SqlGraph, sql_text
from drivine.model.relationship_model import RelationshipModel


@dataclass
class SqlCascade:
    """
    What a cascade reaches from its roots: queries of the ids of the nodes, each relationship followed to them, and for
    each relationship to views of its own class the query of the nodes one level below its max_depth, which the
    cascade would leave behind.
    """

    reached: list[str] = field(default_factory=lambda: [])
    followed: list[RelationshipModel] = field(default_factory=lambda: [])
    beyond: list[CascadeBeyond] = field(default_factory=lambda: [])


def kept_targets(graph: SqlGraph, followed: Sequence[RelationshipModel], union: Literal["UNION", "UNION ALL"]) -> str:
    """
    The nodes of `cascade_targets` a `DELETE_ORPHAN` keeps, as the body of a recursive query named `cascade_kept`. A
    node is kept when a node outside the cascade (`cascade_all`, the roots and the targets) is related to it, or when
    it is one step of a relationship the cascade follows below a kept one: a kept message keeps its attachments, and
    its session is still deleted. What is kept is read before anything is deleted, so a node's relationships to the
    others the cascade takes do not keep it. `union` joins the two: `UNION` where the store reads a recursive query so,
    which keeps each node once, and `UNION ALL` where it must.
    """
    # The far end of the edge e from the kept node k.
    far = "CASE WHEN e.src = k.nid THEN e.dst ELSE e.src END"
    steps: list[RelationshipModel] = []
    for rel in followed:
        if not any(it.type == rel.type and it.direction is rel.direction for it in steps):
            steps.append(rel)

    def holds(rel: RelationshipModel) -> str:
        typed = f"e.type = {sql_text(rel.type)}"
        if rel.direction is Direction.INCOMING:
            return f"({typed} AND e.dst = k.nid)"
        if rel.direction is Direction.UNDIRECTED:
            return f"({typed} AND (e.src = k.nid OR e.dst = k.nid))"
        return f"({typed} AND e.src = k.nid)"

    return "\n    ".join(
        [
            f"SELECT t.nid FROM cascade_targets t WHERE EXISTS (SELECT 1 FROM {graph.edges} e "
            "WHERE (e.src = t.nid AND e.dst NOT IN (SELECT nid FROM cascade_all)) "
            "OR (e.dst = t.nid AND e.src NOT IN (SELECT nid FROM cascade_all)))",
            union,
            f"SELECT {far} FROM cascade_kept k JOIN {graph.edges} e ON {' OR '.join(holds(it) for it in steps)} "
            f"WHERE {far} IN (SELECT nid FROM cascade_targets)",
        ]
    )


def beyond_count(roots: str, within: Sequence[str], beyond: str) -> str:
    """
    The read that counts the nodes `beyond` selects, one level below a relationship's max_depth, that are not nodes of
    the view the cascade takes otherwise (`within`) nor its roots (`roots`, the query of the roots' ids): those the
    delete would leave behind. `within` and `beyond` read the roots from `cascade_roots`. Returns one number:
    `{beyond}`.
    """
    within_text = "\n    UNION ALL ".join(within)
    return "\n".join(
        [
            f"WITH cascade_roots AS ({roots}),",
            f"cascade_levels AS (SELECT t.nid FROM ({within_text}) t)",
            f'SELECT CAST(COUNT(DISTINCT b.nid) AS integer) AS "beyond" FROM ({beyond}) b',
            "WHERE b.nid NOT IN (SELECT nid FROM cascade_levels) AND b.nid NOT IN (SELECT nid FROM cascade_roots)",
        ]
    )
