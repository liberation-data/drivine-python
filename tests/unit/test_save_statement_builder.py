"""The check of the save `update` makes of what it loaded, where a read takes no snapshot (Neo4j)."""

import os
import subprocess
import sys
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

import pytest
from pydantic import Field

from drivine import (
    EmptyWhenAbsent,
    JsonPacked,
    ModelRegistry,
    NodeFragment,
    NodeId,
    NodeLabels,
    NodeStamp,
    NullPolicy,
    PropertyBag,
    SaveOptions,
    StaleObjectError,
    VectorIndex,
)
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from drivine.backend.save_statement_builder import SaveStatementBuilder, stored

STAMP = "0123456789abcdef:fedcba9876543210"


class Claim(NodeFragment, labels=["Claim"]):
    id: Annotated[str, NodeId()]
    text: str | None = None
    note: str | None = None
    status: str = "open"
    stamp: Annotated[str | None, NodeStamp()] = None


class Tagged(NodeFragment, labels=["Tagged"]):
    id: Annotated[str, NodeId()]
    text: str | None = None
    meta: Annotated[dict[str, Any], PropertyBag()] = {}
    stamp: Annotated[str | None, NodeStamp()] = None


class Badge(NodeFragment, labels=["Badge"]):
    """A flat bag and an open set of labels beside a stamp."""

    id: Annotated[str, NodeId()]
    labels: Annotated[list[str], NodeLabels()] = []
    extras: Annotated[dict[str, Any], PropertyBag(flat=True)] = {}
    stamp: Annotated[str | None, NodeStamp()] = None


class Shade(StrEnum):
    RED = "Red"
    GREEN = "Green"
    BLUE = "Blue"


class Swatch(NodeFragment, labels=["Swatch"]):
    """A closed set of labels: the field speaks for the Shade labels and no other."""

    id: Annotated[str, NodeId()]
    shades: Annotated[set[Shade], NodeLabels(Shade)] = set()
    stamp: Annotated[str | None, NodeStamp()] = None


class Note(NodeFragment, labels=["Note"]):
    """Fields whose value a load gives a node that holds no property for them: a default made of another field, an
    empty collection, a packed default."""

    id: Annotated[str, NodeId()]
    title: str
    slug: str = Field(default_factory=lambda data: data["title"].lower())
    tags: Annotated[set[str], EmptyWhenAbsent()]
    extra: Annotated[dict[str, Any], JsonPacked()] = {}
    embedding: Annotated[list[float] | None, VectorIndex(dimensions=2)] = None
    stamp: Annotated[str | None, NodeStamp()] = None


@pytest.fixture
def registry() -> ModelRegistry:
    return ModelRegistry()


def check_of_change(
    registry: ModelRegistry, loaded: Any, changed: Any, grammar: CypherGrammar = NEO4J_GRAMMAR
) -> tuple[str, dict[str, Any]]:
    """The check of the save of `changed`, as update saves it with `loaded`: the root's `_ok`, and what it binds."""
    options = SaveOptions(null_policy=NullPolicy.CLEAR, loaded=loaded)
    statement = SaveStatementBuilder(grammar, registry).build(registry.fragment(type(loaded)), changed, options)
    check = next(it for it in statement.text.split("\n") if it.endswith(" AS _ok"))
    return check, dict(statement.bindings)


def claim(text: str, **fields: Any) -> Claim:
    return Claim(id="c1", text=text, stamp=STAMP, **fields)


def test_matches_the_node_only_while_each_field_is_as_it_was_loaded(registry):
    check, bindings = check_of_change(registry, claim("one"), claim("two"))

    assert check == (
        "WITH n, coalesce(left(n.`__drivine.stamp`, 16) = left($_expected, 16) AND n.note IS NULL AND "
        "NOT coalesce(n.status <> $_loaded1, false) AND "
        "n.text IS NOT NULL AND NOT coalesce(n.text <> $_loaded2, false), false) AS _ok"
    )
    assert (bindings["_loaded2"], bindings["_loaded1"]) == ("one", "open"), "the text as loaded, and not as changed"
    assert check_of_change(registry, claim("a longer text"), claim("two"))[0] == check, "the values are bound"


def test_holds_a_field_to_what_it_was_loaded_with_a_value_a_load_gives_an_absent_property_aside(registry):
    check, _ = check_of_change(registry, claim("one", status="closed"), claim("two"))

    assert "n.status IS NOT NULL AND NOT coalesce(n.status <> $_loaded1, false)" in check


@pytest.mark.parametrize("grammar", [MEMGRAPH_GRAMMAR, FALKORDB_GRAMMAR])
def test_compares_the_stamp_alone_where_a_read_takes_a_snapshot(registry, grammar):
    check, bindings = check_of_change(registry, claim("one"), claim("two"), grammar)

    assert "_loaded" not in check
    assert [it for it in bindings if it.startswith("_loaded")] == []


