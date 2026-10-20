from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from drivine.connection.database_type import DatabaseType
from drivine.connection.transaction_mode import TransactionMode
from drivine.drivine_error import DrivineError


@dataclass(frozen=True)
class ConnectionProperties:
    database_type: DatabaseType
    host: str
    port: int | None = None
    user_name: str | None = None
    password: str | None = None
    idle_timeout: int | None = None
    """Milliseconds."""
    connection_timeout: int | None = None
    """Milliseconds."""
    pool_max: int | None = None
    database_name: str | None = None
    protocol: str | None = None
    transaction_mode: TransactionMode | None = None
    """For engines without multi-statement transactions. Defaults to WARN. See `TransactionMode`."""
    default_graph_path: str | None = None
    """
    On Oracle, the graph to work on: it names the property graph and its tables. Defaults to `drivine`. drivine4j's
    `default-graph-path`.
    """

    @classmethod
    def from_env(cls, connection_name: str | None = None, env: Mapping[str, str] | None = None) -> ConnectionProperties:
        """
        The properties of the named (or default) connection in the environment, read from the variables TypeScript's
        Drivine and drivine4j read: `DATABASE_TYPE`, `DATABASE_HOST` and so on, each with the prefix `<NAME>_` for a
        named connection, as the name is written or in upper case.
        """
        env = os.environ if env is None else env
        prefix = f"{connection_name}_" if connection_name else ""
        # A database registered as "analytics" reads ANALYTICS_DATABASE_TYPE, unless analytics_DATABASE_TYPE is set.
        if prefix and f"{prefix}DATABASE_TYPE" not in env and f"{prefix.upper()}DATABASE_TYPE" in env:
            prefix = prefix.upper()

        def text(name: str) -> str | None:
            return env.get(f"{prefix}{name}") or None

        def number(name: str) -> int | None:
            setting = text(name)
            if setting is None:
                return None
            try:
                return int(setting)
            except ValueError:
                raise DrivineError(f"{prefix}{name} must be a whole number, not {setting!r}.") from None

        database_type = text("DATABASE_TYPE")
        host = text("DATABASE_HOST")
        if not database_type:
            raise DrivineError(f"{prefix}DATABASE_TYPE for named connection is required.")
        if not host:
            raise DrivineError(f"{prefix}DATABASE_HOST for named connection is required.")
        try:
            kind = DatabaseType(database_type.upper())
        except ValueError:
            raise DrivineError(
                f"{prefix}DATABASE_TYPE must be one of: {', '.join(DatabaseType)}, not {database_type!r}."
            ) from None
        mode = text("DATABASE_TRANSACTION_MODE")
        try:
            transaction_mode = TransactionMode(mode.upper()) if mode else None
        except ValueError:
            raise DrivineError(
                f"{prefix}DATABASE_TRANSACTION_MODE must be one of: {', '.join(TransactionMode)}."
            ) from None
        database_name = text("DATABASE_NAME")
        if kind in (DatabaseType.POSTGRES, DatabaseType.ORACLE) and not database_name:
            raise DrivineError(f"{prefix}DATABASE_NAME for named connection is required.")

        return cls(
            database_type=kind,
            host=host,
            port=number("DATABASE_PORT"),
            user_name=text("DATABASE_USER"),
            password=text("DATABASE_PASSWORD"),
            idle_timeout=number("DATABASE_IDLE_TIMEOUT"),
            connection_timeout=number("DATABASE_CONNECTION_TIMEOUT"),
            pool_max=number("DATABASE_POOL_MAX"),
            database_name=database_name,
            protocol=text("DATABASE_PROTOCOL"),
            transaction_mode=transaction_mode,
            default_graph_path=text("DATABASE_DEFAULT_GRAPH_PATH"),
        )
