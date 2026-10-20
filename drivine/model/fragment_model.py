from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from drivine.model.field_mapping import FieldMapping
from drivine.model.node_labels_model import NodeLabelsModel
from drivine.model.property_bag_model import PropertyBagModel


@dataclass(eq=False)
class FragmentModel(FieldMapping):
    """
    A node fragment: a class whose fields are a node's properties.
    """

    type: Any = None
    labels: list[str] = field(default_factory=lambda: [])
    """The class's own labels first, then those it inherits."""
    node_id_field: str | None = None
    node_id_property: str | None = None
    stamp_field: str | None = None
    node_labels: NodeLabelsModel | None = None
    property_bags: list[PropertyBagModel] = field(default_factory=lambda: [])
    validate_on_load: bool = True

    def __repr__(self) -> str:
        return f"FragmentModel({self.type.__name__}, labels={self.labels})"
