# Written by scripts/unasync.py from tests/integration/sql/test_schema_ddl.py:
# change that, and run the script again.
"""
Neo4j schema DDL run as Cypher on each SQL store, through a PersistenceManager: applied through the schema machinery, as
the schema API applies a spec, under the owner `ddl`. Each test has a graph of its own, and a registry and factory made
again stand for the next process. drivine4j's SchemaDdlCrossEngineTest, ported.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from drivine.backend.oracle.oracle_graph import OracleGraph
from drivine.connection.database_type import DatabaseType
from drivine.query.statement import cypher_statement
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.full_text_index_spec import FullTextIndexSpec
from drivine.schema.recorded_inventory import read_inventory
from drivine.schema.schema_item import SchemaItem
from drivine.schema.schema_specs import SchemaSpec, item_name
from drivine.sync import (
    DatabaseRegistry,
    DrivineError,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    SchemaCatalog,
    SchemaManager,
    SchemaOptions,
    StatelessGraphObjectManager,
    full_text_index,
    range_index,
    transaction,
    uniqueness_constraint,
    vector_index,
)
from drivine.sync.manager.graph_object_manager_factory import GraphObjectManagerFactory
from drivine.sync.manager.non_transactional_persistence_manager import NonTransactionalPersistenceManager
from drivine.sync.schema.schema_commands import OWNER
from drivine.sync.schema.schema_manager import inventory_key
from tests.sync_integration.sql.new_graph import drop_graph, registry_on


class Store:
    """A graph of the test's own on one SQL store, and the managers over it that one process has."""

    def __init__(self, store: str, graph: str) -> None:
        self.store = store
        self.graph = graph
        self.registry, self.factory, self.pm, self.gom = self._started()

    def _started(
        self,
    ) -> tuple[DatabaseRegistry, PersistenceManagerFactory, PersistenceManager, StatelessGraphObjectManager]:
        """A registry, a factory and managers made anew over the graph: what the next process has."""
        registry = registry_on(self.store, self.graph)
        factory = PersistenceManagerFactory(registry)
        pm = factory.get(self.store, "NON_TRANSACTIONAL")
        return registry, factory, pm, StatelessGraphObjectManager(pm)

    def restart(self) -> None:
        self.registry.close()
        self.registry, self.factory, self.pm, self.gom = self._started()

    def schema(self, *catalogs: SchemaCatalog) -> SchemaManager:
        return SchemaManager(self.registry, GraphObjectManagerFactory(self.factory), SchemaOptions(list(catalogs)))

    def ddl(self, statement: str, pm: PersistenceManager | None = None) -> None:
        (pm or self.pm).execute(QuerySpecification[Any](cypher_statement(statement)))

    def cypher(self, statement: str) -> list[Any]:
        return self.pm.query(QuerySpecification[Any](cypher_statement(statement)))

    def keys(self, owner: str = OWNER) -> list[str]:
        """The inventory keys of the owner's marker."""
        backend = self.gom.backend
        stored = self.pm.maybe_get_one(backend.schema_marker("items").spec({"scope": owner}))
        recorded = read_inventory(backend.marker_items(stored))
        return sorted(recorded.keys) if recorded is not None else []

    def runtime_keys(self, owner: str = OWNER) -> list[str]:
        backend = self.gom.backend
        stored = self.pm.maybe_get_one(backend.schema_marker("items").spec({"scope": owner}))
        recorded = read_inventory(backend.marker_items(stored))
        return sorted(recorded.runtime) if recorded is not None else []

    def items(self, spec: SchemaSpec) -> list[SchemaItem]:
        if spec.kind == "UNIQUE":
            return self.gom.constraints.list()
        return self.gom.indexes.list(spec.kind)

    def held(self, spec: SchemaSpec) -> SchemaItem | None:
        """The item of the spec's identity the store holds: its label, and its properties in any order."""
        return next(
            (it for it in self.items(spec) if it.label == spec.label and set(it.properties) == set(spec.properties)),
            None,
        )

    def aliases(self, spec: SchemaSpec) -> list[str]:
        found = [it for it in self.items(spec) if it.label == spec.label and it.properties == spec.properties]
        assert len(found) == 1, f"{spec}: {found}"
        return list(found[0].aliases)

    def listed(self) -> list[SchemaItem]:
        """Every item, as a reader that knows of no aliases reads it."""
        found = self.gom.constraints.list()
        for kind in ("RANGE", "VECTOR", "FULLTEXT"):
            found += self.gom.indexes.list(kind)  # pyright: ignore[reportArgumentType]
        return sorted((dataclasses.replace(it, aliases=()) for it in found), key=repr)

    def without_aliases(self) -> str:
        """The SQL that takes `aliases` out of every record's details, as a library that knows of none writes them."""
        catalog = f"{self.graph}_schema"
        if self.pm.type is DatabaseType.ORACLE:
            return f"UPDATE {catalog} SET details = JSON_TRANSFORM(details, REMOVE '$.aliases')"
        if self.pm.type is DatabaseType.SQLITE:
            return f"UPDATE {catalog} SET details = json_remove(details, '$.aliases')"
        return f"UPDATE {catalog} SET details = details - 'aliases'"

    def drop(self) -> None:
        if self.pm.type is DatabaseType.ORACLE:
            graph = OracleGraph(self.graph)
            for table in (graph.texts("Chunk", ["text"]), graph.vectors("Chunk", "embedding")):
                self.pm.execute(QuerySpecification[Any](f"DROP TABLE IF EXISTS {table} PURGE"))
        drop_graph(self.pm, self.graph)
        self.registry.close()


