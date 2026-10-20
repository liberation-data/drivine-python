# Written by scripts/unasync.py from drivine/connection/database_registry.py:
# change that, and run the script again.
from __future__ import annotations

import logging
from collections.abc import Mapping

from drivine.connection.connection_properties import ConnectionProperties
from drivine.sync.connection.connection_provider import ConnectionProvider
from drivine.sync.connection.connection_provider_builder import ConnectionProviderBuilder

logger = logging.getLogger("drivine.connection")

DEFAULT = "default"


class DatabaseRegistry:
    """
    The application's databases, by name. `"default"` is the first one registered.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ConnectionProvider] = {}

    @property
    def providers(self) -> list[ConnectionProvider]:
        return list(self._providers.values())

    def builder(self) -> ConnectionProviderBuilder:
        return ConnectionProviderBuilder(self)

    def build_or_resolve_from_env(
        self, name: str | None = None, env: Mapping[str, str] | None = None
    ) -> ConnectionProvider:
        """
        Registers the database the environment describes, as `DATABASE_TYPE`, `DATABASE_HOST` and so on, or
        `<NAME>_DATABASE_TYPE`… for a named one. A database already registered under the name is returned as it is.
        """
        retained = self.registered(name or DEFAULT)
        if retained is not None:
            return retained
        return self.builder().with_properties(ConnectionProperties.from_env(name, env)).register(name or DEFAULT)

    def connection_provider(self, name: str = DEFAULT) -> ConnectionProvider | None:
        if name == DEFAULT and name not in self._providers:
            return next(iter(self._providers.values()), None)
        return self._providers.get(name)

    def registered(self, name: str) -> ConnectionProvider | None:
        """The provider registered under exactly this name."""
        return self._providers.get(name)

    def register(self, provider: ConnectionProvider) -> None:
        self._providers[provider.name] = provider

    def close(self) -> None:
        """
        Ends every provider: their drivers and pools. Call it once, when the application stops.
        """
        providers = list(self._providers.values())
        self._providers.clear()
        for provider in providers:
            try:
                provider.end()
            except Exception as error:
                logger.warning("Ending %s failed: %s", provider, error)
