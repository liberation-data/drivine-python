from __future__ import annotations

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import cypher_grammar_for
from drivine.backend.graph_backend import GraphBackend
from drivine.connection.database_type import DatabaseType


def backend_for(type: DatabaseType, graph_name: str | None = None) -> GraphBackend:
    """The backend that writes an engine's statements: on Oracle, over the tables of the graph named."""
    if type is DatabaseType.ORACLE:
        from drivine.backend.oracle.oracle_backend import OracleBackend
        from drivine.backend.oracle.oracle_graph import DEFAULT_GRAPH, OracleGraph

        return OracleBackend(OracleGraph(graph_name or DEFAULT_GRAPH))
    return CypherBackend(cypher_grammar_for(type))