@pytest.fixture
def db(store: str) -> Iterator[Store]:
    made = Store(store, f"ddl_{uuid.uuid4().hex[:8]}")
    # The provider's first connection makes the graph's tables.
    made.cypher("MATCH (n) RETURN count(n)")
    yield made
    made.drop()


def messages(failure: BaseException) -> str:
    found: list[str] = []
    cause: BaseException | None = failure
    while cause is not None:
        found.append(str(cause))
        cause = cause.__cause__
    return " | ".join(found)


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [it.getMessage() for it in caplog.records if it.levelno >= logging.WARNING]


def all_matching(report: dict[str, list[EnsureResult[Any]]]) -> bool:
    return all(it.status == "ALREADY_MATCHING" for results in report.values() for it in results)


APOC_SCHEMA = [
    "CREATE FULLTEXT INDEX Chunk_text_fulltext IF NOT EXISTS FOR (n:Chunk) ON EACH [n.`text`];",
    "CREATE RANGE INDEX IF NOT EXISTS FOR (n:ChatSession) ON (n.lastActivityAt);",
    "CREATE RANGE INDEX IF NOT EXISTS FOR (n:ContentElement) ON (n.root_document_id, n.ordinal);",
    "CREATE RANGE INDEX IF NOT EXISTS FOR (n:Message) ON (n.sessionId);",
    "CREATE RANGE INDEX IF NOT EXISTS FOR (n:Document) ON (n.uri);",
    "CREATE CONSTRAINT Attachment_attachmentId_unique IF NOT EXISTS FOR (node:Attachment) "
    "REQUIRE (node.attachmentId) IS UNIQUE;",
    "CREATE CONSTRAINT ChatSession_sessionId_unique IF NOT EXISTS FOR (node:ChatSession) "
    "REQUIRE (node.sessionId) IS UNIQUE;",
    "CREATE CONSTRAINT Chunk_id_unique IF NOT EXISTS FOR (node:Chunk) REQUIRE (node.id) IS UNIQUE;",
    "CREATE CONSTRAINT ContentElement_id_unique IF NOT EXISTS FOR (node:ContentElement) REQUIRE (node.id) IS UNIQUE;",
    "CREATE CONSTRAINT Document_id_unique IF NOT EXISTS FOR (node:Document) REQUIRE (node.id) IS UNIQUE;",
    "CREATE CONSTRAINT Message_id_unique IF NOT EXISTS FOR (node:Message) REQUIRE (node.id) IS UNIQUE;",
    "CREATE CONSTRAINT User_id_unique IF NOT EXISTS FOR (node:User) REQUIRE (node.id) IS UNIQUE;",
    "CREATE CONSTRAINT _DrivineSchema_scope_unique IF NOT EXISTS FOR (node:_DrivineSchema) "
    "REQUIRE (node.scope) IS UNIQUE;",
    "CREATE CONSTRAINT UNIQUE_IMPORT_NAME IF NOT EXISTS FOR (node:`UNIQUE IMPORT LABEL`) "
    "REQUIRE (node.`UNIQUE IMPORT ID`) IS UNIQUE;",
    "CALL db.awaitIndexes(300);",
]
"""An APOC export's schema file: fifteen statements, Drivine's own constraint and the import's among them."""

