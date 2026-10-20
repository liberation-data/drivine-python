"""
drivine4j's GeneratedLoadMatchingTest, which checks that its code generator writes load_matching for a view or a
fragment with a full-text index, and none for other views. Here the searches are the test.
"""

import pytest

from drivine import DatabaseType, DrivineError, QuerySpecification, SchemaCatalog, SchemaOptions
from drivine_sample import RaisedAndAssignedIssue, SampleArticleNode, SampleAssembly, SampleFullTextView
from tests.conftest import Start


@pytest.fixture
async def searchable(start: Start, engine: str) -> SampleAssembly:
    sample = await start(schema=SchemaOptions([SchemaCatalog.from_fragments(SampleArticleNode).for_database(engine)]))
    gom = sample.sample_graph_object_manager()
    await gom.save_all(
        [
            SampleArticleNode(id="a1", body="graph databases store relationships natively"),
            SampleArticleNode(id="a2", body="graph queries"),
            SampleArticleNode(id="a3", body="relational tables"),
        ]
    )
    if gom.persistence_manager.type is DatabaseType.NEO4J:
        await gom.persistence_manager.execute(QuerySpecification[None]("CALL db.awaitIndexes(30)"))
    return sample


async def test_searches_a_view_by_its_roots_full_text_index(searchable: SampleAssembly):
    hits = await searchable.sample_graph_object_manager().load_matching(SampleFullTextView, "graph", top_k=5)
    assert sorted(it.value.article.id for it in hits) == ["a1", "a2"]
    assert isinstance(hits[0].value, SampleFullTextView)


async def test_searches_a_fragment_by_its_full_text_index(searchable: SampleAssembly):
    hits = await searchable.sample_graph_object_manager().load_matching(SampleArticleNode, "relational", top_k=5)
    assert [it.value.id for it in hits] == ["a3"]
    assert isinstance(hits[0].value, SampleArticleNode)


async def test_refuses_a_view_whose_root_has_no_full_text_index(searchable: SampleAssembly):
    with pytest.raises(DrivineError, match="FullTextIndex"):
        await searchable.sample_graph_object_manager().load_matching(RaisedAndAssignedIssue, "graph", top_k=1)
