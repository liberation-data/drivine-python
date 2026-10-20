from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Relationship, Root
from drivine_sample.fragment.organization import Organization
from drivine_sample.fragment.person import Person


class PersonContext(GraphView):
    person: Annotated[Person, Root()]
    works_for: Annotated[list[Organization], Relationship("WORKS_FOR")] = []
