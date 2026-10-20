from dataclasses import dataclass


@dataclass(frozen=True)
class NodeStamp:
    """
    Marks the field that holds the node's stamp, which a save checks to refuse overwriting a newer write:
    `stamp: Annotated[str | None, NodeStamp()] = None`. Stored as `__drivine.stamp`, as TypeScript and drivine4j
    store it.
    """
