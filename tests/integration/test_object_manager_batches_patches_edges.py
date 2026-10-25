import json
from typing import Annotated

import pytest

from drivine import (
    DrivineError,
    GraphProperty,
    GraphView,
    JsonPacked,
    NodeFragment,
    NodeId,
    NodeStamp,
    NullPolicy,
    PatchResult,
    PersistenceManager,
    RelateMode,
    Relationship,
    RelationshipFragment,
    Root,
    StaleObjectError,
    StatelessGraphObjectManager,
    TargetNode,
    node_ref,
    transaction,
)
from tests.integration.stores import equals, nodes
from tests.models import Customer, Dog, Organization, Person, PersonCareer, Puppy, WorkHistory


class Coded(NodeFragment, labels=["Coded"]):
    """Set fields: one stored under another name, one packed as JSON text, and one with a default."""

    id: Annotated[str, NodeId()]
    codes: Annotated[set[str] | None, GraphProperty("code_set")] = None
    packed: Annotated[set[str] | None, JsonPacked()] = None
    badges: set[str] = set()
    stamp: Annotated[str | None, NodeStamp()] = None


class Coding(RelationshipFragment):
    reasons: set[str] = set()
    target: Annotated[Coded, TargetNode()]


class CodedHolder(GraphView):
    root: Annotated[Coded, Root()]
    codings: Annotated[list[Coding], Relationship("CODING")] = []


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


async def bios(pm: PersistenceManager) -> dict[str, object]:
    """Each Person's bio, by uuid, as the store holds it."""
    return {it["uuid"]: it.get("bio") for it in await nodes(pm, "Person")}


