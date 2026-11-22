"""
A time with an offset is sent to the Bolt engines as each takes it (#565): on Neo4j with a fixed offset the driver can
pack, as a Time of that offset; on Memgraph, which has no Time, as text of one width, as drivine4j writes it. A naive
time is left as it is. The integration tests' OffsetTimes saves and binds one on each engine.
"""

import pickle
import uuid
from datetime import UTC, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from neo4j._codec.hydration.bolt.v1.temporal import dehydrate_time, hydrate_time  # pyright: ignore[reportMissingTypeStubs]

from drivine import QuerySpecification
from drivine.connection.database_type import DatabaseType
from drivine.connection.falkordb.falkordb_parameter_coercer import FalkorDbParameterCoercer
from drivine.connection.neo4j.bolt_values import FixedOffset, bolt_parameters
from drivine.connection.neo4j.neo4j_connection import Neo4jConnection
from drivine.connection.neo4j.neo4j_connection_provider import Neo4jConnectionProvider
from drivine.connection.neo4j.neo4j_spec_compiler import Neo4jSpecCompiler
from drivine.sync.connection.neo4j.neo4j_connection import Neo4jConnection as SyncNeo4jConnection
from drivine.sync.connection.neo4j.neo4j_connection_provider import (
    Neo4jConnectionProvider as SyncNeo4jConnectionProvider,
)

PLUS_TWO = timezone(timedelta(hours=2))
STARTS = time(10, 11, 12, 500000, tzinfo=PLUS_TWO)
NAIVE = time(10, 11, 12)


def test_the_driver_cannot_pack_a_time_whose_zone_is_the_standard_librarys():
    try:
        dehydrate_time(STARTS)
    except TypeError as raised:
        assert "utcoffset" in str(raised)
    else:
        raise AssertionError("neo4j packs a time of a datetime.timezone now: bolt_values may no longer be needed")


def test_on_neo4j_a_time_with_an_offset_is_given_one_the_driver_packs_as_a_time_of_that_offset():
    sent = bolt_parameters({"t": STARTS}, DatabaseType.NEO4J)["t"]
    assert isinstance(sent, time)
    assert sent == STARTS
    assert sent.utcoffset() == timedelta(hours=2)
    assert isinstance(sent.tzinfo, FixedOffset)
    packed = dehydrate_time(sent)
    assert packed.tag == b"T"
    assert list(packed.fields) == [(10 * 3600 + 11 * 60 + 12) * 10**9 + 500000 * 1000, 7200]


def test_the_time_the_driver_packed_reads_back_as_the_time_with_its_offset():
    packed = dehydrate_time(bolt_parameters({"t": STARTS}, DatabaseType.NEO4J)["t"])
    read = hydrate_time(*packed.fields).to_native()
    assert read == STARTS
    assert read.utcoffset() == timedelta(hours=2)


def test_on_memgraph_a_time_with_an_offset_is_sent_as_drivine4js_fixed_width_text():
    # Seconds always, nine digits of the second, `Z` for no offset: text that sorts as time, as drivine4j writes it.
    assert bolt_parameters({"t": STARTS}, DatabaseType.MEMGRAPH) == {"t": "10:11:12.500000000+02:00"}
    utc = time(10, 11, 12, tzinfo=UTC)
    assert bolt_parameters({"t": utc}, DatabaseType.MEMGRAPH) == {"t": "10:11:12.000000000Z"}
    minute = time(10, 11, tzinfo=timezone(-timedelta(hours=5, minutes=30)))
    assert bolt_parameters({"t": minute}, DatabaseType.MEMGRAPH) == {"t": "10:11:00.000000000-05:30"}


def test_a_naive_time_is_left_as_it_is_on_each_engine():
    for engine in (DatabaseType.NEO4J, DatabaseType.MEMGRAPH):
        assert bolt_parameters({"t": NAIVE}, engine)["t"] is NAIVE


def test_a_time_whose_zone_gives_no_offset_is_naive_as_python_counts_it():
    named = time(10, 11, 12, tzinfo=ZoneInfo("Europe/Berlin"))
    assert named.utcoffset() is None
    for engine in (DatabaseType.NEO4J, DatabaseType.MEMGRAPH):
        assert bolt_parameters({"t": named}, engine)["t"] == NAIVE
        assert bolt_parameters({"t": named}, engine)["t"].tzinfo is None


