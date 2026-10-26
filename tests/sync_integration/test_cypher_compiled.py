# Written by scripts/unasync.py from tests/integration/test_cypher_compiled.py:
# change that, and run the script again.
"""
What the Cypher spec (tests/cypher) can't say, on Neo4j and on the SQL stores Cypher is compiled for: values that change
on every run (a UUID, the time), parameters of Python's temporal types, statements built in code, a cursor, and what
needs a transaction of its own. drivine4j keeps the same in its Cypher*CrossEngineTest.
"""

import contextlib
import dataclasses
import re
import time as time_module
from collections.abc import Generator
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.cypher.sql.sql_cypher_compiler import NEGATIVE_PRECISION
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
from tests.cypher.cypher_spec import load
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


def test_a_parameter_named_with_a_letter_past_ascii_is_bound(cypher_pm: PersistenceManager):
    # Cypher names a parameter with any letter; a SQL store binds the compiled statement's marker of it by that name.
    clear(cypher_pm)
    assert cypher_pm.query(spec("RETURN $größe", {"größe": 3})) == [3]
    written = spec("CREATE (n:CyWrite {größe: $größe}) RETURN n.größe", {"größe": 4})
    assert cypher_pm.query(written) == [4]


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


def test_one_parameter_of_forty_thousand_characters_is_read_and_written(cypher_pm: PersistenceManager):
    # One text past 32,767 bytes: on Oracle a CLOB, read and written alike. (Oracle won't compare a JSON value so long,
    # ORA-40180, nor read a text of more than 4,000 bytes out of JSON, as the README says: it is handed back whole.)
    clear(cypher_pm)
    text = "x" * 40_000
    assert cypher_pm.query(spec("RETURN $text", {"text": text})) == [text]
    cypher_pm.query(spec("CREATE (n:CyWrite {text: $text})", {"text": text}))
    assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n.text")) == [text]


def test_a_set_or_an_iterator_is_bound_as_a_list_and_an_iterator_named_twice_is_read_once(
    cypher_pm: PersistenceManager,
):
    clear(cypher_pm)
    cypher_pm.query(spec("UNWIND [1, 2, 3] AS i CREATE (:CyWrite {id: i})"))
    # The Neo4j driver takes a list or a tuple, and no set or iterator.
    neo4j = cypher_pm.type is DatabaseType.NEO4J
    for ids in ((1, 2),) if neo4j else ({1, 2}, frozenset({1, 2}), (1, 2)):
        found = cypher_pm.query(spec("MATCH (n:CyWrite) WHERE n.id IN $ids RETURN n.id ORDER BY n.id", {"ids": ids}))
        assert found == [1, 2]
    if neo4j:
        return
    twice = spec("MATCH (n:CyWrite) WHERE n.id IN $ids RETURN [n.id, size($ids)]", {"ids": (it for it in [3])})
    assert cypher_pm.query(twice) == [[3, 1]]


def test_bytes_are_bound_as_their_base64_text_as_drivine4j_writes_a_byte_array(cypher_pm: PersistenceManager):
    if cypher_pm.type is DatabaseType.NEO4J:
        pytest.skip("A graph engine keeps bytes as bytes; a SQL store's JSON has none, and drivine4j writes base64.")
    assert cypher_pm.query(spec("RETURN [$b, $a]", {"b": b"\x00\xffdrivine", "a": bytearray(b"\x00\xff")})) == [
        ["AP9kcml2aW5l", "AP8="]
    ]


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


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_a_text_past_32_kb_set_on_two_nodes_beside_a_whole_float_or_not_leaves_the_session_open(
    factory: PersistenceManagerFactory,
):
    # Oracle crashed the session as the block freed its parameters, held in a JSON variable it had read once for each
    # row (#131). The crash could come after the rows did, so the session is asked again, on the one connection a
    # transaction holds; and a block parsed before rarely crashed, so each statement is new. The nodes are read back
    # whole: see "A parameter past 32 KB set on two nodes" in the spec.
    pm = factory.get("oracle")
    clear(pm)
    text = "x" * 40_000
    written: dict[str, Any] = {}
    with transaction(rollback=True):
        pm.query(spec("CREATE (:CyWrite {id: 1}), (:CyWrite {id: 2})"))
        for i in range(3):
            pm.query(spec(f"MATCH (n:CyWrite) SET n.a{i} = $a{i}", {f"a{i}": text}))
            pm.query(spec(f"MATCH (n:CyWrite) SET n.b{i} = $b{i}, n.f{i} = $f{i}", {f"b{i}": text, f"f{i}": 2.0 + i}))
            written |= {f"a{i}": text, f"b{i}": text, f"f{i}": 2.0 + i}
            rows = pm.query(spec("MATCH (n:CyWrite) RETURN properties(n) ORDER BY n.id"))
            assert rows == [{"id": it, **written} for it in (1, 2)]


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_a_text_past_32_kb_in_a_map_parameter_read_and_set_on_two_nodes_leaves_the_session_open(
    factory: PersistenceManagerFactory,
):
    # Oracle crashed the session where a statement read a value past 32 KB from a map parameter, by a property or by a
    # subscript, for each of two rows, taking the parameter as JSON first and then the value from it (#145): alone in
    # the parameter, or beside a whole float, which makes its text extended JSON. As above, on one connection, each
    # statement new, and the session asked again after it.
    pm = factory.get("oracle")
    clear(pm)
    text = "x" * 40_000
    written: dict[str, Any] = {}
    with transaction(rollback=True):
        pm.query(spec("CREATE (:CyWrite {id: 1}), (:CyWrite {id: 2})"))
        for i in range(3):
            for j, p in enumerate(({"text": text}, {"text": text, "f": 2.0 + i})):
                k = i * 2 + j
                for read in ("$p.text", "$p['text']"):
                    cypher = f"MATCH (n:CyWrite) WHERE n.id > -{k} RETURN {read} ORDER BY n.id"
                    assert pm.query(spec(cypher, {"p": p})) == [text, text]
                if "f" in p:
                    pm.query(spec(f"MATCH (n:CyWrite) SET n.t{k} = $p.text, n.f{k} = $p.f", {"p": p}))
                    written |= {f"t{k}": text, f"f{k}": p["f"]}
                else:
                    pm.query(spec(f"MATCH (n:CyWrite) SET n.t{k} = $p.text", {"p": p}))
                    written |= {f"t{k}": text}
                rows = pm.query(spec("MATCH (n:CyWrite) RETURN properties(n) ORDER BY n.id"))
                assert rows == [{"id": it, **written} for it in (1, 2)]


def seeded(pm: PersistenceManager) -> None:
    """The graph queries.yaml reads, written again."""
    clear(pm)
    pm.query(spec(load("queries.yaml").graph or ""))


def test_collect_distinct_beside_keys_other_aggregates_an_order_a_page_and_a_variable_outside_its_aggregates(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: _aggregates_first's grouping WITH, and the projection over it.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, count(*) + size(collect(DISTINCT m.title)) "
            "ORDER BY count(*) DESC, p.name SKIP 1 LIMIT 1"
        )
    ) == [["Carrie", 4]]
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p, collect(DISTINCT m) AS ms WHERE size(ms) > 1 "
            "RETURN p.name, size(ms) ORDER BY p.name"
        )
    ) == [["Carrie", 2], ["Keanu", 3]]
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p, size(collect(DISTINCT m.title)) + p.born AS x "
            "RETURN p.name, x ORDER BY p.name"
        )
    ) == [["Carrie", 1969], ["Keanu", 1967], ["Laurence", 1962]]


