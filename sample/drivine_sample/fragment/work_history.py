from __future__ import annotations

from typing import Annotated

from drivine import JsonPacked, RelationshipFragment, TargetNode
from drivine_sample.fragment.organization import Organization


class WorkHistory(RelationshipFragment):
    """
    A relationship fragment: the properties of a WORKS_FOR relationship, when someone started and in what role, and the
    organization it leads to. A start date is kept as ISO text (YYYY-MM-DD), as TypeScript's sample keeps it.
    """

    start_date: str
    role: str
    tags: Annotated[list[str] | None, JsonPacked()] = None
    target: Annotated[Organization, TargetNode()]
