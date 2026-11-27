"""
The spec's `store.yaml` `indexKeys` on each SQL store (#511, liberation-data/drivine-cypher-spec#286): each value
stored under each key, as the store holds it at rest, is found by a lookup of it that the module compiles, with a range
index over the key. A lookup compares the class and key the store's index expressions give over the property with those
of the bound value (on Postgres, the property's JSON with the value's), so a module that keys a value otherwise than the
store holds it finds nothing where the node is there: the drift guard. Where the pins can be read without the module's
index expressions, they are checked too: a temporal's sort key, which the module works out (`temporalKey`) both for the
value at rest and for the value bound, on every store; the key the module binds on Oracle (`_cyk_`); and on SQLite, the
class and key the index holds, read through its own expressions as `sqlite_master` records them.

drivine4j's IndexKeysCrossEngineTest reads the index's class and key back through the module's index expressions
(`IndexKey.column`) on every store; the module's JSON entry gives a library none, so on Oracle and Postgres the lookup
stands for that comparison.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from drivine import (
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    range_index,
)
from drivine.backend.sql.value_layout import TagText
from drivine.cypher.shared_compiler import compile_cypher, module_answer
from tests.cypher.cypher_spec import expanded, read
from tests.integration.sql.test_id_key_plans import spec
from tests.integration.stores import create, is_oracle, is_postgres, is_sqlite

LABEL = "IndexKeyProbe"

INDEX_KEYS: dict[str, Any] = read("store.yaml")["indexKeys"]


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
async def indexed(factory: PersistenceManagerFactory, store: str) -> AsyncIterator[None]:
    """A range index over each key of the probe's label, made as the schema API makes one, and dropped after."""
    gom = StatelessGraphObjectManager(factory.get(store))
    specs = [range_index(LABEL, it["key"]) for it in INDEX_KEYS["keys"]]
    for it in specs:
        await gom.indexes.ensure(it)
    yield
    for it in specs:
        await gom.indexes.drop(it)


def columns(ddl: str) -> list[str]:
    """The expressions of a SQLite index's `CREATE INDEX` over the node table, each as written there."""
    body = ddl[ddl.index("(") + 1 : ddl.rindex(") WHERE ")]
    parts: list[str] = []
    depth, start, quoted = 0, 0, False
    for at, it in enumerate(body):
        if it == "'":
            quoted = not quoted
        elif not quoted and it in "()":
            depth += 1 if it == "(" else -1
        elif not quoted and it == "," and depth == 0:
            parts.append(body[start:at])
            start = at + 1
    return [*parts, body[start:]]


async def held(pm: PersistenceManager, name: str) -> list[str]:
    """On SQLite, the class and key the probe's range index over the key stored as `name` holds of its one node."""
    indexes = await pm.query(
        QuerySpecification[Any]("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql LIKE $label").bind(
            {"label": f'%"{LABEL}"%'}
        )
    )
    ddl = next(it["sql"] for it in indexes if f"'$.\"{name}\"'" in it["sql"])
    kind, key = columns(ddl)
    # The nodes the partial index holds: the label's, as its own `WHERE` selects them.
    over = ddl[ddl.rindex(") WHERE ") + 2 :]
    row = await pm.get_one(QuerySpecification[Any](f"SELECT {kind} AS c, {key} AS k FROM drivine_node {over}"))
    return [str(row["c"]), str(row["k"])]


async def found(pm: PersistenceManager, key: str, value: Any) -> int:
    return await pm.get_one(spec(lookup(key), {"v": bound(value)}))


async def stored(pm: PersistenceManager, name: str, value: Any, store: str) -> None:
    """The probe's one node, holding `value` under the key stored as `name`, written outside any object manager."""
    await pm.execute(spec(f"MATCH (n:{LABEL}) DETACH DELETE n"))
    await create(pm, LABEL, {name: at_rest(value, store)})


@pytest.mark.usefixtures("indexed")
async def test_each_value_is_found_through_the_index_by_the_key_the_module_works_out(
    pm: PersistenceManager, store: str
):
    wrong: list[str] = []
    checked = 0
    for row in INDEX_KEYS["values"]:
        pin = pinned(row, store)
        if pin is None:
            continue
        value = expanded(row["value"])
        text = json.dumps(value)[:60]
        if isinstance(value, dict) and "$t" in value:
            # The sort key the module works out, which the value at rest and the value bound both carry, is the one
            # pinned; a duration, which has none, is keyed by its text.
            tag = cast(dict[str, Any], at_rest(value, store))
            if (keyed := [tag["$t"], tag.get("s", tag["v"])]) != pin:
                wrong.append(f"{text}: keyed {keyed!r}, pinned {pin!r}")
        for key in INDEX_KEYS["keys"]:
            name = key.get("stored", key["key"])
            await stored(pm, name, value, store)
            if await found(pm, key["key"], value) != 1:
                wrong.append(f"{text} under {key['key']}: not found by a lookup of it")
            if is_sqlite(pm) and (got := await held(pm, name)) != pin:
                wrong.append(f"{text} under {key['key']}: the index holds {got!r}, pinned {pin!r}")
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
async def test_a_value_the_node_doesnt_hold_is_not_found(pm: PersistenceManager, store: str):
    """So the check above can fail: a lookup of another text, number or moment finds nothing."""
    await stored(pm, "id", "p-123", store)
    assert await found(pm, "id", "p-124") == 0
    assert await found(pm, "id", 5) == 0
    await stored(pm, "id", {"$t": "date", "v": "2026-01-02"}, store)
    assert await found(pm, "id", {"$t": "date", "v": "2026-01-03"}) == 0
    assert await found(pm, "id", {"$t": "date", "v": "2026-01-02"}) == 1
    if not is_postgres(pm):
        # A date's key is no text's: a lookup of the date's digits as a text doesn't find it.
        assert await found(pm, "id", "10000020260102") == 0
