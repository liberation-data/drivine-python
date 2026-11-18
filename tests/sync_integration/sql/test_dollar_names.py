# Written by scripts/unasync.py from tests/integration/sql/test_dollar_names.py:
# change that, and run the script again.
"""
A query DSL filter, order and grouped read of a property whose name begins with `$`, which store layout 2 holds as
`$$score` (drivine-cypher-spec#161). The DSL's own SQL named it as it is, `$score`, so it found no node, ordered by
nothing and grouped every node under no value, silently. The shared module's fragments read it as the layout holds it:
on SQLite since drivine-cypher-spec#235 step 2, and on Postgres since step 3. A port of Drivine for TypeScript's
StatelessGraphObjectManager.dollarNames.spec.
"""

from collections.abc import Iterator
from typing import Annotated

import pytest

from drivine.sync import (
    DatabaseType,
    GraphProperty,
    NodeFragment,
    NodeId,
    PersistenceManager,
    StatelessGraphObjectManager,
    count,
    key,
    refs,
    sum_,
)
from tests.sync_integration.sql.sql_seed import SqlSeed


class Ranked(NodeFragment, labels=["DollarNamed"]):
    id: Annotated[str, NodeId()]
    score: Annotated[int | None, GraphProperty("$score")] = None


@pytest.fixture(autouse=True)
def ranked(pm: PersistenceManager, gom: StatelessGraphObjectManager) -> Iterator[None]:
    seed = SqlSeed(pm, "DollarNamed")
    seed.clear()
    gom.save_all([Ranked(id=f"r{it}", score=it) for it in (1, 2, 3)])
    yield
    seed.clear()


@pytest.fixture
def held(pm: PersistenceManager, request: pytest.FixtureRequest) -> None:
    """
    Held where the DSL's SQL is still the library's own, which names the property as it is: Oracle until
    drivine-cypher-spec#235 step 4. Take the hold out when that step lands.
    """
    if pm.type is DatabaseType.ORACLE:
        request.applymarker(pytest.mark.xfail(strict=True, reason="drivine-cypher-spec#235 step 4"))


def test_saves_and_loads_it_by_its_own_name(gom: StatelessGraphObjectManager):
    loaded = gom.load(Ranked, "r2")
    assert loaded is not None and loaded.score == 2


def test_filters_by_it(gom: StatelessGraphObjectManager, held: None):
    q = refs(Ranked)
    assert sorted(it.id for it in gom.load_all(Ranked, q.score.gt(1))) == ["r2", "r3"]
    assert gom.count(Ranked, q.score.eq(3)) == 1


def test_orders_and_seeks_by_it(gom: StatelessGraphObjectManager, held: None):
    q = refs(Ranked)
    assert [it.id for it in gom.load_all(Ranked, order_by=q.score.desc())] == ["r3", "r2", "r1"]
    assert [it.id for it in gom.load_all(Ranked, order_by=q.score.asc(), seek=q.score.after(1))] == ["r2", "r3"]


def test_groups_and_adds_by_it(gom: StatelessGraphObjectManager, held: None):
    q = refs(Ranked)
    groups = gom.groups.load(
        Ranked,
        keys={"score": key(q.score)},
        aggregates={"nodes": count(), "total": sum_(q.score)},
        order_by=[("score", "desc")],
    )
    assert groups == [
        {"score": 3, "nodes": 1, "total": 3},
        {"score": 2, "nodes": 1, "total": 2},
        {"score": 1, "nodes": 1, "total": 1},
    ]