def test_collect_distinct_in_a_distinct_projection_ordered_by_a_keys_property_or_an_aggregate_no_item_is(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the projection over _aggregates_first's grouping WITH orders by what that WITH holds.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH DISTINCT p, size(collect(DISTINCT m.title)) AS n "
            "ORDER BY p.born DESC RETURN p.name, n"
        )
    ) == [["Carrie", 2], ["Keanu", 3], ["Laurence", 1]]
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) "
            "RETURN DISTINCT p.name, size(collect(DISTINCT m.title)) ORDER BY count(*)"
        )
    ) == [["Laurence", 1], ["Carrie", 2], ["Keanu", 3]]
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p.name AS name, m.title AS t "
            "RETURN name AS nm, size(collect(DISTINCT t)) "
            "ORDER BY any(x IN collect(DISTINCT t) WHERE x STARTS WITH 'J' AND name = 'Keanu'), nm"
        )
    ) == [["Carrie", 2], ["Laurence", 1], ["Keanu", 3]]


def test_collect_distinct_beside_an_item_named_after_a_variable_another_item_is_ordered_by_that_name(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: in an order term, an item's name is that item, not the variable it shadows.
    seeded(cypher_pm)
    rows = cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS person, p.name AS p, size(collect(DISTINCT m.title)) "
            "ORDER BY toLower(p)"
        )
    )
    assert [it[1:] for it in rows] == [["Carrie", 2], ["Keanu", 3], ["Laurence", 1]]


def test_an_item_of_the_relationships_collect_gathers_is_read_in_the_projection_that_gathers_them(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: Oracle reads no aggregate from a subquery, so the item is read from what collect gathered.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[r:ACTED_IN]->() WHERE p.name <> 'Keanu' "
            "RETURN p.name, collect(r)[0].role ORDER BY p.name"
        )
    ) == [["Carrie", "Trinity"], ["Laurence", "Morpheus"]]
    assert cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Laurence'})-[r:ACTED_IN]->() MATCH (:Person {name: 'Carrie'})-[s:ACTED_IN]->() "
            "RETURN size(collect(r) + collect(s)), (collect(r) + collect(s))[0].role, "
            "(collect(r) + collect(s))[-1].role"
        )
    ) == [[4, "Morpheus", "Trinity"]]
    assert cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Laurence'})-[r:ACTED_IN]->() WITH collect(r) AS rs "
            "RETURN rs[9] IS NULL, rs[-9] IS NULL"
        )
    ) == [[True, True]]


def test_collect_distinct_under_a_comprehension_a_quantifier_or_a_reduce_that_names_a_grouping_key_in_its_body(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: _aggregates_first's grouping WITH keeps each variable such a body names, outside the
    # aggregate.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p.name AS name, m.title AS t "
            "RETURN name, size([x IN collect(DISTINCT t) WHERE x <> name]), "
            "any(x IN collect(DISTINCT t) WHERE x = name), "
            "reduce(n = 0, x IN collect(DISTINCT t) | n + size(name)) ORDER BY name"
        )
    ) == [["Carrie", 2, False, 12], ["Keanu", 3, False, 15], ["Laurence", 1, False, 8]]


def test_collect_distinct_beside_a_pattern_or_a_subquery_that_names_a_grouping_key(cypher_pm: PersistenceManager):
    # Not in the spec yet: _aggregates_first's grouping WITH keeps each variable a pattern or a subquery names, outside
    # the aggregate.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) "
            "WITH p, size(collect(DISTINCT m.title)) + COUNT { (p)-[:KNOWS]->() } AS x, "
            "size([(p)-[:KNOWS]->(f) | f.name] + collect(DISTINCT m.title)) AS y, "
            "size(collect(DISTINCT m.title)) > 2 OR EXISTS { (p)-[:KNOWS]->(:Person {name: 'Keanu'}) } AS z "
            "RETURN p.name, x, y, z ORDER BY p.name"
        )
    ) == [["Carrie", 2, 2, False], ["Keanu", 4, 4, True], ["Laurence", 2, 2, True]]


def test_collect_distinct_of_dates_keeps_each_once_as_dates(cypher_pm: PersistenceManager):
    # Not in the spec yet: what _aggregates_first keeps of a collection is still of the kind collect gathered.
    dates = "UNWIND [date('2020-01-01'), date('2020-01-01'), date('2021-05-01')] AS d"
    assert cypher_pm.query(spec(f"{dates} WITH collect(DISTINCT d) AS ds RETURN [x IN ds | x.year]")) == [[2020, 2021]]
    assert cypher_pm.query(spec(f"{dates} RETURN collect(DISTINCT d)[1].month")) == [5]


def test_an_item_of_a_pattern_comprehensions_relationships_is_compared_by_its_id(cypher_pm: PersistenceManager):
    # Not in the spec yet: a pattern comprehension gathers their properties beside their ids, as collect does, but in a
    # subquery.
    seeded(cypher_pm)
    neo = "[(a)-[r:ACTED_IN {role: 'Neo'}]->() | r]"
    assert cypher_pm.query(
        spec(
            f"MATCH (a:Person {{name: 'Keanu'}}) WITH {neo} AS rs, a "
            f"RETURN [{neo}[0] = {neo}[1], {neo}[1] IN rs, {neo}[0].role = 'Neo']"
        )
    ) == [[False, True, True]]


def test_a_list_comprehension_over_nodes_or_relationships_that_keeps_them_gives_a_list_of_them_compared_by_id(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: what _entity_comprehension keeps, with no projection or one of the item, is still a list of
    # nodes or relationships.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Keanu'})-[r:ACTED_IN {role: 'Neo'}]->() WITH collect(r) AS rs "
            "RETURN [[x IN rs WHERE x <> rs[0]][0] = rs[1], [x IN rs | x][0] = rs[1], "
            "size([x IN rs WHERE x = rs[0] | x]), "
            "rs[0] IN [x IN rs WHERE x <> rs[0]], [x IN rs | x.role]]"
        )
    ) == [[True, False, 1, False, ["Neo", "Neo"]]]
    assert cypher_pm.query(
        spec(
            "MATCH p = (:Person {name: 'Laurence'})-[:KNOWS*2]->(b) "
            "RETURN [[n IN nodes(p) WHERE n.name <> 'Laurence'][0] = nodes(p)[1], b IN [n IN nodes(p) | n], "
            "[n IN nodes(p) WHERE n = b | n.name]]"
        )
    ) == [[True, True, ["Carrie"]]]


def test_a_paths_nodes_or_relationships_added_together_are_a_list_of_them_and_of_no_path_null(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: lists of nodes no collect gathers, added, indexed and iterated by id; over a null list, null.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH p = (:Person {name: 'Laurence'})-[:KNOWS*2]->() "
            "RETURN [size(nodes(p) + nodes(p)), (nodes(p) + nodes(p))[3] = nodes(p)[0], "
            "(nodes(p) + nodes(p))[3] = nodes(p)[1], [n IN nodes(p) + nodes(p) | n.name], "
            "relationships(p)[1] IN relationships(p) + relationships(p), size(relationships(p) + relationships(p))]"
        )
    ) == [[6, True, False, ["Laurence", "Keanu", "Carrie", "Laurence", "Keanu", "Carrie"], True, 4]]
    assert cypher_pm.query(
        spec(
            "OPTIONAL MATCH p = (:Nope)-->() RETURN [[n IN nodes(p) | n.name], [n IN nodes(p) WHERE true], nodes(p)[0]]"
        )
    ) == [[None, None, None]]


