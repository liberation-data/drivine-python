from __future__ import annotations

from dataclasses import dataclass, field

from drivine.connection.masking import masked_repr


@dataclass(frozen=True)
class ContainerConnection:
    """Where a datasource reaches the store a container runs, and whatever else the container fixes."""

    host: str
    port: int
    user_name: str | None = None
    password: str | None = field(default=None, repr=False)
    database_name: str | None = None

    def __repr__(self) -> str:
        """Every field, the password as `'***'` (or `None`), so a container's reach can be logged (#503)."""
        return masked_repr(self)
