from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from testcontainers.core.container import DockerContainer

from drivine.testing.container_connection import ContainerConnection


@dataclass(frozen=True)
class TestContainerSpec:
    """A store's container: how to make it, and how a datasource reaches it once it is started."""

    __test__ = False

    image: str
    port: int
    container: Callable[[str], DockerContainer]
    connection: Callable[[DockerContainer], ContainerConnection]
    ready: Callable[[ContainerConnection], None] | None = None
    """Waits until the store takes statements, where a log line or an open port doesn't say so."""