def test_calls_that_write_nested_give_each_row_its_own_variables_with_the_nodes_they_return(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: a caller row at each depth, merged with a node returned from below it.
    clear(cypher_pm)
    assert cypher_pm.query(
        spec(
            "UNWIND [1, 2] AS x CALL { WITH x CREATE (a:CyWrite {x: x}) WITH a, x "
            "CALL { WITH a, x CREATE (b:CyWrite {y: x * 10}) RETURN b } RETURN a, b } RETURN [x, a.x, b.y]"
        )
    ) == [[1, 1, 10], [2, 2, 20]]
    assert cypher_pm.query(
        spec(
            "UNWIND [1, 2] AS i CALL { WITH i CREATE (n:CyWrite {i: i}) RETURN n } SET n.j = i * 10 "
            "RETURN [i, n.i, n.j] ORDER BY i"
        )
    ) == [[1, 1, 10], [2, 2, 20]]
    assert cypher_pm.query(
        spec(
            "WITH date('2020-01-02') AS d CALL { CREATE (n:CyWrite) RETURN date('2021-03-04') AS e } "
            "RETURN [d.year, e.month]"
        )
    ) == [[2020, 3]]


def test_a_foreach_in_calls_that_write_nested_three_deep_leaves_each_callers_row_as_it_was(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the FOREACH loops over rows of its own at the deepest depth, between the caller rows above it
    # being read and the rows the CALLs give.
    clear(cypher_pm)
    assert cypher_pm.query(
        spec(
            "UNWIND [1, 2] AS x CALL { WITH x CREATE (a:CyWrite {x: x}) WITH a, x "
            "CALL { WITH a, x FOREACH (i IN [1, 2] | CREATE (:CyWrite {i: i * x})) CREATE (b:CyWrite {y: x * 10}) "
            "RETURN b } RETURN a, b } RETURN [x, a.x, b.y]"
        )
    ) == [[1, 1, 10], [2, 2, 20]]
    assert cypher_pm.query(spec("MATCH (n:CyWrite) WHERE n.i IS NOT NULL RETURN [count(n), sum(n.i)]")) == [[4, 9]]


def test_collect_distinct_beside_a_key_that_holds_a_parameter_or_a_subquery(cypher_pm: PersistenceManager):
    # Not in the spec yet: _held_first works such a key out first in the grouping WITH _aggregates_first makes, as
    # Oracle groups by no subquery, and binds a parameter as a marker of its own each time it is named.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name + $s AS k, size(collect(DISTINCT m.title + $s)) "
            "ORDER BY k",
            {"s": "!"},
        )
    ) == [["Carrie!", 2], ["Keanu!", 3], ["Laurence!", 1]]
    assert cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN COUNT { (p)-[:KNOWS]->() } AS k, "
            "size(collect(DISTINCT m.title)) AS c ORDER BY k, c"
        )
    ) == [[0, 2], [1, 3]]


def test_a_map_or_a_list_that_holds_a_relationship_is_equal_to_one_whose_numbers_or_keys_are_written_otherwise(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: identities compare as JSON values, not as their text.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Keanu'})-[r1:KNOWS]->() RETURN [{x: r1, y: 1} = {x: r1, y: 1.0}, "
            "[r1, 1] = [r1, 1.0], {a: 1, x: r1} = {x: r1, a: 1}, {x: r1, y: 1} IN [{x: r1, y: 1.0}]]"
        )
    ) == [[True, True, True, True]]


