"""The check of the save `update` makes of what it loaded, where a read takes no snapshot (Neo4j)."""

from enum import StrEnum
from typing import Annotated, Any

import pytest

from drivine import ModelRegistry, NodeFragment, NodeId, NodeLabels, NodeStamp, NullPolicy, PropertyBag, SaveOptions
from drivine.backend.cypher_grammar import FALKORDB_GRAMMAR, MEMGRAPH_GRAMMAR, NEO4J_GRAMMAR, CypherGrammar
from drivine.backend.save_statement_builder import SaveStatementBuilder

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
