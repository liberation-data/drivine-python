# Written by scripts/unasync.py from tests/integration/test_schema.py:
# change that, and run the script again.
import logging
import uuid
from typing import Annotated

import pytest

from drivine.sync import (
    DrivineError,
    FullTextIndex,
    NodeFragment,
    NodeId,
    PersistenceManager,
    RangeIndex,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    StatelessGraphObjectManager,
    Unique,
    VectorIndex,
    full_text_index,
    range_index,
    uniqueness_constraint,
    vector_index,
)
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory
from tests.sync_integration.stores import create


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


@pytest.fixture
def label() -> str:
    """A label of the test's own, so schema items, which outlive a test's data, don't meet another test's."""
    return f"T{uuid.uuid4().hex[:10]}"


class TestIndexes:
    def test_range_index_is_made_once_and_dropped(self, gom, label):
        spec = range_index(label, ["a", "b"])
        assert (gom.indexes.ensure(spec)).status == "CREATED"
        assert (gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
        held = [it for it in gom.indexes.list("RANGE") if it.label == label]
        assert {p for it in held for p in it.properties} == {"a", "b"}
        gom.indexes.drop(spec)
        assert [it for it in gom.indexes.list("RANGE") if it.label == label] == []

    def test_vector_index_drift_and_recreate(self, gom, label):
        spec = vector_index(label, "embedding", 3, "COSINE")
        assert (gom.indexes.ensure(spec)).status == "CREATED"
        assert (gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
        wider = vector_index(label, "embedding", 4, "COSINE")
        drift = gom.indexes.ensure(wider)
        assert drift.status == "DRIFT" and drift.found is not None and drift.found.dimensions == 3
        assert (gom.indexes.recreate(wider)).status == "RECREATED"
        assert (gom.indexes.ensure(wider)).status == "ALREADY_MATCHING"
        gom.indexes.drop(wider)

    def test_full_text_index(self, gom, label):
        spec = full_text_index(label, ["title", "body"])
        assert (gom.indexes.ensure(spec)).status == "CREATED"
        assert (gom.indexes.ensure(spec)).status == "ALREADY_MATCHING"
        gom.indexes.drop(spec)
        assert (gom.indexes.ensure(spec)).status == "CREATED"
        gom.indexes.drop(spec)


class TestConstraints:
    def test_made_once_and_dropped(self, gom, label):
        spec = uniqueness_constraint(label, "code")
        assert (gom.constraints.ensure(spec)).status == "CREATED"
        assert (gom.constraints.ensure(spec)).status == "ALREADY_MATCHING"
        create(gom.persistence_manager, label, {"code": "x"})
        with pytest.raises(DrivineError):
            create(gom.persistence_manager, label, {"code": "x"})
        gom.constraints.drop(spec)
        assert not [it for it in gom.constraints.list() if it.label == label]

    def test_reports_data_that_breaks_it(self, gom, label):
        create(gom.persistence_manager, label, {"code": "dup"}, {"code": "dup"}, {"code": "u"})
        result = gom.constraints.ensure(uniqueness_constraint(label, "code"))
        assert result.status == "VIOLATION" and result.sample is not None
        [held] = result.sample
        assert held["properties"] == {"code": "dup"} and held["count"] == 2 and len(held["nodes"]) == 2
        assert not [it for it in gom.constraints.list() if it.label == label]

    def test_a_violations_sample_names_the_conflicting_nodes(self, gom, label):
        create(gom.persistence_manager, label, {"code": "dup", "name": "a"}, {"code": "dup", "name": "b"})
        create(gom.persistence_manager, label, {"code": "u", "name": "c"})

        result = gom.constraints.ensure(uniqueness_constraint(label, "code", node_id_property="name"))

        assert result.status == "VIOLATION" and result.sample is not None
        [held] = result.sample
        assert held["properties"] == {"code": "dup"} and held["count"] == 2
        assert len({it["elementId"] for it in held["nodes"]}) == 2, "two distinct ids"
        assert sorted(it["name"] for it in held["nodes"]) == ["a", "b"]

    def test_a_violations_sample_names_no_more_nodes_of_a_key_than_its_size(self, gom, label):
        create(gom.persistence_manager, label, {"code": "dup"}, {"code": "dup"}, {"code": "dup"})

        result = gom.constraints.ensure(uniqueness_constraint(label, "code"), 2)

        assert result.sample is not None
        [held] = result.sample
        assert held["count"] == 3 and len(held["nodes"]) == 2


class TestSchemaManager:
    def test_enforces_what_fragments_declare(self, factory, pm, label):
        fragment = type(
            "Chunk",
            (NodeFragment,),
            {
                "__annotations__": {
                    "uuid": Annotated[str, NodeId(), Unique()],
                    "section_id": Annotated[str, RangeIndex()],
                    "text": Annotated[str, FullTextIndex()],
                    "embedding": Annotated[list[float] | None, VectorIndex()],
                },
                "embedding": None,
                "__module__": __name__,
            },
            labels=[label],
        )
        catalog = SchemaCatalog.from_fragments(fragment, dimensions=lambda _label, _prop: 3).named(label)
        assert {(it.kind, it.properties) for it in catalog.items} == {
            ("UNIQUE", ("uuid",)),
            ("RANGE", ("sectionId",)),
            ("FULLTEXT", ("text",)),
            ("VECTOR", ("embedding",)),
        }
        manager = SchemaManager(
            factory.registry, GraphObjectManagerFactory(factory), SchemaOptions([catalog.for_database(pm.database)])
        )
        first = manager.enforce()
        assert sorted(it.status for it in first[pm.database]) == ["CREATED"] * 4
        again = manager.enforce()
        assert [it.status for it in again[pm.database]] == ["ALREADY_MATCHING"] * 4
        gom = StatelessGraphObjectManager(pm)
        for it in catalog.items:
            if it.kind == "UNIQUE":
                gom.constraints.drop(it)
            else:
                gom.indexes.drop(it)

    def test_a_new_version_recreates_and_orphans_are_reported(self, factory, pm, label, caplog):
        first = SchemaCatalog.of(range_index(label, "a"), range_index(label, "b")).named(label).with_version("1")
        managers = GraphObjectManagerFactory(factory)

        def manager(catalog: SchemaCatalog) -> SchemaManager:
            return SchemaManager(factory.registry, managers, SchemaOptions([catalog.for_database(pm.database)]))

        assert [it.status for it in (manager(first).enforce())[pm.database]] == ["CREATED", "CREATED"]
        second = SchemaCatalog.of(range_index(label, "a")).named(label).with_version("2")
        with caplog.at_level(logging.WARNING):
            results = (manager(second).enforce())[pm.database]
        assert [it.status for it in results] == ["RECREATED"]
        assert f"applied 1 item(s) it no longer declares: RANGE_INDEX:{label}:b:{label}_b_range" in caplog.text
        gom = StatelessGraphObjectManager(pm)
        for prop in ("a", "b"):
            gom.indexes.drop(range_index(label, prop))

    def test_a_violation_fails_fast_or_warns(self, factory, pm, label, caplog):
        create(pm, label, {"code": "dup"}, {"code": "dup"})
        catalog = SchemaCatalog.of(uniqueness_constraint(label, "code")).named(label).for_database(pm.database)
        managers = GraphObjectManagerFactory(factory)
        with pytest.raises(DrivineError, match="VIOLATION for the UNIQUE"):
            SchemaManager(factory.registry, managers, SchemaOptions([catalog])).enforce()
        with caplog.at_level(logging.WARNING):
            SchemaManager(factory.registry, managers, SchemaOptions([catalog], mode="WARN")).enforce()
        assert '"dup"' in caplog.text

    def test_a_constraint_one_owner_reads_from_a_fragment_and_another_declares_by_hand_is_one(self, factory, pm, label):
        fragment = type(
            "Member",
            (NodeFragment,),
            {
                "__annotations__": {"uuid": Annotated[str, NodeId()], "email": Annotated[str, Unique()]},
                "__module__": __name__,
            },
            labels=[label],
        )
        scanned = SchemaCatalog.from_fragments(fragment).named(f"{label}-lib").for_database(pm.database)
        by_hand = (
            SchemaCatalog.of(uniqueness_constraint(label, "email")).named(f"{label}-app").for_database(pm.database)
        )
        assert scanned.items == (uniqueness_constraint(label, "email", node_id_property="uuid"),)

        results = SchemaManager(
            factory.registry, GraphObjectManagerFactory(factory), SchemaOptions([scanned, by_hand])
        ).enforce()

        assert [it.status for it in results[pm.database]] == ["CREATED", "ALREADY_MATCHING"]
        StatelessGraphObjectManager(pm).constraints.drop(uniqueness_constraint(label, "email"))


def test_store_identity_is_stable(factory, pm):
    one = StatelessGraphObjectManager(pm).store_identity()
    two = StatelessGraphObjectManager(factory.get(pm.database)).store_identity()
    assert one.id == two.id and one.engine == pm.type
