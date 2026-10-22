# Written by scripts/unasync.py from tests/integration/test_grouped_reads.py:
# change that, and run the script again.
import pytest

from drivine.sync import (
    DrivineError,
    PersistenceManager,
    RelateMode,
    StatelessGraphObjectManager,
    avg,
    count,
    hop,
    key,
    max_,
    min_,
    node_ref,
    path_key,
    refs,
    sum_,
)
from tests.models import Director, Movie
from tests.sync_integration.stores import below


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


@pytest.fixture
def films(pm: PersistenceManager, gom: StatelessGraphObjectManager) -> None:
    gom.save_all(
        [
            Movie(uuid="m1", title="The Matrix", released=1999),
            Movie(uuid="m2", title="The Matrix Reloaded", released=2003),
            Movie(uuid="m3", title="The Matrix Revolutions", released=2003),
            Movie(uuid="m4", title="Speed", released=1994),
            Movie(uuid="m5", title="Point Break", released=1991),
            Director(uuid="d1", name="Lana"),
            Director(uuid="d2", name="Jan"),
            Director(uuid="d3", name="Kathryn"),
        ]
    )
    for movie, director in [("m1", "d1"), ("m2", "d1"), ("m3", "d1"), ("m4", "d2"), ("m5", "d3"), ("m1", "d1")]:
        gom.edges.relate(node_ref(Movie, movie), node_ref(Director, director), "DIRECTED_BY", mode=RelateMode.CREATE)


def test_groups_by_a_property(gom, films):
    q = refs(Movie)
    rows = gom.groups.load(Movie, keys={"year": key(q.released)}, aggregates={"films": count()})
    assert rows == [
        {"year": 1991, "films": 1},
        {"year": 1994, "films": 1},
        {"year": 1999, "films": 1},
        {"year": 2003, "films": 2},
    ]


def test_aggregates_with_filters(pm, gom, films):
    q = refs(Movie)
    rows = gom.groups.load(
        Movie,
        keys={"all": key("dummy")},  # a property no node has: one group of every film, keyed None
        aggregates={
            "films": count(),
            "matrix": count(q.title.starts_with("The Matrix")),
            "nineties": count(below(pm, "released", 2000)),
            "years": sum_(q.released, q.released.gt(2000)),
            "average": avg("released"),
            "first": min_(q.released),
            "last": max_(q.released),
        },
    )
    assert rows == [
        {
            "all": None,
            "films": 5,
            "matrix": 3,
            "nineties": 3,
            "years": 4006,
            "average": 1998.0,
            "first": 1991,
            "last": 2003,
        }
    ]


def test_groups_by_the_node_a_path_reaches(gom, films):
    rows = gom.groups.load(
        Movie,
        keys={"director": path_key(Director, [hop("DIRECTED_BY")])},
        aggregates={"films": count(), "latest": max_("released")},
        order_by=[("films", "desc")],
    )
    assert [(type(it["director"]), it["director"].name, it["films"], it["latest"]) for it in rows] == [
        (Director, "Lana", 3, 2003),  # The Matrix, directed twice over, is one member of Lana's group
        (Director, "Jan", 1, 1994),
        (Director, "Kathryn", 1, 1991),
    ]


def test_having_order_and_pages(gom, films):
    q = refs(Movie)
    rows = gom.groups.load(
        Movie,
        keys={"year": key(q.released)},
        aggregates={"films": count()},
        where=q.released.gt(1991),
        having=[("films", "gte", 1)],
        order_by=[("year", "desc")],
        skip=1,
        limit=2,
    )
    assert [it["year"] for it in rows] == [1999, 1994]
    big = gom.groups.load(
        Movie, keys={"year": key(q.released)}, aggregates={"films": count()}, having=[("films", "gt", 1)]
    )
    assert big == [{"year": 2003, "films": 2}]
    # A count compared with a fraction, as any number is.
    halfway = gom.groups.load(
        Movie, keys={"year": key(q.released)}, aggregates={"films": count()}, having=[("films", "gt", 1.5)]
    )
    assert halfway == [{"year": 2003, "films": 2}]


def test_a_compound_key_and_count_by(gom, films):
    q = refs(Movie)
    rows = gom.groups.load(
        Movie,
        keys={"year": key(q.released), "director": path_key(Director, [hop("DIRECTED_BY")])},
        aggregates={"n": count()},
    )
    assert [(it["year"], it["director"].name, it["n"]) for it in rows] == [
        (1991, "Kathryn", 1),
        (1994, "Jan", 1),
        (1999, "Lana", 1),
        (2003, "Lana", 2),
    ]
    assert gom.groups.count_by(Movie, "released", q.released.lt(2000)) == {1991: 1, 1994: 1, 1999: 1}


def test_refusals(gom, films):
    q = refs(Movie)
    with pytest.raises(DrivineError, match="having names year, which is not an aggregate"):
        gom.groups.load(Movie, keys={"year": key(q.released)}, having=[("year", "gt", 1)])
    with pytest.raises(DrivineError, match="order_by names votes"):
        gom.groups.load(Movie, keys={"year": key(q.released)}, order_by=[("votes", "desc")])
    with pytest.raises(DrivineError, match="no hop of it may name one"):
        path_key(Director, [hop("DIRECTED_BY", order_by="name")])
    with pytest.raises(DrivineError, match="Movie has no field rating"):
        gom.groups.load(Movie, keys={"r": key(q.rating)})
