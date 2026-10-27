from drivine import DatabaseType
from drivine.testing.drivine_test_containers import DrivineTestContainers


class TestImage:
    def test_is_the_latest_neo4j_memgraph_and_falkordb_by_default_and_pins_postgres_and_oracle(self):
        assert DrivineTestContainers.image(DatabaseType.NEO4J, {}) == "neo4j:latest"
        assert DrivineTestContainers.image(DatabaseType.MEMGRAPH, {}) == "memgraph/memgraph:latest"
        assert DrivineTestContainers.image(DatabaseType.FALKORDB, {}) == "falkordb/falkordb:latest"
        assert DrivineTestContainers.image(DatabaseType.POSTGRES, {}) == "pgvector/pgvector:pg16"
        assert DrivineTestContainers.image(DatabaseType.ORACLE, {}) == "gvenzl/oracle-free:23-faststart"

    def test_is_the_types_test_image_where_it_is_set_for_that_type_only(self):
        env = {
            "NEO4J_TEST_IMAGE": "neo4j:5.26-community",
            "MEMGRAPH_TEST_IMAGE": "memgraph/memgraph:3.14.0",
            "FALKORDB_TEST_IMAGE": "falkordb/falkordb:6.0.3",
        }
        assert DrivineTestContainers.image(DatabaseType.NEO4J, env) == "neo4j:5.26-community"
        assert DrivineTestContainers.image(DatabaseType.MEMGRAPH, env) == "memgraph/memgraph:3.14.0"
        assert DrivineTestContainers.image(DatabaseType.FALKORDB, env) == "falkordb/falkordb:6.0.3"
        assert DrivineTestContainers.image(DatabaseType.POSTGRES, env) == "pgvector/pgvector:pg16"

    def test_is_the_default_where_the_test_image_is_blank(self):
        assert DrivineTestContainers.image(DatabaseType.NEO4J, {"NEO4J_TEST_IMAGE": ""}) == "neo4j:latest"
