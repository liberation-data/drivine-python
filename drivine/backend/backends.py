from __future__ import annotations

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import cypher_grammar_for
from drivine.backend.graph_backend import GraphBackend
from drivine.connection.database_type import DatabaseType


def backend_for(type: DatabaseType, graph_name: str | None = None) -> GraphBackend:
    """
    The backend that writes an engine's statements: SQL/PGQ on Oracle and SQL on Postgres, each over the tables of the
    graph named (`drivine` unless named); Cypher elsewhere.
    """
    if type is DatabaseType.ORACLE:
        from drivine.backend.oracle.oracle_backend import OracleBackend
        from drivine.backend.oracle.oracle_graph import DEFAULT_GRAPH, OracleGraph

        return OracleBackend(OracleGraph(graph_name or DEFAULT_GRAPH))
    if type is DatabaseType.POSTGRES:
        from drivine.backend.postgres.postgres_backend import PostgresBackend
        from drivine.backend.postgres.postgres_graph import PostgresGraph
        from drivine.backend.sql.sql_graph import DEFAULT_GRAPH

        return PostgresBackend(PostgresGraph(graph_name or DEFAULT_GRAPH))
    return CypherBackend(cypher_grammar_for(type))