APOC_SPECS: list[SchemaSpec] = [
    full_text_index("Chunk", ["text"]),
    range_index("ChatSession", "lastActivityAt"),
    range_index("ContentElement", ["root_document_id", "ordinal"]),
    range_index("Message", "sessionId"),
    range_index("Document", "uri"),
    uniqueness_constraint("Attachment", "attachmentId"),
    uniqueness_constraint("ChatSession", "sessionId"),
    uniqueness_constraint("Chunk", "id"),
    uniqueness_constraint("ContentElement", "id"),
    uniqueness_constraint("Document", "id"),
    uniqueness_constraint("Message", "id"),
    uniqueness_constraint("User", "id"),
    uniqueness_constraint("UNIQUE IMPORT LABEL", "UNIQUE IMPORT ID"),
]
"""What APOC_SCHEMA makes, as the schema API declares it."""


def test_an_apoc_exports_schema_file_applies_twice_and_the_applications_enforce_finds_no_drift_or_orphans(
    db: Store, caplog: pytest.LogCaptureFixture
):
    for _ in range(2):
        for statement in APOC_SCHEMA:
            db.ddl(statement)

    for spec in APOC_SPECS:
        found = db.held(spec)
        assert found is not None, spec
        assert found.name == item_name(spec)
    assert db.aliases(uniqueness_constraint("UNIQUE IMPORT LABEL", "UNIQUE IMPORT ID")) == ["UNIQUE_IMPORT_NAME"]
    assert db.held(uniqueness_constraint("_DrivineSchema", "scope")) is not None
    # Each item once in the ddl owner's marker, as ensured at runtime; Drivine's own constraint in none.
    assert db.keys() == sorted(inventory_key(it) for it in APOC_SPECS)
    assert db.runtime_keys() == db.keys()

    # The application declares some of them, as its fragments would.
    app_specs = [*APOC_SPECS[:3], APOC_SPECS[5], APOC_SPECS[7]]
    app = db.schema(SchemaCatalog.of(*app_specs).named("app"))
    with caplog.at_level(logging.INFO, logger="drivine.schema"):
        reports = [app.enforce(), app.enforce()]
    assert all(all_matching(it) for it in reports), reports
    assert not [it for it in warnings(caplog) if "no longer declares" in it or "DRIFT" in it], warnings(caplog)
    assert db.keys("app") == sorted(inventory_key(it) for it in app_specs)
    assert db.keys() == sorted(inventory_key(it) for it in APOC_SPECS), "the ddl owner's marker is left"


def test_an_item_made_by_ddl_and_the_same_item_declared_through_the_api_are_one_item_of_one_key(db: Store):
    db.ddl("CREATE INDEX person_name IF NOT EXISTS FOR (p:Person) ON (p.name)")
    db.schema().ensure("app", range_index("Person", "name"))

    assert len([it for it in db.gom.indexes.list("RANGE") if it.label == "Person"]) == 1
    assert db.keys() == [inventory_key(range_index("Person", "name"))]
    assert db.keys("app") == db.keys()
    assert db.aliases(range_index("Person", "name")) == ["person_name"]


