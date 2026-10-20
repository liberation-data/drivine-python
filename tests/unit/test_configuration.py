import pytest

from drivine import (
    ConnectionProperties,
    CursorSpecification,
    DatabaseRegistry,
    DatabaseType,
    DrivineError,
    QuerySpecification,
    TransactionMode,
    cypher_file,
    cypher_statement,
)
from drivine.query.compiler import CypherSpecCompiler


class TestConnectionProperties:
    def test_reads_the_default_datasource(self):
        properties = ConnectionProperties.from_env(
            env={"DATABASE_TYPE": "NEO4J", "DATABASE_HOST": "db", "DATABASE_PORT": "7688", "DATABASE_USER": "neo4j"}
        )
        assert properties == ConnectionProperties(DatabaseType.NEO4J, "db", port=7688, user_name="neo4j")

    def test_reads_a_named_datasource(self):
        env = {
            "ANALYTICS_DATABASE_TYPE": "memgraph",
            "ANALYTICS_DATABASE_HOST": "graph",
            "ANALYTICS_DATABASE_TRANSACTION_MODE": "strict",
            "DATABASE_TYPE": "NEO4J",
        }
        properties = ConnectionProperties.from_env("ANALYTICS", env)
        assert properties.database_type is DatabaseType.MEMGRAPH
        assert properties.host == "graph"
        assert properties.transaction_mode is TransactionMode.STRICT

    @pytest.mark.parametrize(
        "env, message",
        [
            ({"DATABASE_HOST": "db"}, "DATABASE_TYPE for named connection is required"),
            ({"DATABASE_TYPE": "NEO4J"}, "DATABASE_HOST for named connection is required"),
            ({"DATABASE_TYPE": "GREMLIN", "DATABASE_HOST": "db"}, "DATABASE_TYPE must be one of"),
            ({"DATABASE_TYPE": "NEO4J", "DATABASE_HOST": "db", "DATABASE_PORT": "x"}, "must be a whole number"),
            ({"DATABASE_TYPE": "ORACLE", "DATABASE_HOST": "db"}, "DATABASE_NAME for named connection is required"),
            (
                {"DATABASE_TYPE": "NEO4J", "DATABASE_HOST": "db", "DATABASE_TRANSACTION_MODE": "LAX"},
                "DATABASE_TRANSACTION_MODE must be one of",
            ),
        ],
    )
    def test_refuses_what_is_missing_or_malformed(self, env, message):
        with pytest.raises(DrivineError, match=message):
            ConnectionProperties.from_env(env=env)


class TestDatabaseRegistry:
    def test_default_is_the_first_registered(self):
        registry = DatabaseRegistry()
        first = registry.build_or_resolve_from_env(
            "graph", {"graph_DATABASE_TYPE": "MEMGRAPH", "graph_DATABASE_HOST": "localhost"}
        )
        assert registry.connection_provider() is first
        assert registry.connection_provider("graph") is first
        assert registry.connection_provider("other") is None

    def test_registering_a_name_again_returns_the_provider_registered(self):
        registry = DatabaseRegistry()
        env = {"DATABASE_TYPE": "NEO4J", "DATABASE_HOST": "localhost", "DATABASE_USER": "neo4j"}
        assert registry.build_or_resolve_from_env(env=env) is registry.build_or_resolve_from_env(env=env)

    def test_neo4j_requires_a_user(self):
        with pytest.raises(DrivineError, match="Neo4j requires a user name"):
            DatabaseRegistry().builder().database_type(DatabaseType.NEO4J).host("localhost").register()

    def test_an_engine_not_ported_yet_says_so(self):
        builder = DatabaseRegistry().builder().database_type(DatabaseType.FALKORDB).host("localhost")
        with pytest.raises(DrivineError, match="not supported by drivine-python yet"):
            builder.register()


class TestQuerySpecification:
    def test_compiles_positional_parameters_by_position(self):
        spec = QuerySpecification("MATCH (n) WHERE n.a = $1 AND n.b = $2 RETURN n").bind(["x", 2])
        compiled = CypherSpecCompiler(spec.finalized_copy("CYPHER")).compile()
        assert compiled.parameters == {"1": "x", "2": 2}

    def test_compiles_skip_and_limit_into_the_statement(self):
        spec = QuerySpecification("MATCH (n) RETURN n").skip(10).limit(5)
        assert (
            CypherSpecCompiler(spec.finalized_copy("CYPHER")).compile().statement
            == "MATCH (n) RETURN n SKIP 10 LIMIT 5"
        )

    def test_a_limit_of_zero_is_written(self):
        spec = QuerySpecification("MATCH (n) RETURN n").limit(0)
        assert CypherSpecCompiler(spec.finalized_copy("CYPHER")).compile().statement.endswith("LIMIT 0")

    @pytest.mark.parametrize("value", [-1, 1.5, "10", True])
    def test_refuses_skip_and_limit_that_are_not_whole_numbers(self, value):
        with pytest.raises(ValueError, match="whole number"):
            QuerySpecification("RETURN 1").limit(value)
        with pytest.raises(ValueError, match="whole number"):
            QuerySpecification("RETURN 1").skip(value)

    def test_refuses_sql_on_a_cypher_engine(self):
        spec = QuerySpecification().with_statement(cypher_statement("RETURN 1"))
        assert CypherSpecCompiler(spec.finalized_copy("SQL")).statement.language == "CYPHER"
        from drivine import sql_statement

        with pytest.raises(DrivineError, match="SQL is not supported"):
            CypherSpecCompiler(QuerySpecification(sql_statement("SELECT 1")).finalized_copy("CYPHER"))

    def test_finalized_copy_leaves_the_original_as_it_was(self):
        spec = CursorSpecification("RETURN 1").batch_size(7).map(lambda it: it)
        copy = spec.finalized_copy("CYPHER")
        copy.filter(lambda it: True)
        assert spec.statement is not None and spec.statement.language == "PLATFORM_DEFAULT"
        assert len(spec.post_processors) == 1
        assert isinstance(copy, CursorSpecification) and copy.batch == 7

    def test_batch_size_must_be_positive(self):
        with pytest.raises(ValueError):
            CursorSpecification("RETURN 1").batch_size(0)


def test_reads_a_statement_from_a_package():
    statement = cypher_file("tests.unit.statements", "person_named")
    assert statement.language == "CYPHER"
    assert statement.text.startswith("MATCH (p:Person {name: $name})")
    assert cypher_file("tests.unit.statements", "person_named.cypher") == statement
