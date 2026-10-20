import pytest

from drivine import (
    DrivineError,
    PatchResult,
    PersistenceManager,
    StatelessGraphObjectManager,
    above,
    all_of,
    any_of,
    case_of,
    not_,
    refs,
    times,
)
from tests.integration.stores import below
from tests.models import (
    ActorStats,
    Animal,
    Customer,
    Dog,
    Movie,
    Organization,
    Person,
    PersonCareer,
    Puppy,
    Tier,
    WorkHistory,
)


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


@pytest.fixture
async def movies(gom: StatelessGraphObjectManager) -> None:
    await gom.save_all(
        [
            Movie(uuid="m1", title="The Matrix", released=1999),
            Movie(uuid="m2", title="The Matrix Reloaded", released=2003),
            Movie(uuid="m3", title="John Wick", released=2014),
            Movie(uuid="m4", title="Speed", released=1994),
        ]
    )


@pytest.fixture
async def careers(gom: StatelessGraphObjectManager) -> None:
    acme, babbage = Organization(uuid="o1", name="Acme"), Organization(uuid="o2", name="Babbage")

    def career(uuid: str, name: str, bio: str | None, *history: WorkHistory) -> PersonCareer:
        return PersonCareer(person=Person(uuid=uuid, name=name, bio=bio), employment_history=list(history))

    await gom.save_all(
        [
            career(
                "p1",
                "Ada",
                "Lead engineer",
                WorkHistory(start_date="2015", role="Engineer", target=babbage),
                WorkHistory(start_date="2020", role="Lead", target=acme),
            ),
            career("p2", "Grace", "Admiral", WorkHistory(start_date="2018", role="Lead", target=babbage)),
            career("p3", "Alan", None),
        ]
    )


def titles(movies: list[Movie]) -> list[str]:
    return sorted(it.title for it in movies)


class TestConditions:
    async def test_comparisons(self, gom, movies):
        q = refs(Movie)
        assert titles(await gom.load_all(Movie, where=q.released.eq(1999))) == ["The Matrix"]
        assert titles(await gom.load_all(Movie, where=q.released.gt(2003))) == ["John Wick"]
        assert titles(await gom.load_all(Movie, where=q.released.lte(1999))) == ["Speed", "The Matrix"]
        assert len(await gom.load_all(Movie, where=q.released.neq(1999))) == 3
        assert titles(await gom.load_all(Movie, where=q.uuid.in_(["m1", "m3"]))) == ["John Wick", "The Matrix"]
        assert len(await gom.load_all(Movie, where=q.uuid.not_in(["m1", "m3"]))) == 2

    async def test_text(self, gom, movies):
        q = refs(Movie)
        assert titles(await gom.load_all(Movie, where=q.title.starts_with("The"))) == [
            "The Matrix",
            "The Matrix Reloaded",
        ]
        assert titles(await gom.load_all(Movie, where=q.title.ends_with("Wick"))) == ["John Wick"]
        assert titles(await gom.load_all(Movie, where=q.title.contains("Reload"))) == ["The Matrix Reloaded"]
        assert titles(await gom.load_all(Movie, where=q.title.contains_ignore_case("matrix"))) == [
            "The Matrix",
            "The Matrix Reloaded",
        ]
        assert titles(await gom.load_all(Movie, where=q.title.eq_ignore_case("speed"))) == ["Speed"]
        assert titles(await gom.load_all(Movie, where=q.title.matches("J.*k"))) == ["John Wick"]

    async def test_logic(self, gom, movies):
        q = refs(Movie)
        either = any_of(q.released.eq(1994), q.title.eq("John Wick"))
        assert titles(await gom.load_all(Movie, where=either)) == ["John Wick", "Speed"]
        both = [q.title.starts_with("The"), q.released.gt(2000)]
        assert titles(await gom.load_all(Movie, where=both)) == ["The Matrix Reloaded"]
        assert titles(await gom.load_all(Movie, where=all_of(both))) == ["The Matrix Reloaded"]
        assert len(await gom.load_all(Movie, where=not_(q.title.starts_with("The")))) == 2

    async def test_nulls(self, gom, careers):
        q = refs(Person)
        assert [it.name for it in await gom.load_all(Person, where=q.bio.is_null())] == ["Alan"]
        assert len(await gom.load_all(Person, where=q.bio.is_not_null())) == 2
        with pytest.raises(DrivineError, match="use is_null"):
            q.bio.eq(None)

    async def test_across_relationships(self, gom, careers):
        q = refs(PersonCareer)

        async def names(where: object) -> list[str]:
            return sorted(it.person.name for it in await gom.load_all(PersonCareer, where=where))

        assert await names(q.employment_history.target.name.eq("Acme")) == ["Ada"]
        assert await names(q.employment_history.role.eq("Lead")) == ["Ada", "Grace"]
        assert await names(q.person.bio.contains("Lead")) == ["Ada"]
        # Separate conditions may be met by different relationships; any() wants one that meets both.
        separately = [q.employment_history.role.eq("Engineer"), q.employment_history.target.name.eq("Acme")]
        assert await names(separately) == ["Ada"]
        together = q.employment_history.any(lambda it: [it.role.eq("Engineer"), it.target.name.eq("Acme")])
        assert await names(together) == []
        assert await names(q.employment_history.none(lambda it: it.role.eq("Lead"))) == ["Alan"]

    async def test_labels_bags_and_runtime_keys(self, gom):
        await gom.save_all(
            [
                Animal(uuid="a1", name="Generic"),
                Dog(uuid="a2", name="Rex"),
                Puppy(uuid="a3", name="Bit"),
                Customer(uuid="c1", full_name="Ada", tags=["vip"], metadata={"source": "wiki"}, tiers={Tier.GOLD}),
                Customer(uuid="c2", full_name="Grace", metadata={"source": "crm"}),
            ]
        )
        a = refs(Animal)
        assert sorted(it.uuid for it in await gom.load_all(Animal, where=a.instance_of(Dog))) == ["a2", "a3"]
        assert [it.uuid for it in await gom.load_all(Animal, where=a.has_any_label("Puppy", "Cat"))] == ["a3"]
        c = refs(Customer)
        assert [it.uuid for it in await gom.load_all(Customer, where=c.metadata.source.eq("wiki"))] == ["c1"]
        assert [it.uuid for it in await gom.load_all(Customer, where=c.property("metadata.source").eq("crm"))] == ["c2"]
        assert [it.uuid for it in await gom.load_all(Customer, where=c.predicate("fullName", "STARTS_WITH", "Gr"))] == [
            "c2"
        ]
        assert [it.uuid for it in await gom.load_all(Customer, where=c.tiers.has_item("Gold"))] == ["c1"]
        assert [it.uuid for it in await gom.load_all(Customer, where=c.tags.has_item("vip"))] == ["c1"]


