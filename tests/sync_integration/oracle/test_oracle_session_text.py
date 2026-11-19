# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_session_text.py:
# change that, and run the script again.
"""
Compiled Cypher compares text by its code points on Oracle whatever `NLS_COMP` and `NLS_SORT` the session was given
before Drivine took it (#566, drivine-cypher-spec#372). A session set to a linguistic sort such as `BINARY_CI` would
have `'B' < 'a'` false and `'a' = 'A'` true, with no error: each session Drivine's pool opens runs the statement the
shared module states for it first. Here a logon trigger sets each session of the tests' user so, as a database or a DBA
might, and a provider of the tests' own opens new sessions under it.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import oracledb
import pytest

from drivine.backend.store_statements import store_statements
from drivine.query.statement import cypher_statement
from drivine.sync import ConnectionProperties, QuerySpecification
from drivine.sync.connection.connection import Connection
from drivine.sync.connection.oracle.oracle_connection_provider import OracleConnectionProvider
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE

pytestmark = [ON_ORACLE]

LINGUISTIC = "ALTER SESSION SET NLS_COMP = LINGUISTIC NLS_SORT = BINARY_CI"

TRIGGER = "drivine_test_linguistic_logon"

COMPARED = "RETURN {lt: 'B' < 'a', eq: 'a' = 'A'}"
"""Text compared as Cypher compares it: by its code points, case and all."""

AS_CYPHER = {"lt": True, "eq": False}


def dsn(properties: ConnectionProperties) -> str:
    return f"{properties.host}:{properties.port or 1521}/{properties.database_name}"


@pytest.fixture
def linguistic_logon() -> Iterator[ConnectionProperties]:
    """Each session the tests' user opens is set to a linguistic, case-blind sort as it logs on, until the test ends."""
    properties = ConnectionProperties.from_env("oracle")
    admin = oracledb.connect(user=properties.user_name, password=properties.password, dsn=dsn(properties))
    try:
        cursor = admin.cursor()
        cursor.execute(
            f"CREATE OR REPLACE TRIGGER {TRIGGER} AFTER LOGON ON SCHEMA BEGIN EXECUTE IMMEDIATE '{LINGUISTIC}'; END;"
        )
        try:
            yield properties
        finally:
            cursor.execute(f"DROP TRIGGER {TRIGGER}")
    finally:
        admin.close()


@pytest.fixture
def provider(linguistic_logon: ConnectionProperties) -> Iterator[OracleConnectionProvider]:
    """A provider of the tests' graph, with a pool of its own: each session it gives out logs on under the trigger."""
    properties = linguistic_logon
    it = OracleConnectionProvider(
        "oracle_session_text", dsn(properties), properties.user_name, properties.password, graph="drivine"
    )
    yield it
    it.end()


def nls(connection: Connection) -> list[Any]:
    """The session's `NLS_COMP` and `NLS_SORT`."""
    rows = connection.query(
        QuerySpecification[Any](
            "SELECT parameter || '=' || value FROM nls_session_parameters "
            "WHERE parameter IN ('NLS_COMP', 'NLS_SORT') ORDER BY parameter"
        )
    )
    return [next(iter(it.values())) if isinstance(it, dict) else it for it in rows]


def text_compared(connection: Connection, label: str) -> list[Any]:
    """Text compared, ordered and found by a compiled statement: a literal, node properties, an order and a lookup."""
    ordered = connection.query(
        QuerySpecification[Any](cypher_statement(f"MATCH (n:{label}) WITH n ORDER BY n.name RETURN collect(n.name)"))
    )
    found = connection.query(
        QuerySpecification[Any](cypher_statement(f"MATCH (n:{label} {{name: 'a'}}) RETURN count(n)"))
    )
    compared = connection.query(QuerySpecification[Any](cypher_statement(COMPARED)))
    return [compared[0], ordered[0], found[0]]


@pytest.fixture
def connection(provider: OracleConnectionProvider) -> Iterator[tuple[Connection, str]]:
    """A connection of the provider, and the label of three nodes named 'B', 'a' and 'A' made for the test."""
    label = f"NlsText_{uuid.uuid4().hex[:8]}"
    it = provider.connect()
    try:
        it.query(
            QuerySpecification[Any](
                cypher_statement(f"UNWIND ['B', 'a', 'A'] AS name CREATE (:{label} {{id: name, name: name}})")
            )
        )
        yield it, label
        it.query(QuerySpecification[Any](cypher_statement(f"MATCH (n:{label}) DELETE n")))
    finally:
        it.release()


def test_the_logon_sets_each_new_session_of_the_user_to_compare_text_linguistically(
    linguistic_logon: ConnectionProperties,
):
    # The trigger is in force: a session Drivine hasn't set compares case-blind.
    properties = linguistic_logon
    plain = oracledb.connect(user=properties.user_name, password=properties.password, dsn=dsn(properties))
    try:
        cursor = plain.cursor()
        cursor.execute("SELECT CASE WHEN 'a' = 'A' THEN 1 ELSE 0 END FROM dual")
        assert cursor.fetchone() == (1,)
    finally:
        plain.close()


@pytest.mark.skipif(
    not store_statements("oracle", "drivine").session,
    reason="The shared compiler states no Oracle session before 0.51.0 (drivine-cypher-spec#372): #566 is unverified.",
)
def test_a_session_a_logon_set_to_a_linguistic_sort_compares_text_by_its_code_points(
    connection: tuple[Connection, str],
):
    it, label = connection
    assert nls(it) == ["NLS_COMP=BINARY", "NLS_SORT=BINARY"]
    assert text_compared(it, label) == [AS_CYPHER, ["A", "B", "a"], 1]


def test_the_same_comparison_is_linguistic_in_a_session_set_so_after_drivine_took_it(
    connection: tuple[Connection, str],
):
    # What the session statement prevents, so the test above tests it: the comparison and the lookup read the session's
    # sort (the order here doesn't, nor `=` of two literals).
    it, label = connection
    it.query(QuerySpecification[Any](LINGUISTIC))
    assert nls(it) == ["NLS_COMP=LINGUISTIC", "NLS_SORT=BINARY_CI"]
    compared, _, found = text_compared(it, label)
    assert compared["lt"] is False
    assert found == 2
