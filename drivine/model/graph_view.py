from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from drivine.model.property_names import PropertyNames


class GraphView(BaseModel):
    """
    The base of a class that composes a root fragment, marked `Root()`, and the relationships that lead from it into
    one result, loaded by one statement:

        class PersonCareer(GraphView):
            person: Annotated[Person, Root()]
            employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    __drivine_property_names__: ClassVar[PropertyNames | None] = None

    def __init_subclass__(cls, property_names: PropertyNames | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.__drivine_property_names__ = property_names