def test_compares_the_stamp_alone_for_an_object_update_did_not_load(registry):
    statement = SaveStatementBuilder(NEO4J_GRAMMAR, registry).build(registry.fragment(Claim), claim("two"))

    assert "_loaded" not in statement.text


def test_holds_a_prefixed_bag_to_the_keys_and_values_it_was_loaded_with(registry):
    loaded = Tagged(id="t1", text="one", meta={"owner": "ada"}, stamp=STAMP)
    check, bindings = check_of_change(registry, loaded, loaded.model_copy(update={"text": "two"}))

    assert (
        "size([k IN keys(n) WHERE k STARTS WITH $_loadedBag0Prefix]) = size(keys($_loadedBag0)) AND "
        "all(k IN keys($_loadedBag0) WHERE n[k] IS NOT NULL AND NOT coalesce(n[k] <> $_loadedBag0[k], false))"
    ) in check
    assert bindings["_loadedBag0"] == {"meta.owner": "ada"}, "keyed as the node stores them"
    assert bindings["_loadedBag0Prefix"] == "meta."


def test_holds_a_flat_bag_and_an_open_set_of_labels_to_what_they_were_loaded_with(registry):
    loaded = Badge(id="b1", labels=["Shiny"], extras={"size": 1}, stamp=STAMP)
    check, bindings = check_of_change(registry, loaded, loaded.model_copy(update={"extras": {"size": 2}}))

    assert (
        "size([k IN keys(n) WHERE NOT k IN $_loadedBag0Reserved AND "
        "none(p IN $_loadedBag0ReservedPrefixes WHERE k STARTS WITH p)]) = size(keys($_loadedBag0))"
    ) in check
    assert bindings["_loadedBag0"] == {"size": 1}
    assert bindings["_loadedBag0Reserved"] == ["id"], "the properties a load hands to fields"
    assert bindings["_loadedBag0ReservedPrefixes"] == ["__drivine."]
    assert (
        "size([l IN labels(n) WHERE NOT l IN $_loadedLabelsOwn]) = size($_loadedLabels) AND "
        "all(l IN $_loadedLabels WHERE l IN labels(n))"
    ) in check
    assert (bindings["_loadedLabels"], bindings["_loadedLabelsOwn"]) == (["Shiny"], ["Badge"])


def test_holds_a_closed_set_of_labels_to_its_own_members_alone(registry):
    loaded = Swatch(id="w1", shades={Shade.BLUE}, stamp=STAMP)
    check, bindings = check_of_change(registry, loaded, loaded.model_copy(update={"shades": {Shade.RED}}))

    assert "size([l IN labels(n) WHERE l IN $_loadedLabelsClosed]) = size($_loadedLabels)" in check
    assert set(bindings["_loadedLabelsClosed"]) == {"Red", "Green", "Blue"}
    assert bindings["_loadedLabels"] == ["Blue"]


def test_lets_a_field_loaded_as_a_load_gives_an_absent_property_be_held_as_none(registry):
    loaded = Note(id="n1", title="Ada", tags=set(), stamp=STAMP)
    check, bindings = check_of_change(registry, loaded, loaded.model_copy(update={"title": "Grace"}))

    assert "n.embedding IS NULL AND NOT coalesce(n.extra <> $_loaded1, false)" in check, "the packed default"
    assert "AND NOT coalesce(n.slug <> $_loaded2, false)" in check, "the default made of the title as loaded"
    assert f"AND {as_set('n.tags', '$_loaded3')} AND" in check, "the empty collection of EmptyWhenAbsent"
    assert "n.title IS NOT NULL AND NOT coalesce(n.title <> $_loaded4, false)" in check
    assert (bindings["_loaded1"], bindings["_loaded2"], bindings["_loaded3"]) == ("{}", "ada", [])


def as_set(stored: str, loaded: str) -> str:
    """A set field's comparison: each item of the stored list loaded, and each loaded item stored."""
    return (
        f"{stored} IS :: LIST<ANY> AND NOT coalesce(NOT (all(it IN {stored} WHERE it IN {loaded}) AND "
        f"all(it IN {loaded} WHERE it IN {stored})), false)"
    )


def test_compares_a_set_field_as_a_set_and_a_list_or_tuple_item_by_item(registry):
    loaded = Note(id="n1", title="Ada", tags={"zeta", "alpha", "mid"}, stamp=STAMP)
    check, bindings = check_of_change(registry, loaded, loaded.model_copy(update={"title": "Grace"}))

    assert f"n.tags IS NOT NULL AND {as_set('n.tags', '$_loaded3')}" in check
    assert "n.tags <>" not in check
    assert bindings["_loaded3"] == ["alpha", "mid", "zeta"]

    # A set field loaded without validation holds the list itself (drivine-python#129), which is compared as it is.
    listed = Note.model_construct(id="n1", title="Ada", slug="ada", tags=["b", "a", "b"], extra={}, stamp=STAMP)
    check, bindings = check_of_change(registry, listed, listed.model_copy(update={"title": "Grace"}))
    assert "n.tags IS NOT NULL AND NOT coalesce(n.tags <> $_loaded3, false)" in check
    assert bindings["_loaded3"] == ["b", "a", "b"]