def test_drop_by_an_alias_finds_the_item_after_a_restart_and_the_store_no_longer_refuses_a_duplicate(db: Store):
    spec = uniqueness_constraint("UNIQUE IMPORT LABEL", "UNIQUE IMPORT ID")
    db.ddl(APOC_SCHEMA[13])
    db.cypher("CREATE (:`UNIQUE IMPORT LABEL` {`UNIQUE IMPORT ID`: 1})")
    with pytest.raises(Exception):  # noqa: B017, PT011
        db.cypher("CREATE (:`UNIQUE IMPORT LABEL` {`UNIQUE IMPORT ID`: 1})")

    db.restart()
    db.ddl("DROP CONSTRAINT UNIQUE_IMPORT_NAME;")

    assert db.held(spec) is None
    db.cypher("CREATE (:`UNIQUE IMPORT LABEL` {`UNIQUE IMPORT ID`: 1})")
    assert db.cypher("MATCH (n:`UNIQUE IMPORT LABEL`) RETURN count(n)") == [2]
    assert db.keys() == []
    with pytest.raises(Exception) as again:  # noqa: PT011
        db.ddl("DROP CONSTRAINT UNIQUE_IMPORT_NAME")
    assert "no constraint named 'UNIQUE_IMPORT_NAME'" in messages(again.value)
    db.ddl("DROP CONSTRAINT UNIQUE_IMPORT_NAME IF EXISTS")


def test_if_not_exists_adds_only_an_alias_without_it_fails_a_name_taken_is_refused_and_drop_finds_either(
    db: Store,
):
    spec = range_index("Person", "name")
    db.ddl("CREATE INDEX by_name FOR (p:Person) ON (p.name)")
    db.ddl("CREATE INDEX by_name IF NOT EXISTS FOR (p:Person) ON (p.name)")
    with pytest.raises(Exception) as equivalent:  # noqa: PT011
        db.ddl("CREATE INDEX by_name FOR (p:Person) ON (p.name)")
    assert "An equivalent index already exists" in messages(equivalent.value)
    with pytest.raises(Exception):  # noqa: B017, PT011
        db.ddl("CREATE INDEX FOR (p:Person) ON (p.name)")
    db.ddl("CREATE INDEX person_by_name IF NOT EXISTS FOR (p:Person) ON (p.name)")
    assert db.aliases(spec) == ["by_name", "person_by_name"]

    with pytest.raises(Exception) as taken:  # noqa: PT011
        db.ddl("CREATE INDEX by_name IF NOT EXISTS FOR (p:Pet) ON (p.name)")
    assert "already a schema item named 'by_name'" in messages(taken.value)
    with pytest.raises(Exception) as named:  # noqa: PT011
        db.ddl("CREATE CONSTRAINT Person_name_range IF NOT EXISTS FOR (p:Pet) REQUIRE p.name IS UNIQUE")
    assert "already a schema item named 'Person_name_range'" in messages(named.value)
    assert db.held(range_index("Pet", "name")) is None

    db.ddl("CREATE CONSTRAINT pet_id FOR (p:Pet) REQUIRE p.id IS UNIQUE")
    with pytest.raises(Exception) as kind:  # noqa: PT011
        db.ddl("DROP INDEX pet_id")
    assert "DROP INDEX does not drop it" in messages(kind.value)
    db.ddl("DROP CONSTRAINT pet_id")
    assert db.held(uniqueness_constraint("Pet", "id")) is None

    db.ddl("DROP INDEX person_by_name")
    assert db.held(spec) is None
    with pytest.raises(Exception):  # noqa: B017, PT011
        db.ddl("DROP INDEX by_name")
    db.ddl("DROP INDEX by_name IF EXISTS")
    assert db.keys() == []


