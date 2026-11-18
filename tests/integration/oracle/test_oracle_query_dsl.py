"""
The query DSL on Oracle as the shared module's fragments (drivine-cypher-spec#235, step 4), run: each statement with the
hints its fragments ask for after its first SELECT or DELETE, an equality's index key bound as plain text beside the
value (liberation-data/drivine#761), and a comparison that calls the graph's nulls_equal run where the graph was made
with it. tests/unit/test_oracle_query_dsl.py pins the SQL. Drivine for TypeScript's OracleQueryDsl.spec.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest

from drivine import (
    CaseInsensitive,
    NodeFragment,
    NodeId,
    PersistenceManager,
    StatelessGraphObjectManager,
    range_index,
)
from drivine import refs as refs_of
from tests.integration.oracle.oracle_seed import ON_ORACLE, OracleSeed

LABELS = ("OraDsl", "OraDslCiA", "OraDslCiB")


class Note(NodeFragment, labels=["OraDsl"]):
    id: Annotated[str, NodeId()]
    title: str | None = None
    meta: dict[str, Any] | None = None
    tags: list[Any] | None = None


class CiA(NodeFragment, labels=["OraDslCiA"]):
    id: Annotated[str, NodeId()]
    email: Annotated[str | None, CaseInsensitive()] = None


class CiB(NodeFragment, labels=["OraDslCiB"]):
    id: Annotated[str, NodeId()]
    email: Annotated[str | None, CaseInsensitive()] = None


@pytest.fixture(autouse=True)
async def cleared(pm: PersistenceManager) -> AsyncIterator[None]:
    for it in LABELS:
        await OracleSeed(pm, it).clear()
    yield
    for it in LABELS:
        await OracleSeed(pm, it).clear()


pytestmark = ON_ORACLE

q = refs_of(Note)


async def titles(gom: StatelessGraphObjectManager, **query: Any) -> list[str | None]:
    return [it.title for it in await gom.load_all(Note, **query)]


async def test_compares_and_orders_text_by_its_code_points_as_neo4j_does(gom: StatelessGraphObjectManager):
    # B before a, and é after z.
    await gom.save_all([Note(id=f"c-{it}", title=it) for it in ("a", "B", "b", "é", "z")])
    assert await titles(gom, order_by=q.title.asc()) == ["B", "a", "b", "z", "é"]
    assert await titles(gom, order_by=q.title.desc()) == ["é", "z", "b", "a", "B"]
    assert await titles(gom, where=q.title.gt("B"), order_by=q.title.asc()) == ["a", "b", "z", "é"]
    assert await titles(gom, order_by=q.title.asc(), seek=q.title.after("b")) == ["z", "é"]


async def test_finds_a_text_by_its_index_key_up_to_4000_bytes_and_by_its_value_past_them(
    gom: StatelessGraphObjectManager,
):
    # A delete's and a patch's too.
    at, past = "x" * 4000, "x" * 4001
    await gom.save_all([Note(id="at", title=at), Note(id="past", title=past)])
    assert [it.id for it in await gom.load_all(Note, q.title.eq(at))] == ["at"]
    assert [it.id for it in await gom.load_all(Note, q.title.eq(past))] == ["past"]
    assert await gom.count(Note, q.title.neq(at)) == 1
    assert (await gom.patch(Note, "at", set={"tags": ["patched"]}, where=q.title.eq(at))).applied
    assert await gom.delete_all(Note, q.title.eq(past)) == 1
    assert [it.id for it in await gom.load_all(Note)] == ["at"]


async def test_runs_a_comparison_that_calls_the_graphs_nulls_equal_and_gives_what_cypher_gives(
    gom: StatelessGraphObjectManager,
):
    await gom.save_all([Note(id="one", meta={"a": 1}, tags=[[1, 2]]), Note(id="two", meta={"a": 2}, tags=[[1, 3]])])
    # A map or a list that holds a null equals none: Cypher gives null for it.
    assert await gom.count(Note, q.meta.eq({"a": None})) == 0
    assert await gom.count(Note, q.tags.has_item([1, None])) == 0
    assert [it.id for it in await gom.load_all(Note, q.meta.eq({"a": 1}))] == ["one"]


async def test_finds_by_eq_ignore_case_through_the_index_over_the_copy_past_4000_bytes_its_own_label_alone(
    gom: StatelessGraphObjectManager,
):
    # drivine-cypher-spec#373: past 4,000 bytes the module's term over the copy holds for any node, so the statement
    # tests the label itself, and a node of another label with the same copy is not found.
    index = range_index("OraDslCiA", "email", ignore_case=True)
    await gom.indexes.ensure(index)
    try:
        long = "Ab" * 2_500
        await gom.save_all([CiA(id="a-long", email=long), CiA(id="a-short", email="Ann@X.com")])
        await gom.save_all([CiB(id="b-long", email=long.upper()), CiB(id="b-short", email="ann@x.com")])
        a = refs_of(CiA)

        async def found(email: str) -> list[str]:
            return [it.id for it in await gom.load_all(CiA, a.email.eq_ignore_case(email))]

        assert await found(long.upper()) == ["a-long"]
        assert await found("ANN@x.com") == ["a-short"]
        assert await gom.count(CiA, a.email.eq_ignore_case(long)) == 1
    finally:
        await gom.indexes.drop(index)