def test_min_and_max_of_lists_the_statement_makes_compare_them_item_by_item_whatever_their_lengths(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: a nested list (liberation-data/drivine#287). The spec holds DISTINCT and the grouping WITH.
    assert cypher_pm.query(spec("UNWIND [[[1, 2]], [[3]], [[1], [2]]] AS xs RETURN [min(xs), max(xs)]")) == [
        [[[1], [2]], [[3]]]
    ]
    # The least and the greatest moment still, whatever their offsets.
    assert cypher_pm.query(
        spec(
            "UNWIND [datetime('2020-01-01T00:00+01:00'), datetime('2020-01-01T00:30+02:00')] AS x "
            "RETURN [min(x) = datetime('2020-01-01T00:30+02:00'), max(x) = datetime('2020-01-01T00:00+01:00')]"
        )
    ) == [[True, True]]
    if cypher_pm.type is DatabaseType.ORACLE:
        # Oracle puts a null item first and a number before a text (liberation-data/drivine4j#205).
        return
    assert cypher_pm.query(spec("UNWIND [[1, 'a'], ['a', 1], [true]] AS xs RETURN [min(xs), max(xs)]")) == [
        [["a", 1], [1, "a"]]
    ]
    assert cypher_pm.query(spec("UNWIND [[1, null], [1], [null], null] AS xs RETURN [min(xs), max(xs)]")) == [
        [[1], [None]]
    ]


HUB_GRAPH = "cy_hub"
"""The graph a hub is written to on a SQL store: tables of its own, apart from those the other tests read."""


@contextlib.contextmanager
def hub(
    pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory, leaves: int
) -> Generator[PersistenceManager]:
    """
    A manager of the store `pm` is of, over a graph of one node with relationships to that many others, cleared again
    after, however the test ends. On a SQL store the graph's tables are its own: after thousands of rows were written
    and read in the tables every test shares, Oracle (23.26) at times found no rows for a later test's
    `MATCH (:Person {name: 'Keanu'})-[:KNOWS]->(p)`, rows it had just written, on main too.
    """
    if pm.type is DatabaseType.NEO4J:
        apart, nodes, edges = pm, None, None
    else:
        store = "postgres" if pm.type is DatabaseType.POSTGRES else "oracle"
        name = f"{store}_{HUB_GRAPH}"
        if registry.registered(name) is None:
            properties = dataclasses.replace(ConnectionProperties.from_env(store), default_graph_path=HUB_GRAPH)
            registry.builder().with_properties(properties).register(name)
        apart = factory.get(name)
        graph = PostgresGraph(HUB_GRAPH) if store == "postgres" else OracleGraph(HUB_GRAPH)
        apart.execute(QuerySpecification(graph.ensuring))
        nodes, edges = f"{HUB_GRAPH}_node", f"{HUB_GRAPH}_edge"

    def cleared() -> None:
        if nodes is None or edges is None:
            apart.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
        else:
            apart.execute(QuerySpecification(f"DELETE FROM {edges}"))
            apart.execute(QuerySpecification(f"DELETE FROM {nodes}"))

    cleared()
    try:
        made = f"CREATE (h:Hub) WITH h UNWIND range(1, {leaves}) AS i CREATE (h)-[:R]->(:Leaf) RETURN count(*)"
        assert apart.query(spec(made)) == [leaves]
        yield apart
    finally:
        cleared()


def test_a_list_of_a_few_thousand_relationships_is_equal_by_identity_to_a_maps_or_a_lists(
    cypher_pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    # Not in the spec yet: their identity is larger than Oracle's = compares (ORA-40180), so Oracle compares it by
    # JSON_EQUAL.
    with hub(cypher_pm, registry, factory, 2500) as pm:
        assert pm.query(
            spec(
                "MATCH (:Hub)-[r:R]->() WITH collect(r) AS rs "
                "RETURN [[rs][0] = rs, {x: rs}.x = rs, rs IN [rs], size(rs)]"
            )
        ) == [[True, True, True, 2500]]


def test_collect_distinct_keeps_a_few_thousand_lists_or_maps_holding_a_relationship_once_each_in_seconds(
    cypher_pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    # Not in the spec yet: each was compared with every one before it, which took 20 to 40 seconds on Oracle.
    with hub(cypher_pm, registry, factory, 5000) as pm:
        started = time_module.monotonic()
        assert pm.query(
            spec(
                "MATCH p = (:Hub)-[r:R]->() "
                "RETURN [size(collect(DISTINCT relationships(p))), size(collect(DISTINCT {x: r}))]"
            )
        ) == [[5000, 5000]]
        seconds = time_module.monotonic() - started
        assert seconds < 15, seconds


def test_a_whole_float_past_2_to_the_53_written_in_the_statement_or_given_equals_the_same_float_worked_out(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: Postgres writes such a float out in full, by its shortest decimal, not the double's exact
    # value.
    assert cypher_pm.query(
        spec("RETURN [toFloat('1e24') = 1e24, toFloat('1e300') = 1e300, toFloat('1e24') = $x]", {"x": 1e24})
    ) == [[True, True, True]]


def test_a_number_or_a_text_the_statement_gives_added_to_a_text_is_written_as_to_string_writes_it(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: a side the statement gives is written out at compile time, and the other side as it is.
    assert cypher_pm.query(
        spec(
            "RETURN ['a' + 1e7, 1e7 + 'a', 'a' + 2.0, 'a' + $f, $i + 'b', 'a' + $s, $s + 1.5e-7, 'x' + 10, "
            "$m.n + 'y', $j + 'b']",
            {"f": 1.0e-4, "i": 3, "s": "s", "m": {"n": 0.5}, "j": 4},
        )
    ) == [["a1.0E7", "1.0E7a", "a2.0", "a1.0E-4", "3b", "as", "s1.5E-7", "x10", "0.5y", "4b"]]


def test_nan_or_an_infinity_the_statement_gives_added_to_a_text_is_written_as_to_string_writes_it(
    cypher_pm: PersistenceManager,
):
    assert cypher_pm.query(
        spec(
            "RETURN ['a' + $n, $p + 'b', 'c' + $q, $n + toString(1.5)]",
            {"n": float("nan"), "p": float("inf"), "q": float("-inf")},
        )
    ) == [["aNaN", "Infinityb", "c-Infinity", "NaN1.5"]]


def test_a_sum_of_a_text_the_statement_makes_and_a_text_or_a_float_gives_the_text_cypher_writes(
    cypher_pm: PersistenceManager,
):
    # A side the statement makes a text is written as its own text on a SQL store (drivine4j#268); a float beside it
    # still as toString writes it.
    assert cypher_pm.query(
        spec(
            "UNWIND [1, 2] AS i WITH 'x' + toString(i) AS t "
            "RETURN [t + toUpper('b'), 'a' + toUpper(t), t + 2.5, split('p,q', ',')[0] + t] ORDER BY t"
        )
    ) == [["x1B", "aX1", "x12.5", "px1"], ["x2B", "aX2", "x22.5", "px2"]]
    assert cypher_pm.query(
        spec("WITH 1.0E7 AS f, 0.5 AS h RETURN ['x' + toString(1) + f, substring('abc', 1) + h]")
    ) == [["x11.0E7", "bc0.5"]]
    assert cypher_pm.query(
        spec("UNWIND ['a', 'b'] AS s WITH collect(toUpper(s)) AS ss, 1.5 AS f UNWIND ss AS u RETURN u + f ORDER BY u")
    ) == ["A1.5", "B1.5"]
    # A CASE, a coalesce, a list and a CALL's UNION that may give a float beside a text write the float as toString
    # does.
    assert cypher_pm.query(
        spec(
            "WITH 2.5 AS f RETURN [CASE WHEN f > 1 THEN f ELSE 'a' END + 'x', "
            "coalesce(CASE WHEN f < 1 THEN 'a' END, f) + 'x', ['a', f][1] + 'x']"
        )
    ) == [["2.5x", "2.5x", "2.5x"]]
    assert cypher_pm.query(
        spec("CALL { RETURN 2.5 AS v UNION RETURN toUpper('a') AS v } RETURN v + 'x' AS s ORDER BY s")
    ) == ["2.5x", "Ax"]
    # The least and the greatest of texts are a text, put together with a float as Cypher writes it.
    assert cypher_pm.query(
        spec("UNWIND ['b', 'a'] AS s WITH min(toUpper(s)) AS lo, max(s) AS hi RETURN [lo + 2.5, hi + 2.5]")
    ) == [["A2.5", "b2.5"]]


def test_a_quotient_of_floats_is_written_without_the_zeros_its_scale_leaves(cypher_pm: PersistenceManager):
    # The spec's float arithmetic steps skip Postgres, which works a float out in exact decimal (drivine4j#188): these
    # it gives as a double does.
    assert cypher_pm.query(
        spec("RETURN [4.0 / 2, 7 / 2.0, 10.0 / 4, toString(4.0 / 2), toString(10.0 / 4), toString(7 / 2.0)]")
    ) == [[2.0, 3.5, 2.5, "2.0", "2.5", "3.5"]]


def test_to_string_of_a_double_past_oracles_number_range_writes_its_shortest_digits(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: Oracle writes such a double from its own text, not from TO_CHAR's 15 digits.
    assert cypher_pm.query(
        spec(
            "RETURN [toString(1.7976931348623157e308), toString(1.5e300), toString(1.0e-310), toString($a), "
            "toString($b)]",
            {"a": -2.5e-320, "b": -1.5e300},
        )
    ) == [["1.7976931348623157E308", "1.5E300", "1.0E-310", "-2.5E-320", "-1.5E300"]]
    assert cypher_pm.query(spec("RETURN toString(5e-324)")) == ["4.9E-324"]


def test_round_reads_a_precision_from_data_or_a_whole_float_as_a_whole_number_toward_zero_and_a_null_as_null(
    cypher_pm: PersistenceManager,
):
    # Beyond the spec's round() cases: a float, -0.5 and a whole float read from data, and every rounding mode.
    clear(cypher_pm)
    cypher_pm.execute(spec("CREATE (:Round {p: 1.7, q: -0.5, w: 2.0, r: -2, z: 0.0, b: 400, g: 1000000000})"))
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) RETURN [round(2.555, n.p), round(2.55, n.p, 'UP'), round(2.5, n.q), "
            "round(2.5, n.q, 'HALF_UP'), round(2.555, n.w), round(2.555, 2.0, 'HALF_UP')]"
        )
    ) == [[2.6, 2.6, 3.0, 3.0, 2.56, 2.56]]
    assert cypher_pm.query(
        spec(
            "RETURN [round(2.5, 0, 'HALF_UP'), round(-2.5, 0, 'HALF_EVEN'), round(2.5, 0, 'DOWN'), "
            "round(2.1, 0, 'CEILING'), round(2.9, 0, 'FLOOR'), round(-2.1, 0, 'UP'), round(-2.5, 0, 'HALF_DOWN')]"
        )
    ) == [[3.0, -2.0, 2.0, 3.0, 2.0, -3.0, -2.0]]
    assert cypher_pm.query(
        spec("RETURN [round(null, 2), round(2.5, null), round(null, 2, 'UP'), round(2.5, null, 'UP')]")
    ) == [[None, None, None, None]]
    # To 0 places, as round(x) rounds (drivine4j#278): an integer as a float, a huge or tiny number, a precision from
    # data, and the doubles that a binary sum with a half rounds up, the last below a half and the first odd past 2^52;
    # round(x) likewise.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) RETURN [round(3, 0), round(-3, 0), round(1.0E20, 0), round(-1.0E100, 0), "
            "round(1.0E-120, 0), round(-1.0E-120, 0), round(-2.5, n.q), round(-2.5, n.z), "
            "round(0.49999999999999994, 0), round(4503599627370497.0, 0), round(0.49999999999999994), "
            "round(4503599627370497.0), round(-0.5), round(-2.5)]"
        )
    ) == [
        [3.0, -3.0, 1.0e20, -1.0e100, 0.0, 0.0, -2.0, -2.0, 0.0, 4503599627370497.0, 0.0, 4503599627370497.0, 0.0, -2.0]
    ]
    # Places past a number's digits give it back, where Oracle's NUMBER would overflow (drivine4j#266); short of them,
    # it rounds.
    assert cypher_pm.query(
        spec("RETURN [round(2.5, 400), round(1.5, 400), round(2.5, 400, 'HALF_UP'), round(2.5, 400, 'CEILING')]")
    ) == [[2.5, 1.5, 2.5, 2.5]]
    # Read from data, and so far past the digits that writing them all out would not finish, where Postgres's numeric
    # would overflow; a null still rounds to null.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) RETURN [round(2.5, n.b), round(2.5, n.b, 'FLOOR'), round(2.5, n.g), "
            "round(2.5, n.g, 'CEILING'), round(null, n.g), round(n.none, n.g, 'UP'), round(2.5, n.none)]"
        )
    ) == [[2.5, 2.5, 2.5, 2.5, None, None, None]]
    assert cypher_pm.query(
        spec("RETURN [round(2.5, 1000000000), round(2.5, 1000000000, 'HALF_UP'), round(5, 400)]")
    ) == [[2.5, 2.5, 5.0]]
    assert cypher_pm.query(
        spec(
            "RETURN [round(1.0E-100, 120), round(123.456, 200, 'HALF_UP'), round(1.0E100, 30), round(1.0 / 3, 20), "
            "round(1.0E-100, 99), round(-2.5, 300, 'FLOOR')]"
        )
    ) == [[1.0e-100, 123.456, 1.0e100, 0.3333333333333333, 0.0, -2.5]]
    # Zero, whose digits Oracle cannot count by its logarithm, and a precision short of a tiny number's digits but past
    # NUMBER's range.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) RETURN [round(0.0, 400), round(0, 400, 'HALF_UP'), round(n.z, 2), round(n.z, 400), "
            "round(n.z, 2, 'UP'), round(1.0E-120, 130), round(1.0E-120, 130, 'HALF_UP'), "
            "round(-1.0E-120, 136, 'CEILING')]"
        )
    ) == [[0.0, 0.0, 0.0, 0.0, 0.0, 1.0e-120, 1.0e-120, -1.0e-120]]
    # The least numbers Oracle's NUMBER holds, whose logarithm it refuses (ORA-01428), and zero to the most places.
    assert cypher_pm.query(
        spec(
            "RETURN [round(1.0E-130, 2), round(1.0E-130, 140), round(-1.5E-130, 2, 'UP'), round(1.0E-129, 140, 'UP'), "
            "round(1.0E-129, 2), round(-1.0E-129, 2, 'FLOOR'), round(-1.0E-125, 141, 'HALF_EVEN'), "
            "round(1.2345678901234567E-130, 145), round(5.5E-128, 128, 'HALF_EVEN'), round(0.0, 1000000000)]"
        )
    ) == [[0.0, 1.0e-130, -0.01, 1.0e-129, 0.0, -0.01, -1.0e-125, 1.234567890123457e-130, 6.0e-128, 0.0]]
    # A null number past its digits is null.
    assert cypher_pm.query(
        spec("MATCH (n:Round) RETURN [round(null, 400), round(n.none, 400, 'UP'), round(n.none, n.b)]")
    ) == [[None, None, None]]
    # Zero to a null precision is null, not the zero Oracle gives back without rounding.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) RETURN [round(n.z, n.none), round(n.z, n.none, 'UP'), round(0.0, null), "
            "round(0, null, 'HALF_UP')]"
        )
    ) == [[None, None, None, None]]
    # Either side of the last place a double holds, and short of it, where a mode still rounds.
    assert cypher_pm.query(
        spec(
            "RETURN [round(123.456, 14, 'UP'), round(123.456, 15, 'UP'), round(123.456, 16, 'UP'), "
            "round(-123.456, 14, 'FLOOR'), round(1234.5678901234567, 13), round(1234.5678901234567, 12, 'HALF_EVEN'), "
            "round(99.99, 16, 'UP'), round(0.1, 17, 'UP')]"
        )
    ) == [[123.456, 123.456, 123.456, -123.456, 1234.5678901234567, 1234.567890123457, 99.99, 0.1]]
    # Near 10^17, whose digits all come before the point, and negatives past their digits.
    assert cypher_pm.query(
        spec(
            "RETURN [round(123456789012345678, 2), round(99999999999999999.0, 1, 'UP'), round(12345678901234567.0, 1), "
            "round(-9.87654321E16, 3, 'FLOOR'), round(-1234.5678, 400), round(-1234.5678, 2)]"
        )
    ) == [[1.2345678901234568e17, 1.0e17, 1.2345678901234568e16, -9.87654321e16, -1234.5678, -1234.57]]
    # Over an aggregate, where no value is shared, and so the number is written out where it is read.
    assert cypher_pm.query(
        spec(
            "UNWIND [1.0, 2.0, 2.5] AS x RETURN [round(avg(x), 2), round(avg(x), 400), round(avg(x), 400, 'HALF_UP'), "
            "round(round(avg(x), 400), 1, 'DOWN')]"
        )
    ) == [[1.83, 1.8333333333333333, 1.8333333333333333, 1.8]]
    # A negative precision from data fails on zero too, whose places Oracle gives back without rounding.
    message = None if cypher_pm.type is DatabaseType.NEO4J else re.escape(NEGATIVE_PRECISION)
    for statement in ["MATCH (n:Round) RETURN round(n.z, n.r, 'HALF_UP')", "MATCH (n:Round) RETURN round(n.z, n.r)"]:
        with pytest.raises(Exception, match=message):  # noqa: PT011
            cypher_pm.query(spec(statement))
    # The least doubles, whose last digits are short of the 400 places Postgres holds a precision to. Oracle's NUMBER
    # holds none of them.
    if cypher_pm.type is not DatabaseType.ORACLE:
        assert cypher_pm.query(
            spec(
                "RETURN [round(2.2250738585072014E-308, 323), round(2.2250738585072014E-308, 400), "
                "round(4.9E-324, 324, 'UP'), round(4.9E-324, 323, 'UP')]"
            )
        ) == [[2.225073858507201e-308, 2.2250738585072014e-308, 5e-324, 1e-323]]