def test_an_item_recreated_or_recorded_again_keeps_its_aliases(db: Store):
    db.ddl("CREATE INDEX by_name FOR (p:Person) ON (p.name)")
    db.ddl("CREATE CONSTRAINT person_email FOR (p:Person) REQUIRE p.email IS UNIQUE")
    db.gom.indexes.recreate(range_index("Person", "name"))
    db.gom.constraints.recreate(uniqueness_constraint("Person", "email"))
    assert db.aliases(range_index("Person", "name")) == ["by_name"]
    assert db.aliases(uniqueness_constraint("Person", "email")) == ["person_email"]

    # On Oracle a uniqueness constraint over the one property records the range index again, as served by it.
    db.gom.constraints.ensure(uniqueness_constraint("Person", "name"))
    assert db.aliases(range_index("Person", "name")) == ["by_name"]
    # On Oracle the constraint dropped leaves the range index incomplete, and its record is written again as it is
    # made whole.
    db.gom.constraints.drop(uniqueness_constraint("Person", "name"))
    assert (db.gom.indexes.ensure(range_index("Person", "name"))).status in ("CREATED", "ALREADY_MATCHING")
    assert db.aliases(range_index("Person", "name")) == ["by_name"]
    app = SchemaCatalog.of(range_index("Person", "name"), uniqueness_constraint("Person", "email")).named("app")
    db.schema(app).recreate_all()
    assert db.aliases(range_index("Person", "name")) == ["by_name"]
    assert db.aliases(uniqueness_constraint("Person", "email")) == ["person_email"]


def test_a_vector_index_recreated_after_drift_keeps_its_alias_by_the_schema_manager_or_by_hand(db: Store):
    db.ddl(
        "CREATE VECTOR INDEX chunk_embedding FOR (n:Chunk) ON n.embedding OPTIONS {indexConfig: "
        "{`vector.dimensions`: 3}}"
    )
    wider = vector_index("Chunk", "embedding", 4)
    options = SchemaOptions([SchemaCatalog.of(wider).named("app")], recreate_on_drift=True)
    report = SchemaManager(db.registry, GraphObjectManagerFactory(db.factory), options).enforce()

    assert [it.status for results in report.values() for it in results] == ["RECREATED"]
    found = db.held(wider)
    assert found is not None
    assert found.dimensions == 4
    assert db.aliases(wider) == ["chunk_embedding"]

    narrower = vector_index("Chunk", "embedding", 3)
    db.gom.indexes.recreate(narrower)
    assert db.aliases(narrower) == ["chunk_embedding"]


def test_drop_of_an_item_the_application_declares_forgets_it_in_the_ddl_owner_and_leaves_it_in_place(
    db: Store, caplog: pytest.LogCaptureFixture
):
    index = range_index("Person", "name")
    constraint = uniqueness_constraint("Person", "email")
    declared = SchemaCatalog.of(index, constraint).named("app")
    db.schema(declared).enforce()
    db.restart()
    db.ddl("CREATE INDEX by_name IF NOT EXISTS FOR (p:Person) ON (p.name)")
    db.ddl("CREATE CONSTRAINT person_email IF NOT EXISTS FOR (p:Person) REQUIRE p.email IS UNIQUE")
    assert db.keys() == sorted(inventory_key(it) for it in (index, constraint))

    with caplog.at_level(logging.WARNING, logger="drivine.schema"):
        db.ddl("DROP INDEX by_name")
        db.ddl("DROP CONSTRAINT person_email")

    assert db.held(index) is not None
    assert db.held(constraint) is not None
    assert db.keys() == []
    assert db.aliases(index) == []
    assert db.aliases(constraint) == []
    assert len([it for it in warnings(caplog) if "left in place, as 'app' declare(s) it" in it]) == 2
    with pytest.raises(Exception) as gone:  # noqa: PT011
        db.ddl("DROP INDEX by_name")
    assert "no index named 'by_name'" in messages(gone.value)

    caplog.clear()
    db.restart()
    with caplog.at_level(logging.WARNING, logger="drivine.schema"):
        report = db.schema(declared).enforce()
    assert warnings(caplog) == []
    assert all_matching(report), report
    assert db.keys("app") == sorted(inventory_key(it) for it in (index, constraint))


