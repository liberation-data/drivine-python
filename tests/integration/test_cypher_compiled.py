"""
What the Cypher spec (tests/cypher) can't say, on Neo4j and on the SQL stores Cypher is compiled for: values that change
on every run (a UUID, the time), parameters of Python's temporal types, statements built in code, a cursor, and what
needs a transaction of its own. drivine4j keeps the same in its Cypher*CrossEngineTest.
"""

import contextlib
import dataclasses
import re
import time as time_module
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel

from drivine import (
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
from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.backend.postgres.postgres_graph import PostgresGraph
from drivine.model import stamps
from drivine.query.statement import cypher_statement
from tests.cypher.cypher_spec import load
from tests.integration.conftest import ORACLE, POSTGRES, SQL_STORES

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def spec(cypher: str, parameters: dict[str, Any] | None = None) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(cypher)).bind(parameters)


async def clear(pm: PersistenceManager) -> None:
    if pm.type is DatabaseType.NEO4J:
        await pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    else:
        await pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
        await pm.execute(QuerySpecification("DELETE FROM drivine_node"))


async def test_random_uuid_gives_a_new_uuid_each_time(cypher_pm: PersistenceManager):
    a, b = await cypher_pm.query(spec("UNWIND [1, 2] AS x RETURN randomUUID()"))
    assert a != b and UUID.fullmatch(a), (a, b)


async def test_a_day_and_a_moment_are_their_iso_text_on_a_sql_store(cypher_pm: PersistenceManager):
    day, moment = await cypher_pm.get_one(spec("RETURN [date(), localdatetime()]"))
    if cypher_pm.type is DatabaseType.NEO4J:
        # A graph engine's driver gives a day and a moment as values of its own: the standard library's here.
        assert isinstance(day, date) and isinstance(moment, datetime)
        return
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day), day
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6})?", moment), moment


async def test_a_statement_of_hundreds_of_creates_each_naming_nodes_made_before_it_makes_them_all(
    cypher_pm: PersistenceManager,
):
    await clear(cypher_pm)
    clauses = [
        f"CREATE (n{i}:CyWrite {{i: {i}}})" + (f" CREATE (n{i - 1})-[:NEXT]->(n{i})" if i else "") for i in range(300)
    ]
    await cypher_pm.query(spec("\n".join(clauses)))
    counted = "MATCH (n:CyWrite) OPTIONAL MATCH (n)-[r:NEXT]->() RETURN [count(DISTINCT n), count(r)]"
    assert await cypher_pm.query(spec(counted)) == [[300, 299]]


async def test_a_write_whose_text_holds_the_quote_its_function_is_made_in_runs(cypher_pm: PersistenceManager):
    # On Postgres a write is a function, its body quoted with dollars: a text in it of that quote must not end it.
    await clear(cypher_pm)
    assert await cypher_pm.query(spec("CREATE (n:CyWrite {name: 'a$cy$b'}) RETURN n.name")) == ["a$cy$b"]


async def test_drivines_stamp_is_written_as_a_nodes_property_is_and_read_back_as_one(cypher_pm: PersistenceManager):
    await clear(cypher_pm)
    stamp = f"`{stamps.PROPERTY}`"
    await cypher_pm.query(spec(f"CREATE (:CyWrite {{id: 1, {stamp}: 'aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb'}})"))
    read = spec(f"MATCH (n:CyWrite {{id: 1}}) RETURN n.{stamp}")
    assert await cypher_pm.query(read) == ["aaaaaaaaaaaaaaaa:bbbbbbbbbbbbbbbb"]
    await cypher_pm.query(spec(f"MATCH (n:CyWrite {{id: 1}}) SET {stamps.set_clause('n')}"))
    token, links = (await cypher_pm.get_one(read)).split(":")
    assert len(token) == 16 and token != "aaaaaaaaaaaaaaaa" and links == "bbbbbbbbbbbbbbbb", (token, links)
    properties = await cypher_pm.query(spec("MATCH (n:CyWrite {id: 1}) RETURN properties(n)"))
    assert properties == [{"id": 1, stamps.PROPERTY: f"{token}:{links}"}]
    await cypher_pm.query(spec(f"MATCH (n:CyWrite {{id: 1}}) REMOVE n.{stamp}"))
    assert await cypher_pm.query(read) == [None]


async def test_datetimes_epoch_milliseconds_and_timestamp_are_the_moment_the_statement_runs(
    cypher_pm: PersistenceManager,
):
    await clear(cypher_pm)
    before = int(datetime.now(UTC).timestamp() * 1000) - 60_000
    millis, stamp = await cypher_pm.get_one(
        spec("CREATE (n:CyWrite) SET n.at = datetime().epochMillis RETURN [n.at, timestamp()]")
    )
    after = int(datetime.now(UTC).timestamp() * 1000) + 60_000
    assert before <= millis <= after and before <= stamp <= after, (millis, stamp)


async def test_a_parameter_named_with_a_letter_past_ascii_is_bound(cypher_pm: PersistenceManager):
    # Cypher names a parameter with any letter; a SQL store binds the compiled statement's marker of it by that name.
    await clear(cypher_pm)
    assert await cypher_pm.query(spec("RETURN $größe", {"größe": 3})) == [3]
    written = spec("CREATE (n:CyWrite {größe: $größe}) RETURN n.größe", {"größe": 4})
    assert await cypher_pm.query(written) == [4]


