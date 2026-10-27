"""
A set or tuple field loads as its set or tuple, though the store holds a list: whether its fragment is validated on load
or constructed without validation.
"""

import typing
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Annotated, Any

import pytest
from pydantic import ValidationError

from drivine import (
    EmptyWhenAbsent,
    GraphView,
    JsonPacked,
    NodeFragment,
    NodeId,
    Relationship,
    RelationshipFragment,
    Root,
    TargetNode,
)
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


class Tagged(RelationshipFragment):
    tags: set[str] = set()
    path: tuple[str, ...] = ()
    target: Annotated[Unchecked, TargetNode()]


class TaggedHolder(GraphView):
    root: Annotated[Unchecked, Root()]
    tagged: Annotated[list[Tagged], Relationship("TAGGED")] = []


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
        loaded = load(type_, {"id": "1", "codes": {}, "alwaysCodes": {}, "packedCodes": "{}"})
        assert type(loaded.codes) is set and loaded.codes == set()
        assert type(loaded.always_codes) is set and loaded.always_codes == set()
        assert type(loaded.packed_codes) is set and loaded.packed_codes == set()

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


class CheckedPairs(NodeFragment, labels=["CheckedPairs"]):
    id: Annotated[str, NodeId()]
    pairs: Annotated[set[tuple[str, str]], JsonPacked()] = set()
    duo: tuple[str, int] = ("", 0)


class UncheckedPairs(NodeFragment, labels=["UncheckedPairs"], validate_on_load=False):
    id: Annotated[str, NodeId()]
    pairs: Annotated[set[tuple[str, str]], JsonPacked()] = set()
    rows: Annotated[tuple[list[str], ...], JsonPacked()] = ()


def test_a_validated_set_of_tuples_is_left_to_pydantic_to_build():
    loaded = load(CheckedPairs, {"id": "1", "pairs": '[["a", "b"]]'})
    assert loaded.pairs == {("a", "b")}


def test_a_validated_fixed_arity_tuple_still_checks_its_arity():
    with pytest.raises(ValidationError):
        load(CheckedPairs, {"id": "1", "duo": ["a", 1, 2]})


def test_an_unvalidated_set_of_unhashable_items_loads_as_the_stored_list():
    loaded = load(UncheckedPairs, {"id": "1", "pairs": '[["a", "b"]]'})
    assert loaded.pairs == [["a", "b"]]


def test_an_unvalidated_tuple_of_unhashable_items_still_loads_as_a_tuple():
    loaded = load(UncheckedPairs, {"id": "1", "rows": '[["a", "b"], ["c"]]'})
    assert type(loaded.rows) is tuple and loaded.rows == (["a", "b"], ["c"])


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


def test_a_relationship_fragment_and_its_unvalidated_target_load_their_sets_and_tuples():
    registry = ModelRegistry()
    target = {"props": {"id": "2", "codes": ["y"], "pair": ["b"]}, "labels": ["Unchecked"]}
    view = GraphObjectMapper(registry).view(
        registry.view(TaggedHolder),
        {
            "root": {"props": {"id": "1"}, "labels": ["Unchecked"]},
            "rels": {
                "tagged": [
                    {"rel": {"tags": ["t", "t"], "path": ["p", "q"]}, "target": target},
                    {"rel": {"tags": {}}, "target": target},
                ]
            },
        },
    )
    full, legacy = view.tagged
    assert type(full.tags) is set and full.tags == {"t"}
    assert type(full.path) is tuple and full.path == ("p", "q")
    assert type(legacy.tags) is set and legacy.tags == set()
    assert type(full.target.codes) is set and full.target.codes == {"y"}
    assert type(full.target.pair) is tuple and full.target.pair == ("b",)


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


class Spelled(NodeFragment, labels=["Spelled"], validate_on_load=False):
    id: Annotated[str, NodeId()]
    old_set: typing.Set[str] = set()  # noqa: UP006
    old_tuple: typing.Tuple[str, ...] = ()  # noqa: UP006
    abstract: AbstractSet[str] = frozenset()
    sequence: Sequence[str] = []
    nested: list[set[str]] = []
    keyed: dict[str, set[str]] = {}


def test_the_registry_records_every_spelling_of_a_set_or_tuple_field_and_no_other():
    model = ModelRegistry().fragment(Spelled)
    assert model.collections == {"old_set": "set", "old_tuple": "tuple", "abstract": "set"}
    loaded = load(Spelled, {"id": "1", "oldSet": ["a"], "oldTuple": ["b"], "abstract": ["c"], "sequence": ["d"]})
    assert type(loaded.old_set) is set and loaded.old_set == {"a"}
    assert type(loaded.old_tuple) is tuple and loaded.old_tuple == ("b",)
    assert type(loaded.abstract) is set and loaded.abstract == {"c"}
    assert type(loaded.sequence) is list and loaded.sequence == ["d"]
