"""
Values as store layout 2 holds them (value_layout): a save's value written by the shared module on each SQL store, its
text spliced into a statement's JSON as it is, and a column read back, the floats JSON would misread kept.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from enum import Enum
from uuid import UUID

import pytest

from drivine.backend.sql.sql_json import to_json
from drivine.backend.sql.value_layout import Held, TagText, held_key, read, read_after, read_value, write, write_props
from drivine.backend.sqlite.sqlite_cypher_values import with_nan
from drivine.drivine_error import DrivineError

DATE_TAG = '{"$t":"date","v":"2024-01-01","s":"10000020240101"}'


class Colour(Enum):
    RED = "red"


@pytest.mark.parametrize("value", [None, 1, "text", True, [1, "a"], {"a": {"b": [2]}}, Colour.RED, 10**30])
def test_a_value_of_nothing_the_layout_holds_otherwise_is_written_as_it_is_with_no_call(value: object):
    written = write(value, "postgres")
    assert not isinstance(written, Held)
    assert written == (value.value if isinstance(value, Enum) else value)


def test_a_date_a_dollar_key_and_bytes_are_written_as_the_module_writes_them_on_each_store():
    for store in ("postgres", "oracle", "sqlite"):
        assert write(date(2024, 1, 1), store) == Held(DATE_TAG)
        assert write({"$k": 1}, store) == Held('{"$$k":1}')
        assert write(b"\x01\x02\xff", store) == Held("[1,2,-1]")


def test_each_float_is_written_as_the_store_holds_it():
    assert write(math.inf, "sqlite") == Held("9e999")
    assert write(-0.0, "sqlite") == Held("-0.0")
    assert write(2.0, "postgres") == Held("2.0")
    # On Oracle every float is extended JSON's double, which Oracle's statements read EXTENDED as a binary double.
    assert write(1.5, "oracle") == Held('{"$numberDouble":"1.5"}')
    assert write(2.0, "oracle") == Held('{"$numberDouble":"2.0"}')


@pytest.mark.parametrize(("store", "value"), [("postgres", math.nan), ("postgres", math.inf), ("sqlite", math.nan)])
def test_a_float_the_store_holds_as_no_number_is_refused_by_name(store: str, value: float):
    with pytest.raises(DrivineError, match="NaN|infinity"):
        write(value, store)  # pyright: ignore[reportArgumentType]


def test_nan_and_the_infinities_are_written_on_oracle_as_the_binary_doubles_it_holds_them_as():
    assert [write(it, "oracle") for it in (math.nan, math.inf, -math.inf)] == [
        Held('{"$numberDouble":"NaN"}'),
        Held('{"$numberDouble":"Infinity"}'),
        Held('{"$numberDouble":"-Infinity"}'),
    ]


def test_an_object_pydantic_writes_is_written_as_its_json_and_a_set_as_a_list():
    uuid = UUID("12345678-1234-5678-1234-567812345678")
    assert write({"id": uuid, "tags": {"a"}}, "postgres") == {"id": str(uuid), "tags": ["a"]}


def test_props_are_written_each_by_its_name_as_the_layout_holds_it():
    props = write_props({"$price": 1, "at": datetime(2024, 1, 1, tzinfo=UTC), "name": "x"}, "postgres")
    assert set(props) == {"$$price", "at", "name"}
    assert isinstance(props["at"], Held) and props["name"] == "x"
    assert held_key("$a") == "$$a" and held_key("a$") == "a$"


def test_a_held_value_is_spliced_into_the_json_as_it_is_and_a_text_like_its_stand_in_is_left_alone():
    held = write({"x": math.inf}, "sqlite")
    stand_in = "\u0000drivine:held:0"
    text = to_json({"before": stand_in, "value": held, "after": [held]})
    assert text == f'{{"before":{json.dumps(stand_in)},"value":{{"x":9e999}},"after":[{{"x":9e999}}]}}'


def test_a_column_is_read_with_its_tags_their_text_and_its_keys_unescaped():
    assert read(f'{{"d":{DATE_TAG},"$$k":1,"t":"$5"}}') == {"d": "2024-01-01", "$k": 1, "t": "$5"}
    assert read('{"plain":1.5}') == {"plain": 1.5}


def test_a_tag_is_read_as_text_that_knows_its_kind_at_any_depth_and_a_users_tag_like_map_as_the_map():
    # The load reads the kind to refuse a NodeId held as a temporal value by a model that declares it text (#560).
    column = read(f'{{"d":{DATE_TAG},"l":[{{"$t":"duration","v":"PT1S"}}],"m":{{"$$t":"date","v":"x"}},"t":"text"}}')
    assert column == {"d": "2024-01-01", "l": ["PT1S"], "m": {"$t": "date", "v": "x"}, "t": "text"}
    assert isinstance(column["d"], TagText) and column["d"].kind == "date"
    assert isinstance(column["l"][0], TagText) and column["l"][0].kind == "duration"
    assert not isinstance(column["m"]["v"], TagText) and not isinstance(column["t"], TagText)
    parsed = read_value({"d": json.loads(DATE_TAG)})
    assert isinstance(parsed["d"], TagText) and parsed["d"].kind == "date"


def test_a_column_the_store_wrote_its_own_way_is_read_so_first_its_floats_kept_across_the_module():
    text = f'{{"n":{{"$numberDouble":"NaN"}},"d":{DATE_TAG},"z":-0.0,"i":9e999,"s":"\\u0000drivine:float:1"}}'
    found = read_after(text, with_nan)
    assert math.isnan(found["n"]) and found["d"] == "2024-01-01" and found["i"] == math.inf
    assert math.copysign(1, found["z"]) < 0
    # A user's text that begins as a stand-in does comes back the text it was.
    assert found["s"] == "\u0000drivine:float:1"
    # A user's map with the store's own key is held escaped, and read back as the user's map.
    assert read_after('{"m":{"$$numberDouble":"NaN"}}', with_nan) == {"m": {"$numberDouble": "NaN"}}


def test_an_oracle_patch_and_patch_all_write_their_values_in_the_layout_as_a_save_does():
    from typing import Annotated

    from drivine import GraphProperty, NodeFragment, NodeId
    from drivine.backend.oracle.oracle_backend import OracleBackend
    from drivine.backend.oracle.oracle_graph import OracleGraph
    from drivine.model.model_registry import ModelRegistry

    class Patched(NodeFragment, labels=["Patched"]):
        id: Annotated[str, NodeId()]
        dollar: Annotated[int | None, GraphProperty("$dollar")] = None
        born: date | None = None

    model = ModelRegistry().fragment(Patched)
    backend = OracleBackend(OracleGraph())
    values = {"$dollar": 7, "born": date(2024, 1, 1)}
    one = backend.patch_one(model).spec({"id": "p1", "set": values, "vectors": {}}).parameters
    assert to_json(one["set"]) == f'{{"$$dollar":7,"born":{DATE_TAG}}}'
    rows = [{"id": "p1", "props": values, "vectors": {}}]
    many = backend.patch_many(model).spec({"rows": rows}).parameters
    assert to_json(many["rows"]) == f'[{{"id":"p1","props":{{"$$dollar":7,"born":{DATE_TAG}}},"vectors":{{}}}}]'


def test_the_object_managers_own_postgres_rows_keep_a_whole_number_past_a_long_exactly():
    from drivine.connection.postgres.postgres_non_finite import read_compiled_json, read_layout_json

    text = f'{{"big":{2**70},"d":{DATE_TAG}}}'
    assert read_layout_json(text) == {"big": 2**70, "d": "2024-01-01"}
    # A compiled statement's reads a number past a long's range as the float nearest it, as drivine4j's does.
    assert isinstance(read_compiled_json('{"big": 1180591620717411303424}')["big"], float)


def test_a_compiled_statements_postgres_column_reads_a_whole_number_past_a_long_alike_with_a_tag_or_without():
    from drivine.connection.postgres.postgres_non_finite import read_compiled_json

    plain = read_compiled_json(f'{{"big":{2**70}}}')
    tagged = read_compiled_json(f'{{"big":{2**70},"$$k":1,"d":{DATE_TAG}}}')
    assert isinstance(tagged["big"], float) and tagged["big"] == plain["big"]
    assert tagged["$k"] == 1 and tagged["d"] == "2024-01-01"


def test_a_value_a_driver_parsed_already_is_read_in_the_layout_and_a_users_own_sql_is_not():
    from drivine.backend.sql.value_layout import read_value
    from drivine.connection.oracle.oracle_result_mapper import OracleResultMapper

    tag = {"$t": "date", "v": "2024-01-01", "s": "10000020240101"}
    assert read_value({"d": tag, "$$k": [1.5, {"$$m": 1}]}) == {"d": "2024-01-01", "$k": [1.5, {"$m": 1}]}
    assert read_value({"plain": [1, "a"]}) == {"plain": [1, "a"]}
    # Oracle's JSON type, which python-oracledb hands over parsed: read in the layout for Drivine's statements alone.
    mapper = OracleResultMapper()
    assert mapper.map_rows([[{"d": tag}]], ["result"], layout=True) == [{"d": "2024-01-01"}]
    assert mapper.map_rows([[{"d": tag}]], ["result"]) == [{"d": tag}]
