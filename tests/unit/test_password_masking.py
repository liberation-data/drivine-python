"""A password Drivine holds is never printed (#503, port of liberation-data/drivine4j#793)."""

from __future__ import annotations

import dataclasses

from drivine.connection.connection_properties import ConnectionProperties
from drivine.connection.database_type import DatabaseType
from drivine.connection.falkordb.falkordb_connection_provider import FalkorDbConnectionProvider
from drivine.connection.masking import masked_password
from drivine.connection.neo4j.neo4j_connection_provider import neo4j_provider
from drivine.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from drivine.connection.postgres.postgres_connection_provider import PostgresConnectionProvider
from drivine.testing.container_connection import ContainerConnection

SECRET = "s3cret-p4ss"


def test_a_password_prints_as_stars_and_none_where_there_is_none():
    assert masked_password(SECRET) == "***"
    assert masked_password(None) is None
    assert masked_password("") == "***"


def test_properties_print_every_field_and_the_password_masked():
    properties = ConnectionProperties(DatabaseType.NEO4J, "localhost", 7687, "neo4j", SECRET, pool_max=4)
    printed = repr(properties)
    assert SECRET not in printed
    assert SECRET not in str(properties)
    assert SECRET not in f"{[properties]}"
    assert "password='***'" in printed
    # Every field, so one added later isn't dropped from what prints.
    for it in dataclasses.fields(properties):
        assert f"{it.name}=" in printed
    assert printed.index("user_name=") < printed.index("password=") < printed.index("idle_timeout=")
    assert properties.password == SECRET


def test_properties_with_no_password_print_none():
    assert "password=None" in repr(ConnectionProperties(DatabaseType.NEO4J, "localhost"))


def test_properties_read_from_the_environment_print_the_password_masked():
    properties = ConnectionProperties.from_env(
        env={"DATABASE_TYPE": "NEO4J", "DATABASE_HOST": "localhost", "DATABASE_PASSWORD": SECRET}
    )
    assert properties.password == SECRET
    assert SECRET not in repr(properties)


def test_a_container_connection_prints_the_password_masked():
    connection = ContainerConnection("localhost", 7687, "neo4j", SECRET)
    assert SECRET not in repr(connection)
    assert "password='***'" in repr(connection)
    assert connection.password == SECRET


def test_no_provider_prints_the_password():
    providers = [
        FalkorDbConnectionProvider("f", "g", "localhost", 6379, "u", SECRET),
        PostgresConnectionProvider("p", "localhost", 5432, "d", "u", SECRET),
        OracleConnectionProvider("o", "localhost:1521/FREEPDB1", "u", SECRET),
        neo4j_provider("n", DatabaseType.NEO4J, "localhost", 7687, "u", SECRET, None, "bolt", None, None),
    ]
    for provider in providers:
        assert SECRET not in repr(provider)
        assert SECRET not in str(provider)
        assert not hasattr(provider, "password")
        # Nor in any public attribute, which something logging `vars(provider)` would print.
        assert all(SECRET not in repr(value) for key, value in vars(provider).items() if not key.startswith("_"))