async def test_a_parameter_of_a_date_or_a_zoned_datetime_is_a_temporal_value_of_its_kind(cypher_pm: PersistenceManager):
    found = await cypher_pm.get_one(
        spec(
            "RETURN {year: $day.year, later: $at > datetime('2024-01-01T00:00:00Z')}",
            {"day": date(2024, 3, 15), "at": datetime(2024, 6, 1, tzinfo=UTC)},
        )
    )
    assert found == {"year": 2024, "later": True}


async def test_a_parameter_of_a_time_a_local_or_zoned_datetime_or_a_timedelta_is_a_temporal_value_of_its_kind(
    cypher_pm: PersistenceManager,
):
    found = await cypher_pm.get_one(
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


async def test_a_zoned_parameter_keeps_its_kind_in_reduce(cypher_pm: PersistenceManager):
    found = await cypher_pm.query(
        spec(
            "RETURN reduce(s = false, x IN [1] | $a < $b)",
            {
                "a": datetime(2024, 3, 15, 10, 0, tzinfo=timezone(timedelta(hours=2))),
                "b": datetime(2024, 3, 15, 9, 0, tzinfo=UTC),
            },
        )
    )
    assert found == [True]


async def test_a_cursor_reads_a_cypher_statements_rows(cypher_pm: PersistenceManager):
    cursor_spec = CursorSpecification[Any](cypher_statement("UNWIND range(1, 5) AS i RETURN {i: i}")).batch_size(2)
    async with await cypher_pm.open_cursor(cursor_spec) as cursor:
        assert [it async for it in cursor] == [{"i": i} for i in range(1, 6)]


async def test_a_cursor_reads_the_rows_of_a_cypher_statement_that_writes_and_its_writes_are_kept(
    cypher_pm: PersistenceManager,
):
    if cypher_pm.type is DatabaseType.ORACLE:
        pytest.skip("On Oracle a write is a block, not read through a cursor: see the test of that below.")
    await clear(cypher_pm)
    # On Postgres the function the write is compiled to is made before the cursor is opened over it.
    writes = "UNWIND range(1, 3) AS i CREATE (n:CyWrite {i: i}) RETURN n.i"
    async with await cypher_pm.open_cursor(CursorSpecification[Any](cypher_statement(writes)).batch_size(2)) as cursor:
        assert [it async for it in cursor] == [1, 2, 3]
    assert await cypher_pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [3]


async def test_a_cursor_over_a_cypher_statement_that_writes_in_a_transaction_writes_though_closed_unread(
    cypher_pm: PersistenceManager,
):
    if cypher_pm.type is DatabaseType.ORACLE:
        pytest.skip("On Oracle a write is a block, not read through a cursor: see the test of that below.")
    await clear(cypher_pm)
    # On Postgres a server-side cursor would run the function the write is compiled to only at its first fetch.
    async with transaction():
        cursor = await cypher_pm.open_cursor(CursorSpecification[Any](cypher_statement("CREATE (n:CyWrite) RETURN n")))
        await cursor.close()
    assert await cypher_pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [1]


async def test_a_parameter_whose_json_is_longer_than_a_varchar2_is_read_and_written(cypher_pm: PersistenceManager):
    # Bound as its JSON text: on Oracle a CLOB past 32,767 bytes, which a query takes where a VARCHAR2 is refused.
    await clear(cypher_pm)
    ids = [f"id-{i:08d}" for i in range(5000)]
    assert await cypher_pm.query(spec("UNWIND $ids AS id RETURN count(id)", {"ids": ids})) == [5000]
    cursor_spec = CursorSpecification[Any](cypher_statement("UNWIND $ids AS id RETURN count(id)")).bind({"ids": ids})
    async with await cypher_pm.open_cursor(cursor_spec) as cursor:
        assert [it async for it in cursor] == [5000]
    await cypher_pm.query(spec("CREATE (n:CyWrite {ids: $ids})", {"ids": ids}))
    assert await cypher_pm.query(spec("MATCH (n:CyWrite) RETURN size(n.ids)")) == [5000]


async def test_a_parameter_of_fewer_characters_than_a_varchar2_takes_and_more_bytes_is_read(
    cypher_pm: PersistenceManager,
):
    # A character past the Basic Multilingual Plane is four bytes of UTF-8: this list's JSON is 10,351 characters, and
    # 37,351 bytes.
    texts = ["\U0001f600" * 20] * 450
    assert await cypher_pm.query(spec("UNWIND $texts AS text RETURN count(text)", {"texts": texts})) == [450]


async def test_one_parameter_of_forty_thousand_characters_is_read_and_written(cypher_pm: PersistenceManager):
    # One text past 32,767 bytes: on Oracle a CLOB, read and written alike. (Oracle won't compare a JSON value so long,
    # ORA-40180, nor read a text of more than 4,000 bytes out of JSON, as the README says: it is handed back whole.)
    await clear(cypher_pm)
    text = "x" * 40_000
    assert await cypher_pm.query(spec("RETURN $text", {"text": text})) == [text]
    await cypher_pm.query(spec("CREATE (n:CyWrite {text: $text})", {"text": text}))
    assert await cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n.text")) == [text]


async def test_a_set_or_an_iterator_is_bound_as_a_list_and_an_iterator_named_twice_is_read_once(
    cypher_pm: PersistenceManager,
):
    await clear(cypher_pm)
    await cypher_pm.query(spec("UNWIND [1, 2, 3] AS i CREATE (:CyWrite {id: i})"))
    # The Neo4j driver takes a list or a tuple, and no set or iterator.
    neo4j = cypher_pm.type is DatabaseType.NEO4J
    for ids in ((1, 2),) if neo4j else ({1, 2}, frozenset({1, 2}), (1, 2)):
        found = await cypher_pm.query(
            spec("MATCH (n:CyWrite) WHERE n.id IN $ids RETURN n.id ORDER BY n.id", {"ids": ids})
        )
        assert found == [1, 2]
    if neo4j:
        return
    twice = spec("MATCH (n:CyWrite) WHERE n.id IN $ids RETURN [n.id, size($ids)]", {"ids": (it for it in [3])})
    assert await cypher_pm.query(twice) == [[3, 1]]


async def test_bytes_are_bound_as_their_base64_text_as_drivine4j_writes_a_byte_array(cypher_pm: PersistenceManager):
    if cypher_pm.type is DatabaseType.NEO4J:
        pytest.skip("A graph engine keeps bytes as bytes; a SQL store's JSON has none, and drivine4j writes base64.")
    assert await cypher_pm.query(spec("RETURN [$b, $a]", {"b": b"\x00\xffdrivine", "a": bytearray(b"\x00\xff")})) == [
        ["AP9kcml2aW5l", "AP8="]
    ]


class Person(BaseModel):
    name: str
    born: date
    tags: list[str]


async def test_a_cypher_statements_rows_are_transformed_and_mapped_as_a_graph_engines_are(
    cypher_pm: PersistenceManager,
):
    await clear(cypher_pm)
    await cypher_pm.query(spec("CREATE (:CyWrite {name: 'Ada', born: date('1815-12-10'), tags: ['maths', 'engines']})"))
    ada = Person(name="Ada", born=date(1815, 12, 10), tags=["maths", "engines"])
    # A node is its properties, a temporal value its ISO text on a SQL store, which the model reads as a date.
    assert await cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n").transform(Person)) == [ada]
    mapped = spec("MATCH (n:CyWrite) RETURN n.name, n.tags, {born: n.born}").map(
        lambda row: Person(name=row[0], tags=row[1], born=row[2]["born"])
    )
    assert await cypher_pm.query(mapped) == [ada]
    cursor_spec = CursorSpecification[Any](cypher_statement("MATCH (n:CyWrite) RETURN n")).transform(Person)
    async with await cypher_pm.open_cursor(cursor_spec) as cursor:
        assert [it async for it in cursor] == [ada]


async def test_a_cypher_statement_takes_named_parameters_on_a_sql_store(cypher_pm: PersistenceManager):
    positional = QuerySpecification[Any](cypher_statement("RETURN $1")).bind([1])
    if cypher_pm.type is DatabaseType.NEO4J:
        assert await cypher_pm.query(positional) == [1]
        return
    with pytest.raises(DrivineError, match=r"takes named parameters \(\$name\), not a list of them"):
        await cypher_pm.query(positional)


@pytest.mark.parametrize("store", [it for it in SQL_STORES if {"postgres": POSTGRES, "oracle": ORACLE}[it]])
async def test_a_write_in_a_transaction_rolled_back_leaves_nothing_and_runs_again_after(
    store: str, factory: PersistenceManagerFactory
):
    pm = factory.get(store)
    await clear(pm)
    async with transaction(rollback=True):
        await pm.query(spec("CREATE (:CyWrite {name: 'Ada'})"))
        assert await pm.query(spec("MATCH (n:CyWrite) RETURN n.name")) == ["Ada"]
    assert await pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [0]
    # On Postgres the function the write was compiled to went with the transaction, and is made again.
    await pm.query(spec("CREATE (:CyWrite {name: 'Ada'})"))
    assert await pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [1]


@pytest.mark.parametrize("store", [it for it in SQL_STORES if {"postgres": POSTGRES, "oracle": ORACLE}[it]])
async def test_a_cypher_statement_is_compiled_against_the_tables_of_the_graph_its_datasource_names(
    store: str, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    name = "cy_named"
    if registry.registered(f"{store}_{name}") is None:
        properties = dataclasses.replace(ConnectionProperties.from_env(store), default_graph_path=name)
        registry.builder().with_properties(properties).register(f"{store}_{name}")
    pm = factory.get(f"{store}_{name}")
    await pm.execute(QuerySpecification((PostgresGraph(name) if store == "postgres" else OracleGraph(name)).ensuring))
    await pm.execute(QuerySpecification(f"DELETE FROM {name}_edge"))
    await pm.execute(QuerySpecification(f"DELETE FROM {name}_node"))
    await pm.query(spec("CREATE (:CyWrite {name: 'Ada'})"))
    assert await pm.query(spec("MATCH (n:CyWrite) RETURN n.name")) == ["Ada"]
    # A row of plain SQL is a dict of its columns on Postgres, and its one column's value on Oracle.
    made = await pm.get_one(QuerySpecification[Any](f"SELECT COUNT(*) AS made FROM {name}_node"))
    assert (made["made"] if isinstance(made, dict) else made) == 1
    await pm.execute(QuerySpecification(f"DELETE FROM {name}_node"))


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
async def test_nodes_of_two_labels_a_transaction_has_made_and_not_yet_committed_are_found_by_both(
    factory: PersistenceManagerFactory,
):
    pm = factory.get("oracle")
    await clear(pm)
    async with transaction(rollback=True):
        await pm.query(
            spec("CREATE (:CyWrite:Dog {name: 'Buddy'}), (:CyWrite:Cat {name: 'Tom'}), (:CyWrite:Dog {name: 'Max'})")
        )
        assert await pm.query(spec("MATCH (d:CyWrite:Dog) RETURN d.name ORDER BY d.name")) == ["Buddy", "Max"]
        assert await pm.query(spec("MATCH (c) WHERE c:Cat:CyWrite RETURN c.name")) == ["Tom"]


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
async def test_a_cypher_statement_that_writes_is_not_read_through_a_cursor_on_oracle(
    factory: PersistenceManagerFactory,
):
    cursor_spec = CursorSpecification[Any](cypher_statement("CREATE (n:CyWrite) RETURN n"))
    with pytest.raises(DrivineError, match="is a block on Oracle"):
        await factory.get("oracle").open_cursor(cursor_spec)


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
async def test_a_text_past_32_kb_set_on_two_nodes_beside_a_whole_float_or_not_leaves_the_session_open(
    factory: PersistenceManagerFactory,
):
    # Oracle crashed the session as the block freed its parameters, held in a JSON variable it had read once for each
    # row (#131). The crash could come after the rows did, so the session is asked again, on the one connection a
    # transaction holds; and a block parsed before rarely crashed, so each statement is new. The nodes are read back
    # whole: see "A parameter past 32 KB set on two nodes" in the spec.
    pm = factory.get("oracle")
    await clear(pm)
    text = "x" * 40_000
    written: dict[str, Any] = {}
    async with transaction(rollback=True):
        await pm.query(spec("CREATE (:CyWrite {id: 1}), (:CyWrite {id: 2})"))
        for i in range(3):
            await pm.query(spec(f"MATCH (n:CyWrite) SET n.a{i} = $a{i}", {f"a{i}": text}))
            await pm.query(
                spec(f"MATCH (n:CyWrite) SET n.b{i} = $b{i}, n.f{i} = $f{i}", {f"b{i}": text, f"f{i}": 2.0 + i})
            )
            written |= {f"a{i}": text, f"b{i}": text, f"f{i}": 2.0 + i}
            rows = await pm.query(spec("MATCH (n:CyWrite) RETURN properties(n) ORDER BY n.id"))
            assert rows == [{"id": it, **written} for it in (1, 2)]


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
async def test_a_text_past_32_kb_in_a_map_parameter_read_and_set_on_two_nodes_leaves_the_session_open(
    factory: PersistenceManagerFactory,
):
    # Oracle crashed the session where a statement read a value past 32 KB from a map parameter, by a property or by a
    # subscript, for each of two rows, taking the parameter as JSON first and then the value from it (#145): alone in
    # the parameter, or beside a whole float, which makes its text extended JSON. As above, on one connection, each
    # statement new, and the session asked again after it.
    pm = factory.get("oracle")
    await clear(pm)
    text = "x" * 40_000
    written: dict[str, Any] = {}
    async with transaction(rollback=True):
        await pm.query(spec("CREATE (:CyWrite {id: 1}), (:CyWrite {id: 2})"))
        for i in range(3):
            for j, p in enumerate(({"text": text}, {"text": text, "f": 2.0 + i})):
                k = i * 2 + j
                for read in ("$p.text", "$p['text']"):
                    cypher = f"MATCH (n:CyWrite) WHERE n.id > -{k} RETURN {read} ORDER BY n.id"
                    assert await pm.query(spec(cypher, {"p": p})) == [text, text]
                if "f" in p:
                    await pm.query(spec(f"MATCH (n:CyWrite) SET n.t{k} = $p.text, n.f{k} = $p.f", {"p": p}))
                    written |= {f"t{k}": text, f"f{k}": p["f"]}
                else:
                    await pm.query(spec(f"MATCH (n:CyWrite) SET n.t{k} = $p.text", {"p": p}))
                    written |= {f"t{k}": text}
                rows = await pm.query(spec("MATCH (n:CyWrite) RETURN properties(n) ORDER BY n.id"))
                assert rows == [{"id": it, **written} for it in (1, 2)]


async def seeded(pm: PersistenceManager) -> None:
    """The graph queries.yaml reads, written again."""
    await clear(pm)
    await pm.query(spec(load("queries.yaml").graph or ""))


async def test_collect_distinct_beside_keys_other_aggregates_an_order_a_page_and_a_variable_outside_its_aggregates(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: _aggregates_first's grouping WITH, and the projection over it.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, count(*) + size(collect(DISTINCT m.title)) "
            "ORDER BY count(*) DESC, p.name SKIP 1 LIMIT 1"
        )
    ) == [["Carrie", 4]]
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p, collect(DISTINCT m) AS ms WHERE size(ms) > 1 "
            "RETURN p.name, size(ms) ORDER BY p.name"
        )
    ) == [["Carrie", 2], ["Keanu", 3]]
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p, size(collect(DISTINCT m.title)) + p.born AS x "
            "RETURN p.name, x ORDER BY p.name"
        )
    ) == [["Carrie", 1969], ["Keanu", 1967], ["Laurence", 1962]]


