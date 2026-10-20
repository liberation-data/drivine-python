from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from drivine.model.field_mapping import FieldMapping

if TYPE_CHECKING:
    from drivine.model.target_model import TargetModel


@dataclass(eq=False)
class RelationshipFragmentModel(FieldMapping):
    """
    A relationship fragment: a class holding a relationship's properties and the node it leads to.
    """

    type: Any = None
    target_field: str = ""
    target: TargetModel | None = None

    def __repr__(self) -> str:
        return f"RelationshipFragmentModel({self.type.__name__})"
