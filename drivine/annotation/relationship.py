from dataclasses import dataclass

from drivine.annotation.direction import Direction


@dataclass(frozen=True)
class Relationship:
    """
    Marks a view's field as a relationship from its root: `employment_history: Annotated[list[WorkHistory],
    Relationship("WORKS_FOR")] = []`. What it holds, a node fragment, a relationship fragment or a view, is read from
    the annotation, so is whether it is a list.

    A single relationship typed without `None` is required: only roots that have it are loaded. A list is required
    when `required=True`. `max_depth` is how deep a relationship to the view's own class, or one that leads back to
    it, is expanded.
    """

    type: str
    direction: Direction = Direction.OUTGOING
    max_depth: int = 1
    required: bool = False