async def test_collect_distinct_in_a_distinct_projection_ordered_by_a_keys_property_or_an_aggregate_no_item_is(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the projection over _aggregates_first's grouping WITH orders by what that WITH holds.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH DISTINCT p, size(collect(DISTINCT m.title)) AS n "
            "ORDER BY p.born DESC RETURN p.name, n"
        )
    ) == [["Carrie", 2], ["Keanu", 3], ["Laurence", 1]]
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) "
            "RETURN DISTINCT p.name, size(collect(DISTINCT m.title)) ORDER BY count(*)"
        )
    ) == [["Laurence", 1], ["Carrie", 2], ["Keanu", 3]]
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p.name AS name, m.title AS t "
            "RETURN name AS nm, size(collect(DISTINCT t)) "
            "ORDER BY any(x IN collect(DISTINCT t) WHERE x STARTS WITH 'J' AND name = 'Keanu'), nm"
        )
    ) == [["Carrie", 2], ["Laurence", 1], ["Keanu", 3]]


async def test_collect_distinct_beside_an_item_named_after_a_variable_another_item_is_ordered_by_that_name(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: in an order term, an item's name is that item, not the variable it shadows.
    await seeded(cypher_pm)
    rows = await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p AS person, p.name AS p, size(collect(DISTINCT m.title)) "
            "ORDER BY toLower(p)"
        )
    )
    assert [it[1:] for it in rows] == [["Carrie", 2], ["Keanu", 3], ["Laurence", 1]]


