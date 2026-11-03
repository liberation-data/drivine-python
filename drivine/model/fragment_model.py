from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from drivine.model.field_mapping import FieldMapping
from drivine.model.node_labels_model import NodeLabelsModel
from drivine.model.property_bag_model import PropertyBagModel
from drivine.model.schema_declaration import SchemaDeclaration
from drivine.model.vector_field import VectorField


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
    vector_fields: dict[str, VectorField] = field(default_factory=lambda: {})
    """Fields with a vector index, by field: their stored property and similarity."""
    full_text_indexes: list[tuple[str, ...]] = field(default_factory=lambda: [])
    """The full-text indexes the fragment declares, each by its stored property names."""
    schema: list[SchemaDeclaration] = field(default_factory=lambda: [])
    """The schema items the fragment declares, on its fields and as class arguments."""
    lower_copied: list[str] = field(default_factory=lambda: [])
    """
    The stored names of the `CaseInsensitive` fields, in the order of their fields' names, each of which a save gives a
    lowercased copy: see lower_copies.
    """

    def __repr__(self) -> str:
        return f"FragmentModel({self.type.__name__}, labels={self.labels})"