def test_drop_of_an_item_the_application_declares_in_another_order_leaves_it_and_forgets_it_in_the_ddl_owner(
    db: Store,
):
    declared = range_index("Person", ["last", "first"])
    db.schema(SchemaCatalog.of(declared).named("app")).enforce()
    db.restart()

    # By an alias: the item found is the application's, of the other order, which the DDL's spec is ensured as.
    db.ddl("CREATE INDEX by_names IF NOT EXISTS FOR (p:Person) ON (p.first, p.last)")
    assert len([it for it in db.gom.indexes.list("RANGE") if it.label == "Person"]) == 1
    assert db.keys() == [inventory_key(declared)]
    db.ddl("DROP INDEX by_names")
    assert db.held(declared) is not None
    assert db.keys() == []

    # By the name the schema API gives the DDL's own spec.
    db.ddl("CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.first, p.last)")
    db.ddl("DROP INDEX Person_first_last_range")
    assert db.held(declared) is not None
    assert db.keys() == []
    assert db.keys("app") == [inventory_key(declared)]


def test_the_api_name_of_the_ddls_spec_over_an_item_in_another_order_is_an_alias_a_drop_finds_after_a_restart(
    db: Store,
):
    declared = range_index("Person", ["last", "first"])
    db.schema().ensure("app", declared)
    db.restart()

    db.ddl("CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.first, p.last)")
    db.ddl("CREATE INDEX Person_first_last_range IF NOT EXISTS FOR (p:Person) ON (p.first, p.last)")
    assert db.aliases(declared) == ["Person_first_last_range"]
    with pytest.raises(Exception) as taken:  # noqa: PT011
        db.ddl("CREATE INDEX Person_first_last_range IF NOT EXISTS FOR (p:Pet) ON (p.name)")
    assert "already a schema item named 'Person_first_last_range'" in messages(taken.value)

    db.restart()
    db.ddl("DROP INDEX Person_first_last_range")
    assert db.held(declared) is not None
    assert db.aliases(declared) == []
    assert db.keys() == []
    assert db.keys("app") == [inventory_key(declared)]


def test_a_record_a_library_without_aliases_wrote_over_reads_as_it_did_but_for_its_aliases_with_no_drift(
    db: Store, caplog: pytest.LogCaptureFixture
):
    specs: list[SchemaSpec] = [
        range_index("Person", "name"),
        # On Oracle the range index over the one property is then recorded as served by the constraint's index.
        uniqueness_constraint("Person", "name"),
        uniqueness_constraint("Person", "email"),
        FullTextIndexSpec("Chunk", ("text",), analyzer="english"),
        vector_index("Chunk", "embedding", 3, "EUCLIDEAN"),
    ]
    db.ddl("CREATE INDEX by_name FOR (p:Person) ON (p.name)")
    db.ddl("CREATE CONSTRAINT person_name FOR (p:Person) REQUIRE p.name IS UNIQUE")
    db.ddl("CREATE CONSTRAINT person_email FOR (p:Person) REQUIRE p.email IS UNIQUE")
    db.ddl(
        "CREATE FULLTEXT INDEX chunk_text FOR (n:Chunk) ON EACH [n.text] OPTIONS {indexConfig: "
        "{`fulltext.analyzer`: 'english'}}"
    )
    db.ddl(
        "CREATE VECTOR INDEX chunk_embedding FOR (n:Chunk) ON n.embedding OPTIONS {indexConfig: "
        "{`vector.dimensions`: 3, `vector.similarity_function`: 'euclidean'}}"
    )
    before = db.listed()
    for spec in specs:
        assert len(db.aliases(spec)) == 1, spec

    db.pm.execute(QuerySpecification[Any](db.without_aliases()))
    db.restart()

    assert db.listed() == before
    for spec in specs:
        assert db.aliases(spec) == [], spec
    with caplog.at_level(logging.WARNING, logger="drivine.schema"):
        report = db.schema(SchemaCatalog.of(*specs).named("app")).enforce()
    assert warnings(caplog) == []
    assert all_matching(report), report
    # Only the aliases are lost: a DROP by one no longer finds the item.
    with pytest.raises(Exception) as lost:  # noqa: PT011
        db.ddl("DROP INDEX by_name")
    assert "no index named 'by_name'" in messages(lost.value)