async def test_an_item_of_the_relationships_collect_gathers_is_read_in_the_projection_that_gathers_them(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: Oracle reads no aggregate from a subquery, so the item is read from what collect gathered.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[r:ACTED_IN]->() WHERE p.name <> 'Keanu' "
            "RETURN p.name, collect(r)[0].role ORDER BY p.name"
        )
    ) == [["Carrie", "Trinity"], ["Laurence", "Morpheus"]]
    assert await cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Laurence'})-[r:ACTED_IN]->() MATCH (:Person {name: 'Carrie'})-[s:ACTED_IN]->() "
            "RETURN size(collect(r) + collect(s)), (collect(r) + collect(s))[0].role, "
            "(collect(r) + collect(s))[-1].role"
        )
    ) == [[4, "Morpheus", "Trinity"]]
    assert await cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Laurence'})-[r:ACTED_IN]->() WITH collect(r) AS rs "
            "RETURN rs[9] IS NULL, rs[-9] IS NULL"
        )
    ) == [[True, True]]


async def test_collect_distinct_under_a_comprehension_a_quantifier_or_a_reduce_that_names_a_grouping_key_in_its_body(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: _aggregates_first's grouping WITH keeps each variable such a body names, outside the
    # aggregate.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p.name AS name, m.title AS t "
            "RETURN name, size([x IN collect(DISTINCT t) WHERE x <> name]), "
            "any(x IN collect(DISTINCT t) WHERE x = name), "
            "reduce(n = 0, x IN collect(DISTINCT t) | n + size(name)) ORDER BY name"
        )
    ) == [["Carrie", 2, False, 12], ["Keanu", 3, False, 15], ["Laurence", 1, False, 8]]


