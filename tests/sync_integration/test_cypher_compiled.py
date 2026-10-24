# Written by scripts/unasync.py from tests/integration/test_cypher_compiled.py:
# change that, and run the script again.
"""
What the Cypher spec (tests/cypher) can't say, on Neo4j and on the SQL stores Cypher is compiled for: values that change
on every run (a UUID, the time), parameters of Python's temporal types, statements built in code, a cursor, and what
needs a transaction of its own. drivine4j keeps the same in its Cypher*CrossEngineTest.
"""

import dataclasses
import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel

from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.model import stamps
from drivine.query.statement import cypher_statement
from drivine.sync import (
    ConnectionProperties,
    CursorSpecification,
    DatabaseRegistry,
    DatabaseType,
    DrivineError,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    transaction,
)
from tests.sync_integration.conftest import ORACLE, POSTGRES, SQL_STORES

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def spec(cypher: str, parameters: dict[str, Any] | None = None) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(cypher)).bind(parameters)


def clear(pm: PersistenceManager) -> None:
    if pm.type is DatabaseType.NEO4J:
        pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    else:
        pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
        pm.execute(QuerySpecification("DELETE FROM drivine_node"))


def test_random_uuid_gives_a_new_uuid_each_time(cypher_pm: PersistenceManager):
    a, b = cypher_pm.query(spec("UNWIND [1, 2] AS x RETURN randomUUID()"))
    assert a != b and UUID.fullmatch(a), (a, b)


def test_a_day_and_a_moment_are_their_iso_text_on_a_sql_store(cypher_pm: PersistenceManager):
    day, moment = cypher_pm.get_one(spec("RETURN [date(), localdatetime()]"))
    if cypher_pm.type is DatabaseType.NEO4J:
        # A graph engine's driver gives a day and a moment as values of its own: the standard library's here.
        assert isinstance(day, date) and isinstance(moment, datetime)
        return
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day), day
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6})?", moment), moment


def test_a_statement_of_hundreds_of_creates_each_naming_nodes_made_before_it_makes_them_all(
    cypher_pm: PersistenceManager,
):
    clear(cypher_pm)
    clauses = [
        f"CREATE (n{i}:CyWrite {{i: {i}}})" + (f" CREATE (n{i - 1})-[:NEXT]->(n{i})" if i else "") for i in range(300)
    ]
    cypher_pm.query(spec("\n".join(clauses)))
    counted = "MATCH (n:CyWrite) OPTIONAL MATCH (n)-[r:NEXT]->() RETURN [count(DISTINCT n), count(r)]"
    assert cypher_pm.query(spec(counted)) == [[300, 299]]


def test_a_write_whose_text_holds_the_quote_its_function_is_made_in_runs(cypher_pm: PersistenceManager):
    # On Postgres a write is a function, its body quoted with dollars: a text in it of that quote must not end it.
    clear(cypher_pm)
    assert cypher_pm.query(spec("CREATE (n:CyWrite {name: 'a$cy$b'}) RETURN n.name")) == ["a$cy$b"]


def test_drivines_stamp_is_written_as_a_nodes_property_is_and_read_back_as_one(cypher_pm: PersistenceManager):
    clear(cypher_pm)
    stamp = f"`{stamps.PROPERTY}`"
    cypher_pm.query(spec(f"CREATE (:CyWrite {{id: 1, {stamp}: 'aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb'}})"))
    read = spec(f"MATCH (n:CyWrite {{id: 1}}) RETURN n.{stamp}")
    assert cypher_pm.query(read) == ["aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb"]
    cypher_pm.query(spec(f"MATCH (n:CyWrite {{id: 1}}) SET {stamps.set_clause('n')}"))
    token, links = (cypher_pm.get_one(read)).split(":")
    assert len(token) == 16 and token != "aaaaaaaaaaaaaaaa" and links == "bbbbbbbbbbbbbbbb", (token, links)
    properties = cypher_pm.query(spec("MATCH (n:CyWrite {id: 1}) RETURN properties(n)"))
    assert properties == [{"id": 1, stamps.PROPERTY: f"{token}:{links}"}]
    cypher_pm.query(spec(f"MATCH (n:CyWrite {{id: 1}}) REMOVE n.{stamp}"))
    assert cypher_pm.query(read) == [None]


def test_datetimes_epoch_milliseconds_and_timestamp_are_the_moment_the_statement_runs(
    cypher_pm: PersistenceManager,
):
    clear(cypher_pm)
    before = int(datetime.now(UTC).timestamp() * 1000) - 60_000
    millis, stamp = cypher_pm.get_one(
        spec("CREATE (n:CyWrite) SET n.at = datetime().epochMillis RETURN [n.at, timestamp()]")
    )
    after = int(datetime.now(UTC).timestamp() * 1000) + 60_000
    assert before <= millis <= after and before <= stamp <= after, (millis, stamp)