def test_round_to_0_places_rounds_half_toward_positive_infinity_as_round_of_one_argument_does(
    cypher_pm: PersistenceManager,
):
    # The spec's v0.6.8 steps for drivine4j#278, until the spec is synced past the cases of open issues: to 0 places
    # Neo4j rounds as Java's Math.round does, half toward +infinity; to more, half away from 0.
    assert cypher_pm.query(
        spec(
            "RETURN [round(-2.5, 0), round(-1.5, 0), round(-0.5, 0), round(2.5, 0), round(-2.6, 0), round(-2.55, 1), "
            "round(-2.5, 0.7)]"
        )
    ) == [[-2.0, -1.0, 0.0, 3.0, -3.0, -2.6, -2.0]]
    assert cypher_pm.query(spec("UNWIND [0, -0.5, 1, null] AS p RETURN round(-2.5, p)")) == [
        -2.0,
        -2.0,
        -2.5,
        None,
    ]
    # Over an aggregate, where nothing is shared, with the precision written or read from a row, and in a WHERE.
    assert cypher_pm.query(
        spec(
            "UNWIND [-1.0, -1.5] AS x WITH x, 0 AS p "
            "RETURN p, [round(sum(x), 0), round(sum(x), p), round(sum(x), 1), round(avg(x) * 2, p)]"
        )
    ) == [[0, [-2.0, -2.0, -2.5, -2.0]]]
    assert cypher_pm.query(
        spec("UNWIND [-2.5, -0.5, 0.5, 2.5] AS x WITH x WHERE round(x, 0) >= 0 RETURN collect(x)")
    ) == [[-0.5, 0.5, 2.5]]


