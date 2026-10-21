"""
drivine4j's StatelessIndexAdvicePropertyTest: the index advice an application configures, as drivine4j's
`drivine.query.index-advice`, and here the assembly's `index_advice`, reaches the managers the assembly hands out.
"""

from drivine import IndexAdvicePolicy
from tests.conftest import Start


async def test_reaches_the_manager_the_assembly_made(start: Start):
    sample = await start(index_advice=IndexAdvicePolicy.FAIL)
    assert sample.sample_graph_object_manager().index_advice is IndexAdvicePolicy.FAIL


async def test_is_warn_when_nothing_is_configured(start: Start):
    # Ordering without an index is correct, only unindexed: an unset option must not break an application.
    sample = await start()
    assert sample.sample_graph_object_manager().index_advice is IndexAdvicePolicy.WARN
