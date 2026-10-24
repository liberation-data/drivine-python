# Written by scripts/unasync.py from tests/integration/test_schema.py:
# change that, and run the script again.
import logging
import re
import uuid
from typing import Annotated, Any

import pytest

from drivine.schema.schema_item import SchemaItem
from drivine.sync import (
    DatabaseType,
    DrivineError,
    EngineVectorOptions,
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

    def test_a_pinned_quantization_reads_back_matches_and_drifts_when_pinned_the_other_way(self, gom, pm, label):
        if pm.type is not DatabaseType.NEO4J:
            pytest.skip("Quantization is a Neo4j option.")
        unquantized = vector_index(
            label, "embedding", 4, "COSINE", engine_options=(EngineVectorOptions("Neo4j", quantization_enabled=False),)
        )
        assert (gom.indexes.ensure(unquantized)).status == "CREATED"
        [listed] = [it for it in gom.indexes.list("VECTOR") if it.label == label]
        assert listed.quantization_enabled is False
        assert (gom.indexes.ensure(unquantized)).status == "ALREADY_MATCHING"
        quantized = vector_index(
            label, "embedding", 4, "COSINE", engine_options=(EngineVectorOptions("Neo4j", quantization_enabled=True),)
        )
        drift = gom.indexes.ensure(quantized)
        assert drift.status == "DRIFT" and drift.found is not None and drift.found.quantization_enabled is False
        assert (gom.indexes.recreate(quantized)).status == "RECREATED"
        [listed] = [it for it in gom.indexes.list("VECTOR") if it.label == label]
        assert listed.quantization_enabled is True
        gom.indexes.drop(quantized)

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

    def test_with_no_sample_asked_the_engine_refuses_data_that_breaks_it(self, gom, pm, label):
        create(gom.persistence_manager, label, {"code": "dup"}, {"code": "dup"})
        # FalkorDB builds the constraint in the background, and reports at once that the build failed.
        message = (
            re.escape(
                f"FalkorDB could not build the uniqueness constraint on {label}(code): nodes may hold the same values."
            )
            if pm.type is DatabaseType.FALKORDB
            else None
        )
        with pytest.raises(Exception, match=message):
            gom.constraints.ensure(uniqueness_constraint(label, "code"), 0)

    def test_a_violations_sample_names_the_conflicting_nodes(self, gom, label):
        create(gom.persistence_manager, label, {"code": "dup", "name": "a"}, {"code": "dup", "name": "b"})
        create(gom.persistence_manager, label, {"code": "u", "name": "c"})

        result = gom.constraints.ensure(uniqueness_constraint(label, "code", node_id_property="name"))

        assert result.status == "VIOLATION" and result.sample is not None
        [held] = result.sample
        assert held["properties"] == {"code": "dup"} and held["count"] == 2
        assert len({it["elementId"] for it in held["nodes"]}) == 2, "two distinct ids"
        assert sorted(it["name"] for it in held["nodes"]) == ["a", "b"]

    def test_a_violations_sample_names_as_many_keys_and_as_many_of_each_ones_nodes_as_its_size(self, gom, label):
        codes = ["a", "a", "a", "b", "b", "b", "c", "c", "c"]
        create(gom.persistence_manager, label, *({"code": it, "name": f"k{i}"} for i, it in enumerate(codes)))

        result = gom.constraints.ensure(uniqueness_constraint(label, "code", node_id_property="name"), 2)

        assert result.status == "VIOLATION" and result.sample is not None
        assert len(result.sample) == 2
        for held in result.sample:
            assert held["count"] == 3 and len(held["nodes"]) == 2
            holders = {f"k{i}" for i, it in enumerate(codes) if it == held["properties"]["code"]}
            assert {it["name"] for it in held["nodes"]} <= holders

    def test_a_violations_sample_names_many_nodes_of_a_key_when_its_size_asks_for_them(self, gom, label):
        # 150 nodes of one key, each named by an id of 13 characters, pass the 4000 bytes Oracle aggregates text into.
        names = [f"node-key-{i:04d}" for i in range(150)]
        create(gom.persistence_manager, label, *({"code": "a", "name": it} for it in names))

        result = gom.constraints.ensure(uniqueness_constraint(label, "code", node_id_property="name"), 200)

        assert result.status == "VIOLATION" and result.sample is not None
        assert [(it["count"], len(it["nodes"])) for it in result.sample] == [(150, 150)]
        assert sorted(it["name"] for it in result.sample[0]["nodes"]) == names


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

    def test_a_constraint_ensured_at_runtime_fails_fast_naming_the_index_warns_or_replaces_it_by_the_options(
        self, factory, pm, gom, label, caplog
    ):
        neo4j_only(pm)
        pm.execute(QuerySpecification(f"CREATE INDEX hand_{label} FOR (n:{label}) ON (n.e)"))
        spec = uniqueness_constraint(label, "e")
        # On this database alone: ensure targets every graph database the registry holds.
        given = SchemaCatalog.of(spec).named(label).for_database(pm.database)
        managers = GraphObjectManagerFactory(factory)

        def manager(**options: Any) -> SchemaManager:
            return SchemaManager(factory.registry, managers, SchemaOptions([], **options))

        with pytest.raises(DrivineError, match=f"the index hand_{label} is in the way of it"):
            manager().ensure_catalog(given)
        with caplog.at_level(logging.WARNING):
            warned = (manager(mode="WARN").ensure_catalog(given))[pm.database]
        assert [it.status for it in warned] == ["BLOCKED_BY_INDEX"]
        assert f"hand_{label} is in the way" in caplog.text
        assert [it.name for it in ranges(gom, label)] == [f"hand_{label}"], "WARN leaves it"
        replaced = (manager(replace_blocking_index=True).ensure_catalog(given))[pm.database]
        assert [it.status for it in replaced] == ["CREATED"]
        assert ranges(gom, label) == [], "the constraint's own index takes its place"
        assert [it.properties for it in uniques(gom, label)] == [("e",)]
        gom.constraints.drop(spec)

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
