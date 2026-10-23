# Written by scripts/unasync.py from tests/integration/oracle/test_oracle_schema.py:
# change that, and run the script again.
import re
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal

import pytest

from drivine.backend.oracle.oracle_backend import OracleBackend
from drivine.sync import (
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
from tests.sync_integration.oracle.oracle_seed import ON_ORACLE, OracleSeed


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


def tidy(gom: StatelessGraphObjectManager) -> None:
    for it in (BY_EMAIL, BY_REGION_AND_RANK):
        gom.indexes.drop(it)
    gom.constraints.drop(UNIQUE_EMAIL)
    OracleSeed(gom.persistence_manager, "OraSchema").clear()


@pytest.fixture(scope="module")
def schema(factory: PersistenceManagerFactory) -> Iterator[None]:
    gom = StatelessGraphObjectManager(factory.get("oracle"))
    tidy(gom)
    yield
    tidy(gom)


pytestmark = [ON_ORACLE, pytest.mark.usefixtures("schema")]


def test_makes_range_indexes_once_lists_them_and_drops_them(gom: StatelessGraphObjectManager):
    assert (gom.indexes.ensure(BY_EMAIL)).status == "CREATED"
    assert (gom.indexes.ensure(BY_EMAIL)).status == "ALREADY_MATCHING"
    assert (gom.indexes.ensure(BY_REGION_AND_RANK)).status == "CREATED"
    listed = [it.properties for it in gom.indexes.list("RANGE") if it.label == "OraSchema"]
    assert ("email",) in listed and ("region", "rank") in listed
    gom.indexes.drop(BY_EMAIL)
    assert not any(it.label == "OraSchema" and it.properties[0] == "email" for it in gom.indexes.list("RANGE"))


def test_refuses_a_constraint_the_data_breaks_then_makes_and_enforces_it(gom: StatelessGraphObjectManager):
    gom.save_all([Account(id="a1", email="x@y"), Account(id="a2", email="x@y")])
    refused = gom.constraints.ensure(UNIQUE_EMAIL)
    assert refused.status == "VIOLATION" and refused.sample is not None
    [held] = refused.sample
    assert held["properties"] == {"email": "x@y"} and held["count"] == 2 and len(held["nodes"]) == 2
    gom.delete(Account, "a2")
    assert (gom.constraints.ensure(UNIQUE_EMAIL)).status == "CREATED"
    assert (gom.constraints.ensure(UNIQUE_EMAIL)).status == "ALREADY_MATCHING"
    with pytest.raises(DrivineError, match="ORA-00001"):
        gom.save(Account(id="a3", email="x@y"))
    gom.save(Account(id="a3", email="z@y"))


def test_advises_on_an_ordered_query_and_stops_once_the_index_is_there(gom: StatelessGraphObjectManager):
    q = refs(Account)
    gom.index_advice = IndexAdvicePolicy.FAIL
    with pytest.raises(DrivineError, match="no matching range index"):
        gom.load_all(Account, order_by=q.region.asc())
    gom.indexes.ensure(range_index("OraSchema", "region"))
    gom.index_advisor.refresh()
    gom.load_all(Account, order_by=q.region.asc())
    gom.indexes.drop(range_index("OraSchema", "region"))


def test_searches_through_the_tables_made_for_vector_and_full_text_indexes(
    pm: PersistenceManager, gom: StatelessGraphObjectManager
):
    vectors = vector_index("OraSearch", "embedding", 2)
    texts = full_text_index("OraSearch", "text")
    notes = OracleSeed(pm, "OraSearch")
    notes.clear()
    gom.save(Note(id="n1", text="graph databases", embedding=[1, 0]))
    gom.indexes.ensure(vectors)
    gom.indexes.ensure(texts)
    backend = OracleBackend()

    def table_of(kind: Literal["VECTOR", "FULLTEXT"], properties: list[str]) -> str | None:
        return pm.maybe_get_one(backend.search_index_table(kind, "OraSearch", properties).spec())

    assert re.match(r"^drivine_v_OraSearchembedding_[0-9a-f]+$", table_of("VECTOR", ["embedding"]) or "")
    assert re.match(r"^drivine_t_OraSearchtext_[0-9a-f]+$", table_of("FULLTEXT", ["text"]) or "")
    # Saved after the index: the trigger puts it in the tables.
    gom.save(Note(id="n2", text="relational tables", embedding=[0, 1]))
    assert [it.value.id for it in gom.load_nearest(Note, [0, 1], top_k=1)] == ["n2"]
    assert [it.value.id for it in gom.load_matching(Note, "+relational -graph", top_k=5)] == ["n2"]
    assert [it.value.id for it in gom.load_matching(Note, "graph", top_k=5)] == ["n1"]
    gom.indexes.drop(vectors)
    gom.indexes.drop(texts)
    assert table_of("VECTOR", ["embedding"]) is None
    # Without the index, every node of the label is read, to the same answer.
    assert [it.value.id for it in gom.load_matching(Note, "+relational -graph", top_k=5)] == ["n2"]
    assert [it.value.id for it in gom.load_nearest(Note, [0, 1], top_k=1)] == ["n2"]
    notes.clear()


def test_keeps_one_schema_marker_per_owner_an_empty_inventory_read_back_empty(pm: PersistenceManager):
    backend = OracleBackend()
    scope = f"ora-test-{time.time_ns()}"

    def read(property: Literal["version", "items"]) -> Any:
        value = pm.maybe_get_one(backend.schema_marker(property).spec({"scope": scope}))
        return backend.marker_items(value) if property == "items" else value

    assert read("version") is None
    pm.execute(backend.record_schema_marker().spec({"scope": scope, "version": "v1", "items": ""}))
    assert read("items") == []
    pm.execute(backend.record_schema_marker().spec({"scope": scope, "version": "v2", "items": "a\nb"}))
    assert [read("version"), read("items")] == ["v2", ["a", "b"]]
    pm.execute(
        QuerySpecification[Any]("DELETE FROM drivine_node n WHERE JSON_VALUE(n.props, '$.scope') = $scope").bind(
            {"scope": scope}
        )
    )
