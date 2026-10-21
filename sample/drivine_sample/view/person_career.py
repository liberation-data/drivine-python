from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Relationship, Root
from drivine_sample.fragment.person import Person
from drivine_sample.fragment.work_history import WorkHistory


class PersonCareer(GraphView):
    """A person with the properties of each WORKS_FOR relationship and the organization it leads to."""

    person: Annotated[Person, Root()]
    employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []
