from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from drivine.model.aggregate_model import AggregateModel
from drivine.model.fragment_model import FragmentModel
from drivine.model.path_model import PathModel
from drivine.model.relationship_model import RelationshipModel


@dataclass(eq=False)
class GraphViewModel:
    """
    A view: a root fragment, and the relationships, paths and aggregates that lead from it.
    """

    type: Any
    root_field: str
    root: FragmentModel
    relationships: list[RelationshipModel] = field(default_factory=lambda: [])
    paths: list[PathModel] = field(default_factory=lambda: [])
    aggregates: list[AggregateModel] = field(default_factory=lambda: [])

    def __repr__(self) -> str:
        return f"GraphViewModel({self.type.__name__})"
