from datetime import date, datetime

import pytest
from pydantic import BaseModel

from drivine import CursorSpecification, DrivineError, PersistenceManager, QuerySpecification, transaction

# Written in Cypher, which Postgres and Oracle compile to their SQL.
pytestmark = pytest.mark.written_in_cypher


class Person(BaseModel):
    name: str
    age: int
    born: date | None = None


async def make_people(pm: PersistenceManager, count: int) -> None:
    await pm.execute(
        QuerySpecification("UNWIND range(1, $count) AS i CREATE (:Person {name: 'p' + toString(i), age: i})").bind(
            {"count": count}
        )
    )


async def test_maps_a_node_to_its_properties(pm: PersistenceManager):
    await pm.execute(QuerySpecification("CREATE (:Person {name: 'Ada', age: 36})"))
    assert await pm.query(QuerySpecification("MATCH (p:Person) RETURN p")) == [{"name": "Ada", "age": 36}]


async def test_a_row_of_several_columns_is_a_list(pm: PersistenceManager):
    assert await pm.query(QuerySpecification("RETURN 1, 'two', [3]")) == [[1, "two", [3]]]


async def test_binds_named_parameters(pm: PersistenceManager):
    assert await pm.get_one(QuerySpecification("RETURN $a + $b").bind({"a": 1, "b": 2})) == 3


@pytest.mark.graph_engine_only("a Cypher statement compiled for a SQL store takes named parameters, not a list of them")
async def test_binds_positional_parameters(pm: PersistenceManager):
    assert await pm.get_one(QuerySpecification("RETURN $1 + $2").bind([1, 2])) == 3


async def test_transforms_to_a_model(pm: PersistenceManager):
    await make_people(pm, 3)
    spec = (
        QuerySpecification[Person]("MATCH (p:Person) RETURN properties(p) ORDER BY p.age")
        .transform(Person)
        .filter(lambda it: it.age > 1)
    )
    assert await pm.query(spec) == [Person(name="p2", age=2), Person(name="p3", age=3)]


@pytest.mark.graph_engine_only(
    "a SQL store gives a temporal value as its ISO 8601 text, which test_cypher_compiled.py checks"
)
async def test_temporal_values_are_the_standard_librarys(pm: PersistenceManager):
    value = await pm.get_one(QuerySpecification("RETURN date('2024-02-29')"))
    assert value == date(2024, 2, 29)
    stamp = await pm.get_one(QuerySpecification("RETURN localdatetime('2024-02-29T10:15:30')"))
    # FalkorDB has one datetime type, held in UTC, and its client returns it aware of that.
    assert stamp.replace(tzinfo=None) == datetime(2024, 2, 29, 10, 15, 30)


async def test_skip_and_limit(pm: PersistenceManager):
    await make_people(pm, 10)
    spec = QuerySpecification("MATCH (p:Person) RETURN p.age ORDER BY p.age").skip(2).limit(3)
    assert await pm.query(spec) == [3, 4, 5]


async def test_get_one_and_maybe_get_one(pm: PersistenceManager):
    await make_people(pm, 2)
    assert await pm.maybe_get_one(QuerySpecification("MATCH (p:Person {age: 9}) RETURN p")) is None
    assert await pm.maybe_get_one(QuerySpecification("MATCH (p:Person {age: 1}) RETURN p.name")) == "p1"
    with pytest.raises(DrivineError, match="Expected exactly one result, received 0"):
        await pm.get_one(QuerySpecification("MATCH (p:Person {age: 9}) RETURN p"))
    with pytest.raises(DrivineError, match="Expected one result, received 2"):
        await pm.maybe_get_one(QuerySpecification("MATCH (p:Person) RETURN p"))


async def test_a_bad_statement_raises_with_the_database_error(pm: PersistenceManager):
    with pytest.raises(DrivineError) as raised:
        await pm.query(QuerySpecification("MATCH (n RETURN n"))
    assert raised.value.root_cause is not None
    assert raised.value.spec is not None


class TestCursors:
    async def test_streams_outside_a_transaction(self, pm: PersistenceManager):
        await make_people(pm, 25)
        spec = (
            CursorSpecification[int]("MATCH (p:Person) RETURN p.age ORDER BY p.age")
            .batch_size(4)
            .filter(lambda it: it % 5 == 0)
        )
        async with await pm.open_cursor(spec) as cursor:
            assert [it async for it in cursor] == [5, 10, 15, 20, 25]

    async def test_closing_part_way_releases_it(self, pm: PersistenceManager):
        await make_people(pm, 50)
        cursor = await pm.open_cursor(CursorSpecification[int]("MATCH (p:Person) RETURN p.age").batch_size(5))
        assert isinstance(await anext(cursor), int)
        await cursor.close()
        # The connection went back to the pool: the next statement gets one.
        assert await pm.get_one(QuerySpecification("MATCH (p:Person) RETURN count(p)")) == 50

    @pytest.mark.rolls_back
    async def test_interleaves_with_statements_in_a_transaction(self, pm: PersistenceManager):
        await make_people(pm, 12)
        seen: list[int] = []
        async with transaction(rollback=True):
            cursor = await pm.open_cursor(
                CursorSpecification[int]("MATCH (p:Person) RETURN p.age ORDER BY p.age").batch_size(5)
            )
            async for age in cursor:
                seen.append(age)
                if age == 6:
                    await pm.execute(QuerySpecification("CREATE (:Marker {at: $age})").bind({"age": age}))
            assert await pm.get_one(QuerySpecification("MATCH (m:Marker) RETURN m.at")) == 6
        assert seen == list(range(1, 13))
        assert await pm.query(QuerySpecification("MATCH (m:Marker) RETURN m")) == []
