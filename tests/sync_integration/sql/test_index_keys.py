# Written by scripts/unasync.py from tests/integration/sql/test_index_keys.py:
# change that, and run the script again.
"""
The spec's `store.yaml` `indexKeys` on each SQL store (#511, liberation-data/drivine-cypher-spec#286): each value
stored under each key, as the store holds it at rest, is found by a lookup of it, compiled by the module, through a
range index over the key. On Oracle and SQLite a lookup compares the class and key the module works out of the value as
the statement compiles with those the store's index expressions give over the property; on Postgres, the property's JSON
with the value's. A module that works out another key than the store's index holds would find nothing where the node
is there: the drift guard. On Oracle the key the module binds (`_cyk_`) is also the one the spec pins.

drivine4j's IndexKeysCrossEngineTest reads the index's class and key back through the module's index expressions and
compares them with the pins directly; the module's JSON entry gives a library neither, so here the lookup stands for
the comparison.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from drivine.backend.sql.value_layout import TagText
from drivine.cypher.shared_compiler import compile_cypher, module_answer
from drivine.sync import PersistenceManager, PersistenceManagerFactory, StatelessGraphObjectManager, range_index
from tests.sync_integration.sql.test_id_key_plans import spec
from tests.sync_integration.stores import create, is_oracle, is_postgres

LABEL = "IndexKeyProbe"

INDEX_KEYS: dict[str, Any] = yaml.safe_load((Path(__file__).parents[2] / "cypher" / "store.yaml").read_text())[
    "indexKeys"
]


def expanded(value: Any) -> Any:
    """`value` with each `{$repeat: text, times: n}` in it written out, as the spec's runners read it."""
    if isinstance(value, dict):
        given = cast(dict[str, Any], value)
        if set(given) == {"$repeat", "times"}:
            return given["$repeat"] * given["times"]
        return {key: expanded(it) for key, it in given.items()}
    if isinstance(value, list):
        return [expanded(it) for it in cast(list[Any], value)]
    return value


def at_rest(value: Any, store: str) -> Any:
    """
    `value` as the store holds it at rest: a temporal tag with its sort key `s`, as the module works it out, where it
    works one out (a duration has none, and is held as its text alone).
    """
    if not isinstance(value, dict):
        return value
    tag = cast(dict[str, Any], value)
    if "$t" not in tag or "s" in tag:
        return tag
    key = module_answer({"temporalKey": {"kind": tag["$t"], "v": tag["v"]}}, store).get("key")
    return {**tag, "s": key} if key is not None else tag


def bound(value: Any) -> Any:
    """`value` as a statement binds it: a temporal tag as the text of its kind, which keeps any year and nanosecond."""
    if not isinstance(value, dict):
        return value
    tag = cast(dict[str, Any], value)
    return TagText(tag["v"], tag["$t"]) if "$t" in tag else tag


def pinned(row: dict[str, Any], store: str) -> list[str] | None:
    """
    The class and key the spec pins for the row on `store`, or None where it pins none: Postgres's index is over the
    property's JSON, so it is checked for each value Oracle works a key out of.
    """
    found = row.get("oracle" if store == "postgres" else store)
    return None if found is None else [str(it) for it in expanded(found)]


def lookup(key: str) -> str:
    return f"MATCH (n:{LABEL}) WHERE n.`{key}` = $v RETURN count(n) AS c"


@pytest.fixture(scope="module")
def indexed(factory: PersistenceManagerFactory, store: str) -> Iterator[None]:
    """A range index over each key of the probe's label, made as the schema API makes one, and dropped after."""
    gom = StatelessGraphObjectManager(factory.get(store))
    specs = [range_index(LABEL, it["key"]) for it in INDEX_KEYS["keys"]]
    for it in specs:
        gom.indexes.ensure(it)
    yield
    for it in specs:
        gom.indexes.drop(it)


def found(pm: PersistenceManager, key: str, value: Any) -> int:
    return pm.get_one(spec(lookup(key), {"v": bound(value)}))


def stored(pm: PersistenceManager, name: str, value: Any, store: str) -> None:
    """The probe's one node, holding `value` under the key stored as `name`, written outside any object manager."""
    pm.execute(spec(f"MATCH (n:{LABEL}) DETACH DELETE n"))
    create(pm, LABEL, {name: at_rest(value, store)})


@pytest.mark.usefixtures("indexed")
def test_each_value_is_found_through_the_index_by_the_key_the_module_works_out(pm: PersistenceManager, store: str):
    wrong: list[str] = []
    checked = 0
    for row in INDEX_KEYS["values"]:
        pin = pinned(row, store)
        if pin is None:
            continue
        value = expanded(row["value"])
        text = json.dumps(value)[:60]
        for key in INDEX_KEYS["keys"]:
            stored(pm, key.get("stored", key["key"]), value, store)
            if found(pm, key["key"], value) != 1:
                wrong.append(f"{text} under {key['key']}: not found by a lookup of it")
            if is_oracle(pm):
                # The key the module binds, where it binds one, is the one the spec pins: a temporal is compared by the
                # SQL over the value, and binds none.
                binds = compile_cypher(lookup(key["key"]), {"v": bound(value)}, "ORACLE", "drivine").parameters
                if "_cyk_v" in binds and str(binds["_cyk_v"]) != pin[1]:
                    wrong.append(f"{text} under {key['key']}: bound {binds['_cyk_v']!r}, pinned {pin[1]!r}")
            checked += 1
    assert checked >= 40, f"indexKeys checked only {checked}"
    assert wrong == [], "\n".join(wrong)


@pytest.mark.usefixtures("indexed")
def test_a_value_the_node_doesnt_hold_is_not_found(pm: PersistenceManager, store: str):
    """So the check above can fail: a lookup of another text, number or moment finds nothing."""
    stored(pm, "id", "p-123", store)
    assert found(pm, "id", "p-124") == 0
    assert found(pm, "id", 5) == 0
    stored(pm, "id", {"$t": "date", "v": "2026-01-02"}, store)
    assert found(pm, "id", {"$t": "date", "v": "2026-01-03"}) == 0
    assert found(pm, "id", {"$t": "date", "v": "2026-01-02"}) == 1
    if not is_postgres(pm):
        # A date's key is no text's: a lookup of the date's digits as a text doesn't find it.
        assert found(pm, "id", "10000020260102") == 0
