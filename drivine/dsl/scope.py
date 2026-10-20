from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.property_bag_model import PropertyBagModel
from drivine.model.relationship_fragment_model import RelationshipFragmentModel


@dataclass(frozen=True)
class Scope:
    """Where a query's path stands: on a view, a fragment, a property bag, or a relationship fragment."""

    kind: Literal["VIEW", "FRAGMENT", "BAG", "REL_FRAGMENT"]
    model: GraphViewModel | FragmentModel | RelationshipFragmentModel
    bag: PropertyBagModel | None = None
