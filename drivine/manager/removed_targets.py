from enum import StrEnum


class RemovedTargets(StrEnum):
    """
    What a save with `replace` does with a node whose relationship it removed.
    """

    KEEP = "KEEP"
    """Keep it. Only the relationship goes."""

    DELETE_UNREFERENCED = "DELETE_UNREFERENCED"
    """
    Delete it, with its other relationships, if nothing else refers to it the way the field did: for an outgoing field,
    no relationship points at it; for an incoming one, it points at nothing; for an undirected one, it has no
    relationship left. The root, and any node the saved object still holds, is never deleted.
    """
