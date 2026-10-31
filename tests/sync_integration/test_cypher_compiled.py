# Written by scripts/unasync.py from tests/integration/test_cypher_compiled.py:
# change that, and run the script again.
"""
What the Cypher spec (tests/cypher) can't say, on Neo4j and on the SQL stores Cypher is compiled for: values that change
on every run (a UUID, the time), parameters of Python's temporal types, statements built in code, a cursor, and what
needs a transaction of its own. drivine4j keeps the same in its Cypher*CrossEngineTest.
"""

import contextlib
import dataclasses
import json
import math
import re
import time as time_module
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from drivine.cypher.sql.sql_cypher_compiler import PRECISION_OUT_OF_RANGE
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

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
"""A random version 4 UUID, in lower case, as Neo4j's `randomUUID()` gives one."""


def spec(cypher: str, parameters: dict[str, Any] | None = None) -> QuerySpecification[Any]:
    return QuerySpecification[Any](cypher_statement(cypher)).bind(parameters)


def clear(pm: PersistenceManager) -> None:
    if pm.type is DatabaseType.NEO4J:
        pm.execute(QuerySpecification("MATCH (n) DETACH DELETE n"))
    else:
        pm.execute(QuerySpecification("DELETE FROM drivine_edge"))
        pm.execute(QuerySpecification("DELETE FROM drivine_node"))


def long_prefixes(values: list[str]) -> int:
    """How many of the values share more than 4 leading characters with the one before."""
    return sum(1 for a, b in zip(values, values[1:], strict=False) if a[:5] == b[:5])


def test_random_uuid_gives_a_new_random_version_4_uuid_each_time_seldom_sharing_a_long_prefix(
    cypher_pm: PersistenceManager,
):
    # Oracle's SYS_GUID() gave version 0 values that shared their first 12 characters, a counter changing (#252).
    uuids = cypher_pm.query(spec("UNWIND range(1, 50) AS i RETURN randomUUID()"))
    assert len(set(uuids)) == 50, uuids
    assert all(UUID.fullmatch(it) for it in uuids), uuids
    # Two random UUIDs share 5 characters once in about a million; three such pairs in 49, about once in 6e13 runs.
    assert long_prefixes(uuids) <= 2, uuids


@pytest.mark.parametrize("expression", ["randomUUID() + ''", "'id-' + randomUUID()"])
def test_random_uuid_read_into_a_longer_expression_is_made_again_for_each_row(
    cypher_pm: PersistenceManager, expression: str
):
    # An Oracle expression for randomUUID() past SHARED_ABOVE is bound in a subquery of its own, which Oracle makes once
    # for the statement, so each row would read the same one: one made of DBMS_RANDOM digits did
    # (liberation-data/drivine4j#401).
    values = cypher_pm.query(spec(f"UNWIND range(1, 100) AS i RETURN {expression}"))
    assert len(set(values)) == 100, values


