from __future__ import annotations

from typing import TypeAlias

from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.relationship_fragment_model import RelationshipFragmentModel

TargetModel: TypeAlias = FragmentModel | GraphViewModel
"""What a statement loads, and what a relationship leads to: a node fragment, or a view."""

RelatedModel: TypeAlias = FragmentModel | GraphViewModel | RelationshipFragmentModel
"""What a view's relationship field holds."""
