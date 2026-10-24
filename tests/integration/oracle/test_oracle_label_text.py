"""
drivine4j#198: a label after the first is looked for by its text, among the labels as `JSON_SERIALIZE` writes them
(see `holds_label`). It is found whole, whatever characters it holds, and never inside another label.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from drivine import PersistenceManager, PersistenceManagerFactory, QuerySpecification
from drivine.backend.oracle.oracle_graph import has_labels, holds_label, serialized_label
from drivine.query.statement import cypher_statement
from tests.integration.oracle.oracle_seed import ON_ORACLE, OracleSeed

SCOPE = "OraLabelText"

EDGES = ["", '"', "[", "]", "\\", "a\\", ',"', '","', "\u00e9 \U0001f600", "$x"]
"""Labels that try the edges of the text a label is looked for in: brackets, quotes, commas and backslashes."""

UNSCOPED = "DELETE FROM drivine_node WHERE JSON_VALUE(props, '$.id') IN ('labeltext-single', 'labeltext-none')"
"""Removes the nodes of one label and of none, which can't carry the suite's label."""


@pytest.fixture(scope="module")
async def labelled(factory: PersistenceManagerFactory) -> AsyncIterator[None]:
    seed = OracleSeed(factory.get("oracle"), SCOPE)
    await seed.clear()
    await seed.node(["Crafted", 'x","Dog'], {"id": "crafted"})
    await seed.node(["Odd", "a\tb", "c\nd", "e\u0001f", "g\u2028h", "i\u007fj"], {"id": "odd"})
    await seed.node(["Comma", "a,", "b"], {"id": "comma"})
    await seed.node(["Comma", "a[", ",b"], {"id": "bracket"})
    await seed.node(["Comma", "x", ","], {"id": "lone"})
    await seed.node(["Edge", *EDGES], {"id": "edge"})
    await seed.pm.execute(QuerySpecification[Any](UNSCOPED))
    for labels, id in [('["Single"]', "single"), ("[]", "none")]:
        await seed.pm.execute(
            QuerySpecification[Any](
                "INSERT INTO drivine_node (labels, props) "
                f"VALUES (JSON('{labels}'), JSON('{{\"id\":\"labeltext-{id}\"}}'))"
            )
        )
    yield
    await seed.clear()
    await seed.pm.execute(QuerySpecification[Any](UNSCOPED))


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("labelled")]


async def carrying(pm: PersistenceManager, *labels: str) -> set[str]:
    """The ids of this suite's nodes that carry every one of the labels, as the backend tests them."""
    rows = await pm.query(
        QuerySpecification[str](
            f"SELECT JSON_VALUE(n.props, '$.id') FROM drivine_node n WHERE {has_labels('n', [SCOPE])} "
            f"AND {has_labels('n', list(labels))}"
        )
    )
    return set(rows)


async def matching(pm: PersistenceManager, *labels: str) -> set[str]:
    """The ids of this suite's nodes a compiled Cypher `MATCH` of the labels finds."""
    pattern = "".join(f":`{it.replace('`', '``')}`" for it in labels)
    rows = await pm.query(QuerySpecification[Any](cypher_statement(f"MATCH (n{pattern}) WHERE n:{SCOPE} RETURN n.id")))
    return set(rows)


async def test_takes_a_label_holding_a_quoted_comma_for_one_label_not_two(pm: PersistenceManager):
    assert await carrying(pm, "Crafted", "Dog") == set()
    assert await matching(pm, "Crafted", "Dog") == set()
    assert await carrying(pm, "Crafted", 'x","Dog') == {"crafted"}
    assert await matching(pm, "Crafted", 'x","Dog') == {"crafted"}
    assert await carrying(pm, "Crafted", "x") == set()


async def test_finds_a_later_label_holding_a_control_character_or_a_line_separator(pm: PersistenceManager):
    for label in ["a\tb", "c\nd", "e\u0001f", "g\u2028h", "i\u007fj"]:
        assert (label, await carrying(pm, "Odd", label)) == (label, {"odd"})
        assert (label, await matching(pm, "Odd", label)) == (label, {"odd"})
    assert await carrying(pm, "Odd", "a\\tb") == set()


async def test_finds_a_label_that_is_a_comma_only_where_a_node_carries_it(pm: PersistenceManager):
    assert await carrying(pm, "Comma", ",") == {"lone"}
    assert await matching(pm, "Comma", ",") == {"lone"}
    assert await carrying(pm, "Comma", "a,") == {"comma"}
    assert await carrying(pm, "Comma", ",b") == {"bracket"}


async def test_finds_a_later_label_of_brackets_quotes_commas_or_backslashes_whole_and_nothing_near_it(
    pm: PersistenceManager,
):
    for label in EDGES:
        assert (label, await carrying(pm, "Edge", label)) == (label, {"edge"})
    for label in [it for it in EDGES if it]:
        assert (label, await matching(pm, "Edge", label)) == (label, {"edge"})
    for label in ["a", ",", "\\\\", "a\\\\", '""', '["', '"]', 'Edge","', "\u00e9", "\U0001f600", 'Edge",""']:
        assert (label, await carrying(pm, "Edge", label)) == (label, set())


async def test_tells_a_node_of_one_label_or_none_by_its_text_alike(pm: PersistenceManager):
    async def held(where: str) -> set[str]:
        rows = await pm.query(
            QuerySpecification[str](
                "SELECT JSON_VALUE(n.props, '$.id') FROM drivine_node n WHERE JSON_VALUE(n.props, '$.id') IN "
                f"('labeltext-single', 'labeltext-none') AND {where}"
            )
        )
        return set(rows)

    assert await held(has_labels("n", ["Single"])) == {"labeltext-single"}
    assert await held(has_labels("n", ["Single", ""])) == set()
    assert await held(has_labels("n", ["Singl"])) == set()
    assert await held(holds_label("n.labels", "Single")) == {"labeltext-single"}
    assert await held(holds_label("n.labels", "")) == set()
    rows = await pm.query(
        QuerySpecification[Any](cypher_statement("MATCH (n:Single) WHERE n.id = 'labeltext-single' RETURN n.id"))
    )
    assert rows == ["labeltext-single"]


async def test_writes_a_label_as_json_serialize_writes_it_for_every_character_to_u00ff_and_the_others_it_escapes(
    pm: PersistenceManager,
):
    text = "".join(chr(it) for it in range(1, 0x100)) + "\u2027\u2028\u2029\u202a\ufeff\ufffd\U0001f600"
    written = await pm.query(
        QuerySpecification[str]("SELECT JSON_SERIALIZE(JSON_ARRAY($text RETURNING JSON)) FROM dual").bind(
            {"text": text}
        )
    )
    assert written == [f"[{serialized_label(text)}]"]
