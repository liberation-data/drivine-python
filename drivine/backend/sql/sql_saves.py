"""
The walk of an object a SQL store's save makes, beside what save_statement_builder gives every save (the items of a
relationship field, the nodes they hold, and the ids an object holds at any depth).
"""

from __future__ import annotations

from typing import Any

from drivine.backend.save_statement_builder import node_of, target
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.relationship_model import RelationshipModel


def stamp_excluded(model: FragmentModel) -> list[str]:
    """The fields a save writes of a node of the model leave out its stamp, which the store keeps."""
    return [model.stamp_field] if model.stamp_field else []


def fragment_of(rel: RelationshipModel, item: Any) -> Any:
    """The node an item of a relationship field leads to; for a view, its root, which holds the id."""
    node = node_of(rel, item)
    into = target(rel)
    return getattr(node, into.root_field, None) if isinstance(into, GraphViewModel) else node