def test_if_not_exists_over_another_shape_leaves_it_a_text_index_is_refused_and_drivines_own_constraint_stays(
    db: Store,
):
    db.ddl("CREATE VECTOR INDEX FOR (c:Chunk) ON c.embedding OPTIONS {indexConfig: {`vector.dimensions`: 3}}")
    db.ddl(
        "CREATE VECTOR INDEX wider IF NOT EXISTS FOR (c:Chunk) ON c.embedding OPTIONS {indexConfig: "
        "{`vector.dimensions`: 4}}"
    )
    found = db.held(vector_index("Chunk", "embedding", 3))
    assert found is not None
    assert found.dimensions == 3
    assert db.aliases(vector_index("Chunk", "embedding", 3)) == []
    with pytest.raises(Exception) as shape:  # noqa: PT011
        db.ddl("CREATE VECTOR INDEX FOR (c:Chunk) ON c.embedding OPTIONS {indexConfig: {`vector.dimensions`: 4}}")
    assert "of another shape" in messages(shape.value)

    with pytest.raises(Exception) as text:  # noqa: PT011
        db.ddl("CREATE TEXT INDEX FOR (p:Person) ON (p.name)")
    assert "TEXT" in messages(text.value)

    db.ddl("CREATE CONSTRAINT IF NOT EXISTS FOR (m:_DrivineSchema) REQUIRE m.scope IS UNIQUE")
    own = uniqueness_constraint("_DrivineSchema", "scope")
    with pytest.raises(Exception) as kept:  # noqa: PT011
        db.ddl(f"DROP CONSTRAINT {item_name(own)}")
    assert "which Drivine keeps" in messages(kept.value)
    assert db.held(own) is not None
    # The vector index alone is the ddl owner's: neither the TEXT index refused nor Drivine's own constraint is.
    assert db.keys() == [inventory_key(vector_index("Chunk", "embedding", 3))]


def test_inside_a_transaction_a_schema_command_is_refused_and_db_await_indexes_does_nothing(db: Store):
    transactional = db.factory.get(db.store, "TRANSACTIONAL")
    delegating = db.factory.get(db.store)

    with pytest.raises(Exception) as refused:  # noqa: PT011
        with transaction(rollback=True):
            db.ddl("CREATE INDEX FOR (p:Person) ON (p.name)", transactional)
    assert "not run inside a transaction" in messages(refused.value)
    with pytest.raises(Exception):  # noqa: B017, PT011
        with transaction(rollback=True):
            db.ddl("DROP INDEX x IF EXISTS", delegating)
    with pytest.raises(Exception) as point:  # noqa: PT011
        with transaction(rollback=True):
            db.ddl("CREATE POINT INDEX FOR (p:Place) ON (p.at)", transactional)
    assert "A POINT index is Neo4j's" in messages(point.value)
    with transaction(rollback=True):
        awaited = transactional.query(QuerySpecification[Any](cypher_statement("CALL db.awaitIndexes(300)")))
    assert awaited == []
    assert db.held(range_index("Person", "name")) is None

    # Outside a transaction the delegating manager runs it.
    db.ddl("CREATE INDEX FOR (p:Person) ON (p.name)", delegating)
    assert db.held(range_index("Person", "name")) is not None


def test_ddl_through_the_default_databases_manager_and_its_own_names_is_one_databases(db: Store):
    by_default = db.factory.get("default", "NON_TRANSACTIONAL")

    db.ddl("CREATE INDEX FOR (p:Person) ON (p.name)", by_default)
    db.ddl("DROP INDEX Person_name_range")
    assert db.held(range_index("Person", "name")) is None

    with pytest.raises(Exception) as again:  # noqa: PT011
        db.ddl("DROP INDEX Person_name_range", by_default)
    assert "There is no index named 'Person_name_range'" in messages(again.value)
    assert db.keys() == []


