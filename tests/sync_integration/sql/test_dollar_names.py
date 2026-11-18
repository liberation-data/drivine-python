# Written by scripts/unasync.py from tests/integration/sql/test_dollar_names.py:
# change that, and run the script again.
"""
A query DSL filter, order and grouped read of a property whose name begins with `$`, which store layout 2 holds as
`$$score` (drivine-cypher-spec#161). The DSL's own SQL named it as it is, `$score`, so it found no node, ordered by
nothing and grouped every node under no value, silently. The shared module's fragments read it as the layout holds it:
on SQLite since drivine-cypher-spec#235 step 2, and on Postgres since step 3. A raw where reads it by the key it is held
under on each SQL store (drivine-python#543, liberation-data/drivine#793). A port of Drivine for TypeScript's
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


def test_reads_it_in_a_raw_where_by_the_key_it_is_held_under(gom: StatelessGraphObjectManager):
    # A raw where names it as Cypher writes it, in backticks, and reads the key layout 2 holds it under, $$score.
    assert sorted(it.id for it in gom.load_all(Ranked, "n.`$score` IS NOT NULL")) == ["r1", "r2", "r3"]
    assert gom.load_all(Ranked, "n.`$score` IS NULL") == []
    # A patch's raw where, which Postgres and SQLite run with its values written in (PostgresWrites' _meeting).
    assert (gom.patch(Ranked, "r2", set={"score": 20}, where="n.`$score` IS NOT NULL")).applied
    loaded = gom.load(Ranked, "r2")
    assert loaded is not None and loaded.score == 20
    # A grouped read's raw where and raw aggregate filter.
    groups = gom.groups.load(
        Ranked,
        where="n.`$score` IS NOT NULL",
        keys={"id": key("id")},
        aggregates={"scored": count("n.`$score` IS NOT NULL")},
        order_by=[("id", "asc")],
    )
    assert groups == [{"id": "r1", "scored": 1}, {"id": "r2", "scored": 1}, {"id": "r3", "scored": 1}]


def test_compares_it_with_a_number_in_a_raw_where(
    pm: PersistenceManager, gom: StatelessGraphObjectManager, request: pytest.FixtureRequest
):
    if pm.type is DatabaseType.SQLITE:
        # Held, and not for the held key, which the raw where reads: SQLite's raw where reads a number property as its
        # JSON text, which SQLite orders after every number, so `n.score > 1` holds for a score of 1, or of 0, too.
        request.applymarker(
            pytest.mark.xfail(strict=True, reason="SQLite's raw where compares a number property as its JSON text")
        )
    assert sorted(it.id for it in gom.load_all(Ranked, "n.`$score` > 1")) == ["r2", "r3"]
