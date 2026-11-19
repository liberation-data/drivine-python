"""
A time with an offset is sent to the Bolt engines as each takes it (#565): on Neo4j with a fixed offset the driver can
pack, as a Time of that offset; on Memgraph, which has no Time, as its ISO text. A naive time is left as it is. The
integration tests' OffsetTimes saves and binds one on each engine.
"""

import pickle
from datetime import UTC, time, timedelta, timezone
from zoneinfo import ZoneInfo

from neo4j._codec.hydration.bolt.v1.temporal import dehydrate_time, hydrate_time  # pyright: ignore[reportMissingTypeStubs]

from drivine import QuerySpecification
from drivine.connection.database_type import DatabaseType
from drivine.connection.neo4j.bolt_values import FixedOffset, bolt_parameters
from drivine.connection.neo4j.neo4j_spec_compiler import Neo4jSpecCompiler

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