def test_remove_drops_what_no_one_else_declares_leaves_what_a_startup_catalog_does_and_forgets_both(
    db: Store, caplog: pytest.LogCaptureFixture
):
    shared = range_index("Shared", "p")
    own = range_index("Own", "p")
    manager = db.schema(SchemaCatalog.of(shared).named("app"))
    manager.enforce()
    manager.ensure("lib", shared, own)
    assert db.keys("lib") == sorted(inventory_key(it) for it in (own, shared))

    with caplog.at_level(logging.WARNING, logger="drivine.schema"):
        manager.remove("lib", shared, own)

    assert db.held(shared) is not None
    assert db.held(own) is None
    assert db.keys("lib") == []
    assert any("left in place, as 'app' declare(s) it" in it for it in warnings(caplog))
    # The item removed is no longer the owner's at runtime, so an enforce doesn't make it again.
    manager.enforce()
    assert db.held(own) is None
    assert db.keys("app") == [inventory_key(shared)]


def test_remove_by_an_owner_with_no_marker_drops_what_no_one_declares_and_writes_no_marker(db: Store):
    made = range_index("Made", "p")
    kept = range_index("Kept", "p")
    db.gom.indexes.ensure(made)
    db.schema().ensure("app", kept)

    db.schema().remove("nobody", made, kept)

    assert db.held(made) is None
    assert db.held(kept) is not None
    assert db.cypher("MATCH (m:_DrivineSchema {scope: 'nobody'}) RETURN count(m)") == [0]
    assert db.keys("app") == [inventory_key(kept)]
    with pytest.raises(DrivineError, match="cannot carry a version"):
        db.schema().remove_catalog(SchemaCatalog.of(made).named("nobody").with_version("2"))


def test_a_vector_and_a_full_text_index_are_dropped_by_the_apis_name_or_by_an_alias(db: Store):
    vector = vector_index("Chunk", "embedding", 3, "EUCLIDEAN")
    full_text = full_text_index("Chunk", ["text"])
    db.ddl(
        "CREATE VECTOR INDEX FOR (n:Chunk) ON n.embedding OPTIONS {indexConfig: "
        "{`vector.dimensions`: 3, `vector.similarity_function`: 'euclidean'}}"
    )
    db.ddl("CREATE FULLTEXT INDEX chunk_text FOR (n:Chunk) ON EACH [n.text]")
    assert db.keys() == sorted(inventory_key(it) for it in (vector, full_text))

    db.restart()
    db.ddl(f"DROP INDEX {item_name(vector)}")
    db.ddl("DROP INDEX chunk_text")

    assert db.held(vector) is None
    assert db.held(full_text) is None
    assert db.keys() == []


def test_a_uniqueness_constraint_over_keys_held_more_than_once_fails_and_is_not_made(db: Store):
    db.cypher("CREATE (:Person {email: 'a@example.com'}), (:Person {email: 'a@example.com'})")

    with pytest.raises(Exception) as violation:  # noqa: PT011
        db.ddl("CREATE CONSTRAINT person_email FOR (p:Person) REQUIRE p.email IS UNIQUE")

    assert "VIOLATION" in messages(violation.value)
    assert db.held(uniqueness_constraint("Person", "email")) is None


def test_ddl_through_a_manager_made_without_a_factory_runs_through_one_of_its_own(db: Store):
    provider = db.registry.connection_provider(db.store)
    assert provider is not None
    standalone = NonTransactionalPersistenceManager(provider, db.store)

    db.ddl("CREATE INDEX by_name FOR (p:Person) ON (p.name)", standalone)

    assert db.aliases(range_index("Person", "name")) == ["by_name"]
    assert db.keys() == [inventory_key(range_index("Person", "name"))]
    db.ddl("DROP INDEX by_name", standalone)
    assert db.held(range_index("Person", "name")) is None


def test_ddl_through_the_non_transactional_manager_inside_a_transaction_is_not_undone_by_its_rollback(
    db: Store,
):
    with transaction(rollback=True):
        db.ddl("CREATE INDEX by_name FOR (p:Person) ON (p.name)")

    assert db.held(range_index("Person", "name")) is not None
    assert db.keys() == [inventory_key(range_index("Person", "name"))]