def test_round_of_zero_gives_zero_or_null_to_a_null_precision_and_fails_on_a_negative_one(
    cypher_pm: PersistenceManager,
):
    # Zero, which Oracle gives back without rounding, gives zero wherever it is read, among rows that are not zero too.
    clear(cypher_pm)
    cypher_pm.execute(spec("UNWIND [0.0, 1.25, 0.0, -2.5] AS x CREATE (:Round {x: x})"))
    assert cypher_pm.query(spec("MATCH (n:Round) WHERE round(n.x, 400) = 0 RETURN count(*)")) == [2]
    assert cypher_pm.query(
        spec("MATCH (n:Round) WHERE round(n.x, 2) = 0 AND round(n.x, 1, 'UP') = 0 RETURN count(*)")
    ) == [2]
    assert cypher_pm.query(spec("MATCH (n:Round) RETURN round(n.x, 1) AS r, count(*) AS c ORDER BY r")) == [
        [-2.5, 1],
        [0.0, 2],
        [1.3, 1],
    ]
    assert cypher_pm.query(spec("MATCH (n:Round) RETURN n.x ORDER BY round(n.x, 1, 'UP') DESC")) == [
        1.25,
        0.0,
        0.0,
        -2.5,
    ]
    assert cypher_pm.query(
        spec(
            "UNWIND [0.0, 0.0] AS x RETURN [round(sum(x), 2), round(avg(x), 400), round(min(x), 2, 'HALF_UP'), "
            "round(sum(x), 1, 'CEILING')]"
        )
    ) == [[0.0, 0.0, 0.0, 0.0]]
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) WITH n.x AS x WHERE round(x, 2) = 0 "
            "RETURN [round(sum(x), 2), round(avg(x), 400), round(sum(x), 1, 'CEILING')]"
        )
    ) == [[0.0, 0.0, 0.0]]
    assert cypher_pm.query(
        spec(
            "UNWIND range(0, 2999) AS i WITH toFloat(i % 3) * 0.5 AS x "
            "RETURN [sum(round(x, 400)), sum(round(x, 0)), sum(round(x, 0, 'UP')), count(*)]"
        )
    ) == [[1500.0, 2000.0, 2000.0, 3000]]
    assert cypher_pm.query(
        spec(
            "UNWIND range(0, 2999) AS i WITH toFloat(i % 3) * 0.5 AS x "
            "WHERE round(x, 2) = 0 OR round(x, 400, 'UP') > 0 RETURN count(*)"
        )
    ) == [3000]
    assert cypher_pm.query(spec("RETURN [x IN [0.0, 1.55, 0.0] | round(x, 1)]")) == [[0.0, 1.6, 0.0]]
    assert cypher_pm.query(
        spec("RETURN [round($z, 2), round($z, 400, 'FLOOR'), round(0, 2), round(0.0, 2, 'UP')]", {"z": 0.0})
    ) == [[0.0, 0.0, 0.0, 0.0]]
    # The precision is read before zero is given back: a null one gives null, and a negative one from data fails, under
    # an aggregate too, where it is written out where it is read.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) WHERE n.x = 0 RETURN [round(0.0, null), round(0, null, 'HALF_UP'), round(n.x, n.none), "
            "round(n.x, n.none, 'UP')]"
        )
    ) == [[None, None, None, None], [None, None, None, None]]
    cypher_pm.execute(spec("CREATE (:Round {x: 0.0, r: -2})"))
    message = None if cypher_pm.type is DatabaseType.NEO4J else re.escape(NEGATIVE_PRECISION)
    for statement in [
        "MATCH (n:Round) WHERE n.r IS NOT NULL WITH n.x AS x, n.r AS r RETURN r, round(sum(x), r)",
        "MATCH (n:Round) WHERE n.r IS NOT NULL WITH n.x AS x, n.r AS r RETURN r, round(min(x), r, 'HALF_UP')",
    ]:
        with pytest.raises(Exception, match=message):  # noqa: PT011
            cypher_pm.query(spec(statement))


def test_a_negative_precision_from_data_fails_only_on_a_row_that_reads_it_in_a_where_and_an_aggregate_too(
    cypher_pm: PersistenceManager,
):
    # The spec reads a precision from data since v0.6.4; here, in a WHERE and an aggregate, and on rows filtered out.
    # Neo4j fails each with 22N03; a SQL store with the compiler's message.
    clear(cypher_pm)
    cypher_pm.execute(spec("CREATE (:Round {r: -2, k: 1}), (:Round {r: 1, k: 2})"))
    message = None if cypher_pm.type is DatabaseType.NEO4J else re.escape(NEGATIVE_PRECISION)
    for statement in [
        "MATCH (n:Round) RETURN round(1234.5, n.r)",
        "MATCH (n:Round) RETURN round(1234.5, n.r, 'HALF_UP')",
        "MATCH (n:Round) WHERE round(1234.5, n.r) > 0 RETURN n.k",
        "MATCH (n:Round) RETURN sum(round(1234.5, n.r))",
        "MATCH (n:Round) RETURN collect(round(1234.5, n.r, 'HALF_UP'))",
        # A constant the compiler does not read as given is worked out by the store, as one from data.
        "RETURN round(1234.5, 1 - 3)",
        "RETURN round(1234.5, toInteger('-2'))",
        # Of an aggregate, where nothing is shared and the check is written inline, and of zero, which Oracle gives
        # back unrounded: the precision is still read first.
        "UNWIND [0.0, 0.0] AS x WITH x, -2 AS r RETURN r, round(sum(x), r)",
        "UNWIND [0.0, 0.0] AS x WITH x, -2 AS r RETURN r, round(sum(x), r, 'HALF_UP')",
        "MATCH (n:Round) UNWIND [0.0, 0.0] AS x WITH n.r AS r, x RETURN r, round(sum(x), r)",
        "MATCH (n:Round) UNWIND [0.0, 0.0] AS x WITH n.r AS r, x RETURN r, round(sum(x), r, 'HALF_UP')",
        "UNWIND [0.0, 0.0] AS x RETURN round(sum(x), -2)",
        "UNWIND [0.0, 0.0] AS x RETURN round(sum(x), -2, 'HALF_UP')",
    ]:
        with pytest.raises(Exception, match=message):  # noqa: PT011
            cypher_pm.query(spec(statement))
    assert cypher_pm.query(spec("MATCH (n:Round) WHERE n.r >= 0 RETURN round(1234.5, n.r)")) == [1234.5]
    assert cypher_pm.query(spec("MATCH (n:Nothing) RETURN round(1234.5, 1 - 3)")) == []
    assert cypher_pm.query(spec("UNWIND [] AS x RETURN round(1234.5, 1 - 3)")) == []
    assert cypher_pm.query(spec("MATCH (n:Round) WHERE n.k > 5 RETURN round(1234.5, toInteger('-2'))")) == []
    assert cypher_pm.query(spec("MATCH (n:Round) WHERE n.k = 2 RETURN sum(round(1234.5, n.r))")) == [1234.5]
    # A null to round is null, whatever the precision, as Neo4j gives it.
    assert cypher_pm.query(
        spec("MATCH (n:Round) WHERE n.k = 1 RETURN [round(n.none, n.r), round(n.none, n.r, 'HALF_UP')]")
    ) == [[None, None]]
    assert cypher_pm.query(
        spec("MATCH (n:Round) RETURN CASE WHEN n.r < 0 THEN 0.0 ELSE round(1234.5, n.r) END ORDER BY n.k")
    ) == [0.0, 1234.5]
    assert cypher_pm.query(spec("RETURN [round(1234.5, 3 - 1), round(1234.5, toInteger('2'))]")) == [[1234.5, 1234.5]]