async def test_collect_distinct_beside_a_pattern_or_a_subquery_that_names_a_grouping_key(cypher_pm: PersistenceManager):
    # Not in the spec yet: _aggregates_first's grouping WITH keeps each variable a pattern or a subquery names, outside
    # the aggregate.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) "
            "WITH p, size(collect(DISTINCT m.title)) + COUNT { (p)-[:KNOWS]->() } AS x, "
            "size([(p)-[:KNOWS]->(f) | f.name] + collect(DISTINCT m.title)) AS y, "
            "size(collect(DISTINCT m.title)) > 2 OR EXISTS { (p)-[:KNOWS]->(:Person {name: 'Keanu'}) } AS z "
            "RETURN p.name, x, y, z ORDER BY p.name"
        )
    ) == [["Carrie", 2, 2, False], ["Keanu", 4, 4, True], ["Laurence", 2, 2, True]]


async def test_collect_distinct_of_dates_keeps_each_once_as_dates(cypher_pm: PersistenceManager):
    # Not in the spec yet: what _aggregates_first keeps of a collection is still of the kind collect gathered.
    dates = "UNWIND [date('2020-01-01'), date('2020-01-01'), date('2021-05-01')] AS d"
    assert await cypher_pm.query(spec(f"{dates} WITH collect(DISTINCT d) AS ds RETURN [x IN ds | x.year]")) == [
        [2020, 2021]
    ]
    assert await cypher_pm.query(spec(f"{dates} RETURN collect(DISTINCT d)[1].month")) == [5]


