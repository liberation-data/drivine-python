from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from drivine.model.property_names import PropertyNames


class RelationshipFragment(BaseModel):
    """
    The base of a class that holds a relationship's properties, and the node it leads to, marked `TargetNode()`:

        class WorkHistory(RelationshipFragment):
            start_date: str
            role: str
            target: Annotated[Organization, TargetNode()]
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    __drivine_property_names__: ClassVar[PropertyNames | None] = None

    def __init_subclass__(cls, property_names: PropertyNames | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.__drivine_property_names__ = property_names