def test_the_tokens_the_stamp_clauses_make_are_random_so_a_stale_objects_stamp_does_not_come_round_again(
    cypher_pm: PersistenceManager,
):
    # On Oracle a token was the first 16 hex digits of a SYS_GUID(), of which a counter's 4 changed (#252).
    clear(cypher_pm)
    stamp = f"`{stamps.PROPERTY}`"
    cypher_pm.query(spec("UNWIND range(1, 1000) AS i CREATE (:CyWrite {id: i})"))
    cypher_pm.query(spec(f"MATCH (n:CyWrite) SET {stamps.set_clause('n')}"))
    cypher_pm.query(spec(f"MATCH (n:CyWrite) SET {stamps.links_clause('n')}"))
    found = cypher_pm.query(spec(f"MATCH (n:CyWrite) RETURN n.{stamp}"))
    tokens = [token for it in found for token in it.split(":")]
    assert len(set(found)) == 1000
    assert len(set(tokens)) == 2000
    # Each hex digit but the 13th, a version 4 UUID's version, takes nearly every value across the tokens.
    for position in (it for it in range(stamps.TOKEN) if it != 12):
        digits = {it[position] for it in tokens}
        assert len(digits) >= 12, f"position {position} of the tokens took only {sorted(digits)}"
    # Saved one after another, a node's tokens seldom share a long prefix, where SYS_GUID()'s always did.
    saved: list[str] = []
    for _ in range(20):
        cypher_pm.query(spec(f"MATCH (n:CyWrite {{id: 1}}) SET {stamps.set_clause('n')}"))
        saved.append((cypher_pm.get_one(spec(f"MATCH (n:CyWrite {{id: 1}}) RETURN n.{stamp}"))).split(":")[0])
    # Two random tokens share 5 digits once in about a million; three such pairs in 19, about once in 1e15 runs.
    assert long_prefixes(saved) <= 2, saved


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
    # ORA-40180, nor read a text of more than 4,000 bytes out of JSON, as docs/04-statements.md says: it is handed
    # back whole.)
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
    # The provider's first connection makes the graph's tables where they aren't there.
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


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_a_property_past_32_kb_read_on_two_nodes_leaves_the_session_open_on_oracle(
    factory: PersistenceManagerFactory, registry: DatabaseRegistry
):
    # Oracle crashed the session where a statement took a text past 32 KB from the properties with JSON_QUERY as JSON,
    # and fed it on into further SQL, for each of two rows (#144, drivine4j#255). The compiler reads a property a RETURN
    # or a WITH gives through its text to work round it: see OracleCypherCompiler.item_property. As above, each
    # statement is new, and the session is asked again after it, on one connection: not a transaction's, which a
    # refused statement would make rollback-only.
    clear(factory.get("oracle"))
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = provider.connect()

    def query(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return connection.query(spec(cypher, parameters))

    try:
        texts = [it * 40_000 for it in ("x", "y", "z")]
        query("UNWIND range(1, 3) AS id CREATE (:CyWrite {id: id})")
        # Each set by a statement of its own: a write that reads a long text from an unwound parameter, for each of two
        # rows, is not what this test is about.
        for i, a in enumerate(texts):
            query("MATCH (n:CyWrite {id: $id}) SET n.a = $a", {"id": i + 1, "a": a})
        query(
            "MATCH (a:CyWrite {id: 1}), (b:CyWrite {id: 2}) "
            "CREATE (a)-[:CY_LONG {a: $a}]->(b), (b)-[:CY_LONG {a: $a}]->(a)",
            {"a": texts[0]},
        )
        for i in range(3):
            assert sorted(query(f"MATCH (n:CyWrite) WHERE n.id > -{i} RETURN n.a")) == texts
            assert query(f"MATCH (n:CyWrite) WHERE n.id > -{i} RETURN n.a ORDER BY n.id") == texts
            # Not worked around: Oracle sorts and groups JSON only up to about 32 KB. It refuses the statement
            # (ORA-40180), and the session stays open for the statements after it.
            for rest in ("RETURN n.a ORDER BY n.a", "RETURN DISTINCT n.a"):
                with pytest.raises(Exception) as refused:
                    query(f"MATCH (n:CyWrite) WHERE n.id > -{i} {rest}")
                causes: list[BaseException] = []
                error: BaseException | None = refused.value
                while error is not None and error not in causes:
                    causes.append(error)
                    error = error.__cause__ or error.__context__
                assert any("ORA-40180" in str(it) for it in causes), repr(refused.value)
            assert query(f"MATCH (n:CyWrite) WITH n.id AS id, n.a AS a WHERE id > -{i} RETURN [id, a] ORDER BY id") == [
                [j + 1, a] for j, a in enumerate(texts)
            ]
            assert query(f"MATCH ()-[r:CY_LONG]->() RETURN r.a AS a{i}") == [texts[0], texts[0]]
            assert query(f"MATCH (n:CyWrite) RETURN count(n) AS c{i}") == [3]
    finally:
        connection.release()
        clear(factory.get("oracle"))


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_a_text_past_32_kb_set_from_an_unwound_parameter_on_two_nodes_leaves_the_session_open_on_oracle(
    factory: PersistenceManagerFactory, registry: DatabaseRegistry
):
    # Oracle crashed the session where the block took a text past 32 KB from an item of an unwound parameter, as JSON,
    # for each of two rows (#176, drivine4j#309). The compiler reads a text a write takes from its row through the
    # row's text to work round it: see OracleCypherCompiler.row_value. As above, each statement is new, and the session
    # is asked again after it, on one connection.
    clear(factory.get("oracle"))
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = provider.connect()

    def query(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return connection.query(spec(cypher, parameters))

    try:
        texts = [it * 40_000 for it in ("x", "y", "z")]
        items = {"items": [{"id": i + 1, "a": a, "f": 5.0} for i, a in enumerate(texts)]}
        query("UNWIND range(1, 3) AS id CREATE (:CyWrite {id: id})")
        written: list[str] = []
        for i in range(3):
            query(f"UNWIND $items AS t MATCH (n:CyWrite {{id: t.id}}) SET n.a{i} = t.a", items)
            query(f"UNWIND $items AS t MERGE (n:CyWrite {{id: t.id}}) SET n.m{i} = t.a", items)
            query(f"UNWIND $items AS t CREATE (:CyMade {{id: t.id, a: t.a, i: {i}}})", items)
            query(f"UNWIND $texts AS t CREATE (:CyBare {{a: t, i: {i}}})", {"texts": texts})
            written += [f"a{i}", f"m{i}"]
            assert query(f"MATCH (n:CyWrite) WHERE n.id > -{i} RETURN properties(n) ORDER BY n.id") == [
                {"id": j + 1, **dict.fromkeys(written, a)} for j, a in enumerate(texts)
            ]
            # Each read as an item of its own: one inside a list isn't worked around (see item_property).
            assert query(f"MATCH (n:CyMade {{i: {i}}}) RETURN n.a ORDER BY n.id") == texts
            assert sorted(query(f"MATCH (n:CyBare {{i: {i}}}) RETURN n.a")) == texts
            assert query(f"MATCH (n:CyWrite) RETURN count(n) AS c{i}") == [3]
        # A map item that holds the long text, set whole: its whole float stays a float.
        query("UNWIND $items AS t MATCH (n:CyWrite {id: t.id}) SET n += t", items)
        assert query("MATCH (n:CyWrite) RETURN n.a ORDER BY n.id") == texts
        floats = query("MATCH (n:CyWrite) RETURN n.f ORDER BY n.id")
        assert typed(floats) == typed([5.0, 5.0, 5.0]), floats
    finally:
        connection.release()
        clear(factory.get("oracle"))


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_a_map_an_unwound_item_holds_with_a_key_as_extended_jsons_is_written_as_the_map_it_is_on_oracle(
    factory: PersistenceManagerFactory,
):
    # Only a text a write takes from its row is read through the row's text (#176, drivine4j#309): a map is read as
    # JSON, as before, and not from extended JSON, which would read {"$numberLong": "5"} as the number 5.
    pm = factory.get("oracle")
    clear(pm)
    items = [{"id": 1, "m": {"$numberLong": "5"}}, {"id": 2, "m": {"$numberDouble": "2.0"}}]
    pm.query(spec("UNWIND $items AS t CREATE (:CyWrite {id: t.id, m: t.m})", {"items": items}))
    assert pm.query(spec("MATCH (n:CyWrite) RETURN properties(n) ORDER BY n.id")) == items


@pytest.mark.skipif(not ORACLE, reason="Oracle isn't configured: set ORACLE_TESTS=true.")
def test_a_text_past_32_kb_from_an_unwound_parameter_returned_or_passed_on_over_two_rows_leaves_the_session_open(
    factory: PersistenceManagerFactory, registry: DatabaseRegistry
):
    # Oracle crashed the session where a statement took a text past 32 KB from an item of an unwound parameter as JSON,
    # and returned it or passed it on, for each of two rows (#186, drivine4j#322). The compiler reads such a value
    # through the item's text to work round it: see OracleCypherCompiler.item_value. As above, each statement is new,
    # and the session is asked again after it, on one connection.
    clear(factory.get("oracle"))
    provider = registry.connection_provider("oracle")
    assert provider is not None
    connection = provider.connect()

    def query(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return connection.query(spec(cypher, parameters))

    try:
        texts = [it * 40_000 for it in ("x", "y", "z")]
        items = {"items": [{"id": i + 1, "a": a} for i, a in enumerate(texts)]}
        query("UNWIND range(1, 3) AS id CREATE (:CyWrite {id: id})")
        for i in range(3):

            def alive() -> None:
                assert query(f"RETURN 1 AS alive{i}") == [1]  # noqa: B023

            assert query(f"UNWIND $items AS t RETURN t.a AS a{i}", items) == texts
            alive()
            assert query(f"UNWIND $items AS t WITH t.a AS a RETURN a AS a{i}", items) == texts
            alive()
            assert query(f"UNWIND $items AS t WITH t RETURN t.a AS a{i}", items) == texts
            alive()
            made = f"UNWIND $items AS t WITH t.a AS a, t.id AS id CREATE (:CyMade {{id: id, a: a, i: {i}}})"
            query(made, items)
            alive()
            returned = query(
                f"UNWIND $items AS t MATCH (n:CyWrite {{id: t.id}}) SET n.a{i} = t.a RETURN t.a AS a{i}", items
            )
            assert returned == texts
            alive()
            assert query(f"MATCH (n:CyMade {{i: {i}}}) RETURN n.a ORDER BY n.id") == texts
            assert query(f"MATCH (n:CyWrite) WHERE n.id > -{i} RETURN n.a{i} ORDER BY n.id") == texts
        # Not worked around: a pattern that matches on such a text compares it as JSON, which Oracle refuses past about
        # 32 KB (ORA-40180), as it refuses to sort or group it, and the session stays open.
        with pytest.raises(Exception) as refused:
            query("UNWIND $items AS t MERGE (n:CyMerged {a: t.a})", items)
        causes: list[BaseException] = []
        error: BaseException | None = refused.value
        while error is not None and error not in causes:
            causes.append(error)
            error = error.__cause__ or error.__context__
        assert any("ORA-40180" in str(it) for it in causes), repr(refused.value)
        assert query("MATCH (n:CyWrite) RETURN count(n)") == [3]
    finally:
        connection.release()
        clear(factory.get("oracle"))


def test_an_unwound_parameters_values_returned_or_passed_on_by_a_return_or_a_with_come_back_as_they_are_given(
    cypher_pm: PersistenceManager,
):
    # On Oracle such a value is read through the item's text where it is a text (#186): a whole float stays a float, a
    # list, a map, an empty text, a null and a missing key come back as they were, returned, passed on, or after a
    # write.
    clear(cypher_pm)
    items: list[dict[str, Any]] = [
        {
            "id": 1,
            "s": "short",
            "f": 5.0,
            "i": 5,
            "l": [1, 2],
            "fl": [1.0, 2.5],
            "m": {"k": 2.0},
            "z": None,
            "e": "",
        },
        {"id": 2, "s": "x'\"y", "f": -0.5, "i": 0, "l": [], "fl": [3.0], "m": {"k": "v"}, "e": "e"},
    ]
    given = {"items": items}
    cypher_pm.query(spec("CREATE (:CyWrite {id: 1}), (:CyWrite {id: 2})"))
    expected: dict[str, list[Any]] = {
        "s": ["short", "x'\"y"],
        "f": [5.0, -0.5],
        "i": [5, 0],
        "l": [[1, 2], []],
        "fl": [[1.0, 2.5], [3.0]],
        "m": [{"k": 2.0}, {"k": "v"}],
        "z": [None, None],
        "nothing": [None, None],
        "e": ["", "e"],
    }
    for key, values in expected.items():
        for cypher in (
            f"UNWIND $items AS t RETURN t.{key}",
            f"UNWIND $items AS t WITH t.{key} AS v RETURN v",
            f"UNWIND $items AS t WITH t RETURN t.{key}",
            f"UNWIND $items AS t MATCH (n:CyWrite {{id: t.id}}) SET n.seen = true RETURN t.{key} ORDER BY t.id",
            f"CALL {{ UNWIND $items AS t RETURN t }} RETURN t.{key}",
        ):
            rows = cypher_pm.query(spec(cypher, given))
            assert typed(rows) == typed(values), (cypher, rows)
    # An OPTIONAL CALL that unwinds nothing gives a null item, and its value is null.
    optional = "UNWIND [1] AS x OPTIONAL CALL { WITH x UNWIND [] AS t RETURN t } RETURN t.s"
    assert cypher_pm.query(spec(optional)) == [None]
    cypher_pm.query(
        spec(
            "UNWIND $items AS t WITH t.id AS id, t.s AS s, t.f AS f, t.l AS l, t.m.k AS k "
            "CREATE (:CyMade {id: id, s: s, f: f, l: l, k: k})",
            given,
        )
    )
    made = cypher_pm.query(spec("MATCH (n:CyMade) RETURN properties(n) ORDER BY n.id"))
    assert typed(made) == typed(
        [
            {"id": 1, "s": "short", "f": 5.0, "l": [1, 2], "k": 2.0},
            {"id": 2, "s": "x'\"y", "f": -0.5, "l": [], "k": "v"},
        ]
    ), made
    # A map with a key as extended JSON's comes back the map it is, and not the number extended JSON writes.
    keyed = {"items": [{"d": {"$numberLong": "5"}}, {"d": {"$numberDouble": "2.0"}}]}
    maps = [it["d"] for it in keyed["items"]]
    assert cypher_pm.query(spec("UNWIND $items AS t RETURN t.d", keyed)) == maps
    assert cypher_pm.query(spec("UNWIND $items AS t WITH t RETURN t.d", keyed)) == maps


def test_an_unwound_items_text_short_or_past_32_kb_is_returned_only_for_the_nodes_a_labelled_pattern_matches(
    cypher_pm: PersistenceManager,
):
    # On Oracle such a text is read in a CASE on its type (#186), and such a CASE on a node's properties made Oracle
    # drop a label test (see OracleCypherCompiler.item_property): each node a label rules out, on either end of the
    # pattern, is left out.
    clear(cypher_pm)
    cypher_pm.query(
        spec(
            "UNWIND [1, 2, 3] AS id CREATE (:CyWrite {id: id, o: id * 2})-[:R]->(:CyOther {name: 'k'}), "
            "(:CyWrite {id: id, o: id * 2 + 1})-[:R]->(:CyDecoy {name: 'k'}), "
            "(:CyNope {id: id})-[:R]->(:CyOther {name: 'k'})"
        )
    )
    for text in ("s", "x" * 40_000):
        given = {"texts": [{"id": it, "a": f"{text}{it}", "k": "k"} for it in (1, 2, 3)]}
        texts = [f"{text}{it}" for it in (1, 2, 3)]
        for cypher in (
            "UNWIND $texts AS t MATCH (n:CyWrite {id: t.id})-[r]->(k:CyOther) RETURN t.a ORDER BY n.o",
            "UNWIND $texts AS t MATCH (k:CyOther {name: t.k})<-[r]-(n:CyWrite {id: t.id}) RETURN t.a ORDER BY n.o",
            "UNWIND $texts AS t MATCH (n:CyWrite {id: t.id})-[r]->(k:CyOther) SET n.seen = true "
            "RETURN t.a ORDER BY n.o",
        ):
            assert cypher_pm.query(spec(cypher, given)) == texts, cypher
        passed = (
            "UNWIND $texts AS t MATCH (n:CyWrite {id: t.id}) WHERE (n)-->(:CyOther) "
            "WITH t.a AS a, n RETURN [a, n.o] ORDER BY n.o"
        )
        assert cypher_pm.query(spec(passed, given)) == [[f"{text}{it}", it * 2] for it in (1, 2, 3)]
        unlabelled = "UNWIND $texts AS t MATCH (k:CyOther)<--(n {id: t.id}) RETURN t.a ORDER BY t.id"
        assert cypher_pm.query(spec(unlabelled, given)) == [it for it in texts for _ in range(2)]
    # So is a value of an item of a list made of a node's properties, unwound beside the labelled pattern.
    beside = "MATCH (n:CyWrite)-[r]->(k:CyOther) UNWIND [n {.o}] AS m RETURN m.o ORDER BY m.o"
    assert cypher_pm.query(spec(beside)) == [2, 4, 6]
    ruled_out = "MATCH (n) WHERE n.seen AND NOT n:CyWrite OR n:CyWrite AND n.seen AND n.o % 2 = 1 RETURN count(n)"
    assert cypher_pm.query(spec(ruled_out)) == [0]


def test_an_unwound_items_text_short_or_past_32_kb_is_written_only_to_the_nodes_a_labelled_pattern_matches(
    cypher_pm: PersistenceManager,
):
    # On Oracle such a text is read in a CASE on its type (#176), and such a CASE in a read made Oracle drop a label
    # test (see OracleCypherCompiler.item_property): each node a label rules out, on either end of the pattern, is left
    # alone.
    for text in ("s", "x" * 40_000):
        clear(cypher_pm)
        cypher_pm.query(
            spec(
                "UNWIND [1, 2, 3] AS id CREATE (:CyWrite {id: id, o: id * 2})-[:R]->(:CyOther {name: 'k'}), "
                "(:CyWrite {id: id, o: id * 2 + 1})-[:R]->(:CyDecoy {name: 'k'}), "
                "(:CyNope {id: id})-[:R]->(:CyOther {name: 'k'})"
            )
        )
        given = {"texts": [{"id": it, "a": text, "s": "s", "k": "k"} for it in (1, 2, 3)]}
        cypher_pm.query(spec("UNWIND $texts AS t MATCH (n:CyWrite {id: t.id})-[r]->(k:CyOther) SET n.a = t.a", given))
        # A value of the item's worked out beside the node the pattern reached. The `+` is of the short text: on Oracle
        # a `+` of a text past 4,000 bytes leaves that text out (drivine4j#314).
        cypher_pm.query(
            spec(
                "UNWIND $texts AS t MATCH (n:CyWrite {id: t.id})-[r]->(k:CyOther) "
                "SET n.b = CASE k.name WHEN 'k' THEN t.a END, n.e = t.s + k.name",
                given,
            )
        )
        cypher_pm.query(spec("UNWIND $texts AS t MERGE (n:CyWrite {id: t.id, o: t.id * 2}) SET n.c = t.a", given))
        # A labelled pattern in a subquery of the statement that works the value out, beside the item's text in the
        # CASE.
        cypher_pm.query(
            spec(
                "UNWIND $texts AS t MATCH (n:CyWrite {id: t.id}) SET n.f = COUNT { (n)-->(:CyOther {name: t.k}) }",
                given,
            )
        )

        def matched(value: Any) -> list[Any]:
            return [it for _ in range(3) for it in (value, None)]

        assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n.a ORDER BY n.o")) == matched(text)
        assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n.b ORDER BY n.o")) == matched(text)
        assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n.e ORDER BY n.o")) == matched("sk")
        assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n.c ORDER BY n.o")) == matched(text)
        assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN n.f ORDER BY n.o")) == [1, 0] * 3
        assert cypher_pm.query(spec("MATCH (n:CyWrite) RETURN count(n)")) == [6]
        untouched = (
            "MATCH (n:CyNope) WHERE n.a IS NOT NULL OR n.b IS NOT NULL OR n.c IS NOT NULL OR n.e IS NOT NULL "
            "RETURN count(n)"
        )
        assert cypher_pm.query(spec(untouched)) == [0]


def test_an_unwound_parameters_items_are_written_as_they_are_given_by_set_create_merge_and_set_plus(
    cypher_pm: PersistenceManager,
):
    # On Oracle such a value is read through its text (#176): a whole float stays a float, a null or a missing key
    # writes no property, and a list, a key of a map in the item and a short text are written as they were.
    clear(cypher_pm)
    items: list[dict[str, Any]] = [
        {"id": 1, "s": "short", "f": 5.0, "i": 5, "l": [1, 2], "fl": [1.0, 2.5], "m": {"k": 2.0}, "z": None},
        {"id": 2, "s": "x'\"y", "f": -0.5, "i": 0, "l": [], "fl": [3.0], "m": {"k": "v"}},
    ]
    given = {"items": items}
    statements = [
        "UNWIND $items AS t CREATE (:CyWrite {id: t.id, s: t.s, f: t.f, i: t.i, l: t.l, fl: t.fl, k: t.m.k, z: t.z, "
        "w: t.nothing})",
        "UNWIND $items AS t MATCH (n:CyWrite {id: t.id}) SET n.s2 = t.s, n.f2 = t.f, n.z2 = t.z, n.w2 = t.nothing",
        "UNWIND $items AS t MERGE (n:CyWrite {id: t.id}) SET n.f3 = t.f",
        "UNWIND $items AS t MERGE (n:CyWrite {id: t.id + 10}) ON CREATE SET n.f = t.f, n.m = t.m.k",
    ]
    for statement in statements:
        cypher_pm.query(spec(statement, given))
    added = {"added": [{"id": 1, "g": 7.0, "s": None}, {"id": 2, "g": 8}]}
    cypher_pm.query(spec("UNWIND $added AS t MATCH (n:CyWrite {id: t.id}) SET n += t", added))
    cypher_pm.query(spec("UNWIND $floats AS f CREATE (:CyWrite {id: 20, f: f})", {"floats": [6.0]}))
    rows = cypher_pm.query(spec("MATCH (n:CyWrite) RETURN properties(n) ORDER BY n.id"))
    one = {"id": 1, "f": 5.0, "i": 5, "l": [1, 2], "fl": [1.0, 2.5], "k": 2.0, "s2": "short", "f2": 5.0, "f3": 5.0}
    two = {"id": 2, "s": "x'\"y", "f": -0.5, "i": 0, "l": [], "fl": [3.0], "k": "v", "s2": "x'\"y", "f2": -0.5}
    expected = [
        {**one, "g": 7.0},
        {**two, "f3": -0.5, "g": 8},
        {"id": 11, "f": 5.0, "m": 2.0},
        {"id": 12, "f": -0.5, "m": "v"},
        {"id": 20, "f": 6.0},
    ]
    assert typed(rows) == typed(expected), rows


def seeded(pm: PersistenceManager) -> None:
    """The graph queries.yaml reads, written again."""
    clear(pm)
    pm.query(spec(load("queries.yaml").graph or ""))


@contextlib.contextmanager
def one_session(pm: PersistenceManager) -> Generator[None]:
    """
    On Oracle, one connection for the statements run in it, as a transaction holds one, so `asked_again` asks the
    session that ran each statement. Elsewhere, nothing.
    """
    if pm.type is DatabaseType.ORACLE:
        with transaction(rollback=True):
            yield
    else:
        yield


def asked_again(pm: PersistenceManager, cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
    """The rows of `cypher`; on Oracle, the session is asked again after it: a crash can come after the rows did."""
    rows = pm.query(spec(cypher, parameters))
    if pm.type is DatabaseType.ORACLE:
        assert pm.query(spec("RETURN 1")) == [1], f"the session after: {cypher}"
    return rows


def test_an_optional_call_or_an_optional_match_in_a_count_or_an_exists_keeps_the_row_it_finds_nothing_for(
    cypher_pm: PersistenceManager,
):
    # Beside the spec's case since v0.6.17 (#338). Oracle refused an optional join on TRUE in a scalar subquery
    # followed by WHERE … AND … (ORA-00907), and crashed the session on one without (ORA-03113, ORA-07445 in its alert
    # log) (#200): it is joined on 1 = 1 there. drivine4j's CypherQueryContract of liberation-data/drivine4j#338.
    clear(cypher_pm)
    cypher_pm.query(spec("CREATE (:CyOpt {id: 1}), (:CyOpt {k: 'p'}), (:CyOpt {k: 'q'}), (:CyOpt {k: 'q'})"))
    none = "OPTIONAL CALL { MATCH (z:Nope) RETURN z.k AS u }"
    called = "CALL { MATCH (z:Nope) RETURN z.k AS u }"
    with one_session(cypher_pm):

        def counted(subquery: str) -> list[Any]:
            return asked_again(cypher_pm, f"MATCH (n:CyOpt {{id: 1}}) RETURN COUNT {{ {subquery} }}")

        # At the top of a statement, as it ran before.
        assert asked_again(cypher_pm, f"{none} RETURN u") == [None]
        assert asked_again(cypher_pm, f"{none} MATCH (y:CyOpt) WHERE y.k = 'q' RETURN count(y)") == [2]
        # In a COUNT { }: refused on Oracle, with a WHERE … AND … after the join, and else crashed the session.
        assert counted(f"{none} MATCH (y:CyOpt) WHERE y.k = 'q' RETURN y") == [2]
        assert counted(f"{none} RETURN u") == [1]
        assert counted(f"{none} MATCH (y:CyOpt) RETURN y") == [4]
        assert counted(f"MATCH (y:CyOpt) {none} WITH y, u WHERE y.k = u RETURN y") == [0]
        # What a CALL that finds something gives is counted, and a plain CALL that finds nothing gives no row.
        found = "OPTIONAL CALL { MATCH (z:CyOpt) WHERE z.k = 'q' RETURN z.k AS u }"
        assert counted(f"{found} MATCH (y:CyOpt) WHERE y.k = u RETURN y") == [4]
        assert counted(f"{called} MATCH (y:CyOpt) WHERE y.k = 'q' RETURN y") == [0]
        # An OPTIONAL MATCH is the same join.
        assert counted("OPTIONAL MATCH (z:Nope) MATCH (y:CyOpt) WHERE y.k = 'q' RETURN y") == [2]
        assert counted("OPTIONAL MATCH (z:Nope) RETURN z") == [1]
        # In an EXISTS { }, and in a WHERE.
        exists = f"MATCH (n:CyOpt {{id: 1}}) RETURN [EXISTS {{ {none} RETURN u }}, EXISTS {{ {called} RETURN u }}]"
        assert asked_again(cypher_pm, exists) == [[True, False]]
        where = f"MATCH (n:CyOpt) WHERE COUNT {{ {none} RETURN u }} = 1 AND n.id = 1 RETURN n.id"
        assert asked_again(cypher_pm, where) == [1]


def test_an_aggregate_in_a_call_reads_the_variables_the_call_imports(cypher_pm: PersistenceManager):
    # Not in the spec yet (#253). On Oracle, an aggregate in a CALL read a variable of an unwound row as null, so sum()
    # gave 0 and collect() gave []. Postgres refused an aggregate of an imported variable alone, count(o), as the row's
    # query's. drivine4j's CypherQueryContract of liberation-data/drivine4j#368; the rows are Neo4j's (2026.10).
    seeded(cypher_pm)

    def rows_of(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    aggregates = (
        "RETURN sum(i + o) AS s, collect(o) AS c, count(o) AS n, max(i * o) AS m } RETURN o, s, c, n, m ORDER BY o"
    )
    rows = [[1, 9, [1, 1, 1], 3, 3], [2, 12, [2, 2, 2], 3, 6]]
    assert rows_of("UNWIND [2] AS o CALL { WITH o UNWIND range(1, 3) AS i RETURN sum(i + o) AS s } RETURN s") == [12]
    assert rows_of("UNWIND [2] AS o CALL { WITH o UNWIND range(1, 3) AS i RETURN collect(o) AS s } RETURN s") == [
        [2, 2, 2]
    ]
    # Imported by a WITH, or by the CALL's scope clause.
    assert rows_of(f"UNWIND [1, 2] AS o CALL {{ WITH o UNWIND range(1, 3) AS i {aggregates}") == rows
    assert rows_of(f"UNWIND [1, 2] AS o CALL (o) {{ UNWIND range(1, 3) AS i {aggregates}") == rows
    assert rows_of(
        "UNWIND [1, 2] AS o OPTIONAL CALL (o) { UNWIND range(1, 3) AS i RETURN sum(i + o) AS s, collect(o) AS c } "
        "RETURN o, s, c ORDER BY o"
    ) == [[1, 9, [1, 1, 1]], [2, 12, [2, 2, 2]]]
    # Aggregated in a WITH within the CALL.
    assert rows_of(
        "UNWIND [1, 2] AS o CALL { WITH o UNWIND range(1, 3) AS i WITH sum(i + o) AS t RETURN t * 10 AS s } "
        "RETURN o, s ORDER BY o"
    ) == [[1, 90], [2, 120]]
    # Grouped within the CALL, by a key worked out first where it holds a subquery or a parameter.
    grouped = [[1, 0, [3, 5], 2], [1, 1, [2, 4], 2], [2, 0, [4, 6], 4], [2, 1, [3, 5], 4]]
    each = "collect(o + i) AS xs, sum(o) AS s } RETURN o, k, xs, s ORDER BY o, k"
    assert rows_of(f"UNWIND [1, 2] AS o CALL (o) {{ UNWIND range(1, 4) AS i RETURN i % 2 AS k, {each}") == grouped
    assert rows_of(f"UNWIND [1, 2] AS o CALL {{ WITH o UNWIND range(1, 4) AS i RETURN i % 2 AS k, {each}") == grouped
    assert (
        rows_of(f"UNWIND [1, 2] AS o CALL {{ WITH o UNWIND range(1, 4) AS i RETURN i % $m AS k, {each}", {"m": 2})
        == grouped
    )
    # Over an imported node's property.
    assert rows_of(
        "MATCH (p:Person) CALL (p) { UNWIND range(1, 2) AS i "
        "RETURN sum(p.born + i) AS s, collect(p.name) AS ns, max(p.born) AS b } RETURN p.name, s, ns, b ORDER BY p.name"
    ) == [
        ["Carrie", 3937, ["Carrie", "Carrie"], 1967],
        ["Hugo", 0, ["Hugo", "Hugo"], None],
        ["Keanu", 3931, ["Keanu", "Keanu"], 1964],
        ["Laurence", 3925, ["Laurence", "Laurence"], 1961],
    ]
    assert rows_of(
        "MATCH (p:Person) CALL { WITH p UNWIND [10, 20] AS i RETURN count(p) AS n, sum(i) + max(p.born) AS t } "
        "RETURN p.name, n, t ORDER BY p.name"
    ) == [["Carrie", 2, 1997], ["Hugo", 2, None], ["Keanu", 2, 1994], ["Laurence", 2, 1991]]
    # Each row's own values, a null among them; two imports; a map and a relationship.
    assert rows_of(
        "UNWIND [1, null] AS o CALL (o) { UNWIND range(1, 3) AS i RETURN count(o) AS n, collect(o) AS c, max(o) AS s } "
        "RETURN o, n, c, s ORDER BY o"
    ) == [[1, 3, [1, 1, 1], 1], [None, 0, [], None]]
    assert rows_of(
        "UNWIND [1, 2] AS a UNWIND [10, 20] AS b CALL { WITH a, b UNWIND range(1, 2) AS i "
        "RETURN sum(a * b + i) AS s, collect(a) AS ca, count(b) AS nb } RETURN a, b, s, ca, nb ORDER BY a, b"
    ) == [[1, 10, 23, [1, 1], 2], [1, 20, 43, [1, 1], 2], [2, 10, 43, [2, 2], 2], [2, 20, 83, [2, 2], 2]]
    assert rows_of(
        "UNWIND [{a: 1}, {a: 2}] AS o CALL { WITH o UNWIND range(1, 2) AS i "
        "RETURN sum(o.a + i) AS s, collect(o) AS c } RETURN o.a, s, c ORDER BY o.a"
    ) == [[1, 5, [{"a": 1}, {"a": 1}]], [2, 7, [{"a": 2}, {"a": 2}]]]
    assert rows_of(
        "MATCH (p:Person {name: 'Laurence'})-[r:ACTED_IN]->() CALL { WITH r UNWIND [1, 2] AS i "
        "RETURN collect(r.role) AS rs, count(r) AS n } RETURN p.name, rs, n"
    ) == [["Laurence", ["Morpheus", "Morpheus"], 2]]
    # In each query of a UNION in the CALL, in a CALL within the CALL, and in a CALL after a write.
    assert rows_of(
        "UNWIND [1, 2] AS o CALL (o) { UNWIND [1, 2] AS i RETURN sum(i + o) AS s "
        "UNION ALL UNWIND [3] AS i RETURN sum(i + o) AS s } RETURN o, s ORDER BY o, s"
    ) == [[1, 4], [1, 5], [2, 5], [2, 7]]
    assert rows_of(
        "UNWIND [1, 2] AS o CALL (o) { UNWIND [1, 2] AS j CALL (o, j) { UNWIND range(1, 3) AS i "
        "RETURN sum(o * j + i) AS s } RETURN collect(s) AS ss } RETURN o, ss ORDER BY o"
    ) == [[1, [9, 12]], [2, [12, 18]]]
    assert rows_of(
        "CREATE (x:CyCallWrite {v: 1}) WITH x UNWIND range(1, 2) AS o CALL { WITH o, x UNWIND range(1, 2) AS i "
        "RETURN sum(i + o) AS s, collect(x.v) AS xs, count(o) AS n } RETURN o, s, xs, n ORDER BY o"
    ) == [[1, 5, [1, 1], 2], [2, 7, [1, 1], 2]]
    # Under a RETURN * after a WITH that names what it passes on; and a variable the CALL unwinds itself, of a name the
    # row has.
    assert rows_of(
        "UNWIND [1, 2] AS o CALL { WITH o UNWIND range(1, 3) AS i WITH sum(i + o) AS s RETURN * } "
        "RETURN o, s ORDER BY o"
    ) == [[1, 9], [2, 12]]
    assert rows_of(
        "UNWIND [1, 2] AS o CALL (o) { UNWIND range(1, 3) AS i WITH sum(i + o) AS s, collect(o) AS c RETURN * } "
        "RETURN o, s, c ORDER BY o"
    ) == [[1, 9, [1, 1, 1]], [2, 12, [2, 2, 2]]]
    assert rows_of("UNWIND [1, 2] AS o CALL { UNWIND [5, 6] AS o RETURN sum(o) AS s } RETURN o, s ORDER BY o") == [
        [1, 11],
        [2, 11],
    ]
    # Under a RETURN that aggregates after a WITH *; an aggregate within a comprehension or a CASE; an import renamed by
    # the first WITH; a boolean and a path imported.
    assert rows_of(
        "UNWIND [1, 2] AS o CALL (o) { UNWIND range(1, 3) AS i WITH * RETURN sum(i + o) AS s } RETURN o, s ORDER BY o"
    ) == [[1, 9], [2, 12]]
    assert rows_of(
        "UNWIND [1, 2] AS o CALL (o) { UNWIND range(1, 3) AS i RETURN [x IN collect(o) | x + 1] AS c, "
        "CASE WHEN count(o) > 2 THEN 'many' ELSE 'few' END AS k } RETURN o, c, k ORDER BY o"
    ) == [[1, [2, 2, 2], "many"], [2, [3, 3, 3], "many"]]
    assert rows_of(
        "UNWIND [1, 2] AS o CALL (o) { WITH o AS x UNWIND range(1, 3) AS i RETURN sum(x + i) AS s } "
        "RETURN o, s ORDER BY o"
    ) == [[1, 9], [2, 12]]
    assert rows_of(
        "UNWIND [1, 2] AS o WITH o, o > 1 AS b CALL (o, b) { UNWIND [1, 2] AS i "
        "RETURN collect(b) AS bs, sum(i + o) AS s } RETURN o, bs, s ORDER BY o"
    ) == [[1, [False, False], 5], [2, [True, True], 7]]
    assert rows_of(
        "MATCH path = (p:Person {name: 'Laurence'})-[:ACTED_IN]->() CALL (path) { UNWIND [1, 2] AS i "
        "RETURN count(path) AS n, collect(length(path)) AS ls } RETURN n, ls"
    ) == [[2, [1, 1]]]


def test_an_aggregate_beside_a_where_a_grouping_keys_body_or_an_order_term_that_neo4j_takes_gives_neo4js_rows(
    cypher_pm: PersistenceManager,
):
    # #306, #307: beside the aggregates now refused, those Neo4j takes. drivine4j's CypherQueryContract of
    # liberation-data/drivine4j#489 and #482; the rows are Neo4j's (2026.10).
    seeded(cypher_pm)

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    # A WHERE over what an aggregate gave, and one in a subquery's or a CALL's own query, which aggregates in its
    # projection.
    assert query("UNWIND [1, 2] AS i WITH count(*) AS c WHERE c > 1 RETURN c") == [2]
    assert query(
        "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p, collect(m.title) AS ts WHERE size(ts) > 1 RETURN p.name "
        "ORDER BY p.name"
    ) == ["Carrie", "Keanu"]
    assert query(
        "MATCH (p:Person) RETURN p.name, COUNT { MATCH (p)-[:ACTED_IN]->(m) WITH count(m) AS c WHERE c > 1 RETURN c } "
        "AS k ORDER BY p.name"
    ) == [["Carrie", 1], ["Hugo", 0], ["Keanu", 1], ["Laurence", 0]]
    assert query(
        "MATCH (p:Person) WHERE EXISTS { MATCH (p)-[:ACTED_IN]->(m) WITH count(m) AS c WHERE c > 1 RETURN c } "
        "RETURN p.name ORDER BY p.name"
    ) == ["Carrie", "Keanu"]
    assert query(
        "MATCH (p:Person) CALL (p) { MATCH (p)-[:ACTED_IN]->(m) WITH count(m) AS c WHERE c > 1 RETURN c } "
        "RETURN p.name, c ORDER BY p.name"
    ) == [["Carrie", 2], ["Keanu", 3]]
    # An aggregate beside its grouping keys, and a body that names only them, or a variable of its own.
    assert query("UNWIND [1, 2] AS x RETURN x, collect(x) AS c ORDER BY x") == [[1, [1]], [2, [2]]]
    assert query("MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, count(*) ORDER BY p.name") == [
        ["Carrie", 2],
        ["Keanu", 3],
        ["Laurence", 1],
    ]
    assert query("UNWIND [1, 2] AS x RETURN x, [y IN collect(x) | y + x] AS l ORDER BY x") == [[1, [2]], [2, [4]]]
    assert query("UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, [y IN collect(z) | y + x] AS l ORDER BY x") == [
        [1, [4]],
        [2, [5]],
    ]
    assert query("UNWIND [1, 2] AS x RETURN x, reduce(s = 0, y IN collect(x) | s + y + x) AS l ORDER BY x") == [
        [1, 2],
        [2, 4],
    ]
    assert query(
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, size([x IN collect(m.title) WHERE x <> p.name | x + p.name]) "
        "ORDER BY p.name"
    ) == [["Carrie", 2], ["Keanu", 3], ["Laurence", 1]]
    # An order term that names an item's name, or what an item that does not aggregate is.
    assert query("UNWIND [{a: 1}, {a: 2}] AS x RETURN x.a AS a, count(*) AS c ORDER BY x.a + count(*) DESC") == [
        [2, 1],
        [1, 1],
    ]
    assert query("UNWIND [1, 2] AS x RETURN x AS y, count(*) AS c ORDER BY x + count(*) DESC") == [[2, 1], [1, 1]]
    assert query("UNWIND [1, 2] AS x RETURN x AS y, count(*) AS c ORDER BY y + count(*) DESC") == [[2, 1], [1, 1]]
    assert query(
        "UNWIND [1, 2] AS x UNWIND [3] AS z RETURN x, count(*) AS c ORDER BY [w IN collect(z) | w + x][0] DESC"
    ) == [[2, 1], [1, 1]]
    # A key named in another case: Neo4j names a function in any case.
    assert query(
        "UNWIND [{a: 'b'}, {a: 'a'}] AS x RETURN toUpper(x.a) AS u, count(*) AS c ORDER BY TOUPPER(x.a) + count(*) DESC"
    ) == [["B", 1], ["A", 1]]
    assert query(
        "UNWIND [{a: 'b'}, {a: 'a'}] AS x RETURN toUpper(x.a) AS u, collect(DISTINCT x.a) AS c "
        "ORDER BY TOUPPER(x.a) + size(collect(DISTINCT x.a)) DESC"
    ) == [["B", ["b"]], ["A", ["a"]]]


def test_an_aggregating_item_over_a_key_that_is_a_property_groups_by_the_property_as_neo4j_does(
    cypher_pm: PersistenceManager,
):
    # #311: beside the shapes now refused, those Neo4j takes. drivine4j's CypherQueryContract of
    # liberation-data/drivine4j#491; the rows are Neo4j's (2026.10).
    seeded(cypher_pm)

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    roles = ["Johnny", "Morpheus", "Neo", "Trinity"]
    # Grouped by the property, not by its variable: one row for "Neo", which two relationships have.
    assert query("UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x RETURN x.a AS a, x.a + count(*) AS b ORDER BY a") == [
        [1, 3],
        [2, 3],
    ]
    assert query("UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x WITH x.a AS a, x.a + count(*) AS b RETURN a, b ORDER BY a") == [
        [1, 3],
        [2, 3],
    ]
    assert query("MATCH (:Person)-[r:ACTED_IN]->() RETURN r.role AS role, r.role + count(*) AS n ORDER BY role") == [
        [role, n] for role, n in zip(roles, ["Johnny1", "Morpheus1", "Neo2", "Trinity2"], strict=True)
    ]
    assert query(
        "MATCH (:Person)-[r:ACTED_IN]->() RETURN r.role AS role, {role: r.role, n: count(*)} AS n ORDER BY role"
    ) == [[role, {"role": role, "n": n}] for role, n in zip(roles, [1, 1, 2, 2], strict=True)]
    assert query(
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, size(reduce(s = p.name, t IN collect(m.title) | s + t)) AS r "
        "ORDER BY p.name"
    ) == [["Carrie", 35], ["Keanu", 49], ["Laurence", 18]]
    assert query(
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p.name, p.name IN collect(m.title) AS r ORDER BY p.name"
    ) == [["Carrie", False], ["Keanu", False], ["Laurence", False]]
    assert query(
        "UNWIND [{a: {b: 1}}, {a: {b: 1}}, {a: {b: 2}}] AS x RETURN x.a AS k, x.a.b + count(*) AS c ORDER BY k.b"
    ) == [[{"b": 1}, 3], [{"b": 2}, 3]]
    # In a list comprehension's WHERE, the key; in its projection or a quantifier's WHERE, Neo4j groups by its variable
    # too.
    assert query(
        "MATCH (:Person)-[r:ACTED_IN]->(m) RETURN r.role AS role, "
        "size([t IN collect(m.title) WHERE t STARTS WITH r.role | t]) AS n ORDER BY role"
    ) == [[role, n] for role, n in zip(roles, [1, 0, 0, 0], strict=True)]
    each = ["Johnny", "Morpheus", "Neo", "Neo", "Trinity", "Trinity"]
    assert query(
        "MATCH (:Person)-[r:ACTED_IN]->(m) RETURN r.role AS role, [t IN collect(m.title) | r.role] AS n ORDER BY role"
    ) == [[it, [it]] for it in each]
    assert query(
        "MATCH (:Person)-[r:ACTED_IN]->(m) RETURN r.role AS role, any(t IN collect(m.title) WHERE t CONTAINS r.role) "
        "AS n ORDER BY role"
    ) == [[it, it == "Johnny"] for it in each]
    # Unnamed, a map projection is the item p, which an order term reads.
    assert query("MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p {.name}, count(*) AS c ORDER BY p.name") == [
        [{"name": "Carrie"}, 2],
        [{"name": "Keanu"}, 3],
        [{"name": "Laurence"}, 1],
    ]
    assert query("MATCH (p:Person)-[:ACTED_IN]->(m) RETURN p {.name, .born}, count(*) AS c ORDER BY p.born") == [
        [{"name": "Laurence", "born": 1961}, 1],
        [{"name": "Keanu", "born": 1964}, 3],
        [{"name": "Carrie", "born": 1967}, 2],
    ]
    assert query(
        "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p {.name}, count(*) AS c RETURN p.name, c ORDER BY p.name"
    ) == [["Carrie", 2], ["Keanu", 3], ["Laurence", 1]]
    # A null key is one group; two keys of one variable group by both, not by the variable (two rows have a = 1, b = 1).
    assert query("MATCH (m:Movie) RETURN m.tagline AS t, {t: m.tagline, n: count(*)} AS n ORDER BY t") == [
        ["Welcome to the Real World", {"t": "Welcome to the Real World", "n": 1}],
        [None, {"t": None, "n": 2}],
    ]
    assert query(
        "UNWIND [{a: 1, b: 1, c: 1}, {a: 1, b: 1, c: 2}, {a: 1, b: 2, c: 1}, {a: 2, b: 2, c: 1}] AS x "
        "RETURN x.a AS a, x.b AS b, x.a * 10 + x.b + count(*) AS n ORDER BY a, b"
    ) == [[1, 1, 13], [1, 2, 13], [2, 2, 23]]
    # In a comprehension's WHERE inside another's WHERE, the key too; but not where a comprehension binds the key's
    # variable.
    assert query(
        "MATCH (:Person)-[r:ACTED_IN]->(m) RETURN r.role AS role, "
        "size([t IN collect(m.title) WHERE size([u IN [t] WHERE u CONTAINS r.role]) > 0]) AS n ORDER BY role"
    ) == [[role, n] for role, n in zip(roles, [1, 0, 0, 0], strict=True)]
    assert query(
        "UNWIND [{a: 1}, {a: 2}, {a: 2}] AS x RETURN x.a AS a, size([x IN [{a: 5}] WHERE x.a > 1]) + count(*) AS n "
        "ORDER BY a"
    ) == [[1, 2], [2, 3]]
    # A function named in another case is the same function.
    assert query(
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN toUpper(p.name) AS u, count(*) AS c "
        "ORDER BY TOUPPER(p.name) + count(*) DESC"
    ) == [["LAURENCE", 1], ["KEANU", 3], ["CARRIE", 2]]
    assert query(
        "MATCH (p:Person)-[:ACTED_IN]->(m) RETURN TOUPPER(p.name) AS u, count(*) AS c ORDER BY toupper(p.name)"
    ) == [["CARRIE", 2], ["KEANU", 3], ["LAURENCE", 1]]
    assert query(
        "UNWIND [{a: 'x'}, {a: 'x'}, {a: 'y'}] AS x RETURN toUpper(x.a), count(*) ORDER BY TOUPPER(x.a) + count(*)"
    ) == [["X", 2], ["Y", 1]]


def test_an_aggregate_in_the_where_of_a_with_that_groups_compares_keys_as_grouping_does_and_makes_no_variable(
    cypher_pm: PersistenceManager,
):
    # #313: the WHERE of a WITH that aggregates, or is DISTINCT, filters its groups, as SQL's HAVING does. drivine4j's
    # CypherQueryContract of liberation-data/drivine4j#497, less the statements the spec holds since v0.16.0; the rows
    # are Neo4j's (2026.10.1).
    seeded(cypher_pm)

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    ordered: list[tuple[str, list[Any]]] = [
        # A key's property, read through the item it is renamed to, of a map and of a node.
        (
            "UNWIND [{a: 1}, {a: 1}, {a: 2}] AS x WITH x AS y, count(*) AS c WHERE x.a > 1 AND count(*) = 1 "
            "RETURN y.a, c",
            [[2, 1]],
        ),
        (
            "MATCH (p:Person)-[:ACTED_IN]->(m) WITH p AS q, count(*) AS c WHERE p.born > 1965 RETURN q.name, c "
            "ORDER BY q.name",
            [["Carrie", 2]],
        ),
        # A key and an aggregate compared as grouping compares them, a function's name in any case; count(DISTINCT i)
        # is not count(i).
        (
            "UNWIND ['a', 'A', 'b'] AS x WITH toUpper(x) AS u, count(*) AS c WHERE TOUPPER(x) = 'A' RETURN u, c",
            [["A", 2]],
        ),
        ("UNWIND [1, 1, 2] AS i WITH i, Sum(i) AS s WHERE SUM(i) > 1 RETURN i, s ORDER BY i", [[1, 2], [2, 2]]),
        (
            "UNWIND [1, 1, 2] AS i WITH i, count(i) AS c WHERE count(DISTINCT i) = 1 AND count(i) > 1 RETURN i, c",
            [[1, 2]],
        ),
        # A property's name keeps its case: sum(x.A) is not sum(x.a).
        (
            "UNWIND [{a: 1, A: 10}, {a: 2, A: 20}] AS x WITH count(*) AS c, sum(x.a) AS s WHERE sum(x.A) > 25 "
            "RETURN c, s",
            [[2, 3]],
        ),
        # An item quoted as a made item would be named is the statement's own.
        *(
            (
                f"UNWIND [1, 1, 2] AS i WITH i, count(*) AS `#having{n}` WHERE sum(i) > 1 RETURN i, `#having{n}` "
                "ORDER BY i",
                [[1, 2], [2, 1]],
            )
            for n in range(1, 7)
        ),
    ]
    for cypher, rows in ordered:
        assert (cypher, query(cypher)) == (cypher, rows)
    unordered: list[tuple[str, list[Any]]] = [
        ("UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE sum(i) > 1 WITH * RETURN *", [[1, 2], [2, 1]]),
        (
            "UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE sum(i) > 1 RETURN i UNION UNWIND [5] AS `#having2` "
            "RETURN `#having2` AS i",
            [1, 2, 5],
        ),
        ("UNWIND [1, 1, 2] AS i WITH i, count(*) AS c WHERE count(DISTINCT i) = 1 RETURN i, c", [[1, 2], [2, 1]]),
    ]
    for cypher, rows in unordered:
        given = sorted(json.dumps(it) for it in query(cypher))
        assert (cypher, given) == (cypher, sorted(json.dumps(it) for it in rows))


def test_a_date_or_a_moment_from_calendar_fields_an_expression_gives_fails_past_their_range_as_it_runs(
    cypher_pm: PersistenceManager,
):
    # #312: worked out by the store, which fails on a field past its range as Neo4j does. drivine4j's
    # CypherTemporalContract of liberation-data/drivine4j#495, less the statements the spec holds since v0.15.0.
    differing: list[str] = []
    for cypher in (
        "UNWIND [[2023, 2, 0]] AS p RETURN date({year: p[0], week: p[1], dayOfWeek: p[2]})",
        "UNWIND [[2024, 4, 93]] AS p RETURN date({year: p[0], quarter: p[1], dayOfQuarter: p[2]})",
        "UNWIND [[2023, 2.5]] AS p RETURN date({year: p[0], week: p[1]})",
        # A moment's too, which Oracle reads as a JSON value's, null where it is no moment.
        "UNWIND [[2023, 366]] AS p RETURN localdatetime({year: p[0], ordinalDay: p[1], hour: 3})",
        "UNWIND [[2020, 54]] AS p RETURN datetime({year: p[0], week: p[1], timezone: '+01:00'})",
        "UNWIND [[2024, 4, 93]] AS p RETURN localdatetime({year: p[0], quarter: p[1], dayOfQuarter: p[2]})",
    ):
        try:
            differing.append(f"{cypher} gave {cypher_pm.query(spec(cypher))}")
        except Exception as e:  # noqa: BLE001
            # Fails as it runs, as on Neo4j: the store's failure, not a refusal, which is a DrivineError at the root.
            cause: BaseException = e
            while cause.__cause__ is not None:
                cause = cause.__cause__
            if isinstance(cause, DrivineError):
                differing.append(f"{cypher} was refused: {cause}")
    assert differing == []


def test_a_date_or_a_moment_from_calendar_fields_an_expression_gives_is_neo4js_at_the_turn_of_a_year(
    cypher_pm: PersistenceManager,
):
    # #312: the least a null gives, and a moment beside its time. Neo4j's rows (2026.10.1).
    rows = cypher_pm.query(
        spec(
            "UNWIND [[2020, 53, 7, 2024, 366, 4, 92], [2019, 53, 1, 2023, 365, 1, 91], "
            "[2015, 53, 5, 2024, 60, null, null], [2026, 1, 1, 2025, null, 2, 1]] AS p "
            "WITH date({year: p[0], week: p[1], dayOfWeek: p[2]}) AS w, localdatetime({year: p[3], ordinalDay: p[4], "
            "hour: 12}) AS o, datetime({year: p[0], quarter: p[5], dayOfQuarter: p[6], hour: 0, minute: 5, "
            "timezone: '-05:00'}) AS q "
            "RETURN [[w.year, w.month, w.day], [o.year, o.month, o.day, o.hour], [q.year, q.month, q.day, q.minute, "
            "q.timezone]]"
        )
    )
    assert rows == [
        [[2021, 1, 3], [2024, 12, 31, 12], [2020, 12, 31, 5, "-05:00"]],
        [[2019, 12, 30], [2023, 12, 31, 12], [2019, 4, 1, 5, "-05:00"]],
        [[2016, 1, 1], [2024, 2, 29, 12], [2015, 1, 1, 5, "-05:00"]],
        [[2025, 12, 29], [2025, 1, 1, 12], [2026, 4, 1, 5, "-05:00"]],
    ]


def test_an_optional_call_in_a_writes_subquery_counts_what_it_finds_and_the_row_it_finds_nothing_for(
    cypher_pm: PersistenceManager,
):
    # On Oracle an OPTIONAL CALL in a write's subquery was refused (ORA-06550, ORA-00907) until it was joined on 1 = 1,
    # and with no WHERE … AND … after the join, it closed the session (#200). drivine4j's CypherWriteContract.
    clear(cypher_pm)
    cypher_pm.query(spec("CREATE (:CyWrite {id: 1}), (:CyOther {k: 'm'}), (:CyOther {k: 'l'})"))
    given = {"ts": [{"id": 1, "a": "m", "m": {"a": "m"}, "l": {"a": "none"}}]}
    subqueries = {
        "o": "OPTIONAL CALL { WITH t MATCH (z:CyNope) RETURN t AS u } MATCH (y:CyOther) WHERE y.k = u.a RETURN y",
        "f": "OPTIONAL CALL { WITH t RETURN t.m AS u } MATCH (y:CyOther) WHERE y.k = u.a RETURN y",
        "b": "OPTIONAL CALL { WITH t MATCH (z:CyNope) RETURN t AS u } RETURN u",
    }
    with one_session(cypher_pm):
        for name, subquery in subqueries.items():
            statement = f"UNWIND $ts AS t MATCH (n:CyWrite {{id: t.id}}) SET n.{name} = COUNT {{ {subquery} }}"
            asked_again(cypher_pm, statement, given)
        assert cypher_pm.query(spec("MATCH (n:CyWrite {id: 1}) RETURN [n.o, n.f, n.b]")) == [[0, 1, 1]]


def test_texts_text_functions_and_arithmetic_over_an_aggregates_result_beside_keys_a_walk_distinct_and_an_order(
    cypher_pm: PersistenceManager,
):
    # drivine4j#373 (#235 here): an operation over an aggregate's result is worked out over the aggregate's column,
    # where it would bind a value; the three aggregates concatenated compiled to megabytes. drivine4j's
    # CypherQueryContract of liberation-data/drivine4j#373.
    clear(cypher_pm)
    cypher_pm.query(
        spec(
            "CREATE (a:CyAgg {k: 'x', title: 'Dr', first: 'Ann', last: 'Bell', name: ' Bo ', a: 1, b: 10})-[:NEXT]->"
            "(b:CyAgg {k: 'x', title: 'Mr', first: 'Cy', last: 'Ames', name: ' Al ', a: 3, b: 20})-[:NEXT]->"
            "(:CyAgg {k: 'y', title: 'Ms', first: 'Di', last: 'Cole', name: ' Ed ', a: 5, b: 30})"
        )
    )

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    all_ = "MATCH (n:CyAgg)"
    ordered = "MATCH (n:CyAgg) WITH n ORDER BY n.a"
    assert query(f"{all_} RETURN max(n.title) + ': ' + max(n.first) + ' ' + max(n.last)") == ["Ms: Di Cole"]
    heads = "head(collect(n.title)) + ': ' + head(collect(n.first)) + ' ' + head(collect(n.last))"
    assert query(f"{ordered} RETURN {heads}") == ["Dr: Ann Bell"]
    functions = (
        f"{ordered} RETURN [toLower(toLower(toLower(toLower(collect(n.name)[0])))), trim(trim(max(n.name))), "
        "substring(substring(toUpper(max(n.first)), 0), 1), toLower(trim(head(collect(n.name))))]"
    )
    assert query(functions) == [[" bo ", "Ed", "I", "bo"]]
    arithmetic = f"{all_} RETURN [sum(n.a) / count(*), (sum(n.a) + sum(n.b)) * 2 / count(*) - max(n.a)]"
    assert query(arithmetic) == [[3, 41]]
    # Beside a grouping key, ordered by it, and by an operation over aggregates, which Neo4j sorts by only where it is
    # an item.
    keyed = f"{all_} RETURN n.k AS k, max(n.title) + ': ' + toLower(max(n.last)) AS t ORDER BY k"
    assert query(keyed) == [["x", "Mr: bell"], ["y", "Ms: cole"]]
    by_texts = (
        f"{all_} RETURN n.k AS k, toLower(min(n.last)) + max(n.first) AS t "
        "ORDER BY toLower(min(n.last)) + max(n.first) DESC"
    )
    assert query(by_texts) == [["y", "coleDi"], ["x", "amesCy"]]
    by_mean = f"{all_} RETURN n.k AS k, sum(n.b) / count(*) AS m ORDER BY sum(n.b) / count(*) DESC"
    assert query(by_mean) == [["y", 30], ["x", 15]]
    # DISTINCT, with a key and without.
    distinct = f"{all_} RETURN DISTINCT n.k AS k, toLower(max(n.first)) + toString(count(*)) AS t ORDER BY k"
    assert query(distinct) == [["x", "cy2"], ["y", "di1"]]
    distinct_texts = f"{all_} RETURN DISTINCT max(n.title) + ': ' + max(n.first) + ' ' + max(n.last)"
    assert query(distinct_texts) == ["Ms: Di Cole"]
    # Beside a variable-length walk, in an item, and in an order term too.
    walk = "MATCH (s:CyAgg)-[:NEXT*1..2]->(e:CyAgg)"
    assert query(f"{walk} RETURN min(e.title) + ': ' + min(e.first) + ' ' + min(e.last)") == ["Mr: Cy Ames"]
    sorted_ = "toLower(toLower(max(e.last))) + toString(sum(e.a))"
    assert query(f"{walk} RETURN s.first AS s, {sorted_} AS t ORDER BY {sorted_}") == [
        ["Cy", "cole5"],
        ["Ann", "cole8"],
    ]
    paged = f"{walk} RETURN s.first AS s, {sorted_} AS t ORDER BY {sorted_} DESC SKIP 1 LIMIT 1"
    assert query(paged) == [["Cy", "cole5"]]
    # An order term that reads an item by its name beside an aggregate, with a walk and without.
    by_name = f"{all_} RETURN n.k AS k, sum(n.a) AS m ORDER BY toString(10 - sum(n.a)) + toLower(toLower(k))"
    assert query(by_name) == [["y", 5], ["x", 4]]
    walk_by_name = f"{walk} RETURN s.first AS s, sum(e.a) AS c ORDER BY toLower(toLower(s)) + toString(sum(e.a)) DESC"
    assert query(walk_by_name) == [["Cy", 5], ["Ann", 8]]
    # Where a comprehension's own name hides an item's, and where the item's expression reads a name the comprehension
    # binds.
    hidden = f"{all_} RETURN n.k AS k, sum(n.a) AS m ORDER BY toString([k IN [10] | k - m][0]) + toString(sum(n.a))"
    assert query(hidden) == [["y", 5], ["x", 4]]
    bound = f"{all_} RETURN n.k AS k, sum(n.a) AS m ORDER BY [n IN ['b'] | k + n][0] + toString(sum(n.a)) DESC"
    assert query(bound) == [["y", 5], ["x", 4]]
    # Beside a key that is an expression, in a CASE and in a map.
    texts = "toLower(max(n.title)) + ': ' + toLower(max(n.last))"
    assert query(f"{all_} RETURN n.a + 1 AS k, {texts} AS t ORDER BY k") == [
        [2, "dr: bell"],
        [4, "mr: ames"],
        [6, "ms: cole"],
    ]
    cased = f"{all_} RETURN n.k AS k, CASE WHEN count(*) > 1 THEN {texts} ELSE '' END AS t ORDER BY k"
    assert query(cased) == [["x", "mr: bell"], ["y", ""]]
    mapped = f"{all_} RETURN n.k AS k, {{t: {texts}, c: count(*) + 1}} AS m ORDER BY k"
    assert query(mapped) == [["x", {"t": "mr: bell", "c": 3}], ["y", {"t": "ms: cole", "c": 2}]]
    # Over no rows, the aggregate is null, and so is what is worked out over it; a count is 0.
    assert query(f"{all_} WHERE n.k = 'z' RETURN toLower(max(n.title)) + '!'") == [None]
    counted = f"{all_} WHERE n.k = 'z' RETURN [toLower(toString(count(*))) + '!', count(*) + 1]"
    assert query(counted) == [["0!", 1]]
    # A text looked for in another over an aggregate's result: Oracle's CONTAINS binds its CLOBs in a subquery
    # (drivine4j#312), which reads no aggregate of the query it is in.
    contained = (
        f"{all_} RETURN n.k AS k, max(n.last) CONTAINS min(n.first) AS c, toLower(max(n.name)) CONTAINS 'e' AS e"
    )
    assert query(f"{contained} ORDER BY k") == [["x", False, False], ["y", False, True]]
    clear(cypher_pm)


def test_contains_finds_a_text_past_32_kb_in_a_text_of_any_length_given_stored_on_several_rows_and_in_a_write(
    cypher_pm: PersistenceManager,
):
    # On Oracle INSTR refuses to look for a text past 32,767 UTF-16 units (ORA-22828, drivine4j#312). Each text is
    # looked for as a parameter, as a property on the node beside it, and of each node in a cross join, three times on
    # one session. drivine4j's CypherQueryContract of liberation-data/drivine4j#312.
    clear(cypher_pm)
    long = "aB" * 20_000
    pair = "\U0001f600"
    cases: list[tuple[str | None, str | None]] = [
        # A short text in a long one, and texts just within and just past what INSTR looks for.
        (long, "aB"),
        (long, "zz"),
        (long, long[1:32_768]),
        (long, long[1:32_769]),
        (long, long[:32_768] + "z"),
        # A long text in a longer one, found only at its end, and not found.
        (long + "c", "aB" * 19_000 + "c"),
        (long + "c", "aB" * 19_000 + "d"),
        # As long: the issue's own, and one that differs in its last character.
        (long, long),
        (long, long[:-1] + "b"),
        # Longer than the text.
        (long, long + "x"),
        ("abc", long),
        # Empty and null.
        (long, ""),
        ("", long),
        (long, None),
        (None, long),
        # Three-byte characters, past 32 KB within INSTR's units and past them.
        ("€" * 40_000, "€" * 32_767),
        ("€" * 40_000, "€" * 33_000),
        ("€" * 40_000, "€" * 33_000 + "x"),
        # Characters past U+FFFF: across the 1,000th unit the search starts with, across INSTR's 32,767th, and only
        # those.
        ("z" + "x" * 999 + pair + "y" * 40_000, "x" * 999 + pair + "y" * 39_000),
        ("z" + "y" * 32_766 + pair + "y" * 100, "y" * 32_766 + pair + "y" * 10),
        ("z" + pair * 20_000 + "q", pair * 19_000 + "q"),
        ("z" + pair * 20_000 + "q", pair * 19_000 + "z"),
        # A long text whose first units are nowhere in the text, so no place is compared.
        (long, "c" * 33_000),
    ]

    def expected(t: str | None, x: str | None) -> bool | None:
        if t is None or x is None:
            return None
        # Oracle's empty text is null, so CONTAINS of one is null there.
        if cypher_pm.type is DatabaseType.ORACLE and (not t or not x):
            return None
        return x in t

    parameters = {f"{side}{i}": text for i, case in enumerate(cases) for side, text in zip("tx", case, strict=True)}
    # One node at a time: on Oracle, UNWIND of a list of texts past 32 KB into CREATE closes the session.
    for i, (t, x) in enumerate(cases):
        cypher_pm.query(spec("CREATE (:CyContains {i: $i, t: $t, x: $x})", {"i": i, "t": t, "x": x}))
    each = [expected(t, x) for t, x in cases]
    with one_session(cypher_pm):
        for run in range(1, 4):
            for i, (t, x) in enumerate(cases):
                described = f"{None if x is None else len(x)} in {None if t is None else len(t)}, case {i}, run {run}"
                given = asked_again(cypher_pm, f"WITH $t{i} AS t, $x{i} AS x RETURN t CONTAINS x", parameters)
                assert given == [expected(t, x)], f"given, {described}"
            stored = "MATCH (n:CyContains) RETURN n.t CONTAINS n.x ORDER BY n.i"
            assert asked_again(cypher_pm, stored, parameters) == each, f"stored, run {run}"
            # The text of one node, looked for in each other's; and in a write, which runs as a block of its own.
            across = "MATCH (n:CyContains {i: 7}), (m:CyContains) RETURN m.t CONTAINS n.x ORDER BY m.i"
            assert asked_again(cypher_pm, across, parameters) == [expected(t, long) for t, _ in cases], (
                f"across nodes, run {run}"
            )
            written = "MATCH (n:CyContains) SET n.r = n.t CONTAINS n.x WITH n ORDER BY n.i RETURN n.r"
            assert asked_again(cypher_pm, written, parameters) == each, f"written, run {run}"
    clear(cypher_pm)


def test_contains_of_a_text_past_32_kb_in_an_aggregates_argument_over_its_result_and_beside_a_grouping_key(
    cypher_pm: PersistenceManager,
):
    # On Oracle the search of a text past 32,767 UTF-16 units is a subquery (drivine4j#312): here an aggregate reads
    # it, it reads an aggregate's result, and it is a grouping key. drivine4j's CypherQueryContract of
    # liberation-data/drivine4j#312.
    clear(cypher_pm)
    long = "aB" * 20_000
    found = "aB" * 19_000 + "c"
    texts = [(long + "c", found), (long, found), (long + "c", found)]
    for i, (t, x) in enumerate(texts):
        given = {"i": i, "k": i % 2, "t": t, "x": x}
        cypher_pm.query(spec("CREATE (:CyContainsAgg {i: $i, k: $k, t: $t, x: $x})", given))
    all_ = "MATCH (n:CyContainsAgg)"
    counted = "count(CASE WHEN n.t CONTAINS n.x THEN 1 END)"
    assert cypher_pm.query(spec(f"{all_} RETURN {counted}")) == [2]
    assert cypher_pm.query(spec(f"{all_} RETURN n.k AS k, {counted} AS c ORDER BY k")) == [[0, 2], [1, 0]]
    distinct = f"{all_} RETURN DISTINCT n.k AS k, n.t CONTAINS n.x AS c ORDER BY k"
    assert cypher_pm.query(spec(distinct)) == [[0, True], [1, False]]
    beside = f"{all_} RETURN n.i AS i, n.t CONTAINS n.x AS c, count(*) AS m ORDER BY i"
    assert cypher_pm.query(spec(beside)) == [[0, True, 1], [1, False, 1], [2, True, 1]]
    heads = f"{all_} WITH n ORDER BY n.i RETURN head(collect(n.t)) CONTAINS head(collect(n.x))"
    assert cypher_pm.query(spec(heads)) == [True]
    grouped = (
        f"{all_} WITH n ORDER BY n.i RETURN n.k AS k, head(collect(n.t)) CONTAINS last(collect(n.x)) AS c ORDER BY k"
    )
    assert cypher_pm.query(spec(grouped)) == [[0, True], [1, False]]
    clear(cypher_pm)


def test_contains_of_a_text_past_32_kb_filters_in_a_where_and_runs_in_the_step_of_a_reduce(
    cypher_pm: PersistenceManager,
):
    # On Oracle the search of a text past 32,767 UTF-16 units is a subquery (drivine4j#312): here a WHERE filters by
    # it, and a reduce() runs it in its function's own query. drivine4j's CypherQueryContract of
    # liberation-data/drivine4j#312.
    clear(cypher_pm)
    long = "aB" * 20_000
    found = "aB" * 19_000 + "c"
    for i, t in enumerate([long + "c", long, long + "c"]):
        cypher_pm.query(spec("CREATE (:CyContainsWhere {i: $i, t: $t, x: $x})", {"i": i, "t": t, "x": found}))
    all_ = "MATCH (n:CyContainsWhere)"
    assert cypher_pm.query(spec(f"{all_} WHERE n.t CONTAINS n.x RETURN n.i ORDER BY n.i")) == [0, 2]
    assert cypher_pm.query(spec(f"{all_} WHERE NOT n.t CONTAINS n.x RETURN n.i ORDER BY n.i")) == [1]
    reduced = (
        f"{all_} WITH n ORDER BY n.i WITH collect(n.t) AS ts, head(collect(n.x)) AS x "
        "RETURN reduce(acc = [], s IN ts | acc + [s CONTAINS x])"
    )
    assert cypher_pm.query(spec(reduced)) == [[True, False, True]]
    clear(cypher_pm)


def test_a_stored_text_past_32_kb_a_text_function_or_a_sum_reads_beside_a_long_value_on_two_rows_is_read_whole(
    cypher_pm: PersistenceManager,
):
    # On Oracle a value past SHARED_ABOVE's length is bound in a subquery, and a property beside it was bound too, taken
    # out as JSON and read as a CLOB: a text past 32 KB so closed the session from the second run on (ORA-03113,
    # drivine4j#370), as #144's did. A node of another label holds the same text, and is not read. drivine4j's
    # CypherQueryContract of liberation-data/drivine4j#370.
    clear(cypher_pm)
    bio = "a" * 40_000 + "x"
    # A text past 4,000 bytes but within 32 KB, too, which a list is added to; and one of fewer than 32,767 characters
    # that is past 32 KB, each of three bytes.
    mid = "b" * 20_000
    euros = "€" * 12_000
    props = "{name: 'x', first: 'A', bio: $bio, mid: $mid, euros: $euros, span: 'p1d'}"
    given = {"bio": bio, "mid": mid, "euros": euros}
    cypher_pm.query(spec(f"CREATE (:CyBio {props}), (:CyBio {props}), (:CyBioOther {props})", given))

    def long(text: str) -> str:
        # Long to write as SQL, so bound: a coalesce of a parameter's key, trimmed and made lower case.
        return f"toLower(trim(coalesce($p.q, {text})))"

    def each(value: Any) -> list[Any]:
        return [value] * 4

    statements: list[tuple[str, list[Any]]] = [
        (f"MATCH (n:CyBio), (m:CyBio) WHERE n.bio CONTAINS {long('m.name')} RETURN count(*)", [4]),
        (f"MATCH (n:CyBio), (m:CyBio) WHERE n.bio STARTS WITH {long('m.first')} RETURN count(*)", [4]),
        (f"MATCH (n:CyBio), (m:CyBio) WHERE n.bio ENDS WITH {long('m.name')} RETURN count(*)", [4]),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size(n.bio + {long('m.name')})", each(40_002)),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size({long('m.name')} + n.bio)", each(40_002)),
        # A sum of short texts too, which closed the session on Oracle where the property was left where it is read.
        ("MATCH (n:CyBio), (m:CyBio) RETURN n.name + '->' + m.first", each("x->A")),
        ("MATCH (n:CyBio), (m:CyBio) RETURN size(n.bio + '->' + m.name)", each(40_004)),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size(n.mid + {long('m.name')})", each(20_001)),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size(last([{long('m.name')}] + n.mid))", each(20_000)),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size(n.euros + {long('m.name')})", each(12_001)),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size(n.bio + size({long('m.name')}))", each(40_002)),
        # A duration, added to a text as any value is.
        (
            "MATCH (n:CyBio), (m:CyBio) RETURN size(n.bio + duration(toUpper(trim(coalesce($p.q, m.span)))))",
            each(40_004),
        ),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size(split(n.bio, {long('m.name')}))", each(2)),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN size(replace(n.bio, {long('m.name')}, 'yy'))", each(40_002)),
        (f"MATCH (n:CyBio), (m:CyBio) RETURN right(replace(n.bio, 'x', {long('m.first')} + 'b'), 3)", each("aab")),
        (f"MATCH (n:CyBio) WITH n WHERE n.bio CONTAINS {long('n.name')} RETURN count(*)", [2]),
    ]
    with one_session(cypher_pm):
        # Oracle closed the session from the second run on.
        for run in range(1, 4):
            for cypher, expected in statements:
                assert asked_again(cypher_pm, cypher, {"p": {"q": None}}) == expected, f"{cypher}, run {run}"


def test_a_text_past_32_kb_that_is_an_item_of_a_list_read_by_its_place_on_two_rows_is_read_whole(
    cypher_pm: PersistenceManager,
):
    # On Oracle an item of a list was taken out as JSON, JSON_QUERY(list, '$[$i]' RETURNING JSON): a text past 32 KB so
    # closed the session from the second run on (ORA-03113, #279), as #144's did, given two rows or more. A node of
    # another label holds the same values, and is not read. drivine4j's CypherQueryContract of
    # liberation-data/drivine4j#388.
    clear(cypher_pm)
    a, b = "a" * 40_000, "b" * 33_000
    props = "{bio: $bio, tags: $tags, i: 0}"
    given = {"bio": a + "x" + b, "tags": [a, "y"]}
    cypher_pm.query(spec(f"CREATE (:CyItem {props}), (:CyItem {props}), (:CyItemOther {props})", given))
    all_ = "MATCH (n:CyItem)"

    def each(value: Any) -> list[Any]:
        return [value, value]

    statements: list[tuple[str, list[Any]]] = [
        # The issue's: by index, of a list split makes, and of one a WITH passes on.
        (f"{all_} RETURN size(split(n.bio, 'x')[0])", each(40_000)),
        (f"{all_} WITH split(n.bio, 'x') AS parts RETURN size(parts[0])", each(40_000)),
        # Returned or passed on whole, counted from the end, and by an index the statement works out.
        (f"{all_} RETURN split(n.bio, 'x')[0]", each(a)),
        (f"{all_} WITH split(n.bio, 'x') AS parts RETURN parts[1]", each(b)),
        (f"{all_} WITH split(n.bio, 'x')[1] AS part RETURN size(part)", each(33_000)),
        (f"{all_} RETURN size(split(n.bio, 'x')[-1])", each(33_000)),
        (f"{all_} RETURN size(split(n.bio, 'x')[n.i])", each(40_000)),
        # By head and last, and after a slice.
        (f"{all_} RETURN size(head(split(n.bio, 'x')))", each(40_000)),
        (f"{all_} RETURN last(split(n.bio, 'x'))", each(b)),
        (f"{all_} RETURN size(split(n.bio, 'x')[0..1][0])", each(40_000)),
        (f"{all_} RETURN size(split(n.bio, 'x')[1..][0])", each(33_000)),
        # Unwound, which read it whole already.
        (
            f"{all_} UNWIND split(n.bio, 'x') AS part RETURN size(part) AS s ORDER BY s",
            [33_000, 33_000, 40_000, 40_000],
        ),
        # Read by text functions.
        (
            f"{all_} RETURN [size(split(n.bio, 'x')[0] + 'z'), split(n.bio, 'x')[1] ENDS WITH 'b', "
            "substring(split(n.bio, 'x')[0], 39998), right(toUpper(split(n.bio, 'x')[1]), 2)]",
            each([40_001, True, "aa", "BB"]),
        ),
        # Of a stored list.
        (f"{all_} RETURN size(n.tags[0])", each(40_000)),
        (f"{all_} RETURN n.tags[0]", each(a)),
        (f"{all_} RETURN [size(head(n.tags)), last(n.tags)]", each([40_000, "y"])),
        (f"{all_} RETURN count(split(n.bio, 'x')[0])", [2]),
    ]
    with one_session(cypher_pm):
        # Oracle closed the session from the second run on.
        for run in range(1, 4):
            for cypher, expected in statements:
                assert asked_again(cypher_pm, cypher) == expected, f"{cypher}, run {run}"
    # An item of every other kind is read as it was.
    others = (
        "WITH [1, 'a', 2.0, [3], {k: 4}, null] AS l "
        "RETURN [i IN range(0, 5) | l[i]] + [head(['y']), last([2, 1]), head([])]"
    )
    rows = cypher_pm.query(spec(others))
    assert typed(rows) == typed([[1, "a", 2.0, [3], {"k": 4}, None, "y", 1, None]]), rows


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


ZONED = (
    "datetime('2024-03-15T10:00:00+02:00')",
    "datetime('2024-03-15T09:00:00Z')",
    "datetime('2024-03-15T08:30:00Z')",
)
"""As text, 10:00+02:00 (08:00 UTC) sorts after 09:00Z, and 08:30Z before both."""


def test_a_list_of_zoned_values_worked_out_with_temporal_arithmetic_sorts_as_their_moments(
    cypher_pm: PersistenceManager,
):
    # Beside the spec's case since v0.6.17 (#199): its long list is worked out from one moment, not from a CASE, and it
    # has no list collect() gathers of them. drivine4j's test of liberation-data/drivine4j#199, ported for #181. k = 0's
    # start at 08:00 UTC.
    a, b, _ = ZONED

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    at = f"CASE k WHEN 0 THEN {a} ELSE {b} END"
    assert query(
        f"UNWIND [0, 1] AS k WITH k, [x IN range(1, 200) | {at} + duration({{minutes: x}})] AS l RETURN k ORDER BY l"
    ) == [0, 1]
    assert query(
        f"UNWIND [0, 1] AS k UNWIND range(1, 3) AS x WITH k, {at} + duration({{minutes: x}}) AS t "
        "WITH k, collect(t) AS l RETURN k ORDER BY l"
    ) == [0, 1]


def test_a_list_of_zoned_values_sorts_as_their_moments_however_it_was_made_and_an_empty_list_or_null_as_cypher(
    cypher_pm: PersistenceManager,
):
    # Beside the spec's case since v0.6.17 (#199). By moment 10:00+02:00, 08:30Z, 09:00Z; as text 08:30Z, 09:00Z,
    # 10:00+02:00.
    a, b, c = ZONED
    hours = [10, 8, 9]

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    def sorted_by(by: str, list_: str = "[[t]]") -> list[Any]:
        return query(f"UNWIND [{b}, {a}, {c}] AS t WITH t, {list_} AS l RETURN t.hour ORDER BY {by}")

    # Taken from a list of lists, by an index, head(), last(), a slice, reverse(), tail() and a comprehension.
    for by in ("l[0]", "head(l)", "last(l)", "l[0..1]", "reverse(l)", "[x IN l WHERE x IS NOT NULL]", "[x IN l | x]"):
        assert sorted_by(by) == hours, by
    assert sorted_by("tail(l)", "[[t], [t]]") == hours
    # Made by a CASE, collect(DISTINCT), a UNION, and a comprehension over nodes or a pattern.
    assert sorted_by("CASE WHEN t IS NOT NULL THEN [t] END") == hours
    assert (
        query(f"UNWIND [{b}, {a}, {c}] AS t WITH t.hour AS h, collect(DISTINCT [t]) AS l RETURN h ORDER BY l") == hours
    )
    assert (
        query(
            f"CALL {{ UNWIND [{b}] AS t RETURN [[t]] AS l UNION UNWIND [{a}, {c}] AS t RETURN [[t]] AS l }} "
            "RETURN l[0][0].hour ORDER BY l"
        )
        == hours
    )
    clear(cypher_pm)
    cypher_pm.query(
        spec(
            "CREATE (:CyZoned {h: 10})-[:AT]->(:CyZonedAt {at: '2024-03-15T10:00:00+02:00'}), "
            "(:CyZoned {h: 9})-[:AT]->(:CyZonedAt {at: '2024-03-15T09:00:00Z'}), "
            "(:CyZoned {h: 8})-[:AT]->(:CyZonedAt {at: '2024-03-15T08:30:00Z'})"
        )
    )
    assert query("MATCH (z:CyZoned) RETURN z.h ORDER BY [(z)-[:AT]->(w) | [datetime(w.at)]]") == hours
    assert (
        query("MATCH (z:CyZoned)-[:AT]->(w) WITH z, collect(w) AS ws RETURN z.h ORDER BY [n IN ws | [datetime(n.at)]]")
        == hours
    )
    # The empty list first, then a list of a moment, then a null item or no list; as items of a list too.
    ordered = [2, 1, 3]
    cased = f"CASE k WHEN 1 THEN [{a}] WHEN 2 THEN [i IN [{a}] WHERE false] END"
    assert query(f"UNWIND [1, 2, 3] AS k WITH k, {cased} AS l RETURN k ORDER BY l") == ordered
    assert query(f"UNWIND [1, 2, 3] AS k WITH k, {cased} AS l RETURN k ORDER BY l DESC") == ordered[::-1]
    assert query(f"UNWIND [1, 2, 3] AS k WITH k, [{cased}] AS l RETURN k ORDER BY l") == ordered
    assert query(f"UNWIND [1, 2, 3] AS k WITH k, [{cased}] AS l RETURN k ORDER BY l DESC") == ordered[::-1]
    # Two lists of lists of them put together, and min() or max() of lists of them, grouped.
    assert sorted_by("l + l") == hours
    assert query(f"UNWIND [{b}, {a}, {c}] AS t WITH t.hour AS h, min([t]) AS m RETURN h ORDER BY m") == hours
    assert query(f"UNWIND [{b}, {a}, {c}] AS t WITH t.hour AS h, max([[t]]) AS m RETURN h ORDER BY m") == hours
    # Carried past a write, as a list and as a list of lists.
    assert (
        query(f"UNWIND [{b}, {a}, {c}] AS t WITH t, [t] AS l CREATE (:CyZonedWrite) RETURN t.hour ORDER BY l") == hours
    )
    assert (
        query(f"UNWIND [{b}, {a}, {c}] AS t WITH t, [[t]] AS l CREATE (:CyZonedWrite) RETURN t.hour ORDER BY l")
        == hours
    )
    # Read from the row a write works its value out of (row_keys), in a subquery that sorts them.
    assert query(
        f"UNWIND [{b}, {a}, {c}] AS t WITH collect([t]) AS ls CREATE (n:CyZonedWrite) "
        "SET n.first = COUNT { UNWIND ls AS l WITH l ORDER BY l LIMIT 1 WITH l WHERE l[0].hour = 10 RETURN l } "
        "RETURN n.first"
    ) == [1]
    # After a term whose kind is open, which sorts by its kind first (liberation-data/drivine4j#308): the text before
    # the numbers, then by moment.
    opened = f"UNWIND [{b}, {a}, {c}] AS t WITH t, [t] AS l, CASE t.hour WHEN 8 THEN 'x' ELSE 1 END AS o RETURN t.hour"
    assert query(f"{opened} ORDER BY o, l") == [8, 10, 9]
    assert query(f"{opened} ORDER BY o DESC, l DESC") == [9, 10, 8]


def test_a_list_of_temporal_values_worked_out_on_each_row_is_read_item_by_item_in_order_wherever_it_is_read(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: drivine4j's test of liberation-data/drivine4j#331, ported for #194. Oracle reads such a list
    # by its places, not with JSON_TABLE, whose parse of one stalled the session. Of 0, 1 and 2 items, at 10:01, 10:02.
    at = "localdatetime('2024-03-15T10:00:00')"
    rows = f"UNWIND [0, 1, 2] AS k WITH k, [i IN range(1, k) | {at} + duration({{minutes: i}})] AS l"

    def minute(m: int) -> str:
        return f"localdatetime('2024-03-15T10:0{m}:00')"

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    # A comprehension, an IN, the quantifiers, and the list read twice in one statement.
    assert query(
        f"{rows} RETURN k, [x IN l | x.minute], {minute(1)} IN l, {minute(2)} IN l, all(x IN l WHERE x.minute <= k), "
        "none(x IN l WHERE x.minute = k), single(x IN l WHERE x.minute = 1) ORDER BY k"
    ) == [
        [0, [], False, False, True, True, False],
        [1, [1], True, False, True, False, True],
        [2, [1, 2], True, True, True, False, True],
    ]
    # A comprehension within one over the outer item and the row, in order.
    assert query(f"{rows} RETURN [x IN l | [y IN [x, x + duration({{minutes: k}})] | y.minute]] ORDER BY k") == [
        [],
        [[1, 2]],
        [[1, 3], [2, 4]],
    ]
    # An IN over another row's value: each pair whose minute is in the list.
    assert query(
        f"{rows} UNWIND [1, 2] AS j WITH k, j, l WHERE {at} + duration({{minutes: j}}) IN l "
        "WITH k, j ORDER BY j RETURN k, collect(j) ORDER BY k"
    ) == [[1, [1]], [2, [1, 2]]]
    # Null and a null item: no list, and a null that is compared.
    assert query(
        f"{rows} WITH k, CASE WHEN k = 1 THEN l END AS m RETURN k, [x IN m | x.minute], any(x IN m WHERE x.minute = 1) "
        "ORDER BY k"
    ) == [[0, None, None], [1, [1], True], [2, None, None]]
    assert query(
        f"UNWIND [0, 1, 2] AS k WITH k, [i IN range(1, k) | CASE WHEN i > 1 THEN {at} + duration({{minutes: i}}) END] "
        f"AS l RETURN k, [x IN l | x.minute], {minute(1)} IN l ORDER BY k"
    ) == [[0, [], False], [1, [None], None], [2, [None, 2], None]]
    # A slice, from either end, an UNWIND, and a CALL { … }.
    assert query(f"{rows} RETURN [x IN l[1..] | x.minute], [x IN l[-1..] | x.minute] ORDER BY k") == [
        [[], []],
        [[], [1]],
        [[2], [2]],
    ]
    assert query(f"{rows} UNWIND l AS t RETURN k, collect(t.minute) ORDER BY k") == [[1, [1]], [2, [1, 2]]]
    assert query(f"{rows} CALL {{ WITH l UNWIND l AS t RETURN count(t) AS c }} RETURN k, c ORDER BY k") == [
        [0, 0],
        [1, 1],
        [2, 2],
    ]
    # On each side of a UNION, and a list of such lists.
    unioned = query(f"{rows} RETURN [x IN l | x.minute] AS ms UNION {rows} RETURN [x IN reverse(l) | x.minute] AS ms")
    assert sorted(unioned) == [[], [1], [1, 2], [2, 1]]
    assert query(
        f"{rows} WITH k, [l, l] AS ll RETURN [x IN ll | size(x)][1], size([x IN ll[0] WHERE x.minute > 0]) ORDER BY k"
    ) == [[0, 0], [1, 1], [2, 2]]
    # collect(DISTINCT …) of such lists, and of their items, each once where first.
    twice = f"UNWIND [2, 1, 2] AS k WITH k, [i IN range(1, k) | {at} + duration({{minutes: i}})] AS l"
    assert query(f"{twice} WITH collect(DISTINCT l) AS ls RETURN [x IN ls | [y IN x | y.minute]]") == [[[1, 2], [1]]]
    assert query(f"{twice} UNWIND l AS t WITH collect(DISTINCT t) AS ts RETURN [x IN ts | x.minute]") == [[1, 2]]
    # A list that holds a null is kept once, as Cypher keeps it; zoned values of one moment and two offsets are not
    # equal in Cypher, so each is kept, in the order first seen.
    nulls = (
        "UNWIND [2, 1, 2, 1] AS k WITH k, "
        f"[i IN range(1, k) | CASE WHEN i > 1 THEN {at} + duration({{minutes: i}}) END] AS l"
    )
    assert query(f"{nulls} WITH collect(DISTINCT l) AS ls RETURN [x IN ls | [y IN x | y.minute]]") == [
        [[None, 2], [None]]
    ]
    zoned = (
        "UNWIND [0, 1, 2] AS k WITH k, [i IN range(1, 2) | datetime(CASE k WHEN 1 THEN '2024-03-15T10:00:00+02:00' "
        "ELSE '2024-03-15T08:00:00Z' END) + duration({minutes: i})] AS l"
    )
    assert query(f"{zoned} WITH collect(DISTINCT l) AS ls RETURN [x IN ls | [y IN x | y.hour]]") == [[[8, 8], [10, 10]]]
    assert query(f"{zoned} UNWIND l AS t WITH collect(DISTINCT t) AS ts RETURN [x IN ts | x.hour]") == [[8, 8, 10, 10]]


def test_a_coalesce_of_a_list_of_temporal_values_and_lists_or_maps_that_hold_one_deeper_read_item_by_item(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: drivine4j's test of liberation-data/drivine4j#347. Oracle reads these by their places too,
    # not with JSON_TABLE. Of 0, 1 and 2 items, at 10:01, 10:02.
    at = "localdatetime('2024-03-15T10:00:00')"
    rows = f"UNWIND [0, 1, 2] AS k WITH k, [i IN range(1, k) | {at} + duration({{minutes: i}})] AS l"
    each = [[0, []], [1, [1]], [2, [1, 2]]]

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    assert query(
        f"{rows} RETURN k, [x IN coalesce(l, []) | x.minute], size([x IN coalesce(l, []) WHERE x IS NOT NULL]) "
        "ORDER BY k"
    ) == [[0, [], 0], [1, [1], 1], [2, [1, 2], 2]]
    assert query(
        f"{rows} WITH k, CASE WHEN k = 1 THEN l END AS m RETURN k, [x IN coalesce(m, []) | x.minute] ORDER BY k"
    ) == [[0, []], [1, [1]], [2, []]]
    # collect(DISTINCT …) of lists of lists, and of lists of maps, that hold such a list, each once where first.
    twice = f"UNWIND [2, 1, 2] AS k WITH k, [i IN range(1, k) | {at} + duration({{minutes: i}})] AS l"
    assert query(f"{twice} WITH collect(DISTINCT [l, l]) AS ls RETURN [x IN ls | [y IN x | [z IN y | z.minute]]]") == [
        [[[1, 2], [1, 2]], [[1], [1]]]
    ]
    assert query(f"{twice} WITH collect(DISTINCT [x IN l | [x]]) AS ls RETURN [x IN ls | [y IN x | y[0].minute]]") == [
        [[1, 2], [1]]
    ]
    assert query(
        f"{twice} WITH collect(DISTINCT [x IN l | [x, null]]) AS ls RETURN [x IN ls | [y IN x | [y[0].minute, y[1]]]]"
    ) == [[[[1, None], [2, None]], [[1, None]]]]
    assert query(f"{twice} WITH k, [l, l] AS ll WITH collect(DISTINCT ll) AS lls RETURN [x IN lls | size(x[0])]") == [
        [2, 1]
    ]
    assert query(
        f"{twice} WITH collect(DISTINCT [x IN l | {{t: x}}]) AS ms RETURN [x IN ms | [y IN x | y.t.minute]]"
    ) == [[[1, 2], [1]]]
    assert query(f"{rows} UNWIND [x IN l | {{t: x}}] AS m RETURN k, collect(m.t.minute) ORDER BY k") == [
        [1, [1]],
        [2, [1, 2]],
    ]
    # A list taken back out of a map that holds it.
    assert query(f"{rows} WITH k, {{l: l}} AS m RETURN k, [x IN m.l | x.minute] ORDER BY k") == each
    # An item a quantifier takes from a list of such lists, and a coalesce in a list worked out item by item that is a
    # temporal value or a number.
    assert query(
        f"{rows} WITH k, [l, l] AS ll RETURN k, any(x IN ll WHERE size([y IN x WHERE y.minute = 2]) > 0) ORDER BY k"
    ) == [[0, False], [1, False], [2, True]]
    assert (
        query(
            "UNWIND [0, 1, 2] AS k WITH k, [i IN range(1, k) | "
            f"coalesce(CASE WHEN k > 5 THEN k END, {at} + duration({{minutes: i}}))] AS l "
            "RETURN k, [x IN l | x.minute] ORDER BY k"
        )
        == each
    )
    # Such a list a reduce gives, one at a map's key a temporal value's field is named for, and the keys of a map that
    # holds one.
    reduced = "reduce(acc = [], x IN l | acc + [x])"
    assert query(f"{rows} WITH k, {reduced} AS m RETURN k, [x IN m | x.minute] ORDER BY k") == each
    assert query(f"{rows} WITH k, {{minute: l}} AS m RETURN k, [x IN m.minute | x.minute] ORDER BY k") == each
    assert query(f"{rows} WITH k, {{l: l}} AS m RETURN k, keys(m) ORDER BY k") == [
        [0, ["l"]],
        [1, ["l"]],
        [2, ["l"]],
    ]
    # A list of a number and a temporal value sorts, and is searched with IN, as Cypher has it.
    mixed = f"UNWIND [[2, {at} + duration({{minutes: 2}})], [1, {at}], [2, {at} + duration({{minutes: 1}})]] AS l"
    assert query(f"{mixed} RETURN l[0], l[1].minute, {at} + duration({{minutes: 1}}) IN l, 2 IN l ORDER BY l") == [
        [1, 0, False, False],
        [2, 1, True, True],
        [2, 2, False, True],
    ]


def test_a_list_of_temporal_values_a_with_takes_out_of_an_unwound_map_is_read_item_by_item_in_order(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet: drivine4j's test of liberation-data/drivine4j#369. Oracle reads such a list by its places,
    # not with JSON_TABLE. Of 0, 1 and 2 items, at 10:01, 10:02.
    at = "localdatetime('2024-03-15T10:00:00')"
    listed = f"[i IN range(1, k) | {at} + duration({{minutes: i}})]"
    each = [[0, []], [1, [1]], [2, [1, 2]]]

    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    assert (
        query(
            f"UNWIND [0, 1, 2] AS k WITH k, [{{l: {listed}}}] AS ms UNWIND ms AS t WITH k, t.l AS l "
            "RETURN k, [x IN l | x.minute] ORDER BY k"
        )
        == each
    )
    assert (
        query(
            f"UNWIND [0, 1, 2] AS k WITH k, {listed} AS l0 UNWIND [{{l: l0}}] AS t WITH k, t.l AS l "
            "RETURN k, [x IN l | x.minute] ORDER BY k"
        )
        == each
    )
    # A map two levels down, and a list of two maps.
    assert (
        query(
            f"UNWIND [0, 1, 2] AS k WITH k, [{{m: {{l: {listed}}}}}] AS ms UNWIND ms AS t WITH k, t.m AS m "
            "WITH k, m.l AS l RETURN k, [x IN l | x.minute] ORDER BY k"
        )
        == each
    )
    assert query(
        f"UNWIND [0, 1, 2] AS k WITH k, [{{l: {listed}}}, "
        f"{{l: [i IN range(1, k) | {at} + duration({{minutes: 10 + i}})]}}] AS ms UNWIND ms AS t "
        "WITH k, t.l AS l RETURN k, [x IN l | x.minute] AS m ORDER BY k, m"
    ) == [[0, []], [0, []], [1, [1]], [1, [11]], [2, [1, 2]], [2, [11, 12]]]
    # After a write.
    clear(cypher_pm)
    assert (
        query(
            f"UNWIND [0, 1, 2] AS k WITH k, [{{l: {listed}}}] AS ms UNWIND ms AS t CREATE (:CyUnwoundTemporals) "
            "WITH k, t.l AS l RETURN k, [x IN l | x.minute] ORDER BY k"
        )
        == each
    )
    assert query("MATCH (n:CyUnwoundTemporals) RETURN count(n)") == [3]


def test_zoned_duration_and_partly_null_lists_out_of_an_unwound_map_and_numbers_beside_one_are_read_item_by_item(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet (liberation-data/drivine4j#369). Zoned values compare by their moments: 10:02+02:00 alone is
    # past 08:01:30Z.
    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    zoned = "[i IN range(1, k) | datetime('2024-03-15T10:00:00+02:00') + duration({minutes: i})]"
    assert query(
        f"UNWIND [0, 1, 2] AS k WITH k, [{{l: {zoned}}}] AS ms UNWIND ms AS t WITH k, t.l AS l "
        "RETURN k, [x IN l | x.minute], size([x IN l WHERE x > datetime('2024-03-15T08:01:30Z')]) ORDER BY k"
    ) == [[0, [], 0], [1, [1], 0], [2, [1, 2], 1]]
    durations = "[i IN range(1, k) | duration({minutes: i})]"
    assert query(
        f"UNWIND [0, 1, 2] AS k WITH k, [{{l: {durations}}}] AS ms UNWIND ms AS t WITH k, t.l AS l "
        "RETURN k, [x IN l | x.minutes] ORDER BY k"
    ) == [[0, []], [1, [1]], [2, [1, 2]]]
    partly_null = (
        "[i IN range(1, k) | CASE i WHEN 1 THEN null "
        "ELSE localdatetime('2024-03-15T10:00:00') + duration({minutes: i}) END]"
    )
    assert query(
        f"UNWIND [0, 1, 2] AS k WITH k, [{{l: {partly_null}}}] AS ms UNWIND ms AS t WITH k, t.l AS l "
        "RETURN k, [x IN l | x.minute], size([x IN l WHERE x IS NOT NULL]) ORDER BY k"
    ) == [[0, [], 0], [1, [None], 0], [2, [None, 2], 1]]
    # Numbers beside such a list in the map, and the list read two levels down in one WITH.
    local = "[i IN range(1, k) | localdatetime('2024-03-15T10:00:00') + duration({minutes: i})]"
    assert query(
        f"UNWIND [0, 1, 2] AS k WITH k, [{{l: {local}, n: range(1, k)}}] AS ms UNWIND ms AS t "
        "WITH k, t.l AS l, t.n AS n RETURN k, [x IN l | x.minute], [x IN n WHERE x > 1] ORDER BY k"
    ) == [[0, [], []], [1, [1], []], [2, [1, 2], [2]]]
    assert query(
        f"UNWIND [0, 1, 2] AS k WITH k, [{{m: {{l: {local}}}}}] AS ms UNWIND ms AS t WITH k, t.m.l AS l "
        "RETURN k, [x IN l | x.minute] ORDER BY k"
    ) == [[0, []], [1, [1]], [2, [1, 2]]]


def test_aggregates_of_temporal_arithmetic_give_what_they_give_of_it_worked_out_first_grouped_and_over_nulls(
    cypher_pm: PersistenceManager,
):
    # drivine4j's test of liberation-data/drivine4j#333, ported for #193: an aggregate's argument is worked out first,
    # for each row, where its values are bound; it gives what it did. Unbound, it compiled to megabytes.
    def query(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    clear(cypher_pm)
    query(
        """
        UNWIND [
          {id: 1, g: 'a', at: '2024-03-15T10:00:00+02:00', m: 1},
          {id: 2, g: 'a', at: '2024-03-15T10:00:00+02:00', m: 1},
          {id: 3, g: 'a', at: '2024-03-15T10:00:00+02:00', m: 4}, {id: 4, g: 'b', at: '2024-03-15T11:00:00Z', m: 5},
          {id: 5, g: 'b', at: null, m: 3}, {id: 6, g: 'b', at: '2024-03-15T12:30:00+02:00', m: 0}
        ] AS r
        CREATE (:CyAgg {id: r.id, g: r.g, at: r.at, m: r.m})
        """
    )
    t = "datetime(n.at) + duration({minutes: n.m})"
    # b's 12:30+02:00 is 10:30 UTC, before its 11:05Z: min() and max() compare moments, not their texts. Its null is
    # passed over.
    assert query(
        f"""
        MATCH (n:CyAgg) WITH n ORDER BY n.id
        WITH n.g AS g, count({t}) AS c, count(DISTINCT {t}) AS d, min({t}) AS lo, max({t}) AS hi,
          sum(({t}).minute) AS s, avg(({t}).minute) AS a, collect({t}) AS l
        RETURN {{g: g, count: c, distinct: d, lo: [lo.hour, lo.minute], hi: [hi.hour, hi.minute], sum: s, avg: a,
          times: [x IN l | [x.hour, x.minute]]}}
        ORDER BY g
        """
    ) == [
        {
            "g": "a",
            "count": 3,
            "distinct": 2,
            "lo": [10, 1],
            "hi": [10, 4],
            "sum": 6,
            "avg": 2.0,
            "times": [[10, 1], [10, 1], [10, 4]],
        },
        {
            "g": "b",
            "count": 2,
            "distinct": 2,
            "lo": [12, 30],
            "hi": [11, 5],
            "sum": 35,
            "avg": 17.5,
            "times": [[11, 5], [12, 30]],
        },
    ]
    # Kept distinct by collect(DISTINCT …), and read in an item beside the aggregate.
    assert query(
        f"MATCH (n:CyAgg) WITH n ORDER BY n.id WITH n.g AS g, collect(DISTINCT {t}) AS l "
        "RETURN {g: g, minutes: [x IN l | x.minute], n: size(l)} ORDER BY g"
    ) == [{"g": "a", "minutes": [1, 4], "n": 2}, {"g": "b", "minutes": [5, 30], "n": 2}]
    # Sorted by an aggregate of it, which is none of the items.
    assert query(f"MATCH (n:CyAgg) RETURN n.g AS g, count(*) AS c ORDER BY max(({t}).minute) DESC") == [
        ["b", 3],
        ["a", 3],
    ]
    # The size of a list of temporal values that a comprehension keeps, summed: 2 for each of b's moments after 09:30
    # UTC.
    assert query(
        "MATCH (n:CyAgg) RETURN sum(size([x IN [datetime(n.at), datetime(n.at) + duration('PT1H')] "
        "WHERE x > datetime('2024-03-15T09:30:00Z')]))"
    ) == [4]
    # Over no grouping key, the issue's statements.
    z = "datetime('2024-03-15T10:00:00+02:00') + duration({minutes: i})"
    assert query(
        f"UNWIND range(1, 3) AS i RETURN [count({z}), min({z}).minute, max({z}).minute, sum(({z}).minute)]"
    ) == [[3, 1, 3, 6]]
    assert query(f"UNWIND range(1, 4) AS i WITH i % 2 AS k, i WITH k, collect({z}) AS l RETURN k ORDER BY l") == [
        1,
        0,
    ]
    # Inside a CASE and arithmetic, in a WITH a WHERE filters, and in a CALL { … }: Oracle reads no imported variable in
    # an aggregate there, worked out first or not, so this one reads none.
    assert query(f"UNWIND range(1, 3) AS i RETURN [CASE WHEN count({z}) > 1 THEN 'many' END, count({z}) + 1]") == [
        ["many", 4]
    ]
    assert query(f"UNWIND range(1, 4) AS i WITH i % 2 AS k, max(({z}).minute) AS m WHERE m < 4 RETURN k") == [1]
    assert query(
        f"UNWIND [2] AS o CALL {{ WITH o UNWIND range(1, 3) AS i RETURN sum(({z}).minute) AS s }} RETURN s + o"
    ) == [8]
    # A reduce() in the argument, which Oracle declares a function for.
    assert query(f"UNWIND range(1, 3) AS i RETURN sum(reduce(a = 0, x IN [i] | a + ({z}).minute - i + x))") == [6]
    # A list of temporal values, kept distinct, read back by its items' fields: Oracle reads no temporal list by
    # JSON_TABLE (drivine4j#331).
    assert query(
        "WITH [datetime('2024-01-01T00:00Z'), datetime('2024-01-02T00:00Z')] AS l UNWIND [1, 2, 1] AS i "
        "WITH collect(DISTINCT [x IN l | x + duration({days: i})]) AS c RETURN [y IN c | [x IN y | x.day]]"
    ) == [[[2, 3], [3, 4]]]
    # Beside a variable-length walk in the same MATCH.
    query("MATCH (a:CyAgg {id: 1}), (b:CyAgg {id: 2}), (c:CyAgg {id: 3}) CREATE (a)-[:NEXT]->(b)-[:NEXT]->(c)")
    assert query(
        "MATCH (a:CyAgg)-[:NEXT*1..2]->(b:CyAgg) RETURN a.id AS a, "
        "count(datetime(b.at) + duration({minutes: b.m})) AS c, "
        "sum((datetime(b.at) + duration({minutes: b.m})).minute) AS s ORDER BY a"
    ) == [[1, 2, 5], [2, 1, 4]]
    # Sorted there by an aggregate of it that is none of the items, which binds no value beside the walk: see
    # `_project`.
    assert query(
        "MATCH (a:CyAgg)-[:NEXT*1..2]->(b:CyAgg) RETURN a.id AS a, count(*) AS c "
        "ORDER BY sum((datetime(b.at) + duration({minutes: b.m})).minute)"
    ) == [[2, 1], [1, 2]]


def test_an_aggregate_of_floats_gives_a_float_whole_or_not_and_a_sum_of_integers_an_integer(
    cypher_pm: PersistenceManager,
):
    # #189: Oracle's NUMBER keeps no kind, so a sum of floats that was whole came back an integer there. Each value is
    # checked with its type, as 2 == 2.0 in Python.
    clear(cypher_pm)
    cypher_pm.execute(
        spec(
            "CREATE (:F {k: 'a', x: 0.5}), (:F {k: 'a', x: 1.0}), (:F {k: 'a', x: 0.5}), (:F {k: 'b', x: 1}), "
            "(:F {k: 'b', x: 2})"
        )
    )

    def kinds(value: Any) -> Any:
        return [kinds(it) for it in value] if isinstance(value, list) else (value, type(value).__name__)

    cases: list[tuple[str, dict[str, Any], Any]] = [
        (statement, {}, value)
        for statement, value in [
            ("UNWIND [0.5, 1.0, 0.5] AS x RETURN sum(x)", 2.0),
            ("UNWIND [1, 1.0] AS x RETURN sum(x)", 2.0),
            ("UNWIND [1, 0.5, 0.5] AS x RETURN sum(x)", 2.0),
            ("UNWIND [1, 2] AS x RETURN sum(x)", 3),
            ("UNWIND [] AS x RETURN sum(x)", 0),
            ("UNWIND [1.0, 1.0] AS x RETURN sum(DISTINCT x)", 1.0),
            # Summed as doubles: past a NUMBER's range, 1e300 is no NUMBER.
            ("UNWIND [1e300, 1.0] AS x RETURN sum(x)", 1e300),
            # An integer among floats is summed as a double too, as Neo4j sums it, and a computed float is one.
            ("UNWIND [9223372036854775807, 0.5] AS x RETURN sum(x)", 9.223372036854776e18),
            ("UNWIND [0.5, 2] AS x RETURN sum(x * 10)", 25.0),
            ("UNWIND [0.5, 1.5] AS x RETURN avg(x)", 1.0),
            ("UNWIND [1, 3] AS x RETURN avg(x)", 2.0),
            ("UNWIND [] AS x RETURN avg(x)", None),
            ("UNWIND [2.0, 3.5] AS x RETURN min(x)", 2.0),
            ("UNWIND [2.0, 1.5] AS x RETURN max(x)", 2.0),
            ("UNWIND [1, 2.0] AS x RETURN max(x)", 2.0),
            ("UNWIND [0.5, 1.0, 0.5] AS x RETURN [toString(sum(x)), sum(x) + 1, sum(x) * 2]", ["2.0", 3.0, 4.0]),
            (
                "UNWIND [['a', 0.5], ['a', 1.5], ['b', 1], ['b', 1]] AS r WITH r[0] AS k, r[1] AS x "
                "WITH k, [sum(x), avg(x), min(x), max(x)] AS a ORDER BY k RETURN collect(a)",
                [[2.0, 1.0, 0.5, 1.5], [2, 1.0, 1, 1]],
            ),
            ("MATCH (n:F) WITH n.k AS k, sum(n.x) AS s ORDER BY k RETURN collect(s)", [2.0, 3]),
            ("MATCH (n:F) WHERE n.k = 'a' AND n.x = 1.0 RETURN sum(n.x)", 1.0),
        ]
    ] + [
        ("UNWIND $xs AS x RETURN sum(x)", {"xs": xs}, total)
        for xs, total in [
            # Parameters: a whole float is bound so it stays one, and a fraction is a number on Oracle.
            ([0.5, 1.0, 0.5], 2.0),
            ([0.5, 0.5], 1.0),
            ([1.0], 1.0),
            ([2, 3], 5),
            # Oracle writes a fraction of more than 40 digits with an exponent and no point.
            ([1e-41, -1e-41], 0.0),
        ]
    ]
    # Each that differs, so a store's every difference shows at once.
    differing = []
    for statement, parameters, value in cases:
        given = kinds((cypher_pm.query(spec(statement, parameters)))[0])
        if given != kinds(value):
            differing.append(f"{statement} {parameters}: {given}, not {kinds(value)}")
    assert differing == []
    # Not compiled to SQL yet, so they give no integer for a float.
    for statement, value in [
        ("UNWIND [2.0, 3.0] AS x RETURN percentileDisc(x, 0.5)", 2.0),
        ("UNWIND [1.0, 3.0] AS x RETURN percentileCont(x, 0.5)", 2.0),
        ("UNWIND [1.0, 3.0] AS x RETURN stDevP(x)", 1.0),
        ("UNWIND [1.0, 3.0] AS x RETURN stDev(x)", 1.4142135623730951),
        ("UNWIND [1, 3] AS x RETURN percentileDisc(x, 0.5)", 1),
    ]:
        if cypher_pm.type is DatabaseType.NEO4J:
            assert kinds((cypher_pm.query(spec(statement)))[0]) == kinds(value), statement
        else:
            with pytest.raises(DrivineError, match="is Cypher, and is not compiled to SQL yet."):
                cypher_pm.query(spec(statement))


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
    message = None if cypher_pm.type is DatabaseType.NEO4J else re.escape(PRECISION_OUT_OF_RANGE)
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
    # To 0 places Neo4j rounds as Java's Math.round does, half toward +infinity (drivine4j#278), as the spec's steps
    # for a number the statement gives and places from data have it; to more, half away from 0.
    # Zero and just below a half below it, past 2^52, and near the largest Oracle's NUMBER holds: places the statement
    # gives, and places the store works out.
    rounded = [0.0, 0.0, 0.0, -4503599627370497.0, 9.0e125, -9.0e125]
    assert cypher_pm.query(
        spec(
            "RETURN [round(-0.0, 0), round(-0.4, 0), round(-0.49999999999999994, 0), round(-4503599627370497.0, 0), "
            "round(9.0E125, 0), round(-9.0E125, 0)]"
        )
    ) == [rounded]
    assert cypher_pm.query(
        spec(
            "RETURN [x IN [-0.0, -0.4, -0.49999999999999994, -4503599627370497.0, 9.0E125, -9.0E125] "
            "| round(x, toInteger('0'))]"
        )
    ) == [rounded]
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
    # Places a parameter gives, picked when compiling: a fraction, -0.5, negated, or NaN is 0 places, as Neo4j casts
    # NaN to 0, and 1.0 is 1; a mode rounds by it alone.
    assert cypher_pm.query(
        spec(
            "RETURN [round(-2.5, $p), round(-2.5, $q), round(-2.5, -$r), round(-2.5, $z), round(-2.5, $n), "
            "round(-2.5, $o), round(-2.55, $o), round(-2.5, 0.9999), round(-2.5, 1.0), round(-2.5, $p, 'HALF_UP'), "
            "round(-2.5, 0, 'HALF_UP'), round(-2.5, $n, 'HALF_UP'), round(-2.55, $n, 'UP')]",
            {"p": 0.7, "q": -0.5, "r": 0.5, "z": 0, "n": float("nan"), "o": 1.0},
        )
    ) == [[-2.0, -2.0, -2.0, -2.0, -2.0, -2.5, -2.6, -2.0, -2.5, -3.0, -3.0, -3.0, -3.0]]


def test_round_by_a_mode_and_sign_take_the_least_positive_numbers_as_positive_where_oracles_sign_gives_0(
    cypher_pm: PersistenceManager,
):
    # Oracle's SIGN gives 0 for a positive number from 1.0E-130 to under 1.0E-128 (#163); UP, DOWN and the HALF_ modes
    # round by the sign. The spec holds them written and bound; here they are read from a row, beside 0, -0.0 and
    # null, whose sign the rounding does not ask, and under an aggregate.
    assert cypher_pm.query(
        spec(
            "UNWIND [1.0E-129, -1.0E-129, 0.0, -0.0, null] AS x "
            "RETURN [sign(x), round(x, 2, 'UP'), round(x, 129, 'HALF_EVEN'), round(x, 0, 'DOWN')]"
        )
    ) == [
        [1, 0.01, 1.0e-129, 0.0],
        [-1, -0.01, -1.0e-129, 0.0],
        [0, 0.0, 0.0, 0.0],
        [0, 0.0, 0.0, 0.0],
        [None, None, None, None],
    ]
    assert cypher_pm.query(
        spec(
            "UNWIND [1.0E-129, 5.0E-130] AS x "
            "RETURN [sign(sum(x)), sum(sign(x)), sign(min(x)), round(max(x), 2, 'UP'), sign(sign(sign(min(x))))]"
        )
    ) == [[1, 2, 1, 0.01, 1]]
    # A sign is an integer, as on Neo4j, which the comparisons above, where 1 == 1.0, don't tell.
    signs = cypher_pm.query(
        spec(
            "UNWIND [1.0E-129, -1.5] AS x WITH collect(x) AS xs, sum(x) AS total "
            "RETURN [sign(xs[0]), sign(xs[1]), sign(total), sign(sign(xs[0])), sign(0.0), sign(7)]"
        )
    )
    assert [[type(sign) for sign in row] for row in signs] == [[int] * 6]
    # Numbers near NUMBER's greatest, which a sign told by a multiple of them would overflow.
    assert cypher_pm.query(spec("RETURN [sign(9.0E125), sign(-9.0E125), sign(7), sign(-0.5), sign(0.5)]")) == [
        [1, -1, 1, -1, 1]
    ]


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
    # Floats each, whole or not (#189), and the count an integer.
    sums = cypher_pm.query(
        spec(
            "UNWIND range(0, 2999) AS i WITH toFloat(i % 3) * 0.5 AS x "
            "RETURN [sum(round(x, 400)), sum(round(x, 0)), sum(round(x, 0, 'UP')), count(*)]"
        )
    )
    assert [[(it, type(it)) for it in row] for row in sums] == [
        [(1500.0, float), (2000.0, float), (2000.0, float), (3000, int)]
    ]
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
    message = None if cypher_pm.type is DatabaseType.NEO4J else re.escape(PRECISION_OUT_OF_RANGE)
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
    cypher_pm.execute(spec("CREATE (:Round {r: -2, k: 1, x: 1234.5, i: 1234}), (:Round {r: 1, k: 2})"))
    message = None if cypher_pm.type is DatabaseType.NEO4J else re.escape(PRECISION_OUT_OF_RANGE)
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
        # A number to round, by a precision from data: -0.0, an integer, which Oracle's JSON holds as a number
        # of another kind than a float's, and of an aggregate, where nothing is shared.
        "MATCH (n:Round) WHERE n.k = 1 RETURN round(-0.0, n.r)",
        "MATCH (n:Round) WHERE n.k = 1 RETURN round(n.x, n.r)",
        "MATCH (n:Round) WHERE n.k = 1 RETURN round(n.i, n.r)",
        "MATCH (n:Round) WHERE n.k = 1 RETURN round(n.x, n.r, 'HALF_UP')",
        "MATCH (n:Round) WITH n.x AS x, n.r AS r RETURN r, round(min(x), r)",
        "MATCH (n:Round) WITH n.x AS x, n.r AS r RETURN r, round(max(x), r, 'HALF_UP')",
        "MATCH (n:Round) RETURN collect(round(n.x, n.r))",
    ]:
        with pytest.raises(Exception, match=message):  # noqa: PT011
            cypher_pm.query(spec(statement))
    with pytest.raises(Exception, match=message):  # noqa: PT011
        cypher_pm.query(spec("MATCH (n:Round) WHERE n.k = 1 RETURN round($v, n.r)", {"v": 1.5}))
    assert cypher_pm.query(spec("MATCH (n:Round) WHERE n.r >= 0 RETURN round(1234.5, n.r)")) == [1234.5]
    assert cypher_pm.query(spec("MATCH (n:Nothing) RETURN round(1234.5, 1 - 3)")) == []
    assert cypher_pm.query(spec("UNWIND [] AS x RETURN round(1234.5, 1 - 3)")) == []
    assert cypher_pm.query(spec("MATCH (n:Round) WHERE n.k > 5 RETURN round(1234.5, toInteger('-2'))")) == []
    assert cypher_pm.query(spec("MATCH (n:Round) WHERE n.k = 2 RETURN sum(round(1234.5, n.r))")) == [1234.5]
    # A null to round is null, whatever the precision, as Neo4j gives it.
    assert cypher_pm.query(
        spec("MATCH (n:Round) WHERE n.k = 1 RETURN [round(n.none, n.r), round(n.none, n.r, 'HALF_UP')]")
    ) == [[None, None]]
    assert cypher_pm.query(spec("MATCH (n:Round) RETURN round(round(n.none, n.r), n.r)")) == [None, None]
    # Of an aggregate, where nothing is shared.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) WITH n.none AS x, n.r AS r "
            "WITH r, [round(min(x), r), round(max(x), r, 'HALF_UP')] AS v RETURN v"
        )
    ) == [[None, None], [None, None]]
    assert cypher_pm.query(spec("MATCH (n:Round) RETURN collect(round(n.none, n.r))")) == [[]]
    # A null written in the statement, or a parameter's.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) WHERE n.k = 1 RETURN [round(null, n.r), round(null, n.r, 'UP'), round($v, n.r)]",
            {"v": None},
        )
    ) == [[None, None, None]]
    assert cypher_pm.query(
        spec("MATCH (n:Round) RETURN CASE WHEN n.r < 0 THEN 0.0 ELSE round(1234.5, n.r) END ORDER BY n.k")
    ) == [0.0, 1234.5]
    assert cypher_pm.query(spec("RETURN [round(1234.5, 3 - 1), round(1234.5, toInteger('2'))]")) == [[1234.5, 1234.5]]


def test_a_precision_written_below_0_or_past_2147483647_fails_only_on_a_row_that_rounds_a_number_by_it(
    cypher_pm: PersistenceManager,
):
    # The spec's cases since drivine4j#290 and #305, here in a WHERE, an aggregate and a CASE, on rows filtered out, and
    # by a parameter no YAML holds. Neo4j fails each with its own message; a SQL store with the compiler's.
    clear(cypher_pm)
    cypher_pm.execute(spec("CREATE (:Round {x: 1.5, k: 1}), (:Round {k: 2})"))
    message = None if cypher_pm.type is DatabaseType.NEO4J else re.escape(PRECISION_OUT_OF_RANGE)
    for statement, parameters in [
        ("MATCH (n:Round) WHERE n.k = 1 RETURN round(n.x, -2)", {}),
        ("MATCH (n:Round) WHERE round(n.x, 2147483648, 'UP') > 0 RETURN n.k", {}),
        ("MATCH (n:Round) RETURN sum(round(n.x, -$p))", {"p": 2}),
        ("MATCH (n:Round) RETURN collect(round(n.x, $p, 'HALF_UP'))", {"p": 1.0e20}),
        ("RETURN round(1.5, $p)", {"p": math.inf}),
        ("RETURN round(1.5, $p, 'UP')", {"p": -math.inf}),
        ("RETURN round(1.5, -9223372036854775808)", {}),
    ]:
        with pytest.raises(Exception, match=message):  # noqa: PT011
            cypher_pm.query(spec(statement, parameters))
    # A row that holds no number rounds to null, and a row the CASE does not round, or no row at all, fails nothing.
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) WHERE n.k = 2 RETURN [round(n.x, -2), round(n.x, 2147483648, 'UP'), round(n.x, $p)]",
            {"p": 1.0e20},
        )
    ) == [[None, None, None]]
    assert cypher_pm.query(spec("RETURN [round(null, $p), round(null, $p, 'FLOOR')]", {"p": math.inf})) == [
        [None, None]
    ]
    assert cypher_pm.query(
        spec(
            "MATCH (n:Round) RETURN CASE WHEN n.k > 0 THEN 0.0 ELSE round(1.5, -2) + round(n.x, 2147483648) END "
            "ORDER BY n.k"
        )
    ) == [0.0, 0.0]
    assert cypher_pm.query(
        spec("MATCH (n:Round) WHERE n.k = 1 RETURN collect(CASE WHEN n.k = 2 THEN round(n.x, -2, 'UP') END)")
    ) == [[]]
    assert (
        cypher_pm.query(
            spec("MATCH (n:Round) WHERE n.k = 3 RETURN [round(1.5, -2), round(1.5, $p, 'UP')]", {"p": 1.0e20})
        )
        == []
    )
    assert cypher_pm.query(spec("UNWIND [] AS x RETURN round(1.5, -2)")) == []
    # Nor in an ORDER BY whose round() reads nothing of the row. Not in a WHERE: Neo4j may test a condition that reads
    # nothing of a row before the rows' own, and Postgres does.
    assert (
        cypher_pm.query(
            spec(
                "MATCH (n:Round) WHERE n.k = 3 RETURN n.k ORDER BY round(1.5, -2), round(1.5, $p, 'UP')",
                {"p": 1.0e20},
            )
        )
        == []
    )