def _float_count(function: str, what: str) -> str:
    """What the compiler refuses a written float start or length `what` of the function with."""
    return re.escape(f"{function}() takes an integer for its {what}, not a float, as in Neo4j.")


def _count_out_of_range(function: str, what: str) -> str:
    """What the compiler refuses a start or length `what` of the function out of range with."""
    return re.escape(f"{function}() takes a {what} from 0 to 2147483647, as in Neo4j.")


def test_substring_left_and_right_refuse_a_written_float_and_a_given_start_or_length_out_of_range(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: a start or a length (#263). Neo4j refuses a float it knows to be one whatever the text; a
    # bound one it cuts toward zero. It refuses one out of range only where the text is not null.
    compiles = cypher_pm.type is not DatabaseType.NEO4J
    empty = None if cypher_pm.type is DatabaseType.ORACLE else ""
    for statement, parameters, message in [
        ("RETURN left('hello', 2.7)", {}, _float_count("left", "length")),
        ("RETURN left('hello', 2.0)", {}, _float_count("left", "length")),
        ("RETURN right('hello', -0.5)", {}, _float_count("right", "length")),
        ("RETURN substring('hello', 1.0)", {}, _float_count("substring", "start")),
        ("RETURN substring('hello', 1, 2.6)", {}, _float_count("substring", "length")),
        ("RETURN left(null, 2.7)", {}, _float_count("left", "length")),
        ("RETURN left('hello', -1)", {}, _count_out_of_range("left", "length")),
        ("RETURN right('hello', -1)", {}, _count_out_of_range("right", "length")),
        ("RETURN substring('hello', -1, 2)", {}, _count_out_of_range("substring", "start")),
        ("RETURN substring('hello', 1, -1)", {}, _count_out_of_range("substring", "length")),
        ("RETURN left('hello', 9223372036854775807)", {}, _count_out_of_range("left", "length")),
        ("RETURN substring($t, $s)", {"t": "hello", "s": -1}, _count_out_of_range("substring", "start")),
        # Bound, -1.5 is cut to -1, and 1.0E20 is past the greatest.
        ("RETURN left('hello', $n)", {"n": -1.5}, _count_out_of_range("left", "length")),
        ("RETURN left('hello', $n)", {"n": 1.0e20}, _count_out_of_range("left", "length")),
        # Negated, a bound number is negated before it is cut: -2.7 is -2.
        ("RETURN left('hello', -$n)", {"n": 2.7}, _count_out_of_range("left", "length")),
    ]:
        with pytest.raises(Exception, match=message if compiles else None):  # noqa: PT011
            cypher_pm.query(spec(statement, parameters))
    assert cypher_pm.query(
        spec(
            "RETURN [left('hello', 2), right('hello', 2), substring('hello', 1), substring('hello', 1, 2), "
            "left('hello', null), substring('hello', null), substring('hello', 1, null), left(null, -1), "
            "right(null, -1), substring(null, 1, -1), left('hello', 10), left('hello', 0), substring('hello', 10)]"
        )
    ) == [["he", "lo", "ello", "el", None, None, None, None, None, None, "hello", empty, empty]]
    assert cypher_pm.query(
        spec(
            "RETURN [left($t, $m), left('hello', $i), left('hello', $f), substring('hello', $s, $l), "
            "left('hello', $q), right($t, $f), left('hello', $p.f), left($p.none, $m), left('hello', -$m), "
            "left('hello', -$q)]",
            {"t": None, "m": -1, "i": 2, "f": 2.7, "s": 1.7, "l": 2.6, "q": -0.5, "p": {"f": 2.7}},
        )
    ) == [[None, "he", "he", "el", empty, None, "he", None, "h", empty]]
    assert cypher_pm.query(spec("UNWIND $xs AS c RETURN left('hello', c)", {"xs": [2.7, None]})) == ["he", None]
    # The greatest start or length is taken: a start past the text gives the empty text, written, bound or from a list.
    assert cypher_pm.query(
        spec(
            "RETURN [substring('hello', 2147483647), left('hello', 2147483647), right('hello', 2147483647), "
            "substring('hello', 1, 2147483647), substring('hello', $m)]",
            {"m": 2147483647},
        )
    ) == [[empty, "hello", "hello", "ello", empty]]
    assert cypher_pm.query(spec("UNWIND $xs AS s RETURN substring('hello', s)", {"xs": [2147483647]})) == [empty]


def test_a_bound_start_or_length_that_is_nan_is_0_and_one_that_is_an_infinity_out_of_range_as_java_casts_them(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet (#143): Neo4j casts a double to a long, as Java does, NaN to 0 and an infinity to the
    # greatest or least.
    compiles = cypher_pm.type is not DatabaseType.NEO4J
    empty = None if cypher_pm.type is DatabaseType.ORACLE else ""
    assert cypher_pm.query(spec("RETURN [left('hello', $n), substring('hello', $n)]", {"n": float("nan")})) == [
        [empty, "hello"]
    ]
    for n in [float("inf"), float("-inf")]:
        with pytest.raises(Exception, match=_count_out_of_range("left", "length") if compiles else None):  # noqa: PT011
            cypher_pm.query(spec("RETURN left('hello', $n)", {"n": n}))
    if compiles:
        # A Decimal, which the Neo4j driver does not send, is read as the number it is.
        assert cypher_pm.query(
            spec("RETURN [left('hello', $n), substring('hello', $s)]", {"n": Decimal("2.7"), "s": Decimal("1")})
        ) == [["he", "ello"]]


def test_a_start_or_length_from_data_is_cut_toward_zero_and_one_out_of_range_fails_only_on_a_row_that_reads_it(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: a start or a length read from data (#263), in a WHERE and an aggregate too, and on rows
    # filtered out.
    compiles = cypher_pm.type is not DatabaseType.NEO4J
    empty = None if cypher_pm.type is DatabaseType.ORACLE else ""
    clear(cypher_pm)
    cypher_pm.execute(
        spec(
            "CREATE (:Count {k: 1, f: 2.7, w: 2.0, q: -0.5, r: -1.5, m: -1, h: 2147483647.9, hp: 2147483648.5, "
            "t: 'hello'}), (:Count {k: 2, m: 1, t: 'hello'}), (:Count {k: 3, m: -1, r: -1.5, hp: 2147483648.5})"
        )
    )
    for statement, message in [
        ("MATCH (n:Count) RETURN left(n.t, n.m)", _count_out_of_range("left", "length")),
        ("MATCH (n:Count) WHERE left(n.t, n.m) = 'h' RETURN n.k", _count_out_of_range("left", "length")),
        ("MATCH (n:Count) RETURN collect(right(n.t, n.m))", _count_out_of_range("right", "length")),
        ("MATCH (n:Count) RETURN substring(n.t, n.m)", _count_out_of_range("substring", "start")),
        ("MATCH (n:Count) RETURN substring(n.t, 0, n.r)", _count_out_of_range("substring", "length")),
        # -1.5 is cut to -1, and 2147483648.5 to past the greatest.
        ("MATCH (n:Count) RETURN left(n.t, n.r)", _count_out_of_range("left", "length")),
        ("MATCH (n:Count) RETURN left(n.t, n.hp)", _count_out_of_range("left", "length")),
        # Written, below 0, beside a text from data: it fails on the row whose text is not null.
        ("MATCH (n:Count) RETURN left(n.t, -1)", _count_out_of_range("left", "length")),
        # A constant the compiler does not read as given is worked out by the store, as one from data.
        ("RETURN left('hello', 1 - 3)", _count_out_of_range("left", "length")),
        ("RETURN substring('hello', toInteger('-2'))", _count_out_of_range("substring", "start")),
    ]:
        with pytest.raises(Exception, match=message if compiles else None):  # noqa: PT011
            cypher_pm.query(spec(statement))
    assert cypher_pm.query(
        spec(
            "MATCH (n:Count) WHERE n.k = 1 RETURN [left(n.t, n.f), right(n.t, n.w), substring(n.t, n.f), "
            "substring(n.t, n.q, n.f), left(n.t, n.h), left(n.t, n.f * 1), left(n.none, n.f), left(n.t, n.q)]"
        )
    ) == [["he", "lo", "llo", "he", "hello", "he", None, empty]]
    assert cypher_pm.query(
        spec("MATCH (n:Count) WHERE n.k = 1 RETURN [substring(n.t, n.h), substring(n.t, 0, n.h)]")
    ) == [[empty, "hello"]]
    # Grouped by, as a key beside an aggregate.
    assert cypher_pm.query(spec("MATCH (n:Count) WHERE n.k = 2 RETURN substring(n.t, n.m), count(*)")) == [["ello", 1]]
    # Out of range beside a null text is null, as in Neo4j, and so is a null start or length.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Count) WHERE n.k = 3 RETURN [left(n.t, n.m), substring(n.t, n.m), left(n.t, -1), right(n.t, n.r)]"
        )
    ) == [[None, None, None, None]]
    # A constant out of range, which the compiler does not read as given, and one past SQL's integer: null beside a
    # null text, and not refused for the row filtered out.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Count) WHERE n.k = 3 RETURN [left(n.t, 1 - 3), substring(n.t, 0, 1 - 3), left(n.t, n.hp), "
            "left(n.t, 2147483647 + 1)]"
        )
    ) == [[None, None, None, None]]
    # Out of range beside a text of constants alone, on no row: the failure reads the text, so no store works it out
    # before a row, given or not.
    assert (
        cypher_pm.query(
            spec(
                "MATCH (n:Count) WHERE n.k = 99 RETURN [left(toUpper('hello'), -1), left('hello', 1 - 3), "
                "substring(toUpper('hello'), 0, 1 - 3)]"
            )
        )
        == []
    )
    assert cypher_pm.query(
        spec(
            "MATCH (n:Count) WHERE n.k = 2 RETURN [left(n.t, n.none), substring(n.t, n.none), "
            "substring(n.t, 1, n.none)]"
        )
    ) == [[None, None, None]]
    assert cypher_pm.query(
        spec("MATCH (n:Count) RETURN CASE WHEN n.m < 0 THEN 'none' ELSE left(n.t, n.m) END ORDER BY n.k")
    ) == ["none", "h", "none"]


def test_a_union_in_call_whose_queries_return_a_list_with_an_identity_beside_one_without_compiles(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the queries are compiled again without identities, so their columns match.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "CALL { MATCH (:Person {name: 'Keanu'})-[r:ACTED_IN]->() RETURN collect([r]) AS l UNION RETURN [] AS l } "
            "RETURN size(l) ORDER BY size(l)"
        )
    ) == [0, 3]
    assert cypher_pm.query(
        spec(
            "CALL { MATCH (:Person {name: 'Keanu'})-[r:KNOWS]->() RETURN {x: r} AS m UNION RETURN {x: 1} AS m } "
            "RETURN count(*)"
        )
    ) == [2]


def test_a_case_that_gives_a_list_of_relationships_or_of_texts_is_read_and_gathered_but_on_oracle(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: on Oracle the CASE's results are worked out first; gathered, it closes the session, so it is
    # refused.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Keanu'})-[r:ACTED_IN]->(m) WITH r, m ORDER BY m.title "
            "RETURN CASE WHEN m.released > 2000 THEN [r] ELSE [m.title] END"
        )
    ) == [["Johnny Mnemonic"], ["The Matrix"], [{"role": "Neo"}]]
    # A CASE that reads an aggregate is worked out in the query that aggregates, where a subquery would group it again.
    assert cypher_pm.query(
        spec(
            "MATCH (a:Person)-[r:ACTED_IN]->(m) "
            "RETURN a.name, size(CASE WHEN count(*) > 1 THEN collect(r) ELSE [] END) ORDER BY a.name"
        )
    ) == [["Carrie", 2], ["Keanu", 3], ["Laurence", 0]]
    # Counted, by count() or the size() of its collect, it is read on Oracle too, as it was before.
    assert cypher_pm.query(
        spec(
            "MATCH (a:Person)-[r:ACTED_IN]->(m) RETURN a.name, "
            "size(collect(CASE WHEN m.released > 2000 THEN [r] ELSE [] END)), "
            "count(CASE WHEN m.released > 2000 THEN [r] ELSE [] END) ORDER BY a.name"
        )
    ) == [["Carrie", 2, 2], ["Keanu", 3, 3], ["Laurence", 1, 1]]
    gathered = (
        "MATCH (a:Person)-[r:ACTED_IN]->(m) WITH a, r, m ORDER BY m.title "
        "RETURN a.name, [x IN collect(CASE WHEN m.released > 2000 THEN [r] ELSE [m.title] END) | x[0]] ORDER BY a.name"
    )
    # With no ELSE, its null is the value of another kind.
    unmatched = (
        "MATCH (a:Person)-[r:ACTED_IN]->(m) WITH a, r, m ORDER BY m.title "
        "RETURN a.name, [x IN collect(CASE WHEN m.released > 2000 THEN [r] END) | x[0].role] ORDER BY a.name"
    )
    if cypher_pm.type is DatabaseType.ORACLE:
        for statement in (gathered, unmatched):
            with pytest.raises(DrivineError, match="On Oracle, an aggregate of a CASE that gives a list of nodes"):
                cypher_pm.query(spec(statement))
        return
    assert cypher_pm.query(spec(unmatched)) == [["Carrie", ["Trinity"]], ["Keanu", ["Neo"]], ["Laurence", []]]
    assert cypher_pm.query(spec(gathered)) == [
        ["Carrie", ["The Matrix", {"role": "Trinity"}]],
        ["Keanu", ["Johnny Mnemonic", "The Matrix", {"role": "Neo"}]],
        ["Laurence", ["The Matrix"]],
    ]


def test_a_walk_read_through_a_with_then_sorted_by_to_string_in_a_second_with_keeps_that_order(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the walk is a query inside the one the second WITH sorts, and its term, carried out, is bound
    # in no subquery (drivine4j#192).
    seeded(cypher_pm)
    rows = cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Laurence'})-[*]->(b) WITH b WITH b ORDER BY toString(coalesce(b.name, b.title)) "
            "RETURN coalesce(b.name, b.title)"
        )
    )
    assert rows == [
        "Carrie",
        "Johnny Mnemonic",
        "Keanu",
        "The Matrix",
        "The Matrix",
        "The Matrix",
        "The Matrix Reloaded",
        "The Matrix Reloaded",
    ]