def test_a_parameter_of_a_date_or_a_zoned_datetime_is_a_temporal_value_of_its_kind(cypher_pm: PersistenceManager):
    found = cypher_pm.get_one(
        spec(
            "RETURN {year: $day.year, later: $at > datetime('2024-01-01T00:00:00Z')}",
            {"day": date(2024, 3, 15), "at": datetime(2024, 6, 1, tzinfo=UTC)},
        )
    )
    assert found == {"year": 2024, "later": True}


def test_a_parameter_of_a_time_a_local_or_zoned_datetime_or_a_timedelta_is_a_temporal_value_of_its_kind(
    cypher_pm: PersistenceManager,
):
    found = cypher_pm.get_one(
        spec(
            "RETURN {minute: $time.minute, hour: $ldt.hour, zoned: $z.timezone, later: ($ldt + duration('PT36H')).day, "
            "days: $d.days, hours: ($ldt + $d).hour}",
            {
                "time": time(10, 30),
                "ldt": datetime(2024, 3, 15, 10, 0),
                "z": datetime(2024, 3, 15, 10, 0, tzinfo=timezone(timedelta(hours=2))),
                "d": timedelta(days=1, hours=2),
            },
        )
    )
    assert found == {"minute": 30, "hour": 10, "zoned": "+02:00", "later": 16, "days": 1, "hours": 12}


def test_a_zoned_parameter_keeps_its_kind_in_reduce(cypher_pm: PersistenceManager):
    found = cypher_pm.query(
        spec(
            "RETURN reduce(s = false, x IN [1] | $a < $b)",
            {
                "a": datetime(2024, 3, 15, 10, 0, tzinfo=timezone(timedelta(hours=2))),
                "b": datetime(2024, 3, 15, 9, 0, tzinfo=UTC),
            },
        )
    )
    assert found == [True]


def test_a_cursor_reads_a_cypher_statements_rows(cypher_pm: PersistenceManager):
    cursor_spec = CursorSpecification[Any](cypher_statement("UNWIND range(1, 5) AS i RETURN {i: i}")).batch_size(2)
    with cypher_pm.open_cursor(cursor_spec) as cursor:
        assert [it for it in cursor] == [{"i": i} for i in range(1, 6)]


def test_a_cursor_reads_the_rows_of_a_cypher_statement_that_writes_and_its_writes_are_kept(
    cypher_pm: PersistenceManager,
):
    if cypher_pm.type is DatabaseType.ORACLE:
        pytest.skip("On Oracle a write is a block, not read through a cursor: see the test of that below.")
    clear(cypher_pm)
    # On Postgres the function the write is compiled to is made before the cursor is opened over it.
    writes = "UNWIND range(1, 3) AS i CREATE (n:CyWrite {i: i}) RETURN n.i"
    with cypher_pm.open_cursor(CursorSpecification[Any](cypher_statement(writes)).batch_size(2)) as cursor:
        assert [it for it in cursor] == [1, 2, 3]
    assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [3]


def test_a_cursor_over_a_cypher_statement_that_writes_in_a_transaction_writes_though_closed_unread(
    cypher_pm: PersistenceManager,
):
    if cypher_pm.type is DatabaseType.ORACLE:
        pytest.skip("On Oracle a write is a block, not read through a cursor: see the test of that below.")
    clear(cypher_pm)
    # On Postgres a server-side cursor would run the function the write is compiled to only at its first fetch.
    with transaction():
        cursor = cypher_pm.open_cursor(CursorSpecification[Any](cypher_statement("CREATE (n:CyWrite) RETURN n")))
        cursor.close()
    assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [1]


def test_a_parameter_whose_json_is_longer_than_a_varchar2_is_read_and_written(cypher_pm: PersistenceManager):
    # Bound as its JSON text: on Oracle a CLOB past 32,767 bytes, which a query takes where a VARCHAR2 is refused.
    clear(cypher_pm)
    ids = [f"id-{i:08d}" for i in range(5000)]
    assert cypher_pm.query(spec("UNWIND $ids AS id RETURN count(id)", {"ids": ids})) == [5000]
    cursor_spec = CursorSpecification[Any](cypher_statement("UNWIND $ids AS id RETURN count(id)")).bind({"ids": ids})
    with cypher_pm.open_cursor(cursor_spec) as cursor:
        assert [it for it in cursor] == [5000]
    cypher_pm.query(spec("CREATE (n:CyWrite {ids: $ids})", {"ids": ids}))
    assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN size(n.ids)")) == [5000]


def test_a_parameter_of_fewer_characters_than_a_varchar2_takes_and_more_bytes_is_read(
    cypher_pm: PersistenceManager,
):
    # A character past the Basic Multilingual Plane is four bytes of UTF-8: this list's JSON is 10,351 characters, and
    # 37,351 bytes.
    texts = ["\U0001f600" * 20] * 450
    assert cypher_pm.query(spec("UNWIND $texts AS text RETURN count(text)", {"texts": texts})) == [450]


