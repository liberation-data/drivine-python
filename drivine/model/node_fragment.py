from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from drivine.model.property_names import PropertyNames


class NodeFragment(BaseModel):
    """
    The base of a class that maps a node. Its labels are a class argument, and are inherited:

        class Person(NodeFragment, labels=["Person"]):
            uuid: Annotated[str, NodeId()]
            name: str
            bio: str | None = None

    Composite schema items are a class argument too: `schema=[RangeIndex(["a", "b"]), Unique(["c", "d"])]`, over
    field names. Other class arguments: `property_names` ("camel", the default, or "as_written"; see
    `PropertyNames`), and `validate_on_load` (True by default: a loaded node is validated, and with False it is
    constructed as it is).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    __drivine_labels__: ClassVar[tuple[str, ...]] = ()
    __drivine_property_names__: ClassVar[PropertyNames | None] = None
    __drivine_validate_on_load__: ClassVar[bool | None] = None
    __drivine_schema__: ClassVar[tuple[Any, ...]] = ()

    def __init_subclass__(
        cls,
        labels: Iterable[str] = (),
        property_names: PropertyNames | None = None,
        validate_on_load: bool | None = None,
        schema: Iterable[Any] = (),
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if isinstance(labels, str):
            raise TypeError(f"{cls.__name__}: labels is a list of labels, such as labels=[{labels!r}].")
        cls.__drivine_labels__ = tuple(labels)
        cls.__drivine_property_names__ = property_names
        cls.__drivine_validate_on_load__ = validate_on_load
        cls.__drivine_schema__ = tuple(schema)