def _float_count(function: str, what: str) -> str:
    """What the compiler refuses a written float start or length `what` of the function with."""
    return re.escape(f"{function}() takes an integer for its {what}, not a float, as in Neo4j.")


def _count_out_of_range(function: str, what: str) -> str:
    """What the compiler refuses a start or length `what` of the function out of range with."""
    return re.escape(f"{function}() takes a {what} from 0 to 2147483647, as in Neo4j.")


def test_substring_left_and_right_refuse_a_written_float_and_a_given_start_or_length_out_of_range(
    cypher_pm: PersistenceManager,
):
    # A start or a length (#263), past the spec's steps of them. Neo4j refuses a float it knows to be one whatever the
    # text; a bound one it cuts toward zero. It refuses one out of range only where the text is not null.
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
    # A start or a length read from data (#263), past the spec's steps of them: in a WHERE and an aggregate too, and
    # on rows filtered out.
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
    # Bound past what a long holds, beside a text from data: cut to the greatest or least long, as Java casts a double,
    # so no store is given a literal past its number (#169). A Decimal, which the Neo4j driver does not send, is too.
    for n in [1.0e300, -1.0e300, 10**400, -(10**400), *([Decimal("Infinity")] if compiles else [])]:
        with pytest.raises(Exception, match=_count_out_of_range("left", "length") if compiles else None):  # noqa: PT011
            cypher_pm.query(spec("MATCH (n:Count) RETURN left(n.t, $n)", {"n": n}))
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


