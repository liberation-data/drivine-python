from enum import StrEnum


class DatabaseType(StrEnum):
    NEO4J = "NEO4J"
    POSTGRES = "POSTGRES"
    NEPTUNE = "NEPTUNE"
    FALKORDB = "FALKORDB"
    MEMGRAPH = "MEMGRAPH"
    ORACLE = "ORACLE"


class TransactionMode(StrEnum):
    """
    How a connection to an engine without multi-statement transactions (such as FalkorDB) handles transaction
    boundaries. Engines that do support transactions ignore it.
    """

    WARN = "WARN"
    """
    Default. Starting and committing a transaction do nothing, and rolling back logs a warning: every statement has
    already been committed as it ran.
    """

    STRICT = "STRICT"
    """
    Starting a transaction raises, so a `@transactional` method fails at its first statement. Use this to make sure no
    code path relies on multi-statement transactions against the engine.
    """
