from pathlib import Path

from drivine import DatabaseType
from drivine.testing.drivine_test_containers import TEST_CONTAINER_SPECS, DrivineTestContainers


class TestImage:
    def test_is_the_latest_neo4j_memgraph_and_falkordb_by_default_and_pins_postgres_and_oracle(self):
        assert DrivineTestContainers.image(DatabaseType.NEO4J, {}) == "neo4j:latest"
        assert DrivineTestContainers.image(DatabaseType.MEMGRAPH, {}) == "memgraph/memgraph:latest"
        assert DrivineTestContainers.image(DatabaseType.FALKORDB, {}) == "falkordb/falkordb:latest"
        assert DrivineTestContainers.image(DatabaseType.POSTGRES, {}) == "pgvector/pgvector:pg16"
        assert (
            DrivineTestContainers.image(DatabaseType.ORACLE, {})
            == "gvenzl/oracle-free:23-faststart@sha256:33322969a450d72c507b08d7f690d9e1d9b60aebaebc35a75b7c0b8ab4baac77"
        )

    def test_is_the_types_test_image_where_it_is_set_for_that_type_only(self):
        env = {
            "NEO4J_TEST_IMAGE": "neo4j:5.26-community",
            "MEMGRAPH_TEST_IMAGE": "memgraph/memgraph:3.14.0",
            "FALKORDB_TEST_IMAGE": "falkordb/falkordb:6.0.3",
            "ORACLE_TEST_IMAGE": "gvenzl/oracle-free:23-faststart",
        }
        assert DrivineTestContainers.image(DatabaseType.NEO4J, env) == "neo4j:5.26-community"
        assert DrivineTestContainers.image(DatabaseType.MEMGRAPH, env) == "memgraph/memgraph:3.14.0"
        assert DrivineTestContainers.image(DatabaseType.FALKORDB, env) == "falkordb/falkordb:6.0.3"
        assert DrivineTestContainers.image(DatabaseType.ORACLE, env) == "gvenzl/oracle-free:23-faststart"
        assert DrivineTestContainers.image(DatabaseType.POSTGRES, env) == "pgvector/pgvector:pg16"

    def test_is_the_default_where_the_test_image_is_blank(self):
        assert DrivineTestContainers.image(DatabaseType.NEO4J, {"NEO4J_TEST_IMAGE": ""}) == "neo4j:latest"

    def test_ci_pulls_the_oracle_image_the_container_runs(self):
        # ci.yml pulls the image ahead of the tests, so that its pull isn't counted against the container's start: a pin
        # moved in one place and not the other would pull an image the tests don't run.
        ci = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text()
        assert f"docker pull {TEST_CONTAINER_SPECS[DatabaseType.ORACLE].image}\n" in ci
