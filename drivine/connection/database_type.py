from enum import StrEnum


class DatabaseType(StrEnum):
    NEO4J = "NEO4J"
    POSTGRES = "POSTGRES"
    NEPTUNE = "NEPTUNE"
    FALKORDB = "FALKORDB"
    MEMGRAPH = "MEMGRAPH"
    ORACLE = "ORACLE"
