"""The models the object manager's tests load: TypeScript's sample, in Python."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from drivine import (
    Aggregate,
    AggregateFunction,
    CaseInsensitive,
    Count,
    Direction,
    EmptyWhenAbsent,
    GraphPath,
    GraphProperty,
    GraphView,
    JsonPacked,
    NodeFragment,
    NodeId,
    NodeLabels,
    NodeStamp,
    NullAsDefault,
    PropertyBag,
    Relationship,
    RelationshipFragment,
    Root,
    SortedBy,
    TargetNode,
    Transient,
    hop,
)


class Person(NodeFragment, labels=["Person"]):
    uuid: Annotated[str, NodeId()]
    name: str
    bio: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class Organization(NodeFragment, labels=["Organization"]):
    uuid: Annotated[str, NodeId()]
    name: str


class WorkHistory(RelationshipFragment):
    start_date: str
    role: str
    target: Annotated[Organization, TargetNode()]


class PersonCareer(GraphView):
    person: Annotated[Person, Root()]
    employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR"), SortedBy("start_date")] = []


class Location(NodeFragment, labels=["Location"]):
    uuid: Annotated[str, NodeId()]
    name: str
    type: str


class LocationHierarchy(GraphView):
    location: Annotated[Location, Root()]
    sub_locations: Annotated[list[LocationHierarchy], Relationship("HAS_LOCATION", max_depth=3)] = []


class LocationAncestors(GraphView):
    location: Annotated[Location, Root()]
    within: Annotated[list[Location], Relationship("HAS_LOCATION", Direction.INCOMING, max_depth=5)] = []


class Movie(NodeFragment, labels=["Movie"]):
    uuid: Annotated[str, NodeId()]
    title: str
    released: int


class Actor(NodeFragment, labels=["Actor"]):
    uuid: Annotated[str, NodeId()]
    name: str


class Director(NodeFragment, labels=["Director"]):
    uuid: Annotated[str, NodeId()]
    name: str


class ActorStats(GraphView):
    actor: Annotated[Actor, Root()]
    movie_count: Annotated[int, Count("ACTED_IN")] = 0
    average_rating: Annotated[float | None, Aggregate(AggregateFunction.AVG, "RATED", "score")] = None
    best_rating: Annotated[float | None, Aggregate(AggregateFunction.MAX, "RATED", "score")] = None
    directors: Annotated[list[Director], GraphPath([hop("ACTED_IN"), hop("DIRECTED_BY")])] = []
    first_director: Annotated[
        Director | None, GraphPath([hop("ACTED_IN", order_by="released"), hop("DIRECTED_BY")])
    ] = None


class ActorWithDirectors(GraphView):
    """Only actors a director directed a movie of: the path is required, and labelled at each hop."""

    actor: Annotated[Actor, Root()]
    directors: Annotated[
        list[Director],
        GraphPath([hop("ACTED_IN", label="Movie"), hop("DIRECTED_BY", label="Person")], required=True),
    ] = []


class ActorWithMovies(GraphView):
    """Only actors who acted in something: the relationship is required."""

    actor: Annotated[Actor, Root()]
    movies: Annotated[list[Movie], Relationship("ACTED_IN", required=True)] = []


class ActorWithLatestMovie(GraphView):
    actor: Annotated[Actor, Root()]
    movie: Annotated[Movie, Relationship("ACTED_IN")]
    note: Annotated[str, Transient()] = "not loaded"


class Animal(NodeFragment, labels=["Animal"]):
    uuid: Annotated[str, NodeId()]
    name: str


class Dog(Animal, labels=["Dog"]):
    breed: str | None = None


class Puppy(Dog, labels=["Puppy"]):
    age_in_weeks: int = 0


class Tier(StrEnum):
    GOLD = "Gold"
    SILVER = "Silver"


class Customer(NodeFragment, labels=["Customer"]):
    uuid: Annotated[str, NodeId()]
    full_name: str
    status: Annotated[str, NullAsDefault()] = "active"
    tags: Annotated[list[str], EmptyWhenAbsent()] = []
    preferences: Annotated[dict[str, Any], JsonPacked()] = {}
    metadata: Annotated[dict[str, Any], PropertyBag()] = {}
    extras: Annotated[dict[str, Any], PropertyBag(flat=True)] = {}
    tiers: Annotated[set[str], NodeLabels(Tier)] = set()
    legacy_code: Annotated[str | None, GraphProperty("LEGACY")] = None
    scratch: Annotated[str, Transient()] = ""


class AsWritten(NodeFragment, labels=["AsWritten"], property_names="as_written"):
    uuid: Annotated[str, NodeId()]
    full_name: str


class Badge(NodeFragment, labels=["Badge"]):
    """Two bags and an open set of labels beside a stamp: what an update on Neo4j compares as loaded."""

    uuid: Annotated[str, NodeId()]
    name: str
    metadata: Annotated[dict[str, Any], PropertyBag()] = {}
    extras: Annotated[dict[str, Any], PropertyBag(flat=True)] = {}
    kinds: Annotated[list[str], NodeLabels()] = []
    stamp: Annotated[str | None, NodeStamp()] = None


class Flag(NodeFragment, labels=["Flag"]):
    uuid: Annotated[str, NodeId()]
    locked: bool


class Account(NodeFragment, labels=["Account"]):
    """
    An account looked up by its email and its display name whatever their case: a save keeps a lowercased copy of each,
    the display name's under the property it is stored as.
    """

    uuid: Annotated[str, NodeId()]
    email: Annotated[str | None, CaseInsensitive()] = None
    display_name: Annotated[str | None, CaseInsensitive(), GraphProperty("display_name")] = None
    name: str | None = None
    stamp: Annotated[str | None, NodeStamp()] = None


class AccountFriends(GraphView):
    """An account and those it knows: a save writes the copies of each, a lookup reads the root's alone."""

    account: Annotated[Account, Root()]
    friends: Annotated[list[Account], Relationship("KNOWS")] = []