class TestOrderAndPaging:
    async def test_order_limit_skip(self, gom, movies):
        q = refs(Movie)
        newest = await gom.load_all(Movie, order_by=q.released.desc(), limit=2)
        assert [it.released for it in newest] == [2014, 2003]
        page = await gom.load_all(Movie, order_by=q.released.asc(), skip=1, limit=2)
        assert [it.released for it in page] == [1999, 2003]

    async def test_nulls_placed(self, gom, careers):
        q = refs(Person)
        last = await gom.load_all(Person, order_by=q.bio.asc().nulls_last())
        assert last[-1].name == "Alan"
        first = await gom.load_all(Person, order_by=q.bio.asc().nulls_first())
        assert first[0].name == "Alan"

    async def test_seek_pages_through(self, gom):
        await gom.save_all([Movie(uuid=f"m{it:02}", title=f"t{it}", released=2000 + it % 4) for it in range(10)])
        q = refs(Movie)
        seen: list[str] = []
        cursor: Movie | None = None
        while True:
            seek = [q.released.after(cursor.released), q.uuid.after(cursor.uuid)] if cursor else None
            page = await gom.load_all(Movie, order_by=[q.released.desc(), q.uuid.desc()], seek=seek, limit=3)
            if not page:
                break
            seen.extend(it.uuid for it in page)
            cursor = page[-1]
        assert len(seen) == 10 and len(set(seen)) == 10
        everything = await gom.load_all(Movie, order_by=[q.released.desc(), q.uuid.desc()])
        assert seen == [it.uuid for it in everything]

    async def test_seek_refusals(self, gom):
        q = refs(Movie)
        with pytest.raises(DrivineError, match="give order_by"):
            await gom.load_all(Movie, seek=q.released.after(1))
        with pytest.raises(DrivineError, match="two ways to page"):
            await gom.load_all(Movie, order_by=q.released.asc(), seek=q.released.after(1), skip=1)
        with pytest.raises(DrivineError, match="places its nulls"):
            await gom.load_all(Movie, order_by=q.released.asc().nulls_last(), seek=q.released.after(1))

    async def test_an_order_by_an_expression(self, gom, movies):
        q = refs(Movie)
        # Films before 2010 newest first, ranked by minus their year, then the rest, ranked 0.
        ranking = case_of([above(q.released, 2010).then(0)], times(q.released, -1))
        ranked = await gom.load_all(Movie, order_by=[ranking.asc(), q.uuid.asc()])
        assert [it.title for it in ranked] == ["The Matrix Reloaded", "The Matrix", "Speed", "John Wick"]

    async def test_orders_each_views_list(self, gom, careers):
        q = refs(PersonCareer)
        ada = await gom.load_all(
            PersonCareer, where=q.person.name.eq("Ada"), order_by=q.employment_history.start_date.desc()
        )
        assert [it.start_date for it in ada[0].employment_history] == ["2020", "2015"]


class TestOtherUses:
    async def test_count_delete_and_patch_take_conditions(self, gom, careers):
        q = refs(Person)
        assert await gom.count(Person, q.name.starts_with("A")) == 2
        result = await gom.patch(Person, "p2", set={"bio": "Rear admiral"}, where=q.name.eq("Grace"))
        assert result == PatchResult(True, True, True)
        assert await gom.delete(Person, "p1", where=q.name.eq("Nobody")) == 0
        assert await gom.delete_all(Person, q.bio.is_null()) == 1
        assert sorted(it.name for it in await gom.load_all(Person)) == ["Ada", "Grace"]

    async def test_a_raw_where_still_works_beside_an_order(self, pm, gom, movies):
        q = refs(Movie)
        found = await gom.load_all(Movie, below(pm, "released", 2010), order_by=q.released.desc())
        assert [it.released for it in found] == [2003, 1999, 1994]

    async def test_names_a_field_the_class_doesnt_have(self, gom):
        q = refs(PersonCareer)
        with pytest.raises(DrivineError, match="Person has no field nmae"):
            await gom.load_all(PersonCareer, where=q.person.nmae.eq("Ada"))
        with pytest.raises(DrivineError, match="computed when the view is read"):
            await gom.load_all(ActorStats, where=refs(ActorStats).movie_count.gt(1))
        with pytest.raises(TypeError, match="with .eq"):
            q.person.name == "Ada"  # noqa: B015
