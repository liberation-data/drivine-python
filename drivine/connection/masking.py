"""
Keeps a password out of what Drivine's objects print: their `repr` gives it as `'***'`, or `None` where there is none,
as drivine4j's `ConnectionProperties.toString` does (liberation-data/drivine-python#503). The value itself is kept, so a
provider still connects with it.
"""

from __future__ import annotations

import dataclasses
from typing import Any

MASK = "***"


def masked_password(password: object) -> str | None:
    """A password as it is printed: `'***'`, an empty one too as in drivine4j, or `None` where there is none."""
    return None if password is None else MASK


def masked_repr(instance: Any) -> str:
    """A dataclass's `repr` as the generated one gives it, each field in order, its `password` masked."""

    def shown(name: str) -> str:
        value = getattr(instance, name)
        return repr(masked_password(value) if name == "password" else value)

    fields = ", ".join(
        f"{it.name}={shown(it.name)}" for it in dataclasses.fields(instance) if it.repr or it.name == "password"
    )
    return f"{type(instance).__name__}({fields})"
