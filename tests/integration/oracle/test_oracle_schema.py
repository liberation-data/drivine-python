import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

import pytest

from drivine import (
    DatabaseType,
    DrivineError,
    FullTextIndex,
    IndexAdvicePolicy,
    NodeFragment,
    NodeId,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    StatelessGraphObjectManager,
    VectorIndex,
    full_text_index,
    range_index,
    refs,
    uniqueness_constraint,
    vector_index,
)
from drivine.backend.oracle.oracle_backend import OracleBackend
from tests.integration.oracle.oracle_seed import ON_ORACLE, OracleSeed


class Note(NodeFragment, labels=["OraSearch"]):
    id: Annotated[str, NodeId()]
    text: Annotated[str | None, FullTextIndex()] = None
    embedding: Annotated[list[float] | None, VectorIndex(dimensions=2)] = None


class Account(NodeFragment, labels=["OraSchema"]):
    id: Annotated[str, NodeId()]
    email: str | None = None
    region: str | None = None
    rank: int | None = None


BY_EMAIL = range_index("OraSchema", "email")
BY_REGION_AND_RANK = range_index("OraSchema", ["region", "rank"])
UNIQUE_EMAIL = uniqueness_constraint("OraSchema", "email")


async def tidy(gom: StatelessGraphObjectManager) -> None:
    for it in (BY_EMAIL, BY_REGION_AND_RANK):
        await gom.indexes.drop(it)
    await gom.constraints.drop(UNIQUE_EMAIL)
    await OracleSeed(gom.persistence_manager, "OraSchema").clear()


@pytest.fixture(scope="module")
async def schema(factory: PersistenceManagerFactory) -> AsyncIterator[None]:
    gom = StatelessGraphObjectManager(factory.get("oracle"))
    await tidy(gom)
    yield
    await tidy(gom)


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("schema")]


async def test_makes_range_indexes_once_lists_them_and_drops_them(gom: StatelessGraphObjectManager):
    assert (await gom.indexes.ensure(BY_EMAIL)).status == "CREATED"
    assert (await gom.indexes.ensure(BY_EMAIL)).status == "ALREADY_MATCHING"
    assert (await gom.indexes.ensure(BY_REGION_AND_RANK)).status == "CREATED"
    listed = [it.properties for it in await gom.indexes.list("RANGE") if it.label == "OraSchema"]
    assert ("email",) in listed and ("region", "rank") in listed
    await gom.indexes.drop(BY_EMAIL)
    assert not any(it.label == "OraSchema" and it.properties[0] == "email" for it in await gom.indexes.list("RANGE"))


async def test_refuses_a_constraint_the_data_breaks_then_makes_and_enforces_it(gom: StatelessGraphObjectManager):
    await gom.save_all([Account(id="a1", email="x@y"), Account(id="a2", email="x@y")])
    refused = await gom.constraints.ensure(UNIQUE_EMAIL)
    assert refused.status == "VIOLATION" and refused.sample == ("x@y",)
    await gom.delete(Account, "a2")
    assert (await gom.constraints.ensure(UNIQUE_EMAIL)).status == "CREATED"
    assert (await gom.constraints.ensure(UNIQUE_EMAIL)).status == "ALREADY_MATCHING"
    with pytest.raises(DrivineError, match="ORA-00001"):
        await gom.save(Account(id="a3", email="x@y"))
    await gom.save(Account(id="a3", email="z@y"))


async def test_advises_on_an_ordered_query_and_stops_once_the_index_is_there(gom: StatelessGraphObjectManager):
    q = refs(Account)
    gom.index_advice = IndexAdvicePolicy.FAIL
    with pytest.raises(DrivineError, match="no matching range index"):
        await gom.load_all(Account, order_by=q.region.asc())
    await gom.indexes.ensure(range_index("OraSchema", "region"))
    gom.index_advisor.refresh()
    await gom.load_all(Account, order_by=q.region.asc())
    await gom.indexes.drop(range_index("OraSchema", "region"))


async def test_agrees_on_one_store_identity_across_managers_resolving_at_once(pm: PersistenceManager):
    identities = await asyncio.gather(*(StatelessGraphObjectManager(pm).store_identity() for _ in range(5)))
    assert len({it.id for it in identities}) == 1
    assert (await StatelessGraphObjectManager(pm).store_identity()).engine == DatabaseType.ORACLE


async def test_searches_through_the_tables_made_for_vector_and_full_text_indexes(
    pm: PersistenceManager, gom: StatelessGraphObjectManager
):
    vectors = vector_index("OraSearch", "embedding", 2)
    texts = full_text_index("OraSearch", "text")
    notes = OracleSeed(pm, "OraSearch")
    await notes.clear()
    await gom.save(Note(id="n1", text="graph databases", embedding=[1, 0]))
    await gom.indexes.ensure(vectors)
    await gom.indexes.ensure(texts)
    backend = OracleBackend()

    async def table_of(kind: Literal["VECTOR", "FULLTEXT"], properties: list[str]) -> str | None:
        return await pm.maybe_get_one(backend.search_index_table(kind, "OraSearch", properties).spec())

    assert re.match(r"^drivine_v_OraSearchembedding_[0-9a-f]+$", await table_of("VECTOR", ["embedding"]) or "")
    assert re.match(r"^drivine_t_OraSearchtext_[0-9a-f]+$", await table_of("FULLTEXT", ["text"]) or "")
    # Saved after the index: the trigger puts it in the tables.
    await gom.save(Note(id="n2", text="relational tables", embedding=[0, 1]))
    assert [it.value.id for it in await gom.load_nearest(Note, [0, 1], top_k=1)] == ["n2"]
    assert [it.value.id for it in await gom.load_matching(Note, "+relational -graph", top_k=5)] == ["n2"]
    assert [it.value.id for it in await gom.load_matching(Note, "graph", top_k=5)] == ["n1"]
    await gom.indexes.drop(vectors)
    await gom.indexes.drop(texts)
    assert await table_of("VECTOR", ["embedding"]) is None
    # Without the index, every node of the label is read, to the same answer.
    assert [it.value.id for it in await gom.load_matching(Note, "+relational -graph", top_k=5)] == ["n2"]
    assert [it.value.id for it in await gom.load_nearest(Note, [0, 1], top_k=1)] == ["n2"]
    await notes.clear()


async def test_keeps_one_schema_marker_per_owner_an_empty_inventory_read_back_empty(pm: PersistenceManager):
    backend = OracleBackend()
    scope = f"ora-test-{time.time_ns()}"

    async def read(property: Literal["version", "items"]) -> Any:
        value = await pm.maybe_get_one(backend.schema_marker(property).spec({"scope": scope}))
        return backend.marker_items(value) if property == "items" else value

    assert await read("version") is None
    await pm.execute(backend.record_schema_marker().spec({"scope": scope, "version": "v1", "items": ""}))
    assert await read("items") == []
    await pm.execute(backend.record_schema_marker().spec({"scope": scope, "version": "v2", "items": "a\nb"}))
    assert [await read("version"), await read("items")] == ["v2", ["a", "b"]]
    await pm.execute(
        QuerySpecification[Any]("DELETE FROM drivine_node n WHERE JSON_VALUE(n.props, '$.scope') = $scope").bind(
            {"scope": scope}
        )
    )