class TestSaveAll:
    async def test_saves_fragments_and_views_of_any_classes_together(self, pm, gom):
        people = [Person(uuid=f"p{it}", name=f"n{it}") for it in range(5)]
        career = PersonCareer(
            person=Person(uuid="p9", name="Ada"),
            employment_history=[
                WorkHistory(start_date="2020", role="Lead", target=Organization(uuid="o1", name="Acme"))
            ],
        )
        customer = Customer(uuid="c1", full_name="Ada", metadata={"source": "wiki"})
        saved = await gom.save_all([*people, Organization(uuid="o2", name="B"), career, customer])
        assert saved[0] is people[0]
        assert all(it.stamp is not None for it in people)
        assert await gom.count(Person) == 6
        assert await gom.count(Organization) == 2
        loaded = await gom.load(Customer, "c1")
        assert loaded is not None and loaded.metadata == {"source": "wiki"}

    async def test_hands_each_fragment_its_own_stamp_when_given_out_of_the_order_they_are_written_in(self, gom):
        await gom.save(Person(uuid="p1", name="Before"))
        p1 = await gom.load(Person, "p1")
        p1.name = "One"
        objects = [Person(uuid="p2", name="Two"), Organization(uuid="o1", name="Acme"), p1]
        saved = await gom.save_all(objects)
        assert [it.uuid for it in saved] == ["p2", "o1", "p1"]
        assert saved[0].stamp == (await gom.load(Person, "p2")).stamp
        assert saved[2].stamp == (await gom.load(Person, "p1")).stamp
        assert saved[0].stamp != saved[2].stamp

    @pytest.mark.rolls_back
    async def test_a_batch_that_fails_writes_nothing(self, gom):
        await gom.save(Person(uuid="p1", name="Ada"))
        stale = await gom.load(Person, "p1")
        fresh = await gom.load(Person, "p1")
        fresh.name = "changed"
        await gom.save(fresh)
        stale_view = PersonCareer(person=stale)
        with pytest.raises(StaleObjectError):
            await gom.save_all([Person(uuid="p2", name="Grace"), stale_view])
        assert await gom.count(Person) == 1  # Grace, in the same batch, was rolled back

    async def test_splits_a_large_batch_into_statements(self, gom):
        await gom.save_all([Person(uuid=f"p{it:04}", name="x") for it in range(2500)])
        assert await gom.count(Person) == 2500

    async def test_refuses_replace_with_a_fragment(self, gom):
        with pytest.raises(DrivineError, match="replace applies to views"):
            await gom.save_all([Person(uuid="p1", name="Ada")], replace=["x"])

    @pytest.mark.rolls_back
    async def test_joins_the_callers_transaction(self, gom):
        with pytest.raises(ValueError):
            async with transaction():
                await gom.save_all([Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Grace")])
                raise ValueError
        assert await gom.count(Person) == 0


class TestPatch:
    async def test_writes_named_fields_where_the_condition_holds(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada", bio="b0"))
        loaded = await gom.load(Person, "p1")
        result = await gom.patch(
            Person, "p1", set={"bio": "b1"}, where=equals(pm, "name", "name"), bindings={"name": "Ada"}
        )
        assert result == PatchResult(found=True, applied=True, changed=True)
        assert await bios(pm) == {"p1": "b1"}
        with pytest.raises(StaleObjectError):  # the patch gave the node a new stamp
            await gom.save(loaded)
        unchanged = await gom.patch(Person, "p1", set={"bio": "b1"})
        assert unchanged == PatchResult(True, True, False)
        refused = await gom.patch(
            Person, "p1", set={"bio": "b2"}, where=equals(pm, "name", "name"), bindings={"name": "Grace"}
        )
        assert refused == PatchResult(True, False, False)
        assert await gom.patch(Person, "missing", set={"bio": "x"}) == PatchResult(False, False, False)

    async def test_clears_and_refuses_what_it_cant_write(self, pm, gom):
        await gom.save(Person(uuid="p1", name="Ada", bio="b0"))
        await gom.patch(Person, "p1", clear=["bio"])
        assert await bios(pm) == {"p1": None}
        with pytest.raises(DrivineError, match="can't write uuid"):
            await gom.patch(Person, "p1", set={"uuid": "p2"})
        with pytest.raises(DrivineError, match="no field nmae"):
            await gom.patch(Person, "p1", set={"nmae": "x"})

    async def test_patch_all(self, pm, gom):
        await gom.save_all([Person(uuid="p1", name="Ada", bio="a"), Person(uuid="p2", name="Grace", bio="g")])
        results = await gom.patch_all(
            [
                Person(uuid="p2", name="ignored", bio="G"),
                Person(uuid="p3", name="x"),
                Person(uuid="p1", name="y", bio="a"),
            ],
            only=["bio"],
        )
        assert results == [
            PatchResult(True, True, True),
            PatchResult(False, False, False),
            PatchResult(True, True, False),
        ]
        rows = [[it["uuid"], it["name"], it.get("bio")] for it in await nodes(pm, "Person")]
        assert rows == [["p1", "Ada", "a"], ["p2", "Grace", "G"]]
        await gom.patch_all([Person(uuid="p1", name="Ada", bio=None)], only=["bio"], null_policy=NullPolicy.CLEAR)
        assert (await bios(pm))["p1"] is None
        with pytest.raises(DrivineError, match="only or except_"):
            await gom.patch_all([Person(uuid="p1", name="x")])
        with pytest.raises(DrivineError, match="each id once"):
            await gom.patch_all([Person(uuid="p1", name="x"), Person(uuid="p1", name="y")], only=["name"])


class TestEdges:
    async def test_relate_load_and_unrelate(self, gom):
        await gom.save_all([Person(uuid="p1", name="Ada"), Organization(uuid="o1", name="Acme")])
        ada, acme = node_ref(Person, "p1"), node_ref(Organization, "o1")
        assert await gom.edges.relate(ada, acme, "WORKS_FOR", {"startDate": "2020", "role": "Lead"})
        assert await gom.edges.relate(ada, acme, "WORKS_FOR", {"startDate": "2020", "role": "Lead"})  # merged
        assert not await gom.edges.relate(ada, node_ref(Organization, "missing"), "WORKS_FOR")
        career = await gom.load(PersonCareer, "p1")
        assert career is not None and [(it.role, it.target.name) for it in career.employment_history] == [
            ("Lead", "Acme")
        ]
        assert [it.name for it in await gom.edges.load_related(ada, "WORKS_FOR", Organization)] == ["Acme"]
        assert await gom.edges.unrelate(ada, acme, "WORKS_FOR") == 1
        assert await gom.edges.load_related(ada, "WORKS_FOR", Organization) == []

    async def test_create_mode_and_unrelate_all(self, gom):
        await gom.save_all([Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Grace")])
        ada, grace = node_ref(Person, "p1"), node_ref(Person, "p2")
        await gom.edges.relate(ada, grace, "KNOWS", mode=RelateMode.CREATE)
        await gom.edges.relate(ada, grace, "KNOWS", mode=RelateMode.CREATE)
        assert await gom.edges.unrelate_all(ada, "KNOWS") == 2

    async def test_relating_marks_both_ends_so_a_stale_replace_is_refused(self, gom):
        await gom.save_all([Person(uuid="p1", name="Ada"), Organization(uuid="o1", name="Acme")])
        career = await gom.load(PersonCareer, "p1")
        await gom.edges.relate(
            node_ref(Person, "p1"), node_ref(Organization, "o1"), "WORKS_FOR", {"startDate": "x", "role": "y"}
        )
        with pytest.raises(StaleObjectError):
            await gom.save(career, replace=["employment_history"])

    async def test_loads_the_subclass_each_node_is(self, gom):
        await gom.save_all([Person(uuid="p1", name="Ada"), Dog(uuid="d1", name="Rex"), Puppy(uuid="d2", name="Bit")])
        for it in ("d1", "d2"):
            await gom.edges.relate(node_ref(Person, "p1"), node_ref(Dog, it), "OWNS")
        owned = await gom.edges.load_related(node_ref(Person, "p1"), "OWNS", Dog)
        assert sorted(type(it).__name__ for it in owned) == ["Dog", "Puppy"]


class TestSets:
    """No store holds a set, so patch and relate write one as the sorted list of its items, as save does."""

    async def test_patch_writes_a_set_as_its_sorted_items_packed_or_not_and_loads_it_back_as_a_set(self, pm, gom):
        await gom.save(Coded(id="c1", codes={"old"}))
        patched = await gom.patch(
            Coded, "c1", set={"codes": {"y", "x"}, "packed": {"q", "p"}, "badges": {"silver", "gold"}}
        )
        assert patched == PatchResult(True, True, True)
        [props] = await nodes(pm, "Coded")
        assert (props["code_set"], json.loads(props["packed"]), props["badges"]) == (
            ["x", "y"],
            ["p", "q"],
            ["gold", "silver"],
        )
        loaded = await gom.load(Coded, "c1")
        assert loaded is not None
        assert (loaded.codes, loaded.packed, loaded.badges) == ({"x", "y"}, {"p", "q"}, {"gold", "silver"})

        # A loaded set handed back with an item added, and an empty one.
        assert loaded.packed is not None
        await gom.patch(Coded, "c1", set={"packed": {*loaded.packed, "r"}, "badges": set()})
        [props] = await nodes(pm, "Coded")
        assert (json.loads(props["packed"]), props["badges"]) == (["p", "q", "r"], [])
        again = await gom.load(Coded, "c1")
        assert again is not None and (again.packed, again.badges) == ({"p", "q", "r"}, set())

    async def test_patch_all_writes_sets_as_their_sorted_items_packed_or_not(self, pm, gom):
        await gom.save_all([Coded(id="a1"), Coded(id="a2")])
        patched = [
            Coded(id="a1", codes={"x"}, packed={"p"}, badges={"gold"}),
            Coded(id="a2", codes={"z", "y"}, packed={"r", "q"}, badges=set()),
        ]
        await gom.patch_all(patched, only=["codes", "packed", "badges"], null_policy=NullPolicy.CLEAR)
        rows = [[it["code_set"], json.loads(it["packed"]), it["badges"]] for it in await nodes(pm, "Coded")]
        assert rows == [[["x"], ["p"], ["gold"]], [["y", "z"], ["q", "r"], []]]
        loaded = await gom.load(Coded, "a2")
        assert loaded is not None and (loaded.codes, loaded.packed, loaded.badges) == ({"y", "z"}, {"q", "r"}, set())

    async def test_relate_writes_a_set_property_as_its_sorted_items_and_loads_it_in_a_relationship_fragment(self, gom):
        await gom.save_all([Coded(id="root"), Coded(id="target")])
        root, target = node_ref(Coded, "root"), node_ref(Coded, "target")
        assert await gom.edges.relate(root, target, "CODING", {"reasons": {"why", "because"}})
        loaded = await gom.load(CodedHolder, "root")
        assert loaded is not None and [(it.reasons, it.target.id) for it in loaded.codings] == [
            ({"because", "why"}, "target")
        ]

    async def test_relating_again_with_the_same_set_finds_the_relationship_as_it_is(self, gom):
        await gom.save_all([Coded(id="root"), Coded(id="target")])
        root, target = node_ref(Coded, "root"), node_ref(Coded, "target")
        await gom.edges.relate(root, target, "CODING", {"reasons": {"why", "because", "how"}})
        before = await gom.load(Coded, "root")
        assert before is not None

        # The same set, built in another order: stored as the same sorted list, so neither end is marked.
        assert await gom.edges.relate(root, target, "CODING", {"reasons": {"how", "because", "why"}})
        after = await gom.load(Coded, "root")
        assert after is not None and after.stamp == before.stamp
        holder = await gom.load(CodedHolder, "root")
        assert holder is not None and [it.reasons for it in holder.codings] == [{"because", "how", "why"}]
