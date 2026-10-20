from dataclasses import dataclass


@dataclass(frozen=True)
class TargetNode:
    """
    Marks the field of a relationship fragment that holds the node the relationship leads to: a node fragment or a
    view.
    """
