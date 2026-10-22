# Written by scripts/unasync.py from tests/integration/test_object_manager_batches_patches_edges.py:
# change that, and run the script again.
import pytest

from drivine.sync import (
    DrivineError,
    NullPolicy,
    PatchResult,
    PersistenceManager,
    RelateMode,
    StaleObjectError,
    StatelessGraphObjectManager,
    node_ref,
    transaction,
)
from tests.models import Customer, Dog, Organization, Person, PersonCareer, Puppy, WorkHistory
from tests.sync_integration.stores import equals, nodes


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


def bios(pm: PersistenceManager) -> dict[str, object]:
    """Each Person's bio, by uuid, as the store holds it."""
    return {it["uuid"]: it.get("bio") for it in nodes(pm, "Person")}


class TestSaveAll:
    def test_saves_fragments_and_views_of_any_classes_together(self, pm, gom):
        people = [Person(uuid=f"p{it}", name=f"n{it}") for it in range(5)]
        career = PersonCareer(
            person=Person(uuid="p9", name="Ada"),
            employment_history=[
                WorkHistory(start_date="2020", role="Lead", target=Organization(uuid="o1", name="Acme"))
            ],
        )
        customer = Customer(uuid="c1", full_name="Ada", metadata={"source": "wiki"})
        saved = gom.save_all([*people, Organization(uuid="o2", name="B"), career, customer])
        assert saved[0] is people[0]
        assert all(it.stamp is not None for it in people)
        assert gom.count(Person) == 6
        assert gom.count(Organization) == 2
        loaded = gom.load(Customer, "c1")
        assert loaded is not None and loaded.metadata == {"source": "wiki"}

    def test_hands_each_fragment_its_own_stamp_when_given_out_of_the_order_they_are_written_in(self, gom):
        gom.save(Person(uuid="p1", name="Before"))
        p1 = gom.load(Person, "p1")
        p1.name = "One"
        objects = [Person(uuid="p2", name="Two"), Organization(uuid="o1", name="Acme"), p1]
        saved = gom.save_all(objects)
        assert [it.uuid for it in saved] == ["p2", "o1", "p1"]
        assert saved[0].stamp == (gom.load(Person, "p2")).stamp
        assert saved[2].stamp == (gom.load(Person, "p1")).stamp
        assert saved[0].stamp != saved[2].stamp

    @pytest.mark.rolls_back
    def test_a_batch_that_fails_writes_nothing(self, gom):
        gom.save(Person(uuid="p1", name="Ada"))
        stale = gom.load(Person, "p1")
        fresh = gom.load(Person, "p1")
        fresh.name = "changed"
        gom.save(fresh)
        stale_view = PersonCareer(person=stale)
        with pytest.raises(StaleObjectError):
            gom.save_all([Person(uuid="p2", name="Grace"), stale_view])
        assert gom.count(Person) == 1  # Grace, in the same batch, was rolled back

    def test_splits_a_large_batch_into_statements(self, gom):
        gom.save_all([Person(uuid=f"p{it:04}", name="x") for it in range(2500)])
        assert gom.count(Person) == 2500

    def test_refuses_replace_with_a_fragment(self, gom):
        with pytest.raises(DrivineError, match="replace applies to views"):
            gom.save_all([Person(uuid="p1", name="Ada")], replace=["x"])

    @pytest.mark.rolls_back
    def test_joins_the_callers_transaction(self, gom):
        with pytest.raises(ValueError):
            with transaction():
                gom.save_all([Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Grace")])
                raise ValueError
        assert gom.count(Person) == 0


class TestPatch:
    def test_writes_named_fields_where_the_condition_holds(self, pm, gom):
        gom.save(Person(uuid="p1", name="Ada", bio="b0"))
        loaded = gom.load(Person, "p1")
        result = gom.patch(Person, "p1", set={"bio": "b1"}, where=equals(pm, "name", "name"), bindings={"name": "Ada"})
        assert result == PatchResult(found=True, applied=True, changed=True)
        assert bios(pm) == {"p1": "b1"}
        with pytest.raises(StaleObjectError):  # the patch gave the node a new stamp
            gom.save(loaded)
        unchanged = gom.patch(Person, "p1", set={"bio": "b1"})
        assert unchanged == PatchResult(True, True, False)
        refused = gom.patch(
            Person, "p1", set={"bio": "b2"}, where=equals(pm, "name", "name"), bindings={"name": "Grace"}
        )
        assert refused == PatchResult(True, False, False)
        assert gom.patch(Person, "missing", set={"bio": "x"}) == PatchResult(False, False, False)

    def test_clears_and_refuses_what_it_cant_write(self, pm, gom):
        gom.save(Person(uuid="p1", name="Ada", bio="b0"))
        gom.patch(Person, "p1", clear=["bio"])
        assert bios(pm) == {"p1": None}
        with pytest.raises(DrivineError, match="can't write uuid"):
            gom.patch(Person, "p1", set={"uuid": "p2"})
        with pytest.raises(DrivineError, match="no field nmae"):
            gom.patch(Person, "p1", set={"nmae": "x"})

    def test_patch_all(self, pm, gom):
        gom.save_all([Person(uuid="p1", name="Ada", bio="a"), Person(uuid="p2", name="Grace", bio="g")])
        results = gom.patch_all(
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
        rows = [[it["uuid"], it["name"], it.get("bio")] for it in nodes(pm, "Person")]
        assert rows == [["p1", "Ada", "a"], ["p2", "Grace", "G"]]
        gom.patch_all([Person(uuid="p1", name="Ada", bio=None)], only=["bio"], null_policy=NullPolicy.CLEAR)
        assert (bios(pm))["p1"] is None
        with pytest.raises(DrivineError, match="only or except_"):
            gom.patch_all([Person(uuid="p1", name="x")])
        with pytest.raises(DrivineError, match="each id once"):
            gom.patch_all([Person(uuid="p1", name="x"), Person(uuid="p1", name="y")], only=["name"])


class TestEdges:
    def test_relate_load_and_unrelate(self, gom):
        gom.save_all([Person(uuid="p1", name="Ada"), Organization(uuid="o1", name="Acme")])
        ada, acme = node_ref(Person, "p1"), node_ref(Organization, "o1")
        assert gom.edges.relate(ada, acme, "WORKS_FOR", {"startDate": "2020", "role": "Lead"})
        assert gom.edges.relate(ada, acme, "WORKS_FOR", {"startDate": "2020", "role": "Lead"})  # merged
        assert not gom.edges.relate(ada, node_ref(Organization, "missing"), "WORKS_FOR")
        career = gom.load(PersonCareer, "p1")
        assert career is not None and [(it.role, it.target.name) for it in career.employment_history] == [
            ("Lead", "Acme")
        ]
        assert [it.name for it in gom.edges.load_related(ada, "WORKS_FOR", Organization)] == ["Acme"]
        assert gom.edges.unrelate(ada, acme, "WORKS_FOR") == 1
        assert gom.edges.load_related(ada, "WORKS_FOR", Organization) == []

    def test_create_mode_and_unrelate_all(self, gom):
        gom.save_all([Person(uuid="p1", name="Ada"), Person(uuid="p2", name="Grace")])
        ada, grace = node_ref(Person, "p1"), node_ref(Person, "p2")
        gom.edges.relate(ada, grace, "KNOWS", mode=RelateMode.CREATE)
        gom.edges.relate(ada, grace, "KNOWS", mode=RelateMode.CREATE)
        assert gom.edges.unrelate_all(ada, "KNOWS") == 2

    def test_relating_marks_both_ends_so_a_stale_replace_is_refused(self, gom):
        gom.save_all([Person(uuid="p1", name="Ada"), Organization(uuid="o1", name="Acme")])
        career = gom.load(PersonCareer, "p1")
        gom.edges.relate(
            node_ref(Person, "p1"), node_ref(Organization, "o1"), "WORKS_FOR", {"startDate": "x", "role": "y"}
        )
        with pytest.raises(StaleObjectError):
            gom.save(career, replace=["employment_history"])

    def test_loads_the_subclass_each_node_is(self, gom):
        gom.save_all([Person(uuid="p1", name="Ada"), Dog(uuid="d1", name="Rex"), Puppy(uuid="d2", name="Bit")])
        for it in ("d1", "d2"):
            gom.edges.relate(node_ref(Person, "p1"), node_ref(Dog, it), "OWNS")
        owned = gom.edges.load_related(node_ref(Person, "p1"), "OWNS", Dog)
        assert sorted(type(it).__name__ for it in owned) == ["Dog", "Puppy"]