def test_keys_gives_each_rows_own_keys_of_a_node_a_relationship_a_map_or_a_parameter_wherever_it_is_read(
    cypher_pm: PersistenceManager,
):
    # On Oracle every row had the first row's keys (liberation-data/drivine4j#414, here #257): the map's data guide was
    # worked out by a subquery in a lateral view's select list, which Oracle works out once. The spec's cases read one
    # row's keys only. In a comprehension, a quantifier, a DISTINCT and an order as well.
    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    clear(cypher_pm)
    rows(
        "CREATE (a:CyKeyed {id: 1, name: 'Andres', surname: 'Lopez'}), "
        "(b:CyKeyed {id: 2, otherName: 'Andres', otherSurname: 'Lopez'}), (c:CyKeyed {id: 3, a: 1}), "
        "(d:CyKeyed {id: 4, b: 2}), (a)-[:CY_KEYED {w: 1, p: 1}]->(b), (c)-[:CY_KEYED {w: 2, q: 2, z: 3}]->(d)"
    )
    assert rows("MATCH (n:CyKeyed) UNWIND keys(n) AS k RETURN n.id, k ORDER BY n.id, k") == [
        [1, "id"],
        [1, "name"],
        [1, "surname"],
        [2, "id"],
        [2, "otherName"],
        [2, "otherSurname"],
        [3, "a"],
        [3, "id"],
        [4, "b"],
        [4, "id"],
    ]
    # The TCK's Graph8 #2.
    assert rows("MATCH (n:CyKeyed) UNWIND keys(n) AS x RETURN DISTINCT x ORDER BY x") == [
        "a",
        "b",
        "id",
        "name",
        "otherName",
        "otherSurname",
        "surname",
    ]
    assert rows("MATCH (n:CyKeyed) WITH n ORDER BY n.id RETURN collect(size(keys(n)))") == [[3, 3, 2, 2]]
    assert rows("MATCH (n:CyKeyed) RETURN 'b' IN keys(n) ORDER BY n.id") == [False, False, False, True]
    assert rows("MATCH (n:CyKeyed) RETURN [k IN keys(n) WHERE k IN ['a', 'b'] | k + '!'] ORDER BY n.id") == [
        [],
        [],
        ["a!"],
        ["b!"],
    ]
    assert rows("MATCH (:CyKeyed)-[r:CY_KEYED]->() UNWIND keys(r) AS k RETURN r.w, k ORDER BY r.w, k") == [
        [1, "p"],
        [1, "w"],
        [2, "q"],
        [2, "w"],
        [2, "z"],
    ]
    assert rows("UNWIND [{a: 1}, {b: 2}] AS m RETURN keys(m)") == [["a"], ["b"]]
    assert rows("UNWIND $ms AS m RETURN keys(m)", {"ms": [{"a": 1}, {"b": 2}]}) == [["a"], ["b"]]
    assert rows("WITH [{a: 1}, {b: 2}] AS ms RETURN [m IN ms | keys(m)]") == [[["a"], ["b"]]]
    assert rows("UNWIND [{a: 1}, {b: 2}] AS m RETURN [x IN [m, {c: 3}] | keys(x)]") == [
        [["a"], ["c"]],
        [["b"], ["c"]],
    ]
    assert rows("UNWIND [{b: 1}, {a: 2}, {b: 3}] AS m RETURN DISTINCT keys(m) ORDER BY keys(m)") == [
        ["a"],
        ["b"],
    ]
    assert rows("UNWIND [{b: 1}, {a: 2}] AS m RETURN coalesce(m.a, m.b) ORDER BY keys(m)") == [2, 1]
    # A quantifier over each row's keys in a WHERE, the keys of a relationship an OPTIONAL MATCH didn't find, and of
    # maps in a list each row holds.
    assert rows(
        "UNWIND $rows AS row MATCH (n:CyKeyed {id: row.id}) WHERE any(k IN keys(row.props) WHERE k = 'b') RETURN n.id",
        {"rows": [{"id": 1, "props": {"a": 1}}, {"id": 4, "props": {"b": 2}}]},
    ) == [4]
    assert rows("MATCH (n:CyKeyed) OPTIONAL MATCH (n)-[r:CY_KEYED]->() RETURN n.id, size(keys(r)) ORDER BY n.id") == [
        [1, 2],
        [2, None],
        [3, 3],
        [4, None],
    ]
    assert rows("UNWIND [[{a: 1}, {b: 2, c: 3}], [{d: 4, e: 5, f: 6}]] AS l RETURN [x IN l | size(keys(x))]") == [
        [1, 2],
        [3],
    ]
    # A nested map's paths are in its parent's data guide too, and only those one step from the top are keys.
    assert rows("UNWIND [{a: {x: 1}}, {b: {y: 2}, c: 3}] AS m RETURN size(keys(m)), keys(coalesce(m.a, m.b))") == [
        [1, ["x"]],
        [2, ["y"]],
    ]


