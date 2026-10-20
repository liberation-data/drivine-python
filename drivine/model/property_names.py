from __future__ import annotations

from typing import Literal

PropertyNames = Literal["camel", "as_written"]
"""
How a field's name becomes the property it is stored under: `camel`, the default, stores `start_date` as `startDate`,
as TypeScript and JVM models store it; `as_written` stores it as `start_date`.
"""


def graph_name(field: str, naming: PropertyNames) -> str:
    """
    The property a field is stored under. The rule runs one way, from a field to its property.
    """
    if naming == "as_written" or "_" not in field:
        return field
    first, *rest = field.split("_")
    return first + "".join(it[:1].upper() + it[1:] for it in rest if it)
