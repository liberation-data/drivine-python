"""
The declarations a store layout migration is told, from a fragment's fields' annotations (drivine-python#514), with no
store: drivine4j's StoreLayoutMigrationDeclarationsTest and Drivine for TypeScript's declarationsOf.spec.ts, ported.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated, Any

from pydantic import AwareDatetime, NaiveDatetime

from drivine import (
    GraphProperty,
    JsonPacked,
    NodeFragment,
    NodeId,
    NodeLabels,
    PropertyBag,
    RelationshipFragment,
    TargetNode,
    Transient,
)
from drivine.migration.store_layout_migration import MigrationDeclaration, declarations_of, with_declared
from drivine.model.model_registry import ModelRegistry


class Person(NodeFragment, labels=["Person", "Author"]):
    name: Annotated[str, NodeId()]
    born: date
    wakes: time | None = None
    seen: NaiveDatetime | None = None
    at: AwareDatetime | None = None
    logged: datetime | None = None
    tenure: timedelta | None = None
    height: float = 0.0
    photo: bytes | None = None
    days: list[date] = []
    moments: tuple[datetime, ...] = ()
    ratios: set[float] | None = None
    visits: int = 0
    tags: list[str] = []
    seen_at: Annotated[datetime | None, GraphProperty("lastSeen")] = None
    first_met: date | None = None
    dollar: Annotated[date | None, GraphProperty("$at")] = None


class Unmapped(NodeFragment, labels=["Unmapped"]):
    id: Annotated[str, NodeId()]
    packed: Annotated[list[date], JsonPacked()] = []
    skipped: Annotated[date | None, Transient()] = None
    weeks: list[list[date]] = []
    by_day: dict[str, date] = {}
    labels: Annotated[set[str], NodeLabels()] = set()
    extra: Annotated[dict[str, Any], PropertyBag()] = {}


class Animal(NodeFragment, labels=["Animal"]):
    id: Annotated[str, NodeId()]
    born: date | None = None


class Dog(Animal, labels=["Dog"]):
    weight: float | None = None


class Knows(RelationshipFragment):
    since: date
    weight: float | None = None
    note: str | None = None
    target: Annotated[Person, TargetNode()]


def by_label(declared: list[MigrationDeclaration]) -> list[tuple[str, str, str]]:
    return sorted((it.label, it.property, it.kind) for it in declared)


def test_a_node_fragments_temporal_float_and_bytes_fields_are_declared_under_each_of_its_labels():
    expected = [
        ("born", "date"),
        ("wakes", "localtime"),
        ("seen", "localdatetime"),
        ("at", "datetime"),
        ("logged", "datetime"),
        ("tenure", "duration"),
        ("height", "float"),
        ("photo", "bytes"),
        ("days", "date"),
        ("moments", "datetime"),
        ("ratios", "float"),
        # By the name it is stored under: a GraphProperty's, or the registry's naming of the field.
        ("lastSeen", "datetime"),
        ("firstMet", "date"),
        # As the user names it, not as layout 2 holds the key ($$at): the module's declarations name it so.
        ("$at", "date"),
    ]
    assert by_label(declarations_of(Person)) == sorted(
        (label, prop, kind) for label in ("Person", "Author") for prop, kind in expected
    )


def test_a_field_neither_loaded_nor_held_as_its_kind_is_not_declared():
    assert declarations_of(Unmapped) == []


def test_a_fragment_declares_under_the_labels_it_inherits_too_and_its_inherited_fields():
    assert by_label(declarations_of(Dog)) == [
        ("Animal", "born", "date"),
        ("Animal", "weight", "float"),
        ("Dog", "born", "date"),
        ("Dog", "weight", "float"),
    ]


def test_a_relationship_fragments_are_declared_under_the_type_it_is_given_not_the_node_it_leads_to():
    assert by_label(declarations_of(Knows, "KNOWS")) == [("KNOWS", "since", "date"), ("KNOWS", "weight", "float")]


def test_a_registry_names_the_properties_as_it_names_them_for_its_managers():
    assert ("Person", "first_met", "date") in by_label(declarations_of(Person, registry=ModelRegistry("as_written")))


def test_a_hand_declaration_replaces_the_derived_one_of_its_label_and_property_and_the_rest_are_added():
    declared = with_declared(
        [*declarations_of(Animal), *declarations_of(Animal)],
        [MigrationDeclaration("Animal", "born", "localdatetime"), MigrationDeclaration("Animal", "nick", "date")],
    )
    assert declared == [
        MigrationDeclaration("Animal", "born", "localdatetime"),
        MigrationDeclaration("Animal", "nick", "date"),
    ]