def test_lists_and_maps_that_hold_a_null_compare_item_by_item_null_where_no_pair_differs(
    cypher_pm: PersistenceManager,
):
    # Beyond the spec's cases (#116, drivine4j#225): Cypher compares a list or a map item by item, so a null it holds
    # makes the comparison null.
    def rows(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    assert rows("RETURN [[null] = [null], {x: null} = {x: null}, [null, 1] = [null, 1]]") == [[None, None, None]]
    assert rows("RETURN [[1, null] = [2, null], [1, 2] = [1], [null] = [null, null], [] = []]") == [
        [False, False, False, True]
    ]
    assert rows("RETURN [[null] <> [null], [1, null] <> [2, null], [null, 1] <> [null, 1]]") == [[None, True, None]]
    assert rows(
        "RETURN [{x: null} = {y: null}, {x: 1, y: null} = {x: 2, y: null}, {x: null} = {x: 1}, {x: null} = {}, "
        "{a: 1} = {a: 1, b: null}]"
    ) == [[False, False, None, False, False]]
    assert rows(
        "RETURN [[[null]] = [[null]], [[1, null]] = [[2, null]], [{x: null}] = [{x: null}], {a: [null]} = {a: [null]}, "
        "[1, [null]] = [2, [null]], [[null], 1] = [[null], 2]]"
    ) == [[None, False, None, None, False, False]]
    assert rows(
        "RETURN [[1] = ['1'], [null] = [1], [[1]] = [1], [null, 'a'] = [1, 'b'], [1] = [1.0], [null] = [[1]], "
        "[null] = [{x: 1}], [null, 1] = [1, null]]"
    ) == [[False, None, False, False, True, None, None, None]]
    assert rows("RETURN CASE [null] WHEN [null] THEN 'y' ELSE 'n' END") == ["n"]
    # Lists that hold no null compare as before.
    assert rows(
        "RETURN [[1, 2] = [1, 2], [1, 2] <> [1, 3], {x: 1} = {x: 1}, 'a' IN ['a', 'b'], 'c' IN ['a', 'b']]"
    ) == [[True, True, True, True, False]]


def test_in_compares_a_value_with_each_item_as_equality_does(cypher_pm: PersistenceManager):
    # Beyond the spec's cases (#116): true where an item is equal, else null where a comparison is null, else false.
    seeded(cypher_pm)
    # A node with no name is in no list of names, nor out of one: NOT keeps it no more than the IN does.
    counted = (
        "MATCH (n:CyTest) WHERE NOT (n.name IN ['Keanu']) RETURN count(*) UNION ALL "
        "MATCH (n:CyTest) WHERE n.name IN ['Keanu'] AND n.born > 0 OR n.name IN $x RETURN count(*)"
    )
    assert cypher_pm.query(spec(counted, {"x": []})) == [3, 1]
    # A quantifier keeps an item where its condition is true, so one with no name is in all of no list of names.
    quantified = "MATCH (n:CyTest) WHERE all(x IN [1] WHERE n.name IN ['Keanu']) RETURN count(*)"
    assert cypher_pm.query(spec(quantified)) == [1]
    written = (
        "RETURN [null IN [null], [null] IN [[null]], 1 IN [null], 1 IN [1, null], 2 IN [1, null], null IN [], "
        "[null] IN [[1, 2]], [null] IN [[1]], [1, null] IN [[2, null]], [1] IN [[1], null], {x: null} IN [{x: null}], "
        "[1] IN [[2], null], [1] IN [[null]], null IN [1]]"
    )
    assert cypher_pm.query(spec(written)) == [
        [None, None, None, True, None, False, False, None, False, True, None, None, None, None]
    ]
    given = {"x": None, "l": [1, 2], "a": [1, None], "ls": [[1, None], [3]], "n": None}
    assert cypher_pm.query(spec("RETURN [$x IN $l, 2 IN $l, $a IN $ls, $a IN [[2, null]], 1 IN $n]", given)) == [
        [None, True, None, False, None]
    ]
    # Read by a WHERE, a null is in no list that holds one: JSON's null is equal to such an item as JSON.
    nulls = {"l": [None, 2], "m": {"x": None}}
    assert cypher_pm.query(spec("UNWIND [1] AS i WITH i WHERE null IN $l OR $m.x IN $l RETURN i", nulls)) == []
    assert cypher_pm.query(spec("RETURN [x IN [1] WHERE null IN $l OR $m.x IN $l]", nulls)) == [[]]


def test_quantifiers_are_three_valued_over_parameters_nodes_relationships_and_empty_lists_and_in_each_where(
    cypher_pm: PersistenceManager,
):
    # Beyond the spec's cases (#159): a list given, an empty or null one of nodes or relationships, one of them that
    # holds a null, quantifiers nested and under an aggregate, and a WHERE of a comprehension or a subquery, which keeps
    # what a quantifier is true of, read through a NOT that keeps no more where it is null.
    seeded(cypher_pm)

    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    quantified = (
        "RETURN [any(x IN $xs WHERE x = 2), all(x IN $xs WHERE x = 1), none(x IN $xs WHERE x = 2), "
        "single(x IN $xs WHERE x = 1)]"
    )
    assert rows(quantified, {"xs": [1, None]}) == [[None, None, None, None]]
    assert rows(quantified, {"xs": None}) == [[None, None, None, None]]
    assert rows(quantified, {"xs": []}) == [[False, True, True, False]]
    assert rows(
        "MATCH (p:Person {name: 'Nope'}) WITH collect(p) AS ps RETURN [any(x IN ps WHERE x.born > 1), "
        "all(x IN ps WHERE x.born > 1), none(x IN ps WHERE x.born > 1), single(x IN ps WHERE x.born > 1)]"
    ) == [[False, True, True, False]]
    assert rows(
        "MATCH (k:Person {name: 'Keanu'}), (h:Person {name: 'Hugo'}) RETURN [any(x IN [k, h] WHERE x.born > 1960), "
        "all(x IN [k, h] WHERE x.born > 1960), none(x IN [k, h] WHERE x.born < 1960), "
        "single(x IN [k, h] WHERE x.born > 1960), all(x IN [k, null] WHERE x.born > 1960), "
        "any(x IN [h, null] WHERE x.born > 1960)]"
    ) == [[True, None, None, None, None, None]]
    # Keanu's KNOWS have no role.
    assert rows(
        "MATCH (k:Person {name: 'Keanu'}) WITH [(k)-[r]-() | r] AS rs RETURN [any(r IN rs WHERE r.role = 'Neo'), "
        "all(r IN rs WHERE r.role STARTS WITH 'N'), single(r IN rs WHERE r.role = 'Johnny'), "
        "none(r IN rs WHERE r.role = 'X')]"
    ) == [[True, False, None, None]]
    assert rows(
        "RETURN [any(y IN [1, 2] WHERE all(x IN [y, null] WHERE x > 0)), "
        "any(y IN [1, 2] WHERE single(x IN [1, 2, null] WHERE x = y)), all(y IN [1, 2] WHERE none(x IN [3, y] WHERE "
        "x = y + 1)), [y IN [1, 2, 3] WHERE NOT any(x IN [null, 2] WHERE x = y) | y], "
        "[y IN [1, 2, 3] WHERE any(x IN [null, 2] WHERE x = y) OR y = 1 | y]]"
    ) == [[None, None, False, [], [1, 2]]]
    assert rows(
        "MATCH (k:Person {name: 'Keanu'}) RETURN [[(k)-[:ACTED_IN]->(m) WHERE NOT any(x IN [1] WHERE m.nope > 1) | "
        "m.title], [(k)-[:ACTED_IN]->(m) WHERE NOT all(x IN [null, 1] WHERE x = 1) OR m.title = 'X' | m.title], "
        "[(k)-[:ACTED_IN]->(m) WHERE none(x IN [m.nope] WHERE x = 1) | m.title]]"
    ) == [[[], [], []]]
    assert rows(
        "MATCH (p:Person) WHERE EXISTS { MATCH (p)-[r]->() WHERE NOT all(x IN [1] WHERE r.role > 'N') } "
        "RETURN p.name ORDER BY p.name"
    ) == ["Keanu", "Laurence"]
    assert rows(
        "MATCH (p:Person) WHERE any(x IN $xs WHERE x = 2) IS NULL AND NOT none(x IN $xs WHERE x = 1) "
        "RETURN p.name ORDER BY p.name",
        {"xs": [1, None]},
    ) == ["Carrie", "Hugo", "Keanu", "Laurence"]
    assert rows(
        "MATCH (p:Person) WHERE NOT (any(x IN [1] WHERE p.born > 1966) OR p.name = 'Laurence') RETURN p.name"
    ) == ["Keanu"]
    assert rows(
        "MATCH (p:Person) RETURN [any(x IN collect(p.born) WHERE x > 1966), all(x IN collect(p.born) WHERE x > 1960), "
        "none(x IN [n IN collect(p) | n.born] WHERE x > 1970)]"
    ) == [[True, True, None]]


def test_quantifiers_are_three_valued_in_a_projection_a_comparison_an_order_and_a_case_and_over_long_texts(
    cypher_pm: PersistenceManager,
):
    # Beyond the spec's cases (#159): quantifiers read for their value, not by a WHERE, over lists and maps that hold a
    # null, keys and labels, a path's relationships, and texts past Oracle's 4000 bytes, for a value and in a WHERE.
    seeded(cypher_pm)

    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    assert rows(
        "RETURN [l IN [[1, null], [2], [3], null] | [any(x IN l WHERE x = 2), all(x IN l WHERE x = 3), "
        "none(x IN l WHERE x = 2), single(x IN l WHERE x > 1)]]"
    ) == [[[None, False, None, None], [True, False, False, True], [False, True, True, True], [None, None, None, None]]]
    assert rows(
        "RETURN [any(x IN [[1], null] WHERE x[0] = 1), all(x IN [[1], null] WHERE x[0] = 1), "
        "none(x IN [[2], null] WHERE x[0] = 1), single(x IN [[1], [1], null] WHERE x[0] = 1), "
        "single(x IN [[1], null] WHERE x[0] = 1), any(x IN [{a: 1}, {a: null}, null] WHERE x.a = 2), "
        "single(m IN [{a: 1}, {a: 1}, {a: null}] WHERE m.a = 1)]"
    ) == [[True, None, None, False, None, None, False]]
    assert rows(
        "RETURN [any(x IN [1, null] WHERE x = 2) = false, any(x IN [1, null] WHERE x = 2) IS NULL, "
        "all(x IN [] WHERE x = 1) = true, any(x IN [1, 'a'] WHERE x > 0), all(x IN [1, 'a'] WHERE x > 0)]"
    ) == [[None, True, True, True, None]]
    assert rows(
        "UNWIND [[1, null], [2], [3], null, []] AS l WITH l ORDER BY all(x IN l WHERE x > 1), size(l) RETURN collect(l)"
    ) == [[[1, None], [], [2], [3]]]
    assert rows(
        "UNWIND [[1, null], [2], [3]] AS l RETURN CASE WHEN any(x IN l WHERE x = 2) THEN 'y' "
        "WHEN all(x IN l WHERE x = 1) IS NULL THEN 'n' ELSE 'e' END"
    ) == ["n", "y", "e"]
    assert rows(
        "MATCH (k:Person {name: 'Keanu'}) RETURN [any(x IN keys(k) WHERE x = 'born'), "
        "all(x IN keys(k) WHERE x = 'name'), single(x IN labels(k) WHERE x = 'Person')]"
    ) == [[True, False, True]]
    assert rows(
        "MATCH p = (:Person {name: 'Laurence'})-[:KNOWS*]->(b) RETURN [b.name, any(r IN relationships(p) WHERE "
        "r.role = 'x'), none(r IN relationships(p) WHERE r.role = 'x')] ORDER BY b.name"
    ) == [["Carrie", None, None], ["Keanu", None, None]]
    texts = {"t": "a" * 5000 + "b", "u": "a" * 5000}
    assert rows(
        "UNWIND [$t, $u] AS t WITH collect(t) AS ts RETURN [any(x IN ts WHERE x = $t), all(x IN ts WHERE x = $t), "
        "single(x IN ts + [null] WHERE x = $u), single(x IN ts + ts WHERE x = $u)]",
        texts,
    ) == [[True, False, None, False]]
    # Searched, too: Oracle read a text past 4,000 bytes as null where it searched one (#168).
    assert rows(
        "UNWIND [$t, $u] AS t WITH collect(t) AS ts WITH ts WHERE any(x IN ts WHERE x ENDS WITH 'b') "
        "AND none(x IN ts WHERE x STARTS WITH 'c') AND all(x IN ts WHERE x STARTS WITH $u) RETURN size(ts)",
        texts,
    ) == [2]
    assert rows(
        "UNWIND [$t, $u] AS t WITH collect(t) AS ts RETURN [single(x IN ts WHERE x STARTS WITH $u), "
        "single(x IN ts WHERE x ENDS WITH 'b'), none(x IN ts WHERE x CONTAINS 'c')]",
        texts,
    ) == [[False, True, True]]
    # A list long enough to be bound once in a subquery, of values and of nodes, read for a value and in a WHERE.
    comprehended = "[y IN l WHERE y > 0 | y * 2]"
    assert rows(
        f"UNWIND [[1, null], [2], [3], null] AS l "
        f"RETURN [any(x IN {comprehended} WHERE x = 4), all(x IN {comprehended} WHERE x = 4)]"
    ) == [[False, False], [True, True], [False, False], [None, None]]
    assert rows(
        f"UNWIND [[1, null], [2], [3], null] AS l WITH l WHERE any(x IN {comprehended} WHERE x = 4) "
        f"OR NOT all(x IN {comprehended} WHERE x = 6) RETURN collect(l)"
    ) == [[[1, None], [2]]]
    assert rows(
        "MATCH (p:Person) WITH collect(p) AS ps WHERE all(x IN [n IN ps WHERE n.born > 0] WHERE x.born > 1960) "
        "RETURN [size(ps), any(x IN [n IN ps WHERE n.born > 0] WHERE x.born > 1966)]"
    ) == [[4, True]]


def test_values_that_hold_a_null_compare_item_by_item_where_neither_side_is_written_out(
    cypher_pm: PersistenceManager,
):
    # Beyond the spec's cases (#116): parameters, values carried by a WITH and collected lists are compared as they are
    # at run time.
    given = {
        "a": [1, None],
        "b": [1, None],
        "c": [2, None],
        "l": [[1, None], [3]],
        "m": {"x": None},
        "n": {"x": 1},
        "p": [1, 2],
    }
    assert cypher_pm.query(spec("RETURN [$a = $b, $a = $c, $a IN [$b], $a IN $l, $m = $n, $p = $p]", given)) == [
        [None, False, None, None, None, True]
    ]
    assert cypher_pm.query(
        spec(
            "WITH [1, null] AS a, [1, null] AS b, [2, null] AS c, [null, 1] AS d "
            "RETURN [a = b, a = c, a <> c, a = d, a IN [b, c], a IN [c]]"
        )
    ) == [[None, False, True, None, None, False]]
    assert cypher_pm.query(
        spec(
            "WITH [[null], 1] AS a, [[null], 2] AS b, {k: [null]} AS m, {k: [1]} AS n, {k: [1], j: 2} AS o "
            "RETURN [a = b, m = n, m = m, m = o, n = n]"
        )
    ) == [[False, None, None, False, True]]
    # Walked by the store: an empty list or map, a list beside a map, and maps of one size with different keys.
    assert cypher_pm.query(
        spec(
            "WITH [] AS e, {} AS o, [null] AS a, {x: null} AS m, {x: 1} AS p, {x: null, y: 1} AS r, "
            "{x: null, z: 1} AS s, [{}, null] AS f, [[], null] AS g "
            "RETURN [e = a, a = e, o = m, a = m, m = a, m = p, r = s, f = f, g = g, [{}] = f, [[]] = g]"
        )
    ) == [[False, False, False, False, False, None, False, None, None, False, False]]
    assert cypher_pm.query(
        spec(
            "UNWIND [[1, null], [2, null]] AS l WITH collect(l) AS ls "
            "RETURN [ls = [[1, null], [2, null]], ls[0] = ls[1], ls[0] IN ls]"
        )
    ) == [[None, False, None]]
    assert cypher_pm.query(
        spec("UNWIND [[1, null], [2, null], [3]] AS l WITH l WHERE l = [1, null] OR l = [3] RETURN l")
    ) == [[3]]
    # A parameter's list compared item by item as the statement is compiled, at any depth.
    assert cypher_pm.query(
        spec(
            "UNWIND [[1, [2, 3]], [1, [2]], 5] AS l WITH collect(l) AS ls "
            "RETURN [ls[0] = $p, ls[1] = $p, ls[2] = $p, $p = ls[0], ls[0] <> $q, $q = ls[1]]",
            {"p": [None, [2, 3]], "q": [1, [None, 4]]},
        )
    ) == [[None, False, False, None, True, False]]


def test_a_parameter_that_holds_a_null_given_as_an_iterator_or_holding_nan_is_bound_as_it_was_compared(
    cypher_pm: PersistenceManager,
):
    # Whether a parameter holds a null is read from what it is bound as, so an iterator is read once (drivine#340), and
    # NaN, bound as a text, is a value that is not null.
    if cypher_pm.type is not DatabaseType.NEO4J:
        # The Neo4j driver takes no iterator.
        given = {"g": iter([1, None]), "h": iter([1, None]), "m": {"l": iter([1, None])}}
        assert cypher_pm.query(
            spec(
                "RETURN [$g = [1, null], $g IN [[1, null]], $m.l = [1, null], $m.l IN [[2, null]], $g, $h IN [$g]]",
                given,
            )
        ) == [[None, None, None, False, [1, None], None]]
    nan = {"p": [math.nan, None], "q": [math.nan]}
    assert cypher_pm.query(
        spec(
            "UNWIND [[1, null]] AS l WITH collect(l) AS ls "
            "RETURN [ls[0] = $p, $p IN [[1, null]], ls[0] IN [$p], [1] = $q, size($p)]",
            nan,
        )
    ) == [[False, False, False, False, 2]]


def test_a_text_that_holds_a_nulls_text_holds_no_null_and_a_value_past_4000_bytes_is_read_whole_for_one(
    cypher_pm: PersistenceManager,
):
    # Beyond the spec's cases (#116): Oracle reads whether a value holds a null from its JSON's text.
    assert (
        cypher_pm.query(
            spec(
                "UNWIND ['a,null]', 'x:null}', 'null', 'a\\\",null]', 'é😀\\\",null]', 'a\\\\'] AS s "
                "WITH s, [s] AS v, [[s]] AS l, {k: s} AS m, [{k: s}] AS ms RETURN [v IN l, v = [s], m IN ms]"
            )
        )
        == [[True, True, True]] * 6
    )
    # A text that ends in a backslash ends where its quote does.
    assert cypher_pm.query(
        spec("UNWIND ['a\\\\'] AS s WITH s, [s, ',null]'] AS v RETURN [v = [s, ',null]'], v IN [[s, ',null]']]]")
    ) == [[True, True]]
    long = "[x IN range(1, 600) | 'abcdefghij']"
    assert cypher_pm.query(spec(f"WITH {long} AS v, {long} AS w, [{long}] AS l RETURN [v = w, v IN l, v <> w]")) == [
        [True, True, False]
    ]
    # A long value that holds a null is read whole, so the null is found.
    assert cypher_pm.query(
        spec(
            f"WITH {long} + [null] AS v, {long} + [null] AS w, [{long} + [null]] AS l, ['z'] + {long} + [null] AS u "
            "RETURN [v = w, v IN l, v <> w, v = u]"
        )
    ) == [[None, None, None, False]]
    # Either side of 4,000 bytes of JSON's text, `["x…x",null]` and `["x…x","y"]`: the short text read, then the
    # long.
    for size in (3990, 3991, 3992):
        assert cypher_pm.query(
            spec(
                "WITH [$s, null] AS v, [$s, null] AS w, [$s, $m.k] AS u, [$s + 'z', null] AS d "
                "RETURN [v = w, u = u, v = u, v = d]",
                {"s": "x" * size, "m": {"k": "y"}},
            )
        ) == [[None, True, None, False]], size
    assert cypher_pm.query(
        spec(
            "WITH [1, null] AS a, [2, null] AS c RETURN reduce(acc = [], x IN [a, c] | acc + [coalesce(x = a, 'null')])"
        )
    ) == [["null", False]]
    assert cypher_pm.query(
        spec(
            "UNWIND ['a,null]'] AS s WITH s, [s] AS v, [[s]] AS l "
            "RETURN reduce(acc = [], x IN [v] | acc + [x = [s], x IN l])"
        )
    ) == [[True, True]]


def test_texts_past_32_kb_in_lists_that_hold_a_null_compare_as_texts(cypher_pm: PersistenceManager):
    # On Oracle a text past 32 KB is compared as a CLOB, which to_String raises on (ORA-40478) (drivine#340); and a
    # parameter's list with such a text is walked, not written in the statement item by item, past the literal Oracle
    # takes (ORA-01704).
    text = "x" * 40_000
    given = {"a": [text, None], "b": [text, None], "c": [text + "y", None]}
    assert cypher_pm.query(spec("RETURN [$a = $b, $a = $c, $a IN [$b], $a IN [$c]]", given)) == [
        [None, False, None, False]
    ]
    assert cypher_pm.query(spec("WITH $a AS a, $b AS b, $c AS c RETURN [a = b, a = c, {k: a} = {k: b}]", given)) == [
        [None, False, None]
    ]


def test_a_null_node_or_relationship_in_a_list_or_a_map_makes_its_comparison_null(cypher_pm: PersistenceManager):
    # Beyond the spec's cases (#116): a node or a relationship OPTIONAL MATCH finds none of is null in what holds it.
    seeded(cypher_pm)
    assert cypher_pm.query(
        spec(
            "OPTIONAL MATCH (x:Nope)-[r]->() RETURN [[r] = [r], {x: r} = {x: r}, [r] <> [r], r IN [r], [r] IN [[r]], "
            "x IN [x], [x] = [x], {x: x} = {x: x}, [r, 1] = [r, 2]]"
        )
    ) == [[None, None, None, None, None, None, None, None, False]]
    assert cypher_pm.query(
        spec(
            "MATCH (:Person {name: 'Keanu'})-[r1:KNOWS]->() OPTIONAL MATCH (:Person {name: 'Hugo'})-[r2:KNOWS]->() "
            "WITH r1, r2, [r1, r2] AS a, {x: r2} AS m "
            "RETURN [[r1, r2] = [r1, r2], {a: r1, b: r2} = {a: r1, b: r2}, a = a, m = m, a = [r1, r1], r1 IN [r2], "
            "a = [r1]]"
        )
    ) == [[None, None, None, None, None, None, False]]


def test_a_statement_that_writes_compares_a_list_that_holds_a_null_item_by_item(
    cypher_pm: PersistenceManager,
):
    # Beyond the spec's cases (#116); on Oracle a procedure calls no function of its own, so the store's walks them
    # (#188).
    clear(cypher_pm)
    assert cypher_pm.query(
        spec(
            "CREATE (n:CyNullEquality) WITH n, [1, null] AS a, [1, null] AS b, [2, null] AS c "
            "SET n.same = coalesce(a = b, 'null'), n.other = a = c, n.unequal = a <> c, "
            "n.written = coalesce([null] = [1], 'null') RETURN [n.same, n.other, n.unequal, n.written]"
        )
    ) == [["null", False, True, "null"]]
    # Values that hold no null compare as before, a text that holds a null's text and a long value among them.
    long = "[x IN range(1, 600) | 'abcdefghij']"
    assert cypher_pm.query(
        spec(
            f"UNWIND ['a,null]'] AS s WITH s, [s] AS v, [[s]] AS l, {long} AS w, [{long}] AS ws "
            f"CREATE (n:CyNullEquality) SET n.eq = v = [s], n.in = v IN l, n.longEq = w = {long}, n.longIn = w IN ws "
            "RETURN [n.eq, n.in, n.longEq, n.longIn]"
        )
    ) == [[True, True, True, True]]


def test_values_that_may_hold_a_null_are_null_where_written_alike_only_when_one_holds_one(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet (#270): on Oracle two written as the same JSON text are null with no walk, so the text must
    # be written alike only where one holds a null; any written otherwise are walked.
    clear(cypher_pm)
    assert cypher_pm.query(
        spec(
            "CREATE (n:CyWrite) WITH n, CASE WHEN n.x IS NULL THEN [1, 2] ELSE [null] END AS a, "
            "CASE WHEN n.x IS NULL THEN [1, null] ELSE [2] END AS c "
            "RETURN [a = [1, 2], c = [1, null], c = [1.0, null], {a: 1, b: c[1]} = {b: null, a: 1}, "
            "['é', c[1]] = ['\\u00e9', null], [[1, 2], c] = [[1, 2], [1, null]]]"
        )
    ) == [[True, None, None, None, None, None]]
    # And in a read, where the same text is null and any other is walked.
    assert cypher_pm.query(
        spec(
            "WITH [1, null] AS c RETURN [c = [1, null], c = [1.0, null], c = [2, null], "
            "{a: 1, b: null} = {b: null, a: 1}, ['é', null] = ['\\u00e9', null]]"
        )
    ) == [[None, None, False, None, None]]


# Texts compared and sorted by their code points, as Cypher has them, on every store: drivine4j's own tests of
# liberation-data/drivine4j#273, ported for #161, where the spec's cases of them do not hold the same.


def test_a_text_in_a_list_sorts_by_its_code_points_up_to_and_across_its_500th_character(
    cypher_pm: PersistenceManager,
):
    # Postgres keys a text in a list by the bits of its first 500 characters, then by the rest. 500 characters of four
    # bytes are 16,000 bits, near the 16,383 places Postgres holds; the 500th may be of 1 to 4 bytes.
    smile = "\U0001f600"
    s499, s500 = smile * 499, smile * 500
    texts = [s499, s499 + "a", s499 + "～", s500, s500 + "a", s500 + "b"]
    written = ", ".join(f"['{it}']" for it in reversed(texts))

    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    assert rows(f"UNWIND [{written}] AS l RETURN l ORDER BY l") == [[it] for it in texts]
    assert rows(f"UNWIND [{written}] AS l RETURN l ORDER BY l DESC") == [[it] for it in reversed(texts)]
    a499, a500 = "a" * 499, "a" * 500
    assert rows(
        f"UNWIND [['{a500}B'], ['{a500}'], ['{a499}é'], ['{a499}b'], ['{a499}B']] AS l RETURN l ORDER BY l"
    ) == [[a499 + "B"], [a500], [a500 + "B"], [a499 + "b"], [a499 + "é"]]
    # Postgres keys an empty text, which has no bits, as 0: before every other. Oracle reads an empty text as null.
    if cypher_pm.type is not DatabaseType.ORACLE:
        assert rows("UNWIND [['b'], ['', 'a'], ['B'], ['']] AS l RETURN l ORDER BY l") == [
            [""],
            ["", "a"],
            ["B"],
            ["b"],
        ]
    # Long texts given: Postgres keys the first 500 characters of a text in a list by their code points.
    a600 = "a" * 600
    assert rows(
        "UNWIND $ls AS g WITH [g[0], g[1]] AS l RETURN l[1] ORDER BY l",
        {"ls": [[a499 + "b", "3"], [a499 + "B", "2"], [a499, "1"], [a600 + "b", "5"], [a600, "4"]]},
    ) == ["1", "2", "4", "5", "3"]


# Values of several kinds sorted as Cypher orders their kinds, a map, a list, a text, a truth value, a number, then
# null: drivine4j's tests of liberation-data/drivine4j#299, and Drivine for TypeScript's port of them
# (liberation-data/drivine#371), ported for #175, beside the spec's cases since v0.6.16 (#299). Postgres takes
# `jsonb`'s order for every kind but a text: a number before a truth value, a map and a list last
# (liberation-data/drivine4j#307).

KIND_ORDER_ON_POSTGRES = "Postgres takes jsonb's order of kinds (liberation-data/drivine4j#307)."


def typed(value: Any) -> Any:
    """The value in a form that compares by kind as well: true is not 1, and 2 is not 2.0."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, dict):
        return {key: typed(it) for key, it in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [typed(it) for it in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def test_values_of_several_kinds_sort_as_cypher_orders_their_kinds_a_map_a_list_a_text_a_truth_value_a_number(
    cypher_pm: PersistenceManager,
):
    # Beside the spec's case since v0.6.16 (#299): Oracle sorts first by where a value's kind comes, then by the value.
    if cypher_pm.type is DatabaseType.POSTGRES:
        pytest.skip(KIND_ORDER_ON_POSTGRES)

    def rows(cypher: str) -> Any:
        return typed(cypher_pm.query(spec(cypher)))

    values = "[1, {a: 1}, 'b', true, [2], 2.5, null, false, 'A', [1, 2], -1]"
    ascending: list[Any] = [{"a": 1}, [1, 2], [2], "A", "b", False, True, -1, 1, 2.5, None]
    # Carried out of a CALL { … }, grouped by, kept distinct by another name, and sorted by when it is not returned.
    assert rows(f"CALL {{ UNWIND {values} AS t RETURN t ORDER BY t LIMIT 4 }} RETURN t") == typed(
        [{"a": 1}, [1, 2], [2], "A"]
    )
    assert rows("UNWIND [2, 'b', true, 2, 'b', [1]] AS t RETURN t, count(*) ORDER BY t") == typed(
        [[[1], 1], ["b", 2], [True, 1], [2, 2]]
    )
    assert rows("UNWIND [2, 'b', true, 2] AS t RETURN DISTINCT t AS u ORDER BY u DESC") == typed([2, True, "b"])
    assert rows("UNWIND [2, 'b', true] AS t RETURN toString(t) AS s ORDER BY t") == ["b", "true", "2"]
    # Among their own kind: texts by their code points, and whole and floating-point numbers as numbers.
    assert rows("UNWIND ['😀', '～', 1, 'a'] AS t RETURN t ORDER BY t") == typed(["a", "～", "😀", 1])
    assert rows("UNWIND [1, 0.5, true, 1.5, 0] AS t RETURN t ORDER BY t") == typed([True, 0, 0.5, 1, 1.5])
    # Paged by the query that returns them, and nulls first descending, carried out of a CALL { … }.
    assert rows(f"UNWIND {values} AS t RETURN t ORDER BY t SKIP 2 LIMIT 5") == typed([[2], "A", "b", False, True])
    assert rows(f"UNWIND {values} AS t RETURN t ORDER BY t DESC SKIP 1 LIMIT 4") == typed([2.5, 1, -1, True])
    assert rows(
        "CALL { UNWIND [1, null, 'a', true, null, [1]] AS t RETURN t ORDER BY t DESC LIMIT 3 } RETURN t"
    ) == typed([None, None, 1])
    # Collected in the order a WITH gives them, and indexed or iterated there.
    assert rows(f"UNWIND {values} AS t WITH t ORDER BY t DESC WITH collect(t) AS xs RETURN xs") == typed(
        [ascending[:-1][::-1]]
    )
    assert rows(f"UNWIND {values} AS t WITH t ORDER BY t RETURN [x IN collect(t) | x][0..3]") == typed([ascending[:3]])
    assert rows(f"UNWIND {values} AS t WITH t ORDER BY t SKIP 2 LIMIT 5 RETURN collect(t)") == typed(
        [[[2], "A", "b", False, True]]
    )
    # The last query of a UNION sorted, and a UNION carried out of a CALL { … } sorted and paged.
    assert rows(
        "UNWIND [2, 'b', true] AS t RETURN t UNION UNWIND [[1], {a: 1}, null, 'a'] AS t RETURN t ORDER BY t"
    ) == typed([2, "b", True, {"a": 1}, [1], "a", None])
    assert rows(
        "CALL { UNWIND [2, 'b'] AS t RETURN t UNION UNWIND [true, [1], null] AS t RETURN t } RETURN t ORDER BY t"
    ) == typed([[1], "b", True, 2, None])
    assert rows(
        "CALL { UNWIND [2, 'b'] AS t RETURN t UNION UNWIND [true, [1], null] AS t RETURN t } "
        "WITH t ORDER BY t DESC SKIP 1 LIMIT 3 RETURN t"
    ) == typed([2, True, "b"])


def test_a_property_stored_as_several_kinds_sorts_as_cypher_orders_their_kinds(cypher_pm: PersistenceManager):
    # Beside the spec's case since v0.6.16 (#299): a property read from the store may be of any kind, so its kind is
    # ranked first.
    if cypher_pm.type is DatabaseType.POSTGRES:
        pytest.skip(KIND_ORDER_ON_POSTGRES)

    def rows(cypher: str) -> Any:
        return typed(cypher_pm.query(spec(cypher)))

    clear(cypher_pm)
    cypher_pm.query(
        spec(
            "CREATE (:Kind {v: 2}), (:Kind {v: 'b'}), (:Kind {v: true}), (:Kind {v: [1]}), (:Kind {v: 1.5}), "
            "(:Kind {v: 'B'}), (:Kind {v: false}), (:Kind)"
        )
    )
    ascending: list[Any] = [[1], "B", "b", False, True, 1.5, 2, None]
    assert rows("CALL { MATCH (k:Kind) RETURN k.v AS v ORDER BY v DESC LIMIT 2 } RETURN v") == typed([None, 2])
    assert rows("MATCH (k:Kind) WITH k ORDER BY k.v WITH collect(k.v) AS vs RETURN vs") == typed([ascending[:-1]])
    assert rows("MATCH (k:Kind) RETURN k.v ORDER BY k.v DESC SKIP 1 LIMIT 3") == typed([2, 1.5, True])


def test_lists_of_several_kinds_of_item_nested_sort_item_by_item_among_values_of_other_kinds(
    cypher_pm: PersistenceManager,
):
    # Not in the spec yet (#299): a list sorts first among values of other kinds, and among lists item by item, the
    # items of each as Cypher orders their kinds at any depth.
    if cypher_pm.type is DatabaseType.POSTGRES:
        pytest.skip(KIND_ORDER_ON_POSTGRES)
    if cypher_pm.type is DatabaseType.ORACLE:
        pytest.skip(
            "Oracle puts a number before a text and a null first within a list (liberation-data/drivine4j#205)."
        )

    def rows(cypher: str) -> Any:
        return typed(cypher_pm.query(spec(cypher)))

    values = "[[1, 'a'], 'x', [[2], 1], [[2], 'b'], [1, null], 3, [1], [[2], [1]], [{a: 1}], [true]]"
    ascending: list[Any] = [
        [{"a": 1}],
        [[2], [1]],
        [[2], "b"],
        [[2], 1],
        [True],
        [1],
        [1, "a"],
        [1, None],
        "x",
        3,
    ]
    assert rows(f"UNWIND {values} AS t RETURN t ORDER BY t") == typed(ascending)
    assert rows(f"UNWIND {values} AS t RETURN t ORDER BY t DESC") == typed(ascending[::-1])
    assert rows(f"CALL {{ UNWIND {values} AS t RETURN t ORDER BY t LIMIT 4 }} RETURN t") == typed(ascending[:4])


# Texts past 4,000 bytes measured, cut, changed, searched and put together whole, as short ones are: drivine4j's tests
# of liberation-data/drivine4j#298, ported for #168. Oracle read a text from JSON as a VARCHAR2, which holds 4,000
# bytes, and a longer one as null. Beside the spec's cases since v0.6.17 (#298): stored and made by the statement too,
# and more functions and edge cases.


def long_text_functions(t: str, unit: str) -> dict[str, Any]:
    """Each function of the text `t`, a space, `unit` over and over and a space, with what it gives: one a statement."""
    first, last = unit[0], unit[-1]
    return {
        "size(t)": len(t),
        "toUpper(t)": t.upper(),
        "toLower(t)": t.lower(),
        "trim(t)": t.strip(),
        "ltrim(t)": t.lstrip(),
        "rtrim(t)": t.rstrip(),
        "substring(t, 1, size(t) - 2)": t[1:-1],
        "substring(t, 2)": t[2:],
        "left(t, size(t) - 1)": t[:-1],
        "right(t, size(t) - 1)": t[1:],
        f"replace(t, '{last}', '{last}{last.lower()}')": t.replace(last, last + last.lower()),
        f"split(t, '{last}')": t.split(last),
        # A separator past 4,000 bytes, too, but within 32 KB, past which Oracle's REPLACE looks for none.
        "split('x' + t + 'y', left(t, 4100))": ("x" + t + "y").split(t[:4100]),
        "split('x', left(t, 4100))": ["x"],
        f"t STARTS WITH ' {unit}'": True,
        "t STARTS WITH 'x'": False,
        "t STARTS WITH t": True,
        f"t ENDS WITH '{last} '": True,
        "t ENDS WITH t": True,
        "left(t, 10) STARTS WITH t": False,
        "left(t, 10) ENDS WITH t": False,
        # INSTR looks for no text past 32 KB on Oracle.
        f"t CONTAINS '{last}{first}{last}'": True,
        "t CONTAINS 'z'": False,
        "t CONTAINS left(t, 4100)": True,
        f"t =~ ' ({unit})+ '": True,
        "t + 'z'": t + "z",
        "'z' + t": "z" + t,
        "t + t": t + t,
        "t + 1": t + "1",
        "toString(t)": t,
        "isEmpty(t)": False,
    }


@pytest.mark.parametrize(
    ("unit", "length"),
    # Two-byte characters, so a text of fewer than 4,000 characters is past 4,000 bytes. Past 32 KB too, given, stored
    # and made: Oracle closed the session (ORA-03113) after statements that read such a text from a property and from a
    # parameter in turn, and its sessions held megabytes for each compiled statement the driver kept, up to the
    # instance's limit (ORA-04036, liberation-data/drivine4j#313, #218).
    [("aB", 5_000), ("éB", 3_000), ("aB", 40_000)],
)
def test_a_text_past_4000_bytes_is_measured_cut_changed_searched_and_added_to_whole_as_a_short_one_is(
    cypher_pm: PersistenceManager, unit: str, length: int
):
    clear(cypher_pm)
    t = " " + unit * ((length - 2) // 2) + " "
    assert len(t.encode()) > 4_000

    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    rows("CREATE (n:CyLongText {t: $t})", {"t": t})
    parts = {"a": t[: length // 2], "b": t[length // 2 :]}
    for function, expected in long_text_functions(t, unit).items():
        # Given, and stored and read in one row, and made by the statement.
        assert rows(f"WITH $t AS t RETURN {function}", {"t": t}) == [expected], f"{function} given"
        assert rows(f"MATCH (n:CyLongText) WITH n.t AS t RETURN {function}") == [expected], f"{function} stored"
        assert rows(f"WITH $a + $b AS t RETURN {function}", parts) == [expected], f"{function} made"
    clear(cypher_pm)


def test_texts_either_side_of_4000_bytes_are_measured_searched_and_put_together_whole(
    cypher_pm: PersistenceManager,
):
    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    # 3,999, 4,000 and 4,001 bytes, of two-byte characters and one of a byte.
    for t in ("a" + "é" * 1_999, "é" * 2_000, "a" + "é" * 2_000):
        searched = (
            "WITH $t AS t RETURN [size(t), toUpper(t), substring(t, 1), t STARTS WITH left(t, 3), "
            "t ENDS WITH right(t, 3), t CONTAINS right(t, 2), t STARTS WITH 'z', t + 'z', size(split(t, 'é'))]"
        )
        assert rows(searched, {"t": t}) == [
            [len(t), t.upper(), t[1:], True, True, True, False, t + "z", len(t.split("é"))]
        ], len(t.encode())
    # Two short texts whose sum is past 4,000 bytes.
    assert rows("WITH $a AS a RETURN size(a + a)", {"a": "é" * 3_000}) == [6_000]
    # Sums at and around 4,000 bytes, of texts of two-byte characters and of a float, put together short on Oracle only
    # where they fit.
    sides: list[tuple[str, Any]] = [
        ("é" * 1_000, "é" * 1_000),
        ("a" * 1_968, "a" * 1_968),
        ("a" * 1_969, "a" * 1_968),
        ("a" * 2_000, "a" * 2_001),
        ("a" * 3_999, 1.5),
    ]
    for a, b in sides:
        assert rows("WITH $a AS a, $b AS b RETURN a + b", {"a": a, "b": b}) == [f"{a}{b}"], f"{len(a)} + {b}"
    # What to replace, and what with, made by the statement, so their lengths are measured for each row: a short text
    # made past 4,000 bytes, made shorter, and made no longer.
    assert rows(
        "WITH $a AS a RETURN [replace(a, left(a, 1), left(a, 2)), replace(a, left(a, 2), left(a, 1)), "
        "replace(a, left(a, 1), right(a, 1))]",
        {"a": "é" * 1_500},
    ) == [["é" * 3_000, "é" * 750, "é" * 1_500]]
    # Given what to replace and what with: a short text made past 4,000 bytes.
    assert rows("WITH $a AS a RETURN size(replace(a, 'é', 'éé'))", {"a": "é" * 1_500}) == [3_000]
    # A null text, and a function of a short text that gives Oracle's empty text, null, are not read again as long ones
    # to give another value.
    for function in (
        "toUpper(n)",
        "trim(n)",
        "substring(n, 1)",
        "replace(n, 'a', 'b')",
        "split(n, ',')",
        "size(n)",
        "toString(n)",
        "n + 'a'",
        "n STARTS WITH 'a'",
        "n ENDS WITH 'a'",
        "n CONTAINS 'a'",
        "n =~ 'a'",
    ):
        assert rows(f"WITH $n AS n RETURN {function}", {"n": None}) == [None], function
    empty = None if cypher_pm.type is DatabaseType.ORACLE else ""
    assert rows(
        "WITH $s AS s, $w AS w RETURN [substring(s, 5), left(s, 0), replace(s, 'abc', ''), trim(w)]",
        {"s": "abc", "w": "   "},
    ) == [[empty, empty, empty, empty]]
    # Two empty texts put together are empty: on Oracle null, as its empty text is, and not read as a long one.
    assert rows("WITH $e AS e RETURN e + e", {"e": ""}) == [empty]


def test_a_stored_text_past_32_kb_read_in_one_row_is_measured_and_changed_whole_passed_on_or_read_where_used(
    cypher_pm: PersistenceManager,
):
    # On Oracle a WITH passes a property on read through its text (#144), and a function reads a text past 4,000 bytes
    # as a CLOB (#168): the two together. One node: Oracle crashes on a long property read from two rows (#144).
    clear(cypher_pm)
    t = "aB" * 20_000
    cypher_pm.query(spec("CREATE (n:CyLongText {t: $t})", {"t": t}))
    functions: dict[str, Any] = {
        "size(#)": 40_000,
        "toUpper(#)": t.upper(),
        "substring(#, 2)": t[2:],
        "replace(#, 'B', 'b')": t.lower(),
        "# ENDS WITH 'aB'": True,
        "size(# + 'z')": 40_001,
        "toString(#)": t,
    }
    for function, expected in functions.items():
        passed = f"MATCH (n:CyLongText) WITH n.t AS t RETURN {function.replace('#', 't')}"
        assert cypher_pm.query(spec(passed)) == [expected], f"{function}, passed on"
        used = f"MATCH (n:CyLongText) RETURN {function.replace('#', 'n.t')}"
        assert cypher_pm.query(spec(used)) == [expected], f"{function}, read where used"
    clear(cypher_pm)


# Lists and maps written longer than Postgres takes arguments to a function, 100: drivine4j's own tests of
# liberation-data/drivine4j#332, ported for #192. Not in the spec yet.


def _long_item(i: int) -> tuple[str, Any]:
    """The Cypher of the `i`th item of a long literal, and what it gives: of seven kinds in turn, a null among them."""
    match i % 7:
        case 0:
            return f"{i}", i
        case 1:
            return f"'s{i}'", f"s{i}"
        case 2:
            return "null", None
        case 3:
            return f"{i}.5", i + 0.5
        case 4:
            return ("true", True) if i % 2 == 0 else ("false", False)
        case 5:
            return f"[{i}, 'n']", [i, "n"]
        case _:
            return f"{{k: {i}}}", {"k": i}


def test_a_list_literal_of_more_than_100_items_keeps_its_items_returned_tested_with_in_unwound_and_comprehended(
    cypher_pm: PersistenceManager,
):
    for n in (100, 101, 250, 1001):
        # An Oracle session spends 180 MB of its PGA on a statement of a list of 1001, 815 MB on one that reads it three
        # times, and keeps 50 to 217 MB after (#241). Late in the suite, beside its other sessions, that passes Oracle
        # Free's limit (ORA-04036), so Oracle isn't asked a list of 1001; drivine4j asks it all but the comparisons
        # below (liberation-data/drivine4j#360, here #227).
        if n > 250 and cypher_pm.type is DatabaseType.ORACLE:
            continue
        items = [_long_item(i) for i in range(n)]
        listed = "[" + ", ".join(it[0] for it in items) + "]"
        expected = [it[1] for it in items]
        last_whole = max(i for i in range(n) if i % 7 == 0)
        assert cypher_pm.query(spec(f"RETURN {listed}")) == [expected], f"{n} items"
        sized = f"WITH {listed} AS xs RETURN [size(xs), xs[-1], xs[{n - 2}]]"
        assert cypher_pm.query(spec(sized)) == [[n, expected[-1], expected[n - 2]]], f"{n} items"
        assert cypher_pm.query(spec(f"UNWIND {listed} AS x RETURN x")) == expected, f"{n} items"
        kept = f"RETURN [x IN {listed} WHERE x IS NOT NULL | x]"
        assert cypher_pm.query(spec(kept)) == [[it for it in expected if it is not None]], f"{n} items"
        assert cypher_pm.query(spec(f"RETURN size([x IN {listed} | x IS NULL])")) == [n], f"{n} items"
        # A null among the items makes a value not found null, not false, as in Cypher. A list of numbers and nulls is
        # tested whole, as JSON.
        numbers = "[" + ", ".join("null" if i % 7 == 2 else f"{i}" for i in range(n)) + "]"
        found = f"RETURN [{last_whole} IN {numbers}, 999999 IN {numbers}]"
        assert cypher_pm.query(spec(found)) == [[True, None]], f"{n} items"
        # A list of several kinds is compared item by item, in a few hundred characters of SQL each: Oracle runs out of
        # memory reading the comparisons of 1001 even alone (#227). (Not -1, which compiles to ten times the statement.)
        found = f"RETURN [{last_whole} IN {listed}, 999999 IN {listed}]"
        assert cypher_pm.query(spec(found)) == [[True, None]], f"{n} items"
        # Compared with itself item by item, each pair equal but the nulls, which are null beside each other.
        assert cypher_pm.query(spec(f"RETURN {listed} = {listed}")) == [None], f"{n} items"


def test_a_long_list_or_map_literal_without_a_null_equals_the_same_value_given_as_a_parameter_or_collected(
    cypher_pm: PersistenceManager,
):
    # Text holding the brackets and commas a long literal is spliced at on Postgres.
    def item(i: int) -> tuple[str, Any]:
        match i % 4:
            case 0:
                return f"{i}", i
            case 1:
                return f"'a], [b}}, {{{i}'", f"a], [b}}, {{{i}"
            case 2:
                return f"[{i}, [{i}]]", [i, [i]]
            case _:
                return f"{{k: [{i}], m: {{n: {i}}}}}", {"k": [i], "m": {"n": i}}

    for n in (101, 200, 201):
        items = [item(i) for i in range(n)]
        listed = "[" + ", ".join(it[0] for it in items) + "]"
        expected = [it[1] for it in items]
        assert cypher_pm.query(spec(f"RETURN {listed}")) == [expected], f"{n} items"
        assert cypher_pm.query(spec(f"RETURN {listed} = $p", {"p": expected})) == [True], f"{n} items"
        collected = f"UNWIND {listed} AS x WITH collect(x) AS xs RETURN xs = {listed}"
        assert cypher_pm.query(spec(collected)) == [True], f"{n} items"
        mapped = "{" + ", ".join(f"k{i}: {it[0]}" for i, it in enumerate(items)) + "}"
        expected_map = {f"k{i}": it[1] for i, it in enumerate(items)}
        returned = cypher_pm.get_one(spec(f"RETURN {mapped}"))
        assert returned == expected_map, f"{n} entries"
        assert cypher_pm.query(spec(f"RETURN {mapped} = $p", {"p": expected_map})) == [True], f"{n} entries"
        # Each written in parts within a list and a map that are written in one.
        nested = [expected, {"m": expected_map}]
        returned_nested = cypher_pm.get_one(spec(f"RETURN [{listed}, {{m: {mapped}}}]"))
        assert returned_nested == nested, f"{n} nested"
        # Its keys in the order written, alone and within a map, on Postgres as in the next test: dicts are equal
        # whatever the order of their keys.
        if cypher_pm.type is DatabaseType.POSTGRES:
            assert list(returned) == list(expected_map), f"{n} entries"
            assert list(returned_nested[1]["m"]) == list(expected_map), f"{n} nested"
        assert cypher_pm.query(spec(f"RETURN [{listed}, {{m: {mapped}}}] = $p", {"p": nested})) == [True], f"{n} nested"


def test_a_map_literal_of_more_than_50_entries_keeps_its_entries_in_order_returned_read_and_projected(
    cypher_pm: PersistenceManager,
):
    for n in (50, 51, 120):
        entries = [(f"k{i}", _long_item(i)) for i in range(n)]
        written = ", ".join(f"{key}: {cypher}" for key, (cypher, _) in entries)
        mapped = "{" + written + "}"
        expected = {key: value for key, (_, value) in entries}
        returned = cypher_pm.get_one(spec(f"RETURN {mapped}"))
        assert returned == expected, f"{n} entries"
        # Its keys in the order written, where the store keeps them so: Neo4j and Oracle do for a few keys, not for 50.
        if cypher_pm.type is DatabaseType.POSTGRES:
            assert list(returned) == list(expected), f"{n} entries"
        read = f"WITH {mapped} AS m RETURN [size(keys(m)), m.k{n - 1}, m['k{n - 2}']]"
        assert cypher_pm.query(spec(read)) == [[n, expected[f"k{n - 1}"], expected[f"k{n - 2}"]]], f"{n} entries"
        assert cypher_pm.query(spec(f"RETURN {mapped} = {mapped}")) == [None], f"{n} entries"
        # Set over a map by a projection.
        projected = f"WITH {{a: 1}} AS m RETURN m {{.*, {written}}}"
        assert cypher_pm.query(spec(projected)) == [{"a": 1, **expected}], f"{n} entries"


def test_a_map_literal_of_121_entries_holding_a_list_of_250_items_is_written_by_create_merge_set_and_unwind(
    cypher_pm: PersistenceManager,
):
    # What a write makes of a long literal, read back: each written in parts on Postgres, as a read is (#192).
    clear(cypher_pm)
    listed = "[" + ", ".join(f"'s{i}'" for i in range(250)) + "]"
    expected_list = [f"s{i}" for i in range(250)]
    # Values a store keeps as properties, of four kinds in turn: no null, no list of several kinds, no map.
    values = [_long_item(i) for i in range(210) if i % 7 in (0, 1, 3, 4)]
    mapped = "{" + ", ".join(f"k{i}: {cypher}" for i, (cypher, _) in enumerate(values)) + f", xs: {listed}}}"
    expected = {**{f"k{i}": value for i, (_, value) in enumerate(values)}, "xs": expected_list}
    assert len(expected) == 121

    def written(label: str) -> list[Any]:
        return cypher_pm.query(spec(f"MATCH (n:{label}) RETURN properties(n)"))

    cypher_pm.query(spec(f"CREATE (n:CyLongCreate {mapped})"))
    assert written("CyLongCreate") == [expected]
    for _ in range(2):
        cypher_pm.query(spec(f"MERGE (n:CyLongMerge {mapped})"))
    assert written("CyLongMerge") == [expected], "made once, then matched"
    cypher_pm.query(spec(f"CREATE (n:CyLongAdd {{id: 1}}) WITH n SET n += {mapped}"))
    assert written("CyLongAdd") == [{"id": 1, **expected}]
    cypher_pm.query(spec(f"CREATE (n:CyLongSet {{id: 1}}) WITH n SET n = {mapped}"))
    assert written("CyLongSet") == [expected]
    cypher_pm.query(spec(f"CREATE (:CyLongFrom)-[:CY_LONG {mapped}]->(:CyLongTo)"))
    assert cypher_pm.query(spec("MATCH ()-[r:CY_LONG]->() RETURN properties(r)")) == [expected]
    cypher_pm.query(spec(f"UNWIND {listed} AS x CREATE (:CyLongUnwound {{x: x}})"))
    unwound = cypher_pm.query(spec("MATCH (n:CyLongUnwound) RETURN n.x"))
    assert sorted(unwound) == sorted(expected_list)
    clear(cypher_pm)


def test_two_lists_are_ordered_item_by_item_at_any_depth_and_null_where_a_pair_not_ordered_decides(
    cypher_pm: PersistenceManager,
):
    # Beside the spec's cases since v0.6.19: drivine4j's tests of liberation-data/drivine4j#349, ported for #215.
    def row(cypher: str) -> Any:
        return cypher_pm.get_one(spec(cypher))

    # A list that starts the other comes first, at any depth.
    assert row("RETURN [[[]] < [[], 1], [[]] <= [[]], [[], 1] > [[]]]") == [True, True, True]
    # An empty list before one of durations; a null beside a temporal value is not ordered, as on Neo4j.
    assert row(
        "RETURN [[] < [duration('P1D')], [date('2024-01-01'), null] < [date('2024-01-01'), date('2024-01-02')], "
        "[date('2024-01-01'), null] < [date('2024-01-02'), null], "
        "[localdatetime('2024-01-01T10:00'), null] <= [localdatetime('2024-01-01T10:00')], "
        "[datetime('2024-01-01T10:00+02:00'), null] < [datetime('2024-01-01T09:00Z'), null]]"
    ) == [True, None, True, False, True]
    # Lists of lists of temporal values: zoned ones as their moments, and two kinds not ordered.
    assert row(
        "RETURN [[[datetime('2024-01-01T10:00+02:00')]] < [[datetime('2024-01-01T09:00Z')]], "
        "[[date('2024-01-01')]] < [[date('2024-01-01')], []], "
        "[[date('2024-01-01')]] < [[localdatetime('2024-01-02T00:00')]]]"
    ) == [True, True, None]
    # Whole numbers past 2^53, as they are.
    assert row("RETURN [[9007199254740993] > [9007199254740992], [9007199254740992] < [9007199254740993]]") == [
        True,
        True,
    ]


def test_two_lists_are_ordered_in_an_aggregates_argument_beside_a_walk_against_a_long_literal_and_out_of_a_map(
    cypher_pm: PersistenceManager,
):
    # Beside #359, which works an aggregate's argument out first, #362's literals written in parts, and #381's temporal
    # values of an unwound map: on Oracle, the query declares its function once, and calls it.
    clear(cypher_pm)
    cypher_pm.query(spec("CREATE (a:L {m: 1, l: [3]})-[:NEXT]->(b:L {m: 2, l: [1]})-[:NEXT]->(:L {m: 3, l: [1, 5]})"))
    assert cypher_pm.query(spec("MATCH (n:L) RETURN count(CASE WHEN n.l < [2] THEN 1 END)")) == [2]
    assert cypher_pm.query(
        spec(
            "MATCH (a:L)-[:NEXT*1..2]->(b:L) RETURN a.m AS a, count(*) AS c "
            "ORDER BY sum(CASE WHEN b.l < [2] THEN 1 ELSE 0 END)"
        )
    ) == [[2, 1], [1, 2]]
    long = "[" + ", ".join(str(it) for it in range(1, 151)) + "]"
    assert cypher_pm.query(spec(f"UNWIND [[1, 2], [200]] AS l RETURN l < {long}")) == [True, False]
    unwound = (
        "UNWIND [0, 1] AS k WITH k, [{l: [i IN range(1, 3) | localdatetime('2024-03-15T10:00:00') "
        "+ duration({minutes: i + k})], z: [i IN range(1, 3) | datetime('2024-03-15T10:00:00+02:00') "
        "+ duration({minutes: i + k})]}] AS ms UNWIND ms AS t WITH k, t.l AS l, t.z AS z"
    )
    # A zoned value read out of a map compares as its text on the SQL stores, as it sorts, so the one beside it keeps
    # its offset.
    assert cypher_pm.query(
        spec(
            f"{unwound} RETURN [k, l < [localdatetime('2024-03-15T10:02:00')], "
            "z < [datetime('2024-03-15T10:01:30+02:00')]] ORDER BY k"
        )
    ) == [[0, True, True], [1, False, False]]
    clear(cypher_pm)


REVERSED_TEXTS = [
    # 5,000 characters, and past 32 KB.
    " " + "aB" * 2_499 + "é",
    "aB" * 20_000,
    # Multibyte, under 4,000 characters and past 4,000 bytes; and at 4,000 bytes, short, and one byte past.
    "é中" * 1_999 + "x",
    "é" * 2_000,
    "é" * 2_000 + "x",
    # Characters past U+FFFF, two UTF-16 units each: across each 1,000th unit, where Oracle cuts a long text, and
    # reversed, across each other unit; then the other way about.
    "x" + "😀" * 1_500 + "aé" + "😀" * 1_000,
    "😀" * 5_000 + "x",
    # One that ends on a character across the 2,000th unit, so the last piece Oracle cuts is empty; and a piece of the
    # most bytes, 999 three-byte characters and one of four.
    "x" + "😀" * 1_000,
    "中" * 999 + "😀" + "中" * 1_000,
    # What JSON writes otherwise: quotes, a backslash, a line break and a control character.
    'a"","x\\\n\u0001' * 800,
    "abc",
    "a😀é中",
]


@pytest.mark.parametrize("t", REVERSED_TEXTS, ids=lambda t: f"{len(t)} characters, {len(t.encode())} bytes")
def test_a_text_past_4000_bytes_is_reversed_whole_as_a_short_one_is(cypher_pm: PersistenceManager, t: str):
    # Oracle reversed a text with LISTAGG, which gives a VARCHAR2 of 4,000 bytes, and read a longer one from JSON as
    # null (#182). Given, stored and read in one row, and made by the statement, as above; reversed by its characters,
    # so a character past U+FFFF, two UTF-16 units, stays whole. drivine4j's test of liberation-data/drivine4j#311.
    clear(cypher_pm)

    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    rows("CREATE (n:CyLongText {t: $t})", {"t": t})
    parts = {"a": t[: len(t) // 2], "b": t[len(t) // 2 :]}
    reversed_ = t[::-1]
    assert rows("WITH $t AS t RETURN reverse(t)", {"t": t}) == [reversed_], "given"
    assert rows("MATCH (n:CyLongText) RETURN reverse(n.t)") == [reversed_], "stored"
    assert rows("MATCH (n:CyLongText) WITH n.t AS t RETURN reverse(t)") == [reversed_], "passed on"
    assert rows("MATCH (n:CyLongText) RETURN collect(reverse(n.t))") == [[reversed_]], "collected"
    # Not where a sum on Oracle puts a character past U+FFFF across its 4,000th UTF-16 unit: the sum gives two U+FFFD
    # there (#238).
    if cypher_pm.type is not DatabaseType.ORACLE or all(ord(it) <= 0xFFFF for it in t):
        assert rows("WITH $a + $b AS t RETURN reverse(t)", parts) == [reversed_], "made"
    assert rows("WITH $t AS t RETURN reverse(reverse(t))", {"t": t}) == [t], "twice"
    # In a statement that writes, which Oracle runs as a PL/SQL block.
    assert rows("MATCH (n:CyLongText) SET n.r = reverse(n.t) RETURN n.r") == [reversed_], "written"
    clear(cypher_pm)


def test_texts_long_and_short_are_reversed_each_from_its_own_and_a_null_empty_or_written_one_as_cypher_gives(
    cypher_pm: PersistenceManager,
):
    def rows(cypher: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        return cypher_pm.query(spec(cypher, parameters))

    # Several in one statement, each row and each item reversed from its own text, long and short.
    texts = ["aB" * 3_000, "abc", "x" + "😀" * 1_500, "é中" * 1_999 + "x"]
    reversed_ = [it[::-1] for it in texts]
    assert rows("UNWIND $ts AS t RETURN reverse(t)", {"ts": texts}) == reversed_
    assert rows("RETURN [t IN $ts | reverse(t)]", {"ts": texts}) == [reversed_]
    # A null text is null, and an empty one empty: on Oracle null, as its empty text is, and not read again as a long
    # one.
    assert rows("WITH $n AS n RETURN reverse(n)", {"n": None}) == [None]
    empty = None if cypher_pm.type is DatabaseType.ORACLE else ""
    assert rows("WITH $e AS e RETURN reverse(e)", {"e": ""}) == [empty]
    # A literal, a text or a list, whose CLOB Oracle bound without a type, and found no DBMS_LOB.SUBSTR for (PLS-307).
    assert rows("RETURN [reverse('Keanu'), reverse([1, 2, 3])]") == [["unaeK", [3, 2, 1]]]


def test_properties_of_a_node_beside_another_nodes_table_give_the_rows_of_the_pattern_and_that_nodes_values(
    cypher_pm: PersistenceManager,
):
    # Oracle's JSON table transformation lost a node's label test where the query read two of its properties, or one two
    # ways, beside another node's table, and gave a row for each two nodes (#258, liberation-data/drivine4j#415): see
    # OracleCypherCompiler.planned. drivine4j's CypherQueryContract.
    def rows(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    clear(cypher_pm)
    rows(
        "CREATE (a:N1 {n: 1, m: 10, s: 'x', l: [1]}) CREATE (b:N2 {n: 2, m: 20, s: 'y', l: [2]}) "
        "CREATE (c:N3 {n: 3, m: 30, s: 'z', l: [3]}) CREATE (a)-[:R]->(b), (a)-[:R]->(c)"
    )
    # A row of two columns or more comes back as a list of its values.
    cases: list[tuple[str, list[Any]]] = [
        ("MATCH (a:N1), (b:N2) RETURN a.n + b.n AS sum, a.n - b.n AS diff", [[3, -1]]),
        ("MATCH (a:N1), (b:N2) RETURN a.n + b.n, a.n * b.n", [[3, 2]]),
        ("MATCH (a:N1) MATCH (b:N2) RETURN a.n + b.n, a.n - b.n", [[3, -1]]),
        ("MATCH (a:N1), (b:N2) RETURN a.n + b.n, a.n < b.n, a.n = b.n", [[3, True, False]]),
        ("MATCH (a:N1), (b:N2) RETURN CASE WHEN a.n > 0 THEN a.n + b.n ELSE a.n - b.n END", [3]),
        ("MATCH (a:N1), (b:N2) RETURN a.s + b.s, a.l + b.l, a.l + b.n", [["xy", [1, 2], [1, 2]]]),
        ("MATCH (a:N1), (b:N2) RETURN a.n - a.m, [a.n, a.m]", [[-9, [1, 10]]]),
        ("MATCH (a:N1), (b:N2) RETURN toUpper(a.s), size(a.s), toString(a.n)", [["X", 1, "1"]]),
        ("MATCH (a:N1), (b:N2) WHERE a.s STARTS WITH 'x' RETURN a.n - b.n", [-1]),
        ("MATCH (a:N1), (b:N2) RETURN sum(a.n + b.n)", [3]),
        ("MATCH (a:N1)-->(b:N2) RETURN b.n + 1, b.n - 1", [[3, 1]]),
        ("MATCH (b:N2) WITH b MATCH (a:N1) RETURN a.n - a.m, b.n", [[-9, 2]]),
        (
            "MATCH (a:N1), (b:N2) RETURN a.n + b.n AS s UNION ALL MATCH (a:N2), (b:N3) RETURN a.n + b.n AS s",
            [3, 5],
        ),
    ]
    for cypher, expected in cases:
        assert rows(cypher) == expected, cypher
    # Beside a node of no label that a property filters, and between two nodes of one label, the label test went too.
    clear(cypher_pm)
    rows(
        "CREATE (a:N1 {n: 1, m: 10, s: 'x', l: [1]}) CREATE (b:N2 {n: 2, m: 20, s: 'y', l: [2]}) "
        "CREATE (c:N3 {n: 3, m: 30, s: 'z', l: [3]}) CREATE (d:N2 {n: 4, m: 40, s: 'w', l: [4]}) "
        "CREATE (a)-[:R {w: 1}]->(b), (a)-[:R {w: 2}]->(c), (b)-[:R {w: 3}]->(d), (c)-[:R {w: 4}]->(d)"
    )
    beside = "MATCH (a:N1), (b) WHERE b.n > 1 AND b.m < 35 RETURN a.n - a.m, b.n ORDER BY b.n"
    assert rows(beside) == [[-9, 2], [-9, 3]], beside
    same = "MATCH (a:N2)-[r:R]->(b:N2) RETURN a.n - a.m, b.n - b.m, r.w"
    assert rows(same) == [[-18, -36, 3]], same
    # A write reads its rows by a SELECT … INTO, a MERGE its match by a loop, and the rows it returns by a cursor. The
    # first, unhinted, gave a row for each two nodes, and set x and y on the N2 and the N3 too.
    clear(cypher_pm)
    rows(
        "CREATE (a:N1 {n: 1, m: 10, s: 'x', l: [1]}) CREATE (b:N2 {n: 2, m: 20, s: 'y', l: [2]}) "
        "CREATE (c:N3 {n: 3, m: 30, s: 'z', l: [3]}) CREATE (a)-[:R]->(b), (a)-[:R]->(c)"
    )
    writes: list[tuple[str, list[Any]]] = [
        ("MATCH (a:N1), (b:N2) WITH a, b, a.n + b.n AS s, a.n - b.n AS d SET a.x = s, a.y = d RETURN s, d", [[3, -1]]),
        ("MATCH (a:N1) MERGE (a)-[:R]->(b:N2) RETURN b.n - b.m, b.s", [[-18, "y"]]),
        ("MATCH (a:N1), (b:N2) SET a.z = 1 RETURN a.n + b.n, a.n - b.n", [[3, -1]]),
        (
            "MATCH (a) RETURN a.n, a.x, a.y, a.z ORDER BY a.n",
            [[1, 3, -1, 1], [2, None, None, None], [3, None, None, None]],
        ),
    ]
    for cypher, expected in writes:
        assert rows(cypher) == expected, cypher
    clear(cypher_pm)


def test_an_order_term_over_an_aggregate_that_reads_a_grouping_key_is_sorted_by_the_key_as_it_is_grouped_by(
    cypher_pm: PersistenceManager,
):
    # On Oracle `me.age` in the term read the properties, where the key is grouped by as read through its text, and
    # Oracle refused it (ORA-00979): #256, liberation-data/drivine4j#413. drivine4j's CypherQueryContract.
    def rows(cypher: str) -> list[Any]:
        return cypher_pm.query(spec(cypher))

    clear(cypher_pm)
    # Sorted by the key alone, A, B, C, D; by the key and the count, B, A, C, D.
    rows(
        "CREATE (a:CyKey {name: 'A', age: 10, born: date('2000-01-01')}), "
        "(b:CyKey {name: 'B', age: 11, born: date('2000-01-05')}), "
        "(c:CyKey {name: 'C', age: 20, born: date('2000-01-20')}), "
        "(d:CyKey {name: 'D', age: 21, born: date('2000-01-25')}), "
        "(a)-[:K]->(b), (a)-[:K]->(c), (a)-[:K]->(d)"
    )
    match = "MATCH (me:CyKey)--(you:CyKey)"
    by_key = [[10, 3], [11, 1], [20, 1], [21, 1]]
    by_both = [[11, 1], [10, 3], [20, 1], [21, 1]]
    cases: list[tuple[str, list[Any]]] = [
        (f"{match} RETURN me.age AS age, count(you.age) AS cnt ORDER BY me.age + count(you.age)", by_both),
        (
            f"{match} WITH me.age AS age, count(you.age) AS cnt ORDER BY me.age + count(you.age) RETURN age, cnt",
            by_both,
        ),
        (f"{match} RETURN DISTINCT me.age AS age, count(you.age) AS cnt ORDER BY me.age + count(you.age)", by_both),
        (f"{match} RETURN me.age AS age, count(you.age) AS cnt ORDER BY age + count(you.age)", by_both),
        # Sorted, skipped and limited before the WITH's WHERE, as Cypher reads it; a node keying the groups, read by
        # property.
        (
            f"{match} WITH me.age AS age, count(you.age) AS cnt ORDER BY me.age + count(you.age) SKIP 1 LIMIT 2 "
            "WHERE cnt = 1 RETURN age",
            [20],
        ),
        (f"{match} WITH me, count(you) AS cnt ORDER BY me.age * count(you) RETURN me.name", ["B", "C", "D", "A"]),
        # A subquery in a term made of items, which _over_items leaves to be worked out in the query that groups.
        (
            f"{match} WITH me, count(you) AS cnt ORDER BY COUNT {{ (me)--() }} * count(you) * 100 - me.age "
            "RETURN me.name",
            ["D", "C", "B", "A"],
        ),
        # An aggregate that is no item; a comprehension that names its own variable as the key's; the key in a function.
        (f"{match} RETURN me.age AS age, count(*) AS c ORDER BY me.age + count(you.age)", by_both),
        (
            f"{match} RETURN me.age AS age, count(you.age) AS cnt "
            "ORDER BY me.age + size([me IN [1] | me]) + count(you.age)",
            by_both,
        ),
        (f"{match} RETURN me.age AS age, count(*) AS cnt ORDER BY toString(me.age) + count(*)", by_key),
        # Two keys, one a part of the other.
        (
            f"{match} RETURN me.age AS a, me.age + 1 AS b, count(*) AS cnt "
            "ORDER BY me.age + 1 + count(*), me.age * 2 - count(*)",
            [[11, 12, 1], [10, 11, 3], [20, 21, 1], [21, 22, 1]],
        ),
        # Beside a variable-length walk, where the key is read as elsewhere.
        (
            "MATCH (me:CyKey)-[:K*1..1]-(you:CyKey) RETURN me.age AS age, count(you) AS cnt "
            "ORDER BY me.age * count(you)",
            [[11, 1], [20, 1], [21, 1], [10, 3]],
        ),
        (f"{match} RETURN me.age AS age, count(you.age) AS cnt ORDER BY me.age", by_key),
        # A key that is an expression, and several keys.
        (
            f"{match} RETURN me.age + 1 AS k, count(*) AS cnt ORDER BY me.age + 1 + count(*)",
            [[12, 1], [11, 3], [21, 1], [22, 1]],
        ),
        (
            f"{match} RETURN me.name AS name, me.age AS age, count(*) AS cnt ORDER BY me.age * count(*) DESC, me.name",
            [["A", 10, 3], ["D", 21, 1], ["C", 20, 1], ["B", 11, 1]],
        ),
    ]
    # Each that differs, so a store's every difference shows at once.
    differing: list[str] = []
    for cypher, expected in cases:
        try:
            given = rows(cypher)
        except Exception as e:  # noqa: BLE001
            differing.append(f"{cypher}: {str(e).splitlines()[0]}")
            continue
        if given != expected:
            differing.append(f"{cypher}: {given}")
    assert differing == []
    # A temporal key: A's 30 days are past C's 10.
    temporal = (
        f"{match} RETURN me.name AS name, me.born AS born, count(*) AS cnt "
        "ORDER BY me.born + duration({days: 10 * count(*)})"
    )
    assert [it[0] for it in rows(temporal)] == ["B", "C", "A", "D"], temporal
    clear(cypher_pm)


def differing(pm: PersistenceManager, cases: list[tuple[str, list[Any]]]) -> list[str]:
    """The cases whose rows differ, or that fail, so a store's every difference shows at once."""
    found: list[str] = []
    for cypher, expected in cases:
        try:
            given = pm.query(spec(cypher))
        except Exception as e:  # noqa: BLE001
            found.append(f"{cypher}: {str(e).splitlines()[0]}")
            continue
        if given != expected:
            found.append(f"{cypher}: {given}")
    return found


def test_x_in_a_list_each_row_works_out_by_a_subquery_finds_the_rows_items_in_a_where_exists_return_and_unwind(
    cypher_pm: PersistenceManager,
):
    # #272, drivine4j's CypherQueryContract of liberation-data/drivine4j#433. Not in the spec yet: on Oracle such a list
    # was worked out by a subquery in a lateral view's select list, which Oracle works out once, not for each row, where
    # that view is first in a subquery's FROM: the WHEREs found nothing.
    clear(cypher_pm)
    cypher_pm.query(
        spec(
            "CREATE (:K {a: 1}), (:K {b: 2}), (:K {a: 3, b: 4}), (:P {name: 'a'})-[:R]->(:Q {name: 'x'}), "
            "(:P {name: 'b'})-[:R]->(:Q {name: 'y'})"
        )
    )
    each = "UNWIND [1, 2, 3] AS k WITH k WHERE"
    cases: list[tuple[str, list[Any]]] = [
        # The issue's statements.
        ("UNWIND [1, 2] AS k WITH k WHERE 2 IN [i IN range(1, k) | i] RETURN k", [2]),
        ("MATCH (n:K) WHERE 'b' IN keys(n) RETURN count(n)", [2]),
        ("MATCH (n:K) WHERE 'b' IN [k IN keys(n) | k] RETURN count(n)", [2]),
        (
            "UNWIND [{a: 1}, {b: 2}, {a: 3, b: 4}] AS m WITH m WHERE 'b' IN keys(m) RETURN m",
            [{"b": 2}, {"a": 3, "b": 4}],
        ),
        # A comprehension that filters, and a list a pattern comprehension collects, in a WHERE, an EXISTS and a RETURN.
        ("UNWIND [1, 2, 3] AS k WITH k WHERE 3 IN [i IN range(1, k) WHERE i > 1 | i] RETURN k", [3]),
        ("MATCH (p:P) WHERE 'y' IN [(p)-[:R]->(q) | q.name] RETURN p.name", ["b"]),
        ("MATCH (p:P) WHERE EXISTS { MATCH (p)-[:R]->(q) WHERE 'y' IN [(p)-->(r) | r.name] } RETURN p.name", ["b"]),
        ("MATCH (n:K) WHERE EXISTS { MATCH (m:K) WHERE m.a = n.a AND 'b' IN keys(n) } RETURN n.a, n.b", [[3, 4]]),
        ("UNWIND [1, 2] AS k RETURN k, 2 IN [i IN range(1, k) | i] ORDER BY k", [[1, False], [2, True]]),
        ("MATCH (p:P) RETURN p.name, 'y' IN [(p)-[:R]->(q) | q.name] ORDER BY p.name", [["a", False], ["b", True]]),
        # Quantifiers over such a list.
        (f"{each} any(i IN [j IN range(1, k) | j] WHERE i = 3) RETURN k", [3]),
        (f"{each} all(i IN [j IN range(1, k) | j] WHERE i < 3) RETURN k ORDER BY k", [1, 2]),
        (f"{each} none(i IN [j IN range(1, k) | j] WHERE i = 2) RETURN k", [1]),
        (f"{each} single(i IN [j IN range(1, k) | j] WHERE i >= 2) RETURN k", [2]),
        ("MATCH (n:K) WHERE any(x IN keys(n) WHERE x = 'b') RETURN count(n)", [2]),
        ("MATCH (n:K) WHERE none(x IN keys(n) WHERE x = 'b') RETURN count(n)", [1]),
        # Unwound beside the table whose row it reads.
        ("UNWIND [1, 2] AS k UNWIND [i IN range(1, k) | i] AS j RETURN k, j ORDER BY k, j", [[1, 1], [2, 1], [2, 2]]),
        ("MATCH (p:P) UNWIND [(p)-[:R]->(q) | q.name] AS x RETURN p.name, x ORDER BY p.name", [["a", "x"], ["b", "y"]]),
        # A list that may hold temporal values, which Oracle reads by its places.
        (
            "UNWIND [1, 2] AS k WITH k WHERE 2 IN "
            "[i IN range(1, k) | CASE WHEN i = 2 THEN 2 ELSE date('2024-01-01') END] RETURN k",
            [2],
        ),
        (
            "UNWIND [1, 2] AS k WITH k WHERE 2 IN "
            "[i IN range(1, k) | coalesce(CASE WHEN i = 2 THEN 2 END, date('2024-01-01'))] RETURN k",
            [2],
        ),
        (
            "UNWIND [1, 2] AS k UNWIND [i IN range(1, k) | date('2024-01-01') + duration({days: i - 1})] AS t "
            "RETURN k, t.day ORDER BY k, t.day",
            [[1, 1], [2, 1], [2, 2]],
        ),
    ]
    assert differing(cypher_pm, cases) == []
    clear(cypher_pm)


def test_a_list_each_row_works_out_by_a_subquery_is_read_for_each_row_in_a_call_with_optional_match_and_count(
    cypher_pm: PersistenceManager,
):
    # #272, liberation-data/drivine4j#433: each reads keys(n) where a subquery's FROM may begin with the list's view.
    # Before the fix, Oracle gave the OPTIONAL MATCH no m.b and the COUNT 0 on every row.
    clear(cypher_pm)
    cypher_pm.query(spec("CREATE (:K {a: 1}), (:K {b: 2}), (:K {a: 3, b: 4})"))
    cases: list[tuple[str, list[Any]]] = [
        ("MATCH (n:K) CALL { WITH n RETURN keys(n) AS ks } WITH n, ks WHERE 'b' IN ks RETURN count(n)", [2]),
        ("MATCH (n:K) WITH n, [k IN keys(n) | k] AS ks WHERE 'b' IN ks RETURN count(n)", [2]),
        ("MATCH (n:K) WHERE size([k IN keys(n) WHERE k = 'b']) > 0 RETURN count(n)", [2]),
        (
            "MATCH (n:K) WHERE reduce(s = 0, k IN keys(n) | s + CASE WHEN k = 'b' THEN 1 ELSE 0 END) = 1 "
            "RETURN count(n)",
            [2],
        ),
        (
            "MATCH (n:K) OPTIONAL MATCH (m:K) WHERE m.a = n.a AND 'b' IN keys(m) RETURN n.a, m.b ORDER BY n.a",
            [[1, None], [3, 4], [None, None]],
        ),
        (
            "MATCH (n:K) RETURN n.a, COUNT { MATCH (m:K) WHERE 'b' IN keys(n) } ORDER BY n.a",
            [[1, 0], [3, 3], [None, 3]],
        ),
    ]
    assert differing(cypher_pm, cases) == []
    clear(cypher_pm)


def distinct_values(rows: list[Any]) -> int:
    """How many distinct values the rows hold, each item of a row of several apart."""

    def text(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)

    return len({text(it) for row in rows for it in (row if isinstance(row, list) else [row])})


def test_random_uuid_inside_a_larger_expression_in_a_read_is_made_again_for_each_row(
    cypher_pm: PersistenceManager,
):
    # drivine4j's #412 and #471 (#292 here). A term holding randomUUID() was bound in a subquery that reads nothing of
    # the row, or a CALL's was joined so, and the store made it once for the statement: each row read the same one.
    # Each statement gives a value for each of 100 rows, and the count of distinct values among them. Not one in an
    # aggregate's argument, which Neo4j refuses.
    clear(cypher_pm)
    shapes: list[tuple[str, int]] = [
        *(
            (f"UNWIND range(1, 100) AS i RETURN {it}", 100)
            for it in [
                "left(replace(randomUUID(), '-', ''), 16)",
                "substring(randomUUID(), 0, 16)",
                "toUpper(randomUUID())",
                "[x IN [1] | randomUUID()][0]",
                "CASE WHEN i > 0 THEN randomUUID() END",
                "CASE WHEN true THEN toUpper(randomUUID()) END",
                "{u: randomUUID()}.u",
                "{u: randomUUID()}",
                "coalesce(null, randomUUID()) + toString(i % 2)",
            ]
        ),
        ("UNWIND range(1, 100) AS i RETURN [randomUUID(), randomUUID()]", 200),
        ("UNWIND range(1, 100) AS i RETURN [x IN range(1, 3) | toUpper(randomUUID())]", 300),
        ("UNWIND range(1, 100) AS i RETURN reduce(s = '', x IN [1, 2] | s + randomUUID())", 100),
        ("UNWIND range(1, 100) AS i UNWIND [toUpper(randomUUID())] AS u RETURN u", 100),
        ("UNWIND range(1, 100) AS i RETURN toUpper(randomUUID()) AS u ORDER BY u", 100),
        ("UNWIND range(1, 100) AS i CALL { RETURN randomUUID() AS u } RETURN u", 100),
        ("UNWIND range(1, 100) AS i CALL { RETURN toUpper(randomUUID()) + '-' AS u } RETURN u", 100),
        # However the CALL imports, it reads the UUID made for its row, which it never names (#294).
        ("UNWIND range(1, 100) AS i CALL () { RETURN toUpper(randomUUID()) AS u } RETURN u", 100),
        ("UNWIND range(1, 100) AS i CALL (i) { RETURN toString(i) + randomUUID() AS u } RETURN u", 100),
        ("UNWIND range(1, 100) AS i CALL { WITH i RETURN toString(i) + randomUUID() AS u } RETURN u", 100),
        (
            "UNWIND range(1, 100) AS i CALL { WITH i WITH i AS j RETURN toString(j) + randomUUID() AS u } RETURN u",
            100,
        ),
        ("UNWIND range(1, 100) AS i CALL { RETURN toUpper(randomUUID()) AS u UNION RETURN 'x' AS u } RETURN u", 101),
        # A variable quoted as the compiler names its own is the statement's: imported as named, and a UUID made still.
        (
            "UNWIND range(1, 100) AS `#anewi` CALL (`#anewi`) { RETURN toString(`#anewi`) + randomUUID() AS u } "
            "RETURN u",
            100,
        ),
        # ... and a CASE reading it is not taken for a randomUUID() the compiler read beside the row's own.
        (
            "UNWIND range(1, 100) AS i WITH i, 1 AS `#anew1` "
            "RETURN [x IN [1] | CASE WHEN `#anew1` IS NULL THEN null ELSE randomUUID() END][0]",
            100,
        ),
        # A CALL that writes makes one for each row it writes, as it did.
        (
            "UNWIND range(1, 100) AS i CALL (i) { CREATE (n:CyUuidCall {id: toUpper(randomUUID())}) RETURN n.id AS u } "
            "RETURN u",
            100,
        ),
        (
            "UNWIND range(1, 100) AS i CALL () { CREATE (n:CyUuidSet) SET n.id = toUpper(randomUUID()) "
            "RETURN n.id AS u } RETURN u",
            100,
        ),
        # ... and one for each item of a list it writes.
        (
            "UNWIND range(1, 100) AS i CREATE (n:CyUuidList {ids: [x IN [1, 2] | toUpper(randomUUID())]}) RETURN n.ids",
            200,
        ),
        # A CALL in a CALL, and an item made again in a reduce's body.
        ("UNWIND range(1, 100) AS i CALL () { CALL () { RETURN toUpper(randomUUID()) AS u } RETURN u } RETURN u", 100),
        ("UNWIND range(1, 100) AS i RETURN reduce(s = [], x IN [1, 2] | s + [toUpper(randomUUID())])", 200),
        ("UNWIND range(1, 100) AS i WITH i, toUpper(randomUUID()) AS u RETURN u", 100),
        # Two items written alike are a UUID each, as in Neo4j.
        ("UNWIND range(1, 100) AS i RETURN toUpper(randomUUID()) AS a, toUpper(randomUUID()) AS b", 200),
        ("UNWIND range(1, 100) AS i WITH toUpper(randomUUID()) AS a, toUpper(randomUUID()) AS b RETURN a, b", 200),
    ]
    failed: list[str] = []

    def failing(cypher: str, wrong: Callable[[list[Any]], bool]) -> None:
        try:
            rows = cypher_pm.query(spec(cypher))
            if wrong(rows):
                failed.append(f"{cypher}: {str(rows)[:300]}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{cypher}: {str(e)[:300]}")

    for cypher, expected in shapes:
        failing(cypher, lambda rows, expected=expected: distinct_values(rows) != expected)
    # A FOREACH makes one for each node it makes.
    failing(
        "UNWIND range(1, 50) AS i FOREACH (x IN [1, 2] | CREATE (:CyUuidEach {id: toUpper(randomUUID())}))",
        lambda rows: False,
    )
    failing("MATCH (n:CyUuidEach) RETURN n.id", lambda rows: distinct_values(rows) != 100)
    # A condition that reads one keeps about half of 1,000 rows, not all of them or none.
    seeded(cypher_pm)
    for cypher in [
        "UNWIND range(1, 1000) AS i WITH i WHERE left(randomUUID(), 1) < '8' RETURN count(*)",
        "UNWIND range(1, 1000) AS i WITH i WHERE [x IN [1] | left(randomUUID(), 1)][0] < '8' RETURN count(*)",
        "UNWIND range(1, 1000) AS i MATCH (p:Person {name: 'Keanu'}) WHERE left(randomUUID(), 1) < '8' RETURN count(*)",
        # An OPTIONAL MATCH's lateral query read nothing of the row, and Oracle worked it out once.
        "UNWIND range(1, 1000) AS i OPTIONAL MATCH (p:Person {name: 'Keanu'}) WHERE randomUUID() < '8' RETURN count(p)",
        "UNWIND range(1, 1000) AS i OPTIONAL MATCH (p:Person {name: 'Keanu'}) WHERE left(randomUUID(), 1) < '8' "
        "RETURN count(p)",
        "UNWIND range(1, 1000) AS i OPTIONAL MATCH (p:Person {name: 'Keanu'}) "
        "WHERE [x IN [1] | left(randomUUID(), 1)][0] < '8' RETURN count(p)",
        # Made for each of the 4 people a row finds, not once for the row: some of them kept, not all or none.
        "UNWIND range(1, 1000) AS i OPTIONAL MATCH (p:Person) WHERE left(randomUUID(), 1) < '8' "
        "WITH i, count(p) AS c WHERE c > 0 AND c < 4 RETURN count(*)",
        # One in a CALL that imports nothing, made for each row the CALL is joined to.
        "UNWIND range(1, 1000) AS i CALL () { OPTIONAL MATCH (p:Person {name: 'Keanu'}) "
        "WHERE left(randomUUID(), 1) < '8' RETURN p } RETURN count(p)",
        # Two OPTIONAL MATCHes, each a UUID of its own: one kept and not the other in about half the rows.
        "UNWIND range(1, 1000) AS i OPTIONAL MATCH (p:Person {name: 'Keanu'}) WHERE left(randomUUID(), 1) < '8' "
        "OPTIONAL MATCH (q:Person {name: 'Keanu'}) WHERE left(randomUUID(), 1) < '8' "
        "WITH p, q WHERE (p IS NULL) <> (q IS NULL) RETURN count(*)",
        # A shortest path's condition, met where its one path is found.
        "UNWIND range(1, 1000) AS i MATCH (a:Person {name: 'Laurence'}), (b:Person {name: 'Carrie'}) "
        "MATCH p = shortestPath((a)-[:KNOWS*]->(b)) WHERE left(randomUUID(), 1) < '8' RETURN count(p)",
        "UNWIND range(1, 1000) AS i MATCH (a:Person {name: 'Laurence'}), (b:Person {name: 'Carrie'}) "
        "MATCH p = allShortestPaths((a)-[:KNOWS*]->(b)) WHERE left(randomUUID(), 1) < '8' RETURN count(p)",
        # Made again for each item: one of two kept in about half the rows, which Postgres and Oracle kept both or
        # neither of.
        "UNWIND range(1, 1000) AS i WITH i WHERE single(x IN range(1, 2) WHERE left(randomUUID(), 1) < '8') "
        "RETURN count(*)",
        "UNWIND range(1, 1000) AS i WITH i WHERE size([x IN range(1, 2) WHERE left(randomUUID(), 1) < '8' | x]) = 1 "
        "RETURN count(*)",
    ]:
        failing(cypher, lambda rows: rows[0] in (0, 1000))
    # One for each item a pattern comprehension finds, 8 of them, where Postgres refused the statement.
    for cypher in [
        "MATCH (p:Person) RETURN p.name, [(p)-->() | toUpper(randomUUID())]",
        "MATCH (p:Person) RETURN p.name, [(p)-->(m) WHERE left(randomUUID(), 1) <> 'z' | left(randomUUID(), 36)]",
    ]:

        def eight(rows: list[Any]) -> bool:
            made = [it for row in rows for it in row[1]]
            return len(rows) != 4 or len(made) != 8 or len(set(made)) != 8

        failing(cypher, eight)
    # The UUID made first for the row is no variable of the statement's, nor is the variable a CALL's query unwinds it
    # to: a star in the query gives j alone, before u.
    for cypher, columns in [
        ("UNWIND range(1, 2) AS i OPTIONAL MATCH (p:Person {name: 'Keanu'}) WHERE randomUUID() < 'z' RETURN *", 2),
        ("UNWIND range(1, 2) AS i CALL { RETURN toUpper(randomUUID()) AS u } RETURN *", 2),
        ("UNWIND range(1, 2) AS i CALL (*) { RETURN toUpper(randomUUID()) AS u } RETURN *", 2),
        ("UNWIND range(1, 2) AS i WITH * WHERE left(randomUUID(), 1) < 'z' RETURN *, 1 AS one", 2),
        ("UNWIND range(1, 2) AS i CALL { UNWIND [1] AS j RETURN *, toUpper(randomUUID()) AS u } RETURN *", 3),
    ]:
        failing(cypher, lambda rows, columns=columns: len(rows) != 2 or any(len(it) != columns for it in rows))
    # A variable quoted as the compiler names its own is the statement's: it was taken for the UUID's, so read as the
    # UUID, and left out of a star. The first name made is #anew2 here, after the UNWIND's alias.
    for cypher, expected in [
        ("UNWIND [1, 2] AS `#anew2` RETURN `#anew2` AS a, toUpper(randomUUID()) AS u ORDER BY a", [[1], [2]]),
        (
            "UNWIND [1, 2] AS i WITH i, 1 AS `#anew1`, 2 AS `#anew2`, 3 AS `#anew3`, 4 AS `#anew4`, 5 AS `#anew5`, "
            "6 AS `#anew6` CALL { RETURN toUpper(randomUUID()) AS u } RETURN * ORDER BY i",
            [[1, 2, 3, 4, 5, 6, 1], [1, 2, 3, 4, 5, 6, 2]],
        ),
        # A UNION's queries are compiled one by one: the name made in the first was a later one's variable.
        (
            "UNWIND [1] AS i CALL (i) { RETURN toString(i) + randomUUID() AS u } RETURN 0 AS x, u "
            "UNION ALL WITH 2 AS `#anew2` WITH * RETURN `#anew2` AS x, 0 AS u",
            [[0], [2]],
        ),
    ]:
        failing(cypher, lambda rows, expected=expected: [it[:-1] for it in rows] != expected)
    # An OPTIONAL MATCH that finds nothing, or keeps nothing it finds, keeps the row, with nulls.
    for cypher in [
        "UNWIND range(1, 3) AS i OPTIONAL MATCH (p:Person {name: 'Nobody'}) WHERE randomUUID() < 'z' RETURN i, p",
        "UNWIND range(1, 3) AS i OPTIONAL MATCH (p:Person {name: 'Keanu'}) WHERE left(randomUUID(), 1) < '0' "
        "RETURN i, p",
        "UNWIND range(1, 3) AS i OPTIONAL MATCH (p:Person {name: 'Nobody'}) WHERE randomUUID() < 'z' "
        "OPTIONAL MATCH (p)-->(m) WHERE toUpper(randomUUID()) < 'Z' RETURN i, p, m",
    ]:
        failing(cypher, lambda rows: len(rows) != 3 or any(v is not None for it in rows for v in it[1:]))
    assert failed == []


def test_random_uuid_bound_by_a_with_is_one_value_for_each_row_the_same_however_often_the_row_reads_it(
    cypher_pm: PersistenceManager,
):
    # drivine4j's #412: one value for each row, not for each read of it.
    rows = cypher_pm.query(
        spec(
            "UNWIND range(1, 100) AS i WITH i, randomUUID() AS u "
            "RETURN u AS a, u AS b, toUpper(u) AS c, left(replace(u, '-', ''), 16) AS d"
        )
    )
    assert len({it[0] for it in rows}) == 100
    for a, b, upper, short in rows:
        assert [b, upper, short] == [a, a.upper(), a.replace("-", "")[:16]]
    made = cypher_pm.query(
        spec("UNWIND range(1, 100) AS i WITH i, toUpper(randomUUID()) AS u RETURN u AS a, toLower(u) AS b, u + '' AS c")
    )
    assert len({it[0] for it in made}) == 100
    for a, lower, plus in made:
        assert [lower, plus] == [a.lower(), a]


def test_a_call_sees_only_the_variables_it_imports_and_a_name_it_does_not_import_is_a_new_variable_in_it(
    cypher_pm: PersistenceManager,
):
    # drivine4j's own test of liberation-data/drivine4j#410, ported for #294. A CALL saw every variable of its row, so
    # an inner MATCH that reused a name it did not import joined to the row's variable, and gave no rows, or was
    # refused where it bound a relationship of a node's name. The rows are Neo4j's (2026.10).
    seeded(cypher_pm)
    titles = ["Johnny Mnemonic", "The Matrix", "The Matrix Reloaded"]
    people = [["Carrie", 3], ["Hugo", 3], ["Keanu", 3], ["Laurence", 3]]
    cases: list[tuple[str, list[Any]]] = [
        # A CALL with no brackets and no leading WITH imports nothing.
        (
            "MATCH (p:Person) CALL { MATCH (p:Movie) RETURN p.title AS t } "
            "RETURN count(*) AS n, count(DISTINCT t) AS d",
            [[12, 3]],
        ),
        ("MATCH (p:Person) CALL { MATCH (p:Movie) RETURN count(p) AS n } RETURN p.name, n ORDER BY p.name", people),
        # CALL () imports nothing.
        (
            "MATCH (p:Person {name: 'Keanu'}) CALL () { MATCH (p:Movie) RETURN p.title AS t } RETURN p.name, t "
            "ORDER BY t",
            [["Keanu", it] for it in titles],
        ),
        # CALL (a) imports a alone, and a leading WITH a alone: b in it is new.
        (
            "MATCH (a:Person {name: 'Keanu'}), (b:Person {name: 'Carrie'}) "
            "CALL (a) { MATCH (a)-[:ACTED_IN]->(b) RETURN b.title AS t } RETURN b.name, t ORDER BY t",
            [["Carrie", it] for it in titles],
        ),
        (
            "MATCH (a:Person {name: 'Keanu'}), (b:Person {name: 'Carrie'}) "
            "CALL { WITH a MATCH (a)-[:ACTED_IN]->(b) RETURN b.title AS t } RETURN b.name, t ORDER BY t",
            [["Carrie", it] for it in titles],
        ),
        # A node's name, not imported, is a relationship's in the CALL.
        (
            "MATCH (a:Person {name: 'Keanu'}) CALL { MATCH ()-[a:KNOWS]->() RETURN count(a) AS n } RETURN a.name, n",
            [["Keanu", 2]],
        ),
        (
            "MATCH (a:Person {name: 'Laurence'}) CALL () { MATCH ()-[a:KNOWS]->(b) RETURN b.name AS k } "
            "RETURN a.name, k ORDER BY k",
            [["Laurence", "Carrie"], ["Laurence", "Keanu"]],
        ),
        (
            "MATCH (a:Person {name: 'Keanu'}), (b:Movie {title: 'The Matrix'}) "
            "CALL (a) { MATCH (a)-[b]->() RETURN count(b) AS n } RETURN b.title, n",
            [["The Matrix", 4]],
        ),
        # What a leading WITH imports is gone after a WITH that does not pass it on.
        (
            "MATCH (a:Person {name: 'Keanu'}) CALL { WITH a WITH 1 AS one MATCH (a:Movie) RETURN count(a) AS n } "
            "RETURN n",
            [3],
        ),
        # What brackets import is seen throughout the CALL, after a WITH that does not pass it on too, as Neo4j has it.
        (
            "MATCH (a:Person {name: 'Keanu'}), (b:Person {name: 'Carrie'}) "
            "CALL (a, b) { WITH 1 AS one RETURN b.name AS n } RETURN n",
            ["Carrie"],
        ),
        (
            "MATCH (a:Person {name: 'Keanu'}) CALL (a) { MATCH (a)-[:ACTED_IN]->(m) WITH m "
            "MATCH (a)-[:ACTED_IN]->(m) RETURN count(m) AS n } RETURN n",
            [3],
        ),
        (
            "MATCH (a:Person) CALL (a) { UNWIND [1, 2] AS i WITH i RETURN a.name AS n, sum(i) AS s } "
            "RETURN n, s ORDER BY n",
            people,
        ),
        # CALL (*), and a leading WITH *, import every variable.
        (
            "MATCH (a:Person {name: 'Keanu'}) CALL (*) { MATCH (a)-[:ACTED_IN]->(m) RETURN count(m) AS n } RETURN n",
            [3],
        ),
        (
            "MATCH (a:Person {name: 'Keanu'}) CALL (*) { MATCH (a)-[:ACTED_IN]->(m) WITH m "
            "MATCH (a)-[:ACTED_IN]->(m) RETURN count(m) AS n } RETURN n",
            [3],
        ),
        (
            "MATCH (a:Person {name: 'Keanu'}) CALL { WITH * MATCH (a)-[:ACTED_IN]->(m) RETURN count(m) AS n } RETURN n",
            [3],
        ),
        # In a UNION, each query imports what its own leading WITH names.
        (
            "MATCH (a:Person {name: 'Keanu'}) CALL { WITH a MATCH (a)-[:ACTED_IN]->(m) RETURN count(m) AS n "
            "UNION ALL MATCH (a:Movie) RETURN count(a) AS n } RETURN a.name, n",
            [["Keanu", 3], ["Keanu", 3]],
        ),
    ]
    assert differing(cypher_pm, cases) == []


def test_a_call_that_writes_sees_only_the_variables_it_imports_so_a_create_of_a_name_it_does_not_import_makes_one(
    cypher_pm: PersistenceManager,
):
    # drivine4j's own test of liberation-data/drivine4j#410, ported for #294: a CREATE of a name the CALL does not
    # import joined the row's node, so no node was made. The rows are Neo4j's (2026.10).
    clear(cypher_pm)
    cypher_pm.query(spec("CREATE (:CyWrite {id: 1}), (:CyWrite {id: 2})"))
    cases: list[tuple[str, list[Any]]] = [
        (
            "MATCH (a:CyWrite) CALL () { CREATE (a:CyWrite {made: true}) RETURN a.made AS m } RETURN a.id, m "
            "ORDER BY a.id",
            [[1, True], [2, True]],
        ),
        (
            "MATCH (a:CyWrite {id: 1}) CALL { CREATE (a:CyWrite {made: false}) RETURN 1 AS x } RETURN [a.id, x]",
            [[1, 1]],
        ),
        ("MATCH (n:CyWrite) RETURN [count(n.id), count(n.made)]", [[2, 3]]),
        # CALL (a) imports a alone: b in it is new, and the row's b is as it was after it.
        (
            "MATCH (a:CyWrite {id: 1}), (b:CyWrite {id: 2}) CALL (a) { CREATE (a)-[:R]->(b:CyWrite {id: 3}) } RETURN 1",
            [1],
        ),
        ("MATCH (a:CyWrite)-[:R]->(b) RETURN [a.id, b.id]", [[1, 3]]),
        (
            "MATCH (a:CyWrite {id: 1}), (b:CyWrite {id: 2}) "
            "CALL (a) { CREATE (b:CyWrite {id: 4}) RETURN b.id AS made } RETURN [b.id, made]",
            [[2, 4]],
        ),
        # What brackets import is seen after a leading WITH that does not pass it on, as Neo4j has it.
        (
            "MATCH (a:CyWrite {id: 1}), (b:CyWrite {id: 2}) CALL (a, b) { WITH a SET b.seen = true } RETURN b.seen",
            [True],
        ),
        # ... and after any WITH: a CREATE of it joins the row's node, and a SET writes it.
        ("MATCH (a:CyWrite {id: 1}) CALL (a) { WITH 1 AS one CREATE (a)-[:S]->(:CyWrite {id: 5}) } RETURN 1", [1]),
        ("MATCH (a:CyWrite)-[:S]->(b) RETURN [a.id, b.id]", [[1, 5]]),
        (
            "MATCH (a:CyWrite {id: 1}) CALL (a) { MATCH (c:CyWrite {id: 5}) WITH c SET a.next = c.id "
            "RETURN a.next AS n } RETURN n",
            [5],
        ),
        ("MATCH (a:CyWrite {id: 2}) CALL (*) { WITH 1 AS one SET a.one = one } RETURN a.one", [1]),
        # ... after a WITH that renames it, and in a CALL in a CALL.
        (
            "MATCH (a:CyWrite {id: 2}) CALL (a) { WITH a AS b SET b.renamed = true SET a.also = true } "
            "RETURN [a.renamed, a.also]",
            [[True, True]],
        ),
        (
            "MATCH (a:CyWrite {id: 2}) CALL (a) { CALL (a) { WITH 2 AS two SET a.nested = two } } RETURN a.nested",
            [2],
        ),
        # An aggregate over no rows still gives its row, and the WITH's WHERE reads what brackets import.
        (
            "MATCH (a:CyWrite {id: 1}) CALL (a) { MATCH (c:CyNone) WITH count(c) AS n SET a.none = n "
            "RETURN a.none AS v } RETURN v",
            [0],
        ),
        (
            "MATCH (a:CyWrite {id: 1}) CALL (a) { MATCH (c:CyWrite) WITH c WHERE c.id = a.next SET c.prev = a.id "
            "RETURN c.prev AS p, c.id AS i } RETURN [p, i]",
            [[1, 5]],
        ),
        # Reading the imports again after a WITH that aggregates, or keeps rows distinct, gives one row for each row of
        # the call.
        (
            "MATCH (a:CyWrite) WHERE a.id IN [1, 2] CALL (a) { MATCH (c:CyWrite) WITH count(*) AS n SET a.count = n "
            "RETURN n } RETURN [a.id, n, a.count] ORDER BY a.id",
            [[1, 8, 8], [2, 8, 8]],
        ),
        (
            "MATCH (a:CyWrite) WHERE a.id IN [1, 2] CALL (a) { MATCH (c:CyWrite) WITH DISTINCT 1 AS one SET a.d = one "
            "RETURN one } RETURN [a.id, a.d] ORDER BY a.id",
            [[1, 1], [2, 1]],
        ),
    ]
    # In turn: each reads what those before it wrote.
    assert differing(cypher_pm, cases) == []


def test_a_with_star_at_the_start_of_a_call_passes_on_what_the_call_imports_and_nothing_where_nothing_is_bound(
    cypher_pm: PersistenceManager,
):
    # drivine4j's own test of liberation-data/drivine4j#472, ported for #304, whole, as drivine4j keeps it beside the
    # spec's steps of v0.11.0. The star expanded to the variables the CALL's first query bound, not those it imported,
    # so it was refused with "A projection has at least one item"; and a WITH * where nothing was bound was refused so.
    # The rows are Neo4j's (2026.10).
    seeded(cypher_pm)
    cases: list[tuple[str, list[Any]]] = [
        ("UNWIND [1, 2] AS i CALL { WITH * RETURN i AS x } RETURN x", [1, 2]),
        ("UNWIND [1, 2] AS i CALL (*) { WITH * RETURN i AS x } RETURN x", [1, 2]),
        ("UNWIND [1, 2] AS i CALL (i) { WITH * RETURN i AS x } RETURN x", [1, 2]),
        ("UNWIND [1, 2] AS i CALL (i) { WITH * WITH * RETURN i * 10 AS x } RETURN x", [10, 20]),
        ("UNWIND [1] AS i WITH i, 2 AS j CALL { WITH * RETURN i + j AS x } RETURN x", [3]),
        ("UNWIND [1] AS i CALL { WITH * UNWIND [5] AS k WITH * RETURN i + k AS x } RETURN x", [6]),
        (
            "MATCH (p:Person) CALL { WITH * RETURN p.name AS n } RETURN n ORDER BY n",
            ["Carrie", "Hugo", "Keanu", "Laurence"],
        ),
        (
            "UNWIND [1, 2] AS i CALL { WITH * RETURN i AS x UNION WITH * RETURN i * 10 AS x } RETURN x ORDER BY x",
            [1, 2, 10, 20],
        ),
        ("UNWIND [1] AS i CALL { WITH * CALL { WITH * RETURN i AS y } RETURN y AS x } RETURN x", [1]),
        (
            "UNWIND [1, 2] AS i OPTIONAL CALL { WITH * WITH * WHERE i > 1 RETURN i AS x } RETURN i, x",
            [[1, None], [2, 2]],
        ),
        # A star gives none of what brackets import, which the CALL sees throughout anyway.
        ("UNWIND [1, 2] AS i CALL (i) { WITH 1 AS one WITH * RETURN * } RETURN *", [[1, 1], [2, 1]]),
        ("UNWIND [1, 2] AS i CALL (i) { WITH *, 2 AS y RETURN * } RETURN *", [[1, 2], [2, 2]]),
        # Where nothing is bound, a WITH * passes on nothing.
        ("CALL { WITH * RETURN 1 AS x } RETURN x", [1]),
        ("CALL (*) { WITH * RETURN 1 AS x } RETURN x", [1]),
        ("UNWIND [1] AS i CALL () { WITH * RETURN 1 AS x } RETURN i, x", [[1, 1]]),
        ("UNWIND [1, 2] AS i CALL () { WITH * WITH * RETURN 3 AS x } RETURN i, x", [[1, 3], [2, 3]]),
        ("WITH * RETURN 1 AS x", [1]),
        # Its WHERE is met still, on the rows it reads.
        ("WITH * WHERE false RETURN 1 AS x", []),
        ("WITH * WHERE true RETURN 1 AS x", [1]),
        ("RETURN 1 AS x UNION ALL WITH * WHERE false RETURN 2 AS x", [1]),
        ("UNWIND [1, 2] AS i CALL () { WITH * WHERE false RETURN 1 AS x } RETURN i, x", []),
        ("UNWIND [1, 2] AS i CALL (i) { WITH * WHERE i > 1 RETURN i AS x } RETURN x", [2]),
        # A randomUUID() in its WHERE is made for each row it reads, and once for each call of it.
        ("WITH * WHERE randomUUID() = randomUUID() RETURN 1 AS x", []),
        (
            "UNWIND range(1, 40) AS i CALL () { WITH * WHERE randomUUID() < '8' RETURN 1 AS x } "
            "RETURN count(*) > 0 AND count(*) < 40 AS mixed",
            [True],
        ),
        # A CALL without brackets that imports only the UUID the compiler made for it passes on nothing.
        ("CALL { WITH * RETURN toUpper(randomUUID()) AS u } RETURN size(u) AS n", [36]),
        # Made for each row, beside a randomUUID(): not the compiler's own variables (drivine4j#412), which no star
        # shows.
        (
            "UNWIND [1, 2] AS i CALL { WITH * RETURN toString(i) + randomUUID() AS u } RETURN count(DISTINCT u) AS n",
            [2],
        ),
        (
            "UNWIND [1, 2] AS i CALL () { WITH * RETURN toUpper(randomUUID()) AS u } RETURN count(DISTINCT u) AS n",
            [2],
        ),
    ]
    assert differing(cypher_pm, cases) == []
    for cypher in [
        "UNWIND [1] AS i CALL () { WITH * RETURN i AS x } RETURN x",
        # A RETURN * of the CALL returns what it imports, which the row holds already.
        "UNWIND [1, 2] AS i CALL { WITH * RETURN * } RETURN i",
    ]:
        with pytest.raises(Exception):  # noqa: B017, PT011
            cypher_pm.query(spec(cypher))
    # With DISTINCT, an order, SKIP or LIMIT it is refused, not left out: Neo4j gives [], [], [] and [1, 2].
    if cypher_pm.type is not DatabaseType.NEO4J:
        for cypher in [
            "WITH * LIMIT 0 RETURN 1 AS x",
            "WITH * SKIP 1 RETURN 1 AS x",
            "UNWIND [1, 2] AS i CALL (i) { WITH * LIMIT 0 RETURN i AS x } RETURN x",
            "UNWIND [1, 2] AS i CALL (i) { WITH DISTINCT * RETURN i AS x } RETURN x",
        ]:
            with pytest.raises(DrivineError, match="A projection has at least one item."):
                cypher_pm.query(spec(cypher))
    uuids = cypher_pm.query(spec("UNWIND [1, 2] AS i CALL { WITH * RETURN toUpper(randomUUID()) AS u } RETURN *"))
    assert [it[0] for it in uuids] == [1, 2]
    assert all(len(it) == 2 and len(it[1]) == 36 for it in uuids)
    renamed = cypher_pm.query(
        spec("UNWIND [1, 2] AS i CALL { WITH * WITH i AS j, toUpper(randomUUID()) AS u RETURN * } RETURN *")
    )
    assert [it[:2] for it in renamed] == [[1, 1], [2, 2]]
    assert all(len(it) == 3 and len(it[2]) == 36 for it in renamed)
    bracketed = cypher_pm.query(
        spec("UNWIND [1, 2] AS i CALL (i) { WITH * RETURN i AS x, toUpper(randomUUID()) AS u } RETURN *")
    )
    assert [[it[0], it[2]] for it in bracketed] == [[1, 1], [2, 2]]
    assert len({it[1] for it in bracketed}) == 2


def test_a_pattern_comprehension_whose_body_reads_none_of_its_patterns_variables_gives_an_item_for_each_match(
    cypher_pm: PersistenceManager,
):
    # drivine4j's own test of liberation-data/drivine4j#467, ported for #305. Its aggregate read only the row's
    # columns, so it was the row query's, which Postgres refused ("must appear in the GROUP BY clause").
    seeded(cypher_pm)
    sizes = [["Carrie", 2], ["Hugo", 0], ["Keanu", 4], ["Laurence", 2]]
    assert cypher_pm.query(spec("MATCH (p:Person) RETURN p.name, [(p)-->() | p.name] ORDER BY p.name")) == [
        ["Carrie", ["Carrie", "Carrie"]],
        ["Hugo", []],
        ["Keanu", ["Keanu", "Keanu", "Keanu", "Keanu"]],
        ["Laurence", ["Laurence", "Laurence"]],
    ]
    same_sizes = [
        "MATCH (p:Person) RETURN p.name, size([(p)-->(m) | p]) ORDER BY p.name",
        "MATCH (p:Person) WITH p, 7 AS k RETURN p.name, size([(p)-->() | k]) ORDER BY p.name",
        "MATCH (p:Person) RETURN p.name, size([(p)-->(m) | COUNT { (p)-->() }]) ORDER BY p.name",
        "MATCH (p:Person) CALL (p) { RETURN [(p)-->() | p.name] AS names } RETURN p.name, size(names) ORDER BY p.name",
        "MATCH (p:Person) CALL { WITH p RETURN [(p)-->(m) | p.name] AS names } RETURN p.name, size(names) "
        "ORDER BY p.name",
        "OPTIONAL MATCH (p:Person) RETURN p.name, size([(p)-->() | p.name]) ORDER BY p.name",
        # Its WHERE may read only the row too.
        "MATCH (p:Person) RETURN p.name, size([(p)-->(m) WHERE p.born > 0 OR p.born IS NULL | p.name]) ORDER BY p.name",
        # A name of the pattern's that the body binds again for its own, and reads only so, is not the pattern's.
        "MATCH (p:Person) RETURN p.name, size([(p)-->(m) | [m IN [p.name] | m]]) ORDER BY p.name",
        "MATCH (p:Person) RETURN p.name, size([(p)-->(m) | reduce(m = '', x IN [1] | m + p.name)]) ORDER BY p.name",
        "MATCH (p:Person) RETURN p.name, size([(p)-->(m) | any(m IN [p.name] WHERE m = 'Keanu')]) ORDER BY p.name",
        "MATCH (p:Person) RETURN p.name, size([(p)-[r]->() | p {.name, n: [r IN [1] | r]}]) ORDER BY p.name",
        "MATCH (p:Person) RETURN p.name, size([(p)-->(m) | [(p)-->() | [m IN [p.name] | m]]]) ORDER BY p.name",
        # Its path variable is its own, where the row holds that name too.
        "MATCH (p:Person) WITH p, 1 AS q RETURN p.name, size([q = (p)-->() | length(q)]) ORDER BY p.name",
    ]
    assert differing(cypher_pm, [(it, sizes) for it in same_sizes]) == []
    # One that reads no row, a parameter, is gathered as it was.
    parameter = "MATCH (p:Person) RETURN p.name, size([(p)-->() | $x]) ORDER BY p.name"
    assert cypher_pm.query(spec(parameter, {"x": 5})) == sizes
    cases: list[tuple[str, list[Any]]] = [
        # Read by a relationship of the row, in another comprehension's body, or in a WHERE.
        (
            "MATCH (p:Person)-[r:KNOWS]->(q) RETURN p.name, [(q)-->() | type(r)] ORDER BY p.name",
            [["Keanu", ["KNOWS", "KNOWS"]], ["Laurence", ["KNOWS", "KNOWS", "KNOWS", "KNOWS"]]],
        ),
        (
            "MATCH (p:Person)-[r:KNOWS]->(q) RETURN p.name, size([(q)-->() | r]) ORDER BY p.name",
            [["Keanu", 2], ["Laurence", 4]],
        ),
        (
            "MATCH (p:Person) UNWIND [(p)-->(m) | [(m)-->() | p.name]] AS l WITH p, l WHERE size(l) > 0 "
            "RETURN p.name, size(l) ORDER BY p.name",
            [["Keanu", 2], ["Laurence", 4]],
        ),
        (
            "MATCH (p:Person) WHERE size([(p)-->() | p.name]) > 1 RETURN p.name ORDER BY p.name",
            ["Carrie", "Keanu", "Laurence"],
        ),
        # A UUID made for each match, as it was.
        (
            "MATCH (p:Person) UNWIND [(p)-->(m) | p.name + randomUUID()] AS u RETURN count(u), count(DISTINCT u)",
            [[8, 8]],
        ),
    ]
    assert differing(cypher_pm, cases) == []
    # In a write, which Postgres refused.
    cypher_pm.query(spec("MATCH (p:Person) SET p.knows = size([(p)-->() | p.name])"))
    assert cypher_pm.query(spec("MATCH (p:Person) RETURN p.name, p.knows ORDER BY p.name")) == sizes
