from __future__ import annotations

from typing import TYPE_CHECKING

from drivine.connection.database_registry import DEFAULT, DatabaseRegistry
from drivine.drivine_error import DrivineError
from drivine.manager.delegating_persistence_manager import DelegatingPersistenceManager
from drivine.manager.non_transactional_persistence_manager import NonTransactionalPersistenceManager
from drivine.manager.persistence_manager import PersistenceManager
from drivine.manager.persistence_manager_type import PersistenceManagerType
from drivine.manager.transactional_persistence_manager import TransactionalPersistenceManager
from drivine.schema.schema_ddl import STORES

if TYPE_CHECKING:
    from drivine.schema.schema_commands import SchemaCommands


class PersistenceManagerFactory:
    """
    Makes the persistence managers for each registered database, once.
    """

    def __init__(self, registry: DatabaseRegistry) -> None:
        self.registry = registry
        self._managers: dict[str, DelegatingPersistenceManager] = {}
        self._schema_commands: dict[str, SchemaCommands] = {}
        """
        The schema commands of each SQL store's database, by its registered name: one for each database, so that the
        managers asked for by `"default"` and by the database's own name run DDL with one lock and one record of what
        DDL ensured.
        """

    def get(self, database: str = DEFAULT, type: PersistenceManagerType = "DELEGATING") -> PersistenceManager:
        manager = self._managers.get(database)
        if manager is None:
            provider = self.registry.connection_provider(database)
            if provider is None:
                raise DrivineError(f"No database is registered under name: {database}")
            non_transactional = NonTransactionalPersistenceManager(provider, database)
            # Neo4j schema DDL given as Cypher to a SQL store is applied through the schema machinery, by the commands
            # of the database however it is asked for.
            if provider.type in STORES:
                commands = self._schema_commands.get(provider.name)
                if commands is None:
                    from drivine.schema.schema_commands import SchemaCommands

                    commands = SchemaCommands.of(self, provider.name)
                    self._schema_commands[provider.name] = commands
                non_transactional.schema_commands = commands
            manager = DelegatingPersistenceManager(
                TransactionalPersistenceManager(provider, database), non_transactional
            )
            self._managers[database] = manager
        if type == "DELEGATING":
            return manager
        if type == "TRANSACTIONAL":
            return manager.transactional
        if type == "NON_TRANSACTIONAL":
            return manager.non_transactional
        raise DrivineError(f"Invalid PersistenceManagerType: {type}")