class Person(BaseModel):
    name: str
    born: date
    tags: list[str]


def test_a_cypher_statements_rows_are_transformed_and_mapped_as_a_graph_engines_are(
    cypher_pm: PersistenceManager,
):
    clear(cypher_pm)
    cypher_pm.query(spec("CREATE (:CyWrite {name: 'Ada', born: date('1815-12-10'), tags: ['maths', 'engines']})"))
    ada = Person(name="Ada", born=date(1815, 12, 10), tags=["maths", "engines"])
    # A node is its properties, a temporal value its ISO text on a SQL store, which the model reads as a date.
    assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n").transform(Person)) == [ada]
    mapped = spec("MATCH (n:CyWrite) RETURN n.name, n.tags, {born: n.born}").map(
        lambda row: Person(name=row[0], tags=row[1], born=row[2]["born"])
    )
    assert cypher_pm.query(mapped) == [ada]
    cursor_spec = CursorSpecification[Any](cypher_statement("MATCH (n:CyWrite) RETURN n")).transform(Person)
    with cypher_pm.open_cursor(cursor_spec) as cursor:
        assert [it for it in cursor] == [ada]


def test_a_cypher_statement_takes_named_parameters_on_a_sql_store(cypher_pm: PersistenceManager):
    positional = QuerySpecification[Any](cypher_statement("RETURN $1")).bind([1])
    if cypher_pm.type is DatabaseType.NEO4J:
        assert cypher_pm.query(positional) == [1]
        return
    with pytest.raises(DrivineError, match=r"takes named parameters \(\$name\), not a list of them"):
        cypher_pm.query(positional)


@pytest.mark.parametrize("store", [it for it in SQL_STORES if {"postgres": POSTGRES, "oracle": ORACLE}[it]])
def test_a_write_in_a_transaction_rolled_back_leaves_nothing_and_runs_again_after(
    store: str, factory: PersistenceManagerFactory
):
    pm = factory.get(store)
    clear(pm)
    with transaction(rollback=True):
        pm.query(spec("CREATE (:CyWrite {name: 'Ada'})"))
        assert pm.query(spec("MATCH (n:CyWrite) RETURN n.name")) == ["Ada"]
    assert pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [0]
    # On Postgres the function the write was compiled to went with the transaction, and is made again.
    pm.query(spec("CREATE (:CyWrite {name: 'Ada'})"))
    assert pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [1]


@pytest.mark.parametrize("store", [it for it in SQL_STORES if {"postgres": POSTGRES, "oracle": ORACLE}[it]])
def test_a_cypher_statement_is_compiled_against_the_tables_of_the_graph_its_datasource_names(
    store: str, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    name = "cy_named"
    if registry.registered(f"{store}_{name}") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env(store), default_graph_path=name)
        registry.builder().with_properties(properties).register(f"{store}_{name}")
    pm = factory.get(f"{store}_{name}")
    pm.execute(QuerySpecification((PostgresGraph(name) if store == "postgres" else OracleGraph(name)).ensuring))
    pm.execute(QuerySpecification(f"DELETE FROM {name}_edge"))
    pm.execute(QuerySpecification(f"DELETE FROM {name}_node"))
    pm.query(spec("CREATE (:CyWrite {name: 'Ada'})"))
    assert pm.query(spec("MATCH (n:CyWrite) RETURN n.name")) == ["Ada"]
    # A row of plain SQL is a dict of its columns on Postgres, and its one column's value on Oracle.
    made = pm.get_one(QuerySpecification[Any](f"SELECT COUNT(*) AS made FROM {name}_node"))
    assert (made["made"] if isinstance(made, dict) else made) == 1
    pm.execute(QuerySpecification(f"DELETE FROM {name}_node"))


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_nodes_of_two_labels_a_transaction_has_made_and_not_yet_committed_are_found_by_both(
    factory: PersistenceManagerFactory,
):
    pm = factory.get("oracle")
    clear(pm)
    with transaction(rollback=True):
        pm.query(
            spec("CREATE (:CyWrite:Dog {name: 'Buddy'}), (:CyWrite:Cat {name: 'Tom'}), (:CyWrite:Dog {name: 'Max'})")
        )
        assert pm.query(spec("MATCH (d:CyWrite:Dog) RETURN d.name ORDER BY d.name")) == ["Buddy", "Max"]
        assert pm.query(spec("MATCH (c) WHERE c:Cat:CyWrite RETURN c.name")) == ["Tom"]


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_a_cypher_statement_that_writes_is_not_read_through_a_cursor_on_oracle(
    factory: PersistenceManagerFactory,
):
    cursor_spec = CursorSpecification[Any](cypher_statement("CREATE (n:CyWrite) RETURN n"))
    with pytest.raises(DrivineError, match="is a block on Oracle"):
        factory.get("oracle").open_cursor(cursor_spec)