async def test_an_item_of_a_pattern_comprehensions_relationships_is_compared_by_its_id(cypher_pm: PersistenceManager):
    # Not in the spec yet: a pattern comprehension gathers their properties beside their ids, as collect does, but in a
    # subquery.
    await seeded(cypher_pm)
    neo = "[(a)-[r:ACTED_IN {role: 'Neo'}]->() | r]"
    assert await cypher_pm.query(
        spec(
            f"MATCH (a:Person {{name: 'Keanu'}}) WITH {neo} AS rs, a "
            f"RETURN [{neo}[0] = {neo}[1], {neo}[1] IN rs, {neo}[0].role = 'Neo']"
        )
    ) == [[False, True, True]]


async def test_a_list_comprehension_over_nodes_or_relationships_that_keeps_them_gives_a_list_of_them_compared_by_id(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: what _entity_comprehension keeps, with no projection or one of the item, is still a list of
    # nodes or relationships.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Keanu'})-[r:ACTED_IN {role: 'Neo'}]->() WITH collect(r) AS rs "
            "RETURN [[x IN rs WHERE x <> rs[0]][0] = rs[1], [x IN rs | x][0] = rs[1], "
            "size([x IN rs WHERE x = rs[0] | x]), "
            "rs[0] IN [x IN rs WHERE x <> rs[0]], [x IN rs | x.role]]"
        )
    ) == [[True, False, 1, False, ["Neo", "Neo"]]]
    assert await cypher_pm.query(
        spec(
            "MATCH p = (:Person {name: 'Laurence'})-[:KNOWS*2]->(b) "
            "RETURN [[n IN nodes(p) WHERE n.name <> 'Laurence'][0] = nodes(p)[1], b IN [n IN nodes(p) | n], "
            "[n IN nodes(p) WHERE n = b | n.name]]"
        )
    ) == [[True, True, ["Carrie"]]]


async def test_a_paths_nodes_or_relationships_added_together_are_a_list_of_them_and_of_no_path_null(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: lists of nodes no collect gathers, added, indexed and iterated by id; over a null list, null.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH p = (:Person {name: 'Laurence'})-[:KNOWS*2]->() "
            "RETURN [size(nodes(p) + nodes(p)), (nodes(p) + nodes(p))[3] = nodes(p)[0], "
            "(nodes(p) + nodes(p))[3] = nodes(p)[1], [n IN nodes(p) + nodes(p) | n.name], "
            "relationships(p)[1] IN relationships(p) + relationships(p), size(relationships(p) + relationships(p))]"
        )
    ) == [[6, True, False, ["Laurence", "Keanu", "Carrie", "Laurence", "Keanu", "Carrie"], True, 4]]
    assert await cypher_pm.query(
        spec(
            "OPTIONAL MATCH p = (:Nope)-->() RETURN [[n IN nodes(p) | n.name], [n IN nodes(p) WHERE true], nodes(p)[0]]"
        )
    ) == [[None, None, None]]


async def test_calls_that_write_nested_give_each_row_its_own_variables_with_the_nodes_they_return(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: a caller row at each depth, merged with a node returned from below it.
    await clear(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "UNWIND [1, 2] AS x CALL { WITH x CREATE (a:CyWrite {x: x}) WITH a, x "
            "CALL { WITH a, x CREATE (b:CyWrite {y: x * 10}) RETURN b } RETURN a, b } RETURN [x, a.x, b.y]"
        )
    ) == [[1, 1, 10], [2, 2, 20]]
    assert await cypher_pm.query(
        spec(
            "UNWIND [1, 2] AS i CALL { WITH i CREATE (n:CyWrite {i: i}) RETURN n } SET n.j = i * 10 "
            "RETURN [i, n.i, n.j] ORDER BY i"
        )
    ) == [[1, 1, 10], [2, 2, 20]]
    assert await cypher_pm.query(
        spec(
            "WITH date('2020-01-02') AS d CALL { CREATE (n:CyWrite) RETURN date('2021-03-04') AS e } "
            "RETURN [d.year, e.month]"
        )
    ) == [[2020, 3]]


async def test_a_foreach_in_calls_that_write_nested_three_deep_leaves_each_callers_row_as_it_was(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the FOREACH loops over rows of its own at the deepest depth, between the caller rows above it
    # being read and the rows the CALLs give.
    await clear(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "UNWIND [1, 2] AS x CALL { WITH x CREATE (a:CyWrite {x: x}) WITH a, x "
            "CALL { WITH a, x FOREACH (i IN [1, 2] | CREATE (:CyWrite {i: i * x})) CREATE (b:CyWrite {y: x * 10}) "
            "RETURN b } RETURN a, b } RETURN [x, a.x, b.y]"
        )
    ) == [[1, 1, 10], [2, 2, 20]]
    assert await cypher_pm.query(spec("MATCH (n:CyWrite) WHERE n.i IS NOT NULL RETURN [count(n), sum(n.i)]")) == [
        [4, 9]
    ]


async def test_collect_distinct_beside_a_key_that_holds_a_parameter_or_a_subquery(cypher_pm: PersistenceManager):
    # Not in the spec yet: _held_first works such a key out first in the grouping WITH _aggregates_first makes, as
    # Oracle groups by no subquery, and binds a parameter as a marker of its own each time it is named.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name + $s AS k, size(collect(DISTINCT m.title + $s)) "
            "ORDER BY k",
            {"s": "!"},
        )
    ) == [["Carrie!", 2], ["Keanu!", 3], ["Laurence!", 1]]
    assert await cypher_pm.query(
        spec(
            "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN COUNT { (p)-[:KNOWS]->() } AS k, "
            "size(collect(DISTINCT m.title)) AS c ORDER BY k, c"
        )
    ) == [[0, 2], [1, 3]]


