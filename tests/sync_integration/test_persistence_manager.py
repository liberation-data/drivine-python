# Written by scripts/unasync.py from tests/integration/test_persistence_manager.py:
# change that, and run the script again.
from datetime import date, datetime

import pytest
from pydantic import BaseModel

from drivine.sync import CursorSpecification, DrivineError, PersistenceManager, QuerySpecification, transaction

# Written in Cypher, which Postgres and Oracle compile to their SQL.
pytestmark = pytest.mark.written_in_cypher


class Person(BaseModel):
    name: str
    age: int
    born: date | None = None


def make_people(pm: PersistenceManager, count: int) -> None:
    pm.execute(
        QuerySpecification("UNWIND range(1, $count) AS i CREATE (:Person {name: 'p' + toString(i), age: i})").bind(
            {"count": count}
        )
    )


def test_maps_a_node_to_its_properties(pm: PersistenceManager):
    pm.execute(QuerySpecification("CREATE (:Person {name: 'Ada', age: 36})"))
    assert pm.query(QuerySpecification("MATCH (p:Person) RETURN p")) == [{"name": "Ada", "age": 36}]


def test_a_row_of_several_columns_is_a_list(pm: PersistenceManager):
    assert pm.query(QuerySpecification("RETURN 1, 'two', [3]")) == [[1, "two", [3]]]


def test_binds_named_parameters(pm: PersistenceManager):
    assert pm.get_one(QuerySpecification("RETURN $a + $b").bind({"a": 1, "b": 2})) == 3


@pytest.mark.graph_engine_only("a Cypher statement compiled for a SQL store takes named parameters, not a list of them")
def test_binds_positional_parameters(pm: PersistenceManager):
    assert pm.get_one(QuerySpecification("RETURN $1 + $2").bind([1, 2])) == 3


def test_transforms_to_a_model(pm: PersistenceManager):
    make_people(pm, 3)
    spec = (
        QuerySpecification[Person]("MATCH (p:Person) RETURN properties(p) ORDER BY p.age")
        .transform(Person)
        .filter(lambda it: it.age > 1)
    )
    assert pm.query(spec) == [Person(name="p2", age=2), Person(name="p3", age=3)]


@pytest.mark.graph_engine_only(
    "a SQL store gives a temporal value as its ISO 8601 text, which test_cypher_compiled.py checks"
)
def test_temporal_values_are_the_standard_librarys(pm: PersistenceManager):
    value = pm.get_one(QuerySpecification("RETURN date('2024-02-29')"))
    assert value == date(2024, 2, 29)
    stamp = pm.get_one(QuerySpecification("RETURN localdatetime('2024-02-29T10:15:30')"))
    # FalkorDB has one datetime type, held in UTC, and its client returns it aware of that.
    assert stamp.replace(tzinfo=None) == datetime(2024, 2, 29, 10, 15, 30)


def test_skip_and_limit(pm: PersistenceManager):
    make_people(pm, 10)
    spec = QuerySpecification("MATCH (p:Person) RETURN p.age ORDER BY p.age").skip(2).limit(3)
    assert pm.query(spec) == [3, 4, 5]


def test_get_one_and_maybe_get_one(pm: PersistenceManager):
    make_people(pm, 2)
    assert pm.maybe_get_one(QuerySpecification("MATCH (p:Person {age: 9}) RETURN p")) is None
    assert pm.maybe_get_one(QuerySpecification("MATCH (p:Person {age: 1}) RETURN p.name")) == "p1"
    with pytest.raises(DrivineError, match="Expected exactly one result, received 0"):
        pm.get_one(QuerySpecification("MATCH (p:Person {age: 9}) RETURN p"))
    with pytest.raises(DrivineError, match="Expected one result, received 2"):
        pm.maybe_get_one(QuerySpecification("MATCH (p:Person) RETURN p"))


def test_a_bad_statement_raises_with_the_database_error(pm: PersistenceManager):
    with pytest.raises(DrivineError) as raised:
        pm.query(QuerySpecification("MATCH (n RETURN n"))
    assert raised.value.root_cause is not None
    assert raised.value.spec is not None


class TestCursors:
    def test_streams_outside_a_transaction(self, pm: PersistenceManager):
        make_people(pm, 25)
        spec = (
            CursorSpecification[int]("MATCH (p:Person) RETURN p.age ORDER BY p.age")
            .batch_size(4)
            .filter(lambda it: it % 5 == 0)
        )
        with pm.open_cursor(spec) as cursor:
            assert [it for it in cursor] == [5, 10, 15, 20, 25]

    def test_closing_part_way_releases_it(self, pm: PersistenceManager):
        make_people(pm, 50)
        cursor = pm.open_cursor(CursorSpecification[int]("MATCH (p:Person) RETURN p.age").batch_size(5))
        assert isinstance(next(cursor), int)
        cursor.close()
        # The connection went back to the pool: the next statement gets one.
        assert pm.get_one(QuerySpecification("MATCH (p:Person) RETURN count(p)")) == 50

    @pytest.mark.rolls_back
    def test_interleaves_with_statements_in_a_transaction(self, pm: PersistenceManager):
        make_people(pm, 12)
        seen: list[int] = []
        with transaction(rollback=True):
            cursor = pm.open_cursor(
                CursorSpecification[int]("MATCH (p:Person) RETURN p.age ORDER BY p.age").batch_size(5)
            )
            for age in cursor:
                seen.append(age)
                if age == 6:
                    pm.execute(QuerySpecification("CREATE (:Marker {at: $age})").bind({"age": age}))
            assert pm.get_one(QuerySpecification("MATCH (m:Marker) RETURN m.at")) == 6
        assert seen == list(range(1, 13))
        assert pm.query(QuerySpecification("MATCH (m:Marker) RETURN m")) == []
