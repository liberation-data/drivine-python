from dataclasses import dataclass


@dataclass(frozen=True)
class NodeId:
    """
    Marks the field that identifies a node: `uuid: Annotated[str, NodeId()]`. A fragment has at most one, and needs
    one to be loaded or saved by id.
    """