async def test_a_map_or_a_list_that_holds_a_relationship_is_equal_to_one_whose_numbers_or_keys_are_written_otherwise(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: identities compare as JSON values, not as their text.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Keanu'})-[r1:KNOWS]->() RETURN [{x: r1, y: 1} = {x: r1, y: 1.0}, "
            "[r1, 1] = [r1, 1.0], {a: 1, x: r1} = {x: r1, a: 1}, {x: r1, y: 1} IN [{x: r1, y: 1.0}]]"
        )
    ) == [[True, True, True, True]]


HUB_GRAPH = "cy_hub"
"""The graph a hub is written to on a SQL store: tables of its own, apart from those the other tests read."""


@contextlib.asynccontextmanager
async def hub(
    pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory, leaves: int
) -> AsyncGenerator[PersistenceManager]:
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
        await apart.execute(QuerySpecification(graph.ensuring))
        nodes, edges = f"{HUB_GRAPH}_node", f"{HUB_GRAPH}_edge"

    async def cleared() -> None:
        if nodes is None or edges is None:
            await apart.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
        else:
            await apart.execute(QuerySpecification(f"DELETE FROM {edges}"))
            await apart.execute(QuerySpecification(f"DELETE FROM {nodes}"))

    await cleared()
    try:
        made = f"CREATE (h:Hub) WITH h UNWIND range(1, {leaves}) AS i CREATE (h)-[:R]->(:Leaf) RETURN count(*)"
        assert await apart.query(spec(made)) == [leaves]
        yield apart
    finally:
        await cleared()


