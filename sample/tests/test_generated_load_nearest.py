"""
drivine4j's GeneratedLoadNearestTest, which checks that its code generator writes load_nearest for a view whose root has
a vector index, with search_k, none for other views, and count for every view. Here the searches are the test.
"""

import pytest

from drivine import DatabaseType, DrivineError, QuerySpecification, SchemaCatalog, SchemaOptions
from drivine_sample import RaisedAndAssignedIssue, SampleAssembly, SampleDocNode, SampleVectorView
from tests.conftest import Start
from tests.issue_world import seed_issue


@pytest.fixture
async def searchable(start: Start, engine: str) -> SampleAssembly:
    catalog = SchemaCatalog.from_fragments(SampleDocNode, dimensions=lambda label, prop: 3).for_database(engine)
    sample = await start(schema=SchemaOptions([catalog]))
    gom = sample.sample_graph_object_manager()
    pm = gom.persistence_manager
    if pm.type is DatabaseType.MEMGRAPH:
        # Memgraph's vector search returns nodes deleted since its last garbage collection, and fails on them.
        await pm.execute(QuerySpecification[None]("FREE MEMORY"))
    await gom.save_all(
        [
            SampleDocNode(id="north", title="north", embedding=[1, 0, 0]),
            SampleDocNode(id="north-east", title="north-east", embedding=[0.8, 0.6, 0]),
            SampleDocNode(id="up", title="up", embedding=[0, 0, 1]),
        ]
    )
    if pm.type is DatabaseType.NEO4J:
        await pm.execute(QuerySpecification[None]("CALL db.awaitIndexes(30)"))
    return sample


async def test_searches_a_view_by_its_roots_vector_index_with_search_k_widening_the_search(searchable: SampleAssembly):
    gom = searchable.sample_graph_object_manager()
    # Vector indexes are approximate: asked for one hit only, Neo4j's can return the second nearest.
    [nearest] = await gom.load_nearest(SampleVectorView, [1, 0.1, 0], top_k=1, search_k=3)
    assert isinstance(nearest.value, SampleVectorView) and nearest.value.doc.id == "north"
    close = await gom.load_nearest(SampleVectorView, [1, 0.1, 0], top_k=2, search_k=3, threshold=0.9)
    assert [it.value.doc.id for it in close] == ["north", "north-east"]


async def test_refuses_a_view_whose_root_has_no_vector_index(searchable: SampleAssembly):
    with pytest.raises(DrivineError, match="VectorIndex"):
        await searchable.sample_graph_object_manager().load_nearest(RaisedAndAssignedIssue, [1, 0, 0], top_k=1)


async def test_counts_any_view(searchable: SampleAssembly):
    gom = searchable.sample_graph_object_manager()
    assert await gom.count(SampleVectorView) == 3
    await seed_issue(gom, id=1, title="Counted", body="b", person="Ada", bio="bio", org="Acme")
    assert await gom.count(RaisedAndAssignedIssue) == 1
