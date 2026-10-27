"""
A set or tuple field loads as its set or tuple, though the store holds a list: whether its fragment is validated on load
or constructed without validation.
"""

from typing import Annotated, Any

import pytest

from drivine import EmptyWhenAbsent, GraphView, JsonPacked, NodeFragment, NodeId, Relationship, Root
from drivine.mapper.graph_object_mapper import GraphObjectMapper
from drivine.model.model_registry import ModelRegistry


class Checked(NodeFragment, labels=["Checked"]):
    id: Annotated[str, NodeId()]
    codes: set[str] = set()
    pair: tuple[str, ...] = ()
    maybe_codes: set[str] | None = None
    always_codes: Annotated[set[str], EmptyWhenAbsent()] = set()
    always_pair: Annotated[tuple[str, ...], EmptyWhenAbsent()] = ()
    packed_codes: Annotated[set[str], JsonPacked()] = set()
    names: list[str] = []


class Unchecked(NodeFragment, labels=["Unchecked"], validate_on_load=False):
    id: Annotated[str, NodeId()]
    codes: set[str] = set()
    pair: tuple[str, ...] = ()
    maybe_codes: set[str] | None = None
    always_codes: Annotated[set[str], EmptyWhenAbsent()] = set()
    always_pair: Annotated[tuple[str, ...], EmptyWhenAbsent()] = ()
    packed_codes: Annotated[set[str], JsonPacked()] = set()
    names: list[str] = []


class UncheckedHolder(GraphView):
    root: Annotated[Unchecked, Root()]
    held: Annotated[list[Unchecked], Relationship("HOLDS")] = []


FRAGMENTS = [pytest.param(Checked, id="validated"), pytest.param(Unchecked, id="unvalidated")]


def load(type_: type[NodeFragment], props: dict[str, Any]) -> Any:
    registry = ModelRegistry()
    model = registry.fragment(type_)
    return GraphObjectMapper(registry).fragment(model, {"props": props, "labels": model.labels})


@pytest.mark.parametrize("type_", FRAGMENTS)
class TestFragment:
    def test_a_stored_list_loads_as_a_set_and_a_tuple(self, type_: type[NodeFragment]):
        loaded = load(
            type_,
            {
                "id": "1",
                "codes": ["x", "y", "x"],
                "pair": ["a", "b"],
                "maybeCodes": ["m"],
                "alwaysCodes": ["p", "q"],
                "alwaysPair": ["c", "d"],
                "packedCodes": '["j", "k"]',
                "names": ["n", "n"],
            },
        )
        assert type(loaded.codes) is set and loaded.codes == {"x", "y"}
        assert type(loaded.pair) is tuple and loaded.pair == ("a", "b")
        assert type(loaded.maybe_codes) is set and loaded.maybe_codes == {"m"}
        assert type(loaded.always_codes) is set and loaded.always_codes == {"p", "q"}
        assert type(loaded.always_pair) is tuple and loaded.always_pair == ("c", "d")
        assert type(loaded.packed_codes) is set and loaded.packed_codes == {"j", "k"}
        assert type(loaded.names) is list and loaded.names == ["n", "n"]

    def test_an_empty_stored_list_loads_as_an_empty_set_and_tuple(self, type_: type[NodeFragment]):
        loaded = load(
            type_,
            {"id": "1", "codes": [], "pair": [], "maybeCodes": [], "alwaysCodes": [], "alwaysPair": []},
        )
        assert type(loaded.codes) is set and loaded.codes == set()
        assert type(loaded.pair) is tuple and loaded.pair == ()
        assert type(loaded.maybe_codes) is set and loaded.maybe_codes == set()
        assert type(loaded.always_codes) is set and loaded.always_codes == set()
        assert type(loaded.always_pair) is tuple and loaded.always_pair == ()

    def test_an_empty_map_a_set_was_once_held_as_loads_as_an_empty_set(self, type_: type[NodeFragment]):
        loaded = load(type_, {"id": "1", "codes": {}, "alwaysCodes": {}})
        assert type(loaded.codes) is set and loaded.codes == set()
        assert type(loaded.always_codes) is set and loaded.always_codes == set()

    def test_an_absent_field_keeps_its_default_and_an_empty_when_absent_one_is_empty(self, type_: type[NodeFragment]):
        loaded = load(type_, {"id": "1"})
        assert loaded.codes == set() and loaded.pair == () and loaded.maybe_codes is None
        assert type(loaded.always_codes) is set and loaded.always_codes == set()
        assert type(loaded.always_pair) is tuple and loaded.always_pair == ()

    def test_a_stored_null_stays_null_unless_empty_when_absent(self, type_: type[NodeFragment]):
        loaded = load(type_, {"id": "1", "maybeCodes": None, "alwaysCodes": None, "alwaysPair": None})
        assert loaded.maybe_codes is None
        assert type(loaded.always_codes) is set and loaded.always_codes == set()
        assert type(loaded.always_pair) is tuple and loaded.always_pair == ()


def test_the_fragments_a_view_holds_load_their_sets_and_tuples():
    registry = ModelRegistry()
    view = GraphObjectMapper(registry).view(
        registry.view(UncheckedHolder),
        {
            "root": {"props": {"id": "1", "codes": ["x"], "pair": ["a"]}, "labels": ["Unchecked"]},
            "rels": {"held": [{"props": {"id": "2", "codes": ["y"], "pair": ["b"]}, "labels": ["Unchecked"]}]},
        },
    )
    assert type(view.root.codes) is set and view.root.codes == {"x"}
    assert type(view.root.pair) is tuple and view.root.pair == ("a",)
    assert type(view.held[0].codes) is set and view.held[0].codes == {"y"}
    assert type(view.held[0].pair) is tuple and view.held[0].pair == ("b",)


def test_the_registry_records_the_set_and_tuple_fields():
    model = ModelRegistry().fragment(Unchecked)
    assert model.collections == {
        "codes": "set",
        "pair": "tuple",
        "maybe_codes": "set",
        "always_codes": "set",
        "always_pair": "tuple",
        "packed_codes": "set",
    }