def test_compares_a_packed_field_as_stored_and_a_vector_in_the_engines_form(registry):
    loaded = Note(id="n1", title="Ada", slug="custom", tags={"x"}, extra={"a": 1}, embedding=[0.5, 1.0], stamp=STAMP)
    check, bindings = check_of_change(registry, loaded, loaded.model_copy(update={"title": "Grace"}))

    assert "n.slug IS NOT NULL AND NOT coalesce(n.slug <> $_loaded2, false)" in check
    assert "n.extra IS NOT NULL AND NOT coalesce(n.extra <> $_loaded1, false)" in check
    assert f"n.embedding IS NOT NULL AND NOT coalesce(n.embedding <> {NEO4J_GRAMMAR.vector_value('$_loaded0')}" in check
    assert (bindings["_loaded0"], bindings["_loaded1"], bindings["_loaded3"]) == ([0.5, 1.0], '{"a": 1}', ["x"])


class Logged(NodeFragment, labels=["Logged"]):
    id: Annotated[str, NodeId()]
    at: datetime
    meta: Annotated[dict[str, Any], PropertyBag()] = {}
    stamp: Annotated[str | None, NodeStamp()] = None


def test_binds_a_datetime_the_driver_handed_back_in_utc_as_one_no_comparison_tells_apart(registry):
    pytz = pytest.importorskip("pytz")
    # The driver hands an offset of zero back as the zone UTC, and binds it as the zone: Neo4j holds that unequal to
    # the offset a datetime may have been saved with.
    utc = datetime(2026, 10, 23, 9, 30, tzinfo=pytz.FixedOffset(0))
    eastern = datetime(2026, 10, 23, 9, 30, tzinfo=pytz.FixedOffset(600))
    loaded = Logged(id="l1", at=utc, meta={"seen": utc, "since": eastern}, stamp=STAMP)
    check, bindings = check_of_change(registry, loaded, loaded.model_copy(update={"meta": {}}))

    assert "n.at IS NOT NULL AND NOT coalesce(n.at <> $_loaded0, false)" in check
    assert bindings["_loaded0"] is None
    assert bindings["_loadedBag0"] == {"meta.seen": None, "meta.since": eastern}, "an offset other than zero is kept"


def test_says_a_field_is_not_as_loaded_when_a_save_is_refused_under_the_stamp_it_expected():
    refused = StaleObjectError(Claim, "c1", STAMP, STAMP, False)
    assert str(refused) == (
        f"Claim 'c1' changed since it was loaded, under the stamp {STAMP}: a field is not as it was loaded."
    )


def test_compares_a_packed_field_as_the_text_the_load_read_and_packs_one_the_node_held_none_for(registry):
    loaded = Note(id="n1", title="T", tags=set(), extra={"a": 1}, stamp=STAMP)
    changed = loaded.model_copy(update={"title": "U"})

    def packed(loaded_properties: dict[str, Any]) -> list[Any]:
        options = SaveOptions(null_policy=NullPolicy.CLEAR, loaded=loaded, loaded_properties=loaded_properties)
        statement = SaveStatementBuilder(NEO4J_GRAMMAR, registry).build(registry.fragment(Note), changed, options)
        return [value for key, value in statement.bindings.items() if key.startswith("_loaded") and "a" in str(value)]

    assert packed({"id": "n1", "extra": '{"a":1}'}) == ['{"a":1}'], "as another writer packed it"
    assert packed({"id": "n1"}) == ['{"a": 1}']
    assert packed({"id": "n1", "extra": ["a"]}) == [["a"]], "held as other than text"


def test_stores_a_set_as_the_same_sorted_list_in_every_process_and_one_that_wont_sort_as_it_iterates():
    # A string's hash, which orders a set, differs in each process: each is given another seed.
    script = (
        "from drivine.backend.save_statement_builder import stored; "
        "print(stored({'zeta', 'alpha', 'mid', 'beta', 'omega', 'kappa', 'nu'}))"
    )
    written = {
        subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in (0, 1, 2, 3, 5, 42)
    }
    assert written == {"['alpha', 'beta', 'kappa', 'mid', 'nu', 'omega', 'zeta']\n"}

    assert stored(frozenset({Shade.RED, Shade.BLUE})) == ["Blue", "Red"], "sorted as stored, an Enum as its value"
    assert sorted(stored({"a", 1}), key=str) == [1, "a"], "items that don't order are stored as the set iterates"
    assert stored(("b", "a")) == ["b", "a"], "a tuple keeps its order"