async def test_a_list_of_a_few_thousand_relationships_is_equal_by_identity_to_a_maps_or_a_lists(
    cypher_pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    # Not in the spec yet: their identity is larger than Oracle's = compares (ORA-40180), so Oracle compares it by
    # JSON_EQUAL.
    async with hub(cypher_pm, registry, factory, 2500) as pm:
        assert await pm.query(
            spec(
                "MATCH (:Hub)-[r:R]->() WITH collect(r) AS rs "
                "RETURN [[rs][0] = rs, {x: rs}.x = rs, rs IN [rs], size(rs)]"
            )
        ) == [[True, True, True, 2500]]


async def test_collect_distinct_keeps_a_few_thousand_lists_or_maps_holding_a_relationship_once_each_in_seconds(
    cypher_pm: PersistenceManager, registry: DatabaseRegistry, factory: PersistenceManagerFactory
):
    # Not in the spec yet: each was compared with every one before it, which took 20 to 40 seconds on Oracle.
    async with hub(cypher_pm, registry, factory, 5000) as pm:
        started = time_module.monotonic()
        assert await pm.query(
            spec(
                "MATCH p = (:Hub)-[r:R]->() "
                "RETURN [size(collect(DISTINCT relationships(p))), size(collect(DISTINCT {x: r}))]"
            )
        ) == [[5000, 5000]]
        seconds = time_module.monotonic() - started
        assert seconds < 15, seconds


async def test_a_whole_float_past_2_to_the_53_written_in_the_statement_or_given_equals_the_same_float_worked_out(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: Postgres writes such a float out in full, by its shortest decimal, not the double's exact
    # value.
    assert await cypher_pm.query(
        spec("RETURN [toFloat('1e24') = 1e24, toFloat('1e300') = 1e300, toFloat('1e24') = $x]", {"x": 1e24})
    ) == [[True, True, True]]


async def test_a_number_or_a_text_the_statement_gives_added_to_a_text_is_written_as_to_string_writes_it(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: a side the statement gives is written out at compile time, and the other side as it is.
    assert await cypher_pm.query(
        spec(
            "RETURN ['a' + 1e7, 1e7 + 'a', 'a' + 2.0, 'a' + $f, $i + 'b', 'a' + $s, $s + 1.5e-7, 'x' + 10, "
            "$m.n + 'y', $j + 'b']",
            {"f": 1.0e-4, "i": 3, "s": "s", "m": {"n": 0.5}, "j": 4},
        )
    ) == [["a1.0E7", "1.0E7a", "a2.0", "a1.0E-4", "3b", "as", "s1.5E-7", "x10", "0.5y", "4b"]]


async def test_nan_or_an_infinity_the_statement_gives_added_to_a_text_is_written_as_to_string_writes_it(
    cypher_pm: PersistenceManager,
):
    assert await cypher_pm.query(
        spec(
            "RETURN ['a' + $n, $p + 'b', 'c' + $q, $n + toString(1.5)]",
            {"n": float("nan"), "p": float("inf"), "q": float("-inf")},
        )
    ) == [["aNaN", "Infinityb", "c-Infinity", "NaN1.5"]]


async def test_a_quotient_of_floats_is_written_without_the_zeros_its_scale_leaves(cypher_pm: PersistenceManager):
    # The spec's float arithmetic steps skip Postgres, which works a float out in exact decimal (drivine4j#188): these
    # it gives as a double does.
    assert await cypher_pm.query(
        spec("RETURN [4.0 / 2, 7 / 2.0, 10.0 / 4, toString(4.0 / 2), toString(10.0 / 4), toString(7 / 2.0)]")
    ) == [[2.0, 3.5, 2.5, "2.0", "2.5", "3.5"]]


async def test_to_string_of_a_double_past_oracles_number_range_writes_its_shortest_digits(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: Oracle writes such a double from its own text, not from TO_CHAR's 15 digits.
    assert await cypher_pm.query(
        spec(
            "RETURN [toString(1.7976931348623157e308), toString(1.5e300), toString(1.0e-310), toString($a), "
            "toString($b)]",
            {"a": -2.5e-320, "b": -1.5e300},
        )
    ) == [["1.7976931348623157E308", "1.5E300", "1.0E-310", "-2.5E-320", "-1.5E300"]]
    assert await cypher_pm.query(spec("RETURN toString(5e-324)")) == ["4.9E-324"]


async def test_round_reads_a_precision_from_data_or_a_whole_float_as_a_whole_number_toward_zero_and_a_null_as_null(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: its round() case takes the precision as written or given. A negative one from data is #148.
    await clear(cypher_pm)
    await cypher_pm.execute(spec("CREATE (:Round {p: 1.7, q: -0.5, w: 2.0})"))
    assert await cypher_pm.query(
        spec(
            "MATCH (n:Round) RETURN [round(2.555, n.p), round(2.55, n.p, 'UP'), round(2.5, n.q), "
            "round(2.5, n.q, 'HALF_UP'), round(2.555, n.w), round(2.555, 2.0, 'HALF_UP')]"
        )
    ) == [[2.6, 2.6, 3.0, 3.0, 2.56, 2.56]]
    assert await cypher_pm.query(
        spec(
            "RETURN [round(2.5, 0, 'HALF_UP'), round(-2.5, 0, 'HALF_EVEN'), round(2.5, 0, 'DOWN'), "
            "round(2.1, 0, 'CEILING'), round(2.9, 0, 'FLOOR'), round(-2.1, 0, 'UP'), round(-2.5, 0, 'HALF_DOWN')]"
        )
    ) == [[3.0, -2.0, 2.0, 3.0, 2.0, -3.0, -2.0]]
    assert await cypher_pm.query(
        spec("RETURN [round(null, 2), round(2.5, null), round(null, 2, 'UP'), round(2.5, null, 'UP')]")
    ) == [[None, None, None, None]]


async def test_a_union_in_call_whose_queries_return_a_list_with_an_identity_beside_one_without_compiles(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the queries are compiled again without identities, so their columns match.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "CALL { MATCH (:Person {name: 'Keanu'})-[r:ACTED_IN]->() RETURN collect([r]) AS l UNION RETURN [] AS l } "
            "RETURN size(l) ORDER BY size(l)"
        )
    ) == [0, 3]
    assert await cypher_pm.query(
        spec(
            "CALL { MATCH (:Person {name: 'Keanu'})-[r:KNOWS]->() RETURN {x: r} AS m UNION RETURN {x: 1} AS m } "
            "RETURN count(*)"
        )
    ) == [2]


async def test_a_case_that_gives_a_list_of_relationships_or_of_texts_is_read_and_gathered_but_on_oracle(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: on Oracle the CASE's results are worked out first; gathered, it closes the session, so it is
    # refused.
    await seeded(cypher_pm)
    assert await cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Keanu'})-[r:ACTED_IN]->(m) WITH r, m ORDER BY m.title "
            "RETURN CASE WHEN m.released > 2000 THEN [r] ELSE [m.title] END"
        )
    ) == [["Johnny Mnemonic"], ["The Matrix"], [{"role": "Neo"}]]
    # A CASE that reads an aggregate is worked out in the query that aggregates, where a subquery would group it again.
    assert await cypher_pm.query(
        spec(
            "MATCH (a:Person)-[r:ACTED_IN]->(m) "
            "RETURN a.name, size(CASE WHEN count(*) > 1 THEN collect(r) ELSE [] END) ORDER BY a.name"
        )
    ) == [["Carrie", 2], ["Keanu", 3], ["Laurence", 0]]
    # Counted, by count() or the size() of its collect, it is read on Oracle too, as it was before.
    assert await cypher_pm.query(
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
                await cypher_pm.query(spec(statement))
        return
    assert await cypher_pm.query(spec(unmatched)) == [["Carrie", ["Trinity"]], ["Keanu", ["Neo"]], ["Laurence", []]]
    assert await cypher_pm.query(spec(gathered)) == [
        ["Carrie", ["The Matrix", {"role": "Trinity"}]],
        ["Keanu", ["Johnny Mnemonic", "The Matrix", {"role": "Neo"}]],
        ["Laurence", ["The Matrix"]],
    ]


async def test_a_walk_read_through_a_with_then_sorted_by_to_string_in_a_second_with_keeps_that_order(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: the walk is a query inside the one the second WITH sorts, and its term, carried out, is bound
    # in no subquery (drivine4j#192).
    await seeded(cypher_pm)
    rows = await cypher_pm.query(
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
