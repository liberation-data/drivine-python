from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field

from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy, PortWaitStrategy

from drivine.connection.database_type import DatabaseType

logger = logging.getLogger("drivine.testing")

TEST_CONTAINER_USER = "drivine"
"""The user every container that takes one is made with."""
TEST_CONTAINER_PASSWORD = "drivine-test"
"""At least eight characters, which Neo4j wants."""


@dataclass(frozen=True)
class ContainerConnection:
    """Where a datasource reaches the store a container runs, and whatever else the container fixes."""

    host: str
    port: int
    user_name: str | None = None
    password: str | None = None
    database_name: str | None = None


@dataclass(frozen=True)
class TestContainerSpec:
    """A store's container: how to make it, and how a datasource reaches it once it is started."""

    __test__ = False

    image: str
    port: int
    container: Callable[[str], DockerContainer]
    connection: Callable[[DockerContainer], ContainerConnection]


def _neo4j(image: str) -> DockerContainer:
    return (
        DockerContainer(image)
        .with_env("NEO4J_AUTH", f"neo4j/{TEST_CONTAINER_PASSWORD}")
        .with_exposed_ports(7687)
        .waiting_for(LogMessageWaitStrategy(re.compile(r"Started\.")).with_startup_timeout(120))
    )


def _memgraph(image: str) -> DockerContainer:
    # Memgraph logs that it is running before Bolt takes connections: wait on the port instead.
    return DockerContainer(image).with_exposed_ports(7687).waiting_for(PortWaitStrategy(7687))


def _reach(port: int, user_name: str | None = None, password: str | None = None):
    def connection(started: DockerContainer) -> ContainerConnection:
        return ContainerConnection(
            started.get_container_host_ip(), int(started.get_exposed_port(port)), user_name, password
        )

    return connection


TEST_CONTAINER_SPECS: dict[DatabaseType, TestContainerSpec] = {
    DatabaseType.NEO4J: TestContainerSpec(
        "neo4j:5.26-community", 7687, _neo4j, _reach(7687, "neo4j", TEST_CONTAINER_PASSWORD)
    ),
    DatabaseType.MEMGRAPH: TestContainerSpec("memgraph/memgraph:3.12.0", 7687, _memgraph, _reach(7687)),
}
"""The containers Drivine can start, by the type of the datasource that wants one."""

_TYPE_KEY = re.compile(r"^(?:(.+)_)?DATABASE_TYPE$")


@dataclass
class DrivineTestContainers:
    """
    Test containers for the datasources configured in the environment, as TypeScript's Drivine and drivine4j start
    them.

    For each datasource (`DATABASE_TYPE`, or `<NAME>_DATABASE_TYPE` for a named one) of a type that has a container,
    a container is started, and the datasource's host and port, and whatever else the container fixes, are rewritten
    to reach it. Datasources of one type share one container.

    `USE_LOCAL_<TYPE>=true` starts no container for that type and leaves its datasources as they are, to work against
    an instance of your own. `<TYPE>_TEST_IMAGE` picks another image.
    """

    started: dict[DatabaseType, DockerContainer] = field(default_factory=lambda: {})

    @staticmethod
    def use_local(type: DatabaseType, env: MutableMapping[str, str]) -> bool:
        return env.get(f"USE_LOCAL_{type.value}", "").lower() == "true"

    @staticmethod
    def datasources(env: MutableMapping[str, str]) -> list[tuple[str, DatabaseType]]:
        """The prefix of each datasource in the environment, '' for the default one, with its type."""
        found: list[tuple[str, DatabaseType]] = []
        for key, value in env.items():
            match = _TYPE_KEY.match(key)
            if match is None or not value.strip():
                continue
            try:
                kind = DatabaseType(value.strip().upper())
            except ValueError:
                continue
            found.append((f"{match.group(1)}_" if match.group(1) else "", kind))
        return found

    @classmethod
    def start(cls, env: MutableMapping[str, str] | None = None) -> DrivineTestContainers:
        env = os.environ if env is None else env
        containers = cls()
        try:
            for prefix, kind in cls.datasources(env):
                spec = TEST_CONTAINER_SPECS.get(kind)
                if spec is None or cls.use_local(kind, env):
                    continue
                started = containers.started.get(kind)
                if started is None:
                    image = env.get(f"{kind.value}_TEST_IMAGE") or spec.image
                    logger.info("Starting %s for %s", image, kind.value)
                    started = spec.container(image).start()
                    containers.started[kind] = started
                reach = spec.connection(started)
                env[f"{prefix}DATABASE_HOST"] = reach.host
                env[f"{prefix}DATABASE_PORT"] = str(reach.port)
                for name, value in (
                    ("DATABASE_USER", reach.user_name),
                    ("DATABASE_PASSWORD", reach.password),
                    ("DATABASE_NAME", reach.database_name),
                ):
                    if value is not None:
                        env[f"{prefix}{name}"] = value
        except BaseException:
            containers.stop()
            raise
        return containers

    def stop(self) -> None:
        for kind, container in self.started.items():
            try:
                container.stop()
            except Exception as error:
                logger.warning("Stopping the %s container failed: %s", kind.value, error)
        self.started.clear()
