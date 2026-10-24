# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_label_text.py:
# change that, and run the script again.
"""
drivine4j#198: a label after the first is looked for by its text, among the labels as `JSON_SERIALIZE` writes them
(see `holds_label`). It is found whole, whatever characters it holds, and never inside another label.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from drivine.backend.oracle.oracle_graph import has_labels, holds_label, serialized_label
from drivine.query.statement import cypher_statement
from drivine.sync import PersistenceManager, PersistenceManagerFactory, QuerySpecification
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE, OracleSeed

SCOPE = "OraLabelText"

EDGES = ["", '"', "[", "]", "\\", "a\\", ',"', '","', "\u00e9 \U0001f600", "$x"]
"""Labels that try the edges of the text a label is looked for in: brackets, quotes, commas and backslashes."""

UNSCOPED = "DELETE FROM drivine_node WHERE JSON_VALUE(props, '$.id') IN ('labeltext-single', 'labeltext-none')"
"""Removes the nodes of one label and of none, which can't carry the suite's label."""


@pytest.fixture(scope="module")
def labelled(factory: PersistenceManagerFactory) -> Iterator[None]:
    seed = OracleSeed(factory.get("oracle"), SCOPE)
    seed.clear()
    seed.node(["Crafted", 'x","Dog'], {"id": "crafted"})
    seed.node(["Odd", "a\tb", "c\nd", "e\u0001f", "g\u2028h", "i\u007fj"], {"id": "odd"})
    seed.node(["Comma", "a,", "b"], {"id": "comma"})
    seed.node(["Comma", "a[", ",b"], {"id": "bracket"})
    seed.node(["Comma", "x", ","], {"id": "lone"})
    seed.node(["Edge", *EDGES], {"id": "edge"})
    seed.pm.execute(QuerySpecification[Any](UNSCOPED))
    for labels, id in [('["Single"]', "single"), ("[]", "none")]:
        seed.pm.execute(
            QuerySpecification[Any](
                "INSERT INTO drivine_node (labels, props) "
                f"VALUES (JSON('{labels}'), JSON('{{\"id\":\"labeltext-{id}\"}}'))"
            )
        )
    yield
    seed.clear()
    seed.pm.execute(QuerySpecification[Any](UNSCOPED))


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("labelled")]


def carrying(pm: PersistenceManager, *labels: str) -> set[str]:
    """The ids of this suite's nodes that carry every one of the labels, as the backend tests them."""
    rows = pm.query(
        QuerySpecification[str](
            f"SELECT JSON_VALUE(n.props, '$.id') FROM drivine_node n WHERE {has_labels('n', [SCOPE])} "
            f"AND {has_labels('n', list(labels))}"
        )
    )
    return set(rows)


def matching(pm: PersistenceManager, *labels: str) -> set[str]:
    """The ids of this suite's nodes a compiled Cypher `MATCH` of the labels finds."""
    pattern = "".join(f":`{it.replace('`', '``')}`" for it in labels)
    rows = pm.query(QuerySpecification[Any](cypher_statement(f"MATCH (n{pattern}) WHERE n:{SCOPE} RETURN n.id")))
    return set(rows)


def test_takes_a_label_holding_a_quoted_comma_for_one_label_not_two(pm: PersistenceManager):
    assert carrying(pm, "Crafted", "Dog") == set()
    assert matching(pm, "Crafted", "Dog") == set()
    assert carrying(pm, "Crafted", 'x","Dog') == {"crafted"}
    assert matching(pm, "Crafted", 'x","Dog') == {"crafted"}
    assert carrying(pm, "Crafted", "x") == set()


def test_finds_a_later_label_holding_a_control_character_or_a_line_separator(pm: PersistenceManager):
    for label in ["a\tb", "c\nd", "e\u0001f", "g\u2028h", "i\u007fj"]:
        assert (label, carrying(pm, "Odd", label)) == (label, {"odd"})
        assert (label, matching(pm, "Odd", label)) == (label, {"odd"})
    assert carrying(pm, "Odd", "a\\tb") == set()


def test_finds_a_label_that_is_a_comma_only_where_a_node_carries_it(pm: PersistenceManager):
    assert carrying(pm, "Comma", ",") == {"lone"}
    assert matching(pm, "Comma", ",") == {"lone"}
    assert carrying(pm, "Comma", "a,") == {"comma"}
    assert carrying(pm, "Comma", ",b") == {"bracket"}


def test_finds_a_later_label_of_brackets_quotes_commas_or_backslashes_whole_and_nothing_near_it(
    pm: PersistenceManager,
):
    for label in EDGES:
        assert (label, carrying(pm, "Edge", label)) == (label, {"edge"})
    for label in [it for it in EDGES if it]:
        assert (label, matching(pm, "Edge", label)) == (label, {"edge"})
    for label in ["a", ",", "\\\\", "a\\\\", '""', '["', '"]', 'Edge","', "\u00e9", "\U0001f600", 'Edge",""']:
        assert (label, carrying(pm, "Edge", label)) == (label, set())


def test_tells_a_node_of_one_label_or_none_by_its_text_alike(pm: PersistenceManager):
    def held(where: str) -> set[str]:
        rows = pm.query(
            QuerySpecification[str](
                "SELECT JSON_VALUE(n.props, '$.id') FROM drivine_node n WHERE JSON_VALUE(n.props, '$.id') IN "
                f"('labeltext-single', 'labeltext-none') AND {where}"
            )
        )
        return set(rows)

    assert held(has_labels("n", ["Single"])) == {"labeltext-single"}
    assert held(has_labels("n", ["Single", ""])) == set()
    assert held(has_labels("n", ["Singl"])) == set()
    assert held(holds_label("n.labels", "Single")) == {"labeltext-single"}
    assert held(holds_label("n.labels", "")) == set()
    rows = pm.query(
        QuerySpecification[Any](cypher_statement("MATCH (n:Single) WHERE n.id = 'labeltext-single' RETURN n.id"))
    )
    assert rows == ["labeltext-single"]


def test_writes_a_label_as_json_serialize_writes_it_for_every_character_to_u00ff_and_the_others_it_escapes(
    pm: PersistenceManager,
):
    text = "".join(chr(it) for it in range(1, 0x100)) + "\u2027\u2028\u2029\u202a\ufeff\ufffd\U0001f600"
    written = pm.query(
        QuerySpecification[str]("SELECT JSON_SERIALIZE(JSON_ARRAY($text RETURNING JSON)) FROM dual").bind(
            {"text": text}
        )
    )
    assert written == [f"[{serialized_label(text)}]"]