def test_a_time_in_a_list_or_a_map_is_made_so_too():
    given = {"props": {"starts": STARTS, "times": [STARTS, NAIVE], "name": "s"}, "ts": (STARTS,)}
    assert bolt_parameters(given, DatabaseType.MEMGRAPH) == {
        "props": {"starts": "10:11:12.500000000+02:00", "times": ["10:11:12.500000000+02:00", NAIVE], "name": "s"},
        "ts": ["10:11:12.500000000+02:00"],
    }
    on_neo4j = bolt_parameters(given, DatabaseType.NEO4J)
    assert isinstance(on_neo4j["props"]["times"][0].tzinfo, FixedOffset)
    assert on_neo4j["props"]["times"][1] is NAIVE


def test_parameters_without_a_time_are_given_back_as_they_are():
    given = {"a": [1, {"b": "c"}], "d": None}
    assert bolt_parameters(given, DatabaseType.MEMGRAPH) is given


def test_the_fixed_offset_compares_hashes_and_pickles_by_its_offset():
    offset = FixedOffset(timedelta(hours=2))
    assert offset == FixedOffset(timedelta(hours=2)) != FixedOffset(timedelta(hours=1))
    assert hash(offset) == hash(FixedOffset(timedelta(hours=2)))
    assert pickle.loads(pickle.dumps(offset)) == offset
    assert offset.utcoffset(None) == offset.utcoffset(NAIVE) == timedelta(hours=2)


def test_the_spec_compiler_makes_named_and_positional_parameters_so_for_its_engine():
    named = QuerySpecification("RETURN $t").bind({"t": STARTS}).finalized_copy("CYPHER")
    assert Neo4jSpecCompiler(named, DatabaseType.MEMGRAPH).compile().parameters == {"t": "10:11:12.500000000+02:00"}
    positional = QuerySpecification("RETURN $1").bind([STARTS]).finalized_copy("CYPHER")
    assert isinstance(Neo4jSpecCompiler(positional).compile().parameters["1"].tzinfo, FixedOffset)


async def test_a_connection_is_given_its_providers_engine_so_memgraph_is_sent_the_text():
    provider = Neo4jConnectionProvider("mg", DatabaseType.MEMGRAPH, "bolt://localhost:7687", None)
    sync_provider = SyncNeo4jConnectionProvider("mg", DatabaseType.MEMGRAPH, "bolt://localhost:7687", None)
    try:
        connection = await provider.connect()
        sync_connection = sync_provider.connect()
        assert isinstance(connection, Neo4jConnection)
        assert isinstance(sync_connection, SyncNeo4jConnection)
        assert connection.engine is sync_connection.engine is DatabaseType.MEMGRAPH
    finally:
        await provider.end()
        sync_provider.end()


@pytest.mark.parametrize("engine", [DatabaseType.NEO4J, DatabaseType.MEMGRAPH])
def test_a_uuid_is_sent_as_its_text_in_lists_and_maps_too_as_the_other_libraries_hold_one(engine: DatabaseType):
    # The driver sends a UUID only over Bolt 6.1, and Neo4j's aligned store format holds none (#583).
    key = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
    text = "12345678-9abc-def0-1234-56789abcdef0"
    assert bolt_parameters({"id": key, "ids": [key], "map": {"k": key}, "n": 1}, engine) == {
        "id": text,
        "ids": [text],
        "map": {"k": text},
        "n": 1,
    }


def test_falkordb_is_sent_a_uuid_as_its_text_where_it_refused_one():
    key = uuid.UUID(int=5)
    # A save's rows are lists of maps.
    assert FalkorDbParameterCoercer().coerce({"id": key, "ids": [key], "rows": [{"props": {"k": key}}]}) == {
        "id": str(key),
        "ids": [str(key)],
        "rows": [{"props": {"k": str(key)}}],
    }


def test_memgraph_is_sent_a_uuid_as_its_text_beside_a_time_it_sends_as_text():
    key = uuid.UUID(int=5)
    at = time(10, 11, 12, tzinfo=timezone(timedelta(hours=2)))
    assert bolt_parameters({"rows": [{"id": key, "at": at}]}, DatabaseType.MEMGRAPH) == {
        "rows": [{"id": str(key), "at": "10:11:12.000000000+02:00"}]
    }
