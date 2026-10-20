from enum import StrEnum


class NullPolicy(StrEnum):
    """
    How a field that is None is written when an object is saved. It governs the object's own fields, and for a view
    its root's; a node reached through a relationship is always written as under IGNORE.
    """

    IGNORE = "IGNORE"
    """The default. Only fields that aren't None are written; a field that is None leaves the stored value."""

    CLEAR = "CLEAR"
    """
    The object is authoritative: a field assigned None clears the stored property. A field left at a default of None,
    never assigned, isn't written: Pydantic's `model_fields_set` tells the two apart.
    """
