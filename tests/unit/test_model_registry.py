from typing import Annotated

import pytest

from drivine import (
    Count,
    DrivineError,
    GraphPath,
    GraphProperty,
    GraphView,
    ModelRegistry,
    NodeFragment,
    NodeId,
    NodeStamp,
    PropertyBag,
    ReadOnly,
    Relationship,
    RelationshipFragment,
    Root,
    SortedBy,
    TargetNode,
    hop,
)
from drivine.model.fragment_model import FragmentModel
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from tests.models import (
    ActorStats,
    ActorWithLatestMovie,
    ActorWithMovies,
    Animal,
    Customer,
    Dog,
    LocationHierarchy,
    Person,
    PersonCareer,
    Puppy,
)


@pytest.fixture
def registry() -> ModelRegistry:
    return ModelRegistry()


class TestFragments:
    def test_names_properties_in_camel_case(self, registry):
        model = registry.fragment(Customer)
        assert model.graph_properties["full_name"] == "fullName"
        assert model.graph_properties["legacy_code"] == "LEGACY"
        assert "scratch" not in model.graph_properties
        assert model.node_id_property == "uuid"
        assert [(it.field, it.namespace, it.flat) for it in model.property_bags] == [
            ("metadata", "metadata.", False),
            ("extras", "", True),
        ]
        assert model.node_labels is not None and model.node_labels.members == ("Gold", "Silver")

    def test_a_registry_can_name_them_as_written(self):
        assert ModelRegistry("as_written").fragment(Customer).graph_properties["full_name"] == "full_name"

    def test_labels_are_inherited_own_first(self, registry):
        assert registry.fragment(Puppy).labels == ["Puppy", "Dog", "Animal"]

    def test_picks_the_most_specific_subtype_a_node_carries(self, registry):
        animal = registry.fragment(Animal)
        assert registry.fragment_for(animal, ["Animal", "Dog"]).type is Dog
        assert registry.fragment_for(animal, ["Puppy", "Animal", "Dog"]).type is Puppy
        assert registry.fragment_for(animal, ["Animal", "Puppy"]).type is Animal

    def test_needs_a_label(self, registry):
        class Unlabelled(NodeFragment):
            name: str

        with pytest.raises(DrivineError, match="has no labels"):
            registry.fragment(Unlabelled)

    def test_labels_must_be_a_list(self):
        with pytest.raises(TypeError, match="labels is a list"):

            class Wrong(NodeFragment, labels="Wrong"):  # type: ignore[arg-type]
                pass

    def test_one_id_and_one_stamp(self, registry):
        class TwoIds(NodeFragment, labels=["T"]):
            a: Annotated[str, NodeId()]
            b: Annotated[str, NodeId()]

        class TwoStamps(NodeFragment, labels=["T"]):
            a: Annotated[str | None, NodeStamp()] = None
            b: Annotated[str | None, NodeStamp()] = None

        with pytest.raises(DrivineError, match="2 NodeId fields"):
            registry.fragment(TwoIds)
        with pytest.raises(DrivineError, match="2 NodeStamp fields"):
            registry.fragment(TwoStamps)

    def test_refuses_two_fields_stored_as_one_property(self, registry):
        class Clash(NodeFragment, labels=["T"]):
            full_name: str
            other: Annotated[str, GraphProperty("fullName")]

        with pytest.raises(DrivineError, match="both stored as 'fullName'"):
            registry.fragment(Clash)

    def test_refuses_a_view_marker(self, registry):
        class Misplaced(NodeFragment, labels=["T"]):
            friend: Annotated[str, Relationship("KNOWS")]

        with pytest.raises(DrivineError, match="Relationship can't be used on a NodeFragment"):
            registry.fragment(Misplaced)

    def test_refuses_overlapping_bags(self, registry):
        class Overlap(NodeFragment, labels=["T"]):
            a: Annotated[dict[str, str], PropertyBag(prefix="m")] = {}
            b: Annotated[dict[str, str], PropertyBag(prefix="m.x")] = {}

        with pytest.raises(DrivineError, match="overlap"):
            registry.fragment(Overlap)


class TestViews:
    def test_reads_what_a_relationship_holds_from_its_annotation(self, registry):
        career = registry.view(PersonCareer)
        rel = career.relationships[0]
        assert (rel.field, rel.key, rel.type, rel.is_collection, rel.required) == (
            "employment_history",
            "employmentHistory",
            "WORKS_FOR",
            True,
            False,
        )
        assert isinstance(rel.related, RelationshipFragmentModel)
        assert isinstance(rel.related.target, FragmentModel)

    def test_a_single_relationship_without_none_is_required(self, registry):
        assert registry.view(ActorWithLatestMovie).relationships[0].required
        assert registry.view(ActorWithMovies).relationships[0].required  # a list that says so

    def test_a_view_may_refer_to_itself(self, registry):
        hierarchy = registry.view(LocationHierarchy)
        assert hierarchy.relationships[0].related is hierarchy

    def test_paths_and_aggregates(self, registry):
        stats = registry.view(ActorStats)
        assert [it.key for it in stats.aggregates] == ["movieCount", "averageRating", "bestRating"]
        assert [(it.field, it.is_collection, it.required) for it in stats.paths] == [
            ("directors", True, False),
            ("first_director", False, False),
        ]

    def test_a_view_needs_one_root_of_a_fragment(self, registry):
        class NoRoot(GraphView):
            person: Person

        class RootOfAView(GraphView):
            career: Annotated[PersonCareer, Root()]

        with pytest.raises(DrivineError, match="not a root, relationship, path or aggregate"):
            registry.view(NoRoot)
        with pytest.raises(DrivineError, match="must be a NodeFragment"):
            registry.view(RootOfAView)

    def test_refuses_what_a_relationship_cant_hold(self, registry):
        class HoldsText(GraphView):
            person: Annotated[Person, Root()]
            notes: Annotated[list[str], Relationship("HAS_NOTE")] = []

        class Knows(RelationshipFragment):
            since: int
            target: Annotated[Person, TargetNode()]

        class DeepFragments(GraphView):
            person: Annotated[Person, Root()]
            friends: Annotated[list[Knows], Relationship("KNOWS", max_depth=2)] = []

        with pytest.raises(DrivineError, match="holds str, which is not"):
            registry.view(HoldsText)
        with pytest.raises(DrivineError, match="max_depth can't be above 1"):
            registry.view(DeepFragments)

    def test_checks_markers_on_view_fields(self, registry):
        class SortedSingle(GraphView):
            person: Annotated[Person, Root()]
            boss: Annotated[Person | None, Relationship("REPORTS_TO"), SortedBy("name")] = None

        class ReadOnlyPlain(GraphView):
            person: Annotated[Person, Root()]
            note: Annotated[str, ReadOnly()] = ""

        class TwoOrders(GraphView):
            person: Annotated[Person, Root()]
            x: Annotated[list[Person], GraphPath([hop("A", order_by="a"), hop("B", order_by="b")])] = []

        class CountAndRelationship(GraphView):
            person: Annotated[Person, Root()]
            n: Annotated[int, Count("KNOWS"), Relationship("KNOWS")] = 0

        with pytest.raises(DrivineError, match="sorts a list"):
            registry.view(SortedSingle)
        with pytest.raises(DrivineError, match="marks a relationship field"):
            registry.view(ReadOnlyPlain)
        with pytest.raises(DrivineError, match="order on more than one hop"):
            registry.view(TwoOrders)
        with pytest.raises(DrivineError, match="can be one of"):
            registry.view(CountAndRelationship)
