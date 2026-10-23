# Written by scripts/unasync.py from tests/integration/test_schema.py:
# change that, and run the script again.
import logging
import uuid
from typing import Annotated, Any

import pytest

from drivine.schema.schema_item import SchemaItem
from drivine.sync import (
    DatabaseType,
    DrivineError,
    FullTextIndex,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
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
        assert result.status == "VIOLATION" and result.sample == ("dup",)
        assert not [it for it in gom.constraints.list() if it.label == label]


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


def test_store_identity_is_stable(factory, pm):
    one = StatelessGraphObjectManager(pm).store_identity()
    two = StatelessGraphObjectManager(factory.get(pm.database)).store_identity()
    assert one.id == two.id and one.engine == pm.type


def neo4j_only(pm: PersistenceManager) -> None:
    if pm.type is not DatabaseType.NEO4J:
        pytest.skip("Only Neo4j's constraint brings its own index, so only there is a plain one in the way of it.")


def ranges(gom: StatelessGraphObjectManager, label: str) -> list[SchemaItem]:
    return [it for it in gom.indexes.list("RANGE") if it.label == label]


def uniques(gom: StatelessGraphObjectManager, label: str) -> list[SchemaItem]:
    return [it for it in gom.constraints.list() if it.label == label]


class TestAnIndexInTheWayOfAConstraint:
    def test_is_reported_by_name_and_left_then_replaced_when_asked(self, gom, pm, label):
        neo4j_only(pm)
        pm.execute(QuerySpecification(f"CREATE INDEX hand_{label} FOR (n:{label}) ON (n.e)"))
        spec = uniqueness_constraint(label, "e")

        blocked = gom.constraints.ensure(spec)

        assert blocked.status == "BLOCKED_BY_INDEX" and blocked.index is not None
        assert blocked.index.name == f"hand_{label}"
        assert [it.name for it in ranges(gom, label)] == [f"hand_{label}"], "nothing changed"
        assert uniques(gom, label) == []

        assert (gom.constraints.ensure(spec, replace_blocking_index=True)).status == "CREATED"
        assert ranges(gom, label) == [], "the constraint's own index takes its place"
        assert [it.properties for it in uniques(gom, label)] == [("e",)]
        gom.constraints.drop(spec)

    def test_is_kept_when_a_value_repeats_though_no_sample_is_asked_for(self, gom, pm, label):
        neo4j_only(pm)
        gom.indexes.ensure(range_index(label, "e"))
        create(pm, label, {"e": "x"}, {"e": "x"})

        result = gom.constraints.ensure(uniqueness_constraint(label, "e"), 0, replace_blocking_index=True)

        assert result.status == "VIOLATION"
        assert len(ranges(gom, label)) == 1
        gom.indexes.drop(range_index(label, "e"))

    def test_with_the_opt_in_the_constraint_is_made_and_a_range_index_kept_where_it_is_not_in_the_way(
        self, gom, pm, label
    ):
        gom.indexes.ensure(range_index(label, "e"))

        result = gom.constraints.ensure(uniqueness_constraint(label, "e"), replace_blocking_index=True)

        assert result.status == "CREATED"
        assert [it.properties for it in uniques(gom, label)] == [("e",)]
        if pm.type is DatabaseType.NEO4J:
            assert ranges(gom, label) == []
        else:
            assert ranges(gom, label) != [], "made beside the constraint, or needed by it: kept"
        gom.constraints.drop(uniqueness_constraint(label, "e"))
        gom.indexes.drop(range_index(label, "e"))

    def test_a_catalog_of_both_fails_fast_naming_the_index_warns_or_replaces_it_by_the_options(
        self, factory, pm, gom, label, caplog
    ):
        catalog = SchemaCatalog.of(range_index(label, "e"), uniqueness_constraint(label, "e")).named(label)
        managers = GraphObjectManagerFactory(factory)

        def manager(**options: Any) -> SchemaManager:
            return SchemaManager(
                factory.registry, managers, SchemaOptions([catalog.for_database(pm.database)], **options)
            )

        if pm.type is DatabaseType.NEO4J:
            with pytest.raises(DrivineError, match=f"the index {label}_e_range is in the way of it"):
                manager().enforce()
            with caplog.at_level(logging.WARNING):
                warned = (manager(mode="WARN").enforce())[pm.database]
            assert [it.status for it in warned] == ["ALREADY_MATCHING", "BLOCKED_BY_INDEX"]
            assert f"{label}_e_range is in the way" in caplog.text
            assert len(ranges(gom, label)) == 1, "WARN leaves it"
        replaced = (manager(replace_blocking_index=True).enforce())[pm.database]
        assert replaced[-1].status == "CREATED"
        assert [it.properties for it in uniques(gom, label)] == [("e",)]
        again = (manager(replace_blocking_index=True).enforce())[pm.database]
        assert [it.status for it in again][-1] == "ALREADY_MATCHING"
        gom.constraints.drop(uniqueness_constraint(label, "e"))
        gom.indexes.drop(range_index(label, "e"))

    def test_the_markers_key_reclaims_a_plain_index_made_on_it_by_hand(self, factory, pm, gom, label):
        neo4j_only(pm)
        marker = uniqueness_constraint("_DrivineSchema", "scope")
        gom.constraints.drop(marker)
        pm.execute(QuerySpecification("CREATE INDEX hand_marker_scope FOR (n:_DrivineSchema) ON (n.scope)"))
        catalog = SchemaCatalog.of(range_index(label, "a")).named(label).with_version("1").for_database(pm.database)

        SchemaManager(factory.registry, GraphObjectManagerFactory(factory), SchemaOptions([catalog])).enforce()

        assert [it.properties for it in uniques(gom, "_DrivineSchema")] == [("scope",)]
        assert ranges(gom, "_DrivineSchema") == []
        gom.indexes.drop(range_index(label, "a"))

    def test_the_store_keys_constraint_reclaims_a_plain_index_made_on_it_by_hand(self, factory, pm, gom):
        neo4j_only(pm)
        gom.constraints.drop(uniqueness_constraint("DrivineStore", "key"))
        pm.execute(QuerySpecification("CREATE INDEX hand_store_key FOR (n:DrivineStore) ON (n.key)"))

        StatelessGraphObjectManager(factory.get(pm.database)).store_identity()

        assert [it.properties for it in uniques(gom, "DrivineStore")] == [("key",)]
        assert ranges(gom, "DrivineStore") == []
