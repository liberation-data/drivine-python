from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContainerConnection:
    """Where a datasource reaches the store a container runs, and whatever else the container fixes."""

    host: str
    port: int
    user_name: str | None = None
    password: str | None = None
    database_name: str | None = None
