from __future__ import annotations

from dataclasses import dataclass, field

from drivine.model.field_shape import CollectionKind


@dataclass
class FieldMapping:
    """
    How a class's plain fields are stored and loaded: shared by node and relationship fragments.
    """

    graph_properties: dict[str, str] = field(default_factory=lambda: {})
    """Each loaded and saved field, by the property it is stored under."""
    transient_fields: set[str] = field(default_factory=lambda: set())
    """Fields neither loaded nor saved."""
    json_packed: set[str] = field(default_factory=lambda: set())
    """Fields stored as JSON strings."""
    null_as_default: set[str] = field(default_factory=lambda: set())
    """Fields whose default a stored null loads as."""
    empty_when_absent: dict[str, CollectionKind] = field(default_factory=lambda: {})
    """Fields loaded as an empty collection when absent or null, by the collection's kind."""
    collections: dict[str, CollectionKind] = field(default_factory=lambda: {})
    """
    Fields holding a set or a tuple, by its kind. A store holds them as lists, and a load gives the set or tuple, to a
    fragment constructed without validation as to a validated one.
    """

    @property
    def fields_by_property(self) -> dict[str, str]:
        return {name: field for field, name in self.graph_properties.items()}
