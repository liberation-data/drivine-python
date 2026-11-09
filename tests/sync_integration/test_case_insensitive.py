# Written by scripts/unasync.py from tests/integration/test_case_insensitive.py:
# change that, and run the script again.
"""
CaseInsensitive on every store: the lowercased copy each write keeps, the items over it, filling it in, and the lookups
that read it once it is. drivine4j's CaseInsensitiveCrossEngineTest.
"""

import contextlib
import os
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from pydantic import create_model

from drivine.query.statement import cypher_statement
from drivine.sync import (
    CaseInsensitive,
    ConnectionProperties,
    DatabaseRegistry,
    DatabaseType,
    DrivineError,
    GraphProperty,
    GraphView,
    NodeFragment,
    NodeId,
    NodeStamp,
    NullPolicy,
    PersistenceManager,
    PersistenceManagerFactory,
    QuerySpecification,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    count,
    key,
    lower_copies,
    range_index,
    refs,
    uniqueness_constraint,
)
from drivine.sync.schema.index_manager import IndexNotBuilt
from drivine.testing.container_connection import ContainerConnection
from drivine.testing.drivine_test_containers import TEST_CONTAINER_SPECS, DrivineTestContainers
from tests.sync_integration.stores import create, is_oracle, is_postgres, is_sqlite, nodes

COPY = "__drivine.lower.email"
NAME_COPY = "__drivine.lower.display_name"


def fragment(label: str) -> Any:
    """An account of a label of the test's own, so the schema items and records, which outlive its data, are its own."""

    class Base(NodeFragment, labels=[label]):
        uuid: Annotated[str, NodeId()]
        email: Annotated[str | None, CaseInsensitive()] = None
        display_name: Annotated[str | None, CaseInsensitive(), GraphProperty("display_name")] = None
        name: str | None = None
        stamp: Annotated[str | None, NodeStamp()] = None

    return Base


def view_of(account: Any) -> Any:
    return create_model(
        "Friends",
        __base__=GraphView,
        account=(Annotated[account, Root()], ...),
        friends=(Annotated[list[account], Relationship("KNOWS")], []),
    )


@pytest.fixture
def gom(pm: PersistenceManager) -> StatelessGraphObjectManager:
    return StatelessGraphObjectManager(pm)


@pytest.fixture
def label() -> str:
    return f"Acct{uuid.uuid4().hex[:10]}"


def stored(pm: PersistenceManager, label: str) -> dict[str, dict[str, Any]]:
    return {it["uuid"]: it for it in nodes(pm, label)}


def set_copy(pm: PersistenceManager, label: str, uuid_: str, value: str) -> None:
    """Sets a node's copy outside the object manager, to tell a lookup that reads the copy from one that doesn't."""
    if is_oracle(pm):
        statement = (
            f"UPDATE drivine_node n SET n.props = JSON_TRANSFORM(n.props, SET '$.\"{COPY}\"' = $value) "
            f"WHERE JSON_EXISTS(n.labels, '$?(@ == \"{label}\")') AND JSON_VALUE(n.props, '$.uuid') = $uuid"
        )
    elif is_postgres(pm):
        statement = (
            f"UPDATE drivine_node n SET props = n.props || jsonb_build_object('{COPY}', CAST($value AS text)) "
            f"WHERE n.labels @> '[\"{label}\"]' AND (n.props ->> 'uuid') = $uuid"
        )
    elif is_sqlite(pm):
        statement = (
            f"UPDATE drivine_node SET props = json_set(props, '$.\"{COPY}\"', CAST($value AS TEXT)) "
            f"WHERE instr(labels, '\"{label}\"') > 0 AND (props ->> '$.uuid') = $uuid"
        )
    else:
        statement = f"MATCH (n:{label} {{uuid: $uuid}}) SET n.`{COPY}` = $value"
    pm.execute(QuerySpecification[Any](statement).bind({"uuid": uuid_, "value": value}))


@pytest.fixture(scope="module")
def own_neo4j(request: pytest.FixtureRequest) -> Iterator[ContainerConnection]:
    """
    A Neo4j of the module's own, for the one test that writes a text too long for a range index. Neo4j checks the size
    of a write's index entries before it logs the write, but not against an index still populating: a write checked
    while a new index populates, and applied once it is online, is refused only after it is in the log. Neo4j 2026.10
    then fails to apply it and panics ("The database has encountered a critical error, and needs to be restarted"), and
    every later test on that Neo4j fails. TODO(upstream): link the Neo4j report. Started only when that test runs on
    Neo4j, and stopped after the module.
    """
    if not request.config.getini("drivine_test_containers") or DrivineTestContainers.use_local(
        DatabaseType.NEO4J, os.environ
    ):
        pytest.skip("Runs on a Neo4j container of its own, and test containers are off for Neo4j")
    spec = TEST_CONTAINER_SPECS[DatabaseType.NEO4J]
    started = spec.container(DrivineTestContainers.image(DatabaseType.NEO4J, os.environ) or spec.image).start()
    try:
        yield spec.connection(started)
    finally:
        started.stop()


@contextlib.contextmanager
def own_manager(reach: ContainerConnection) -> Iterator[PersistenceManager]:
    """A manager of the Neo4j `own_neo4j` started, in a registry of its own, closed after."""
    registry = DatabaseRegistry()
    registry.builder().with_properties(
        ConnectionProperties(DatabaseType.NEO4J, reach.host, reach.port, reach.user_name, reach.password)
    ).register("own_neo4j")
    try:
        yield PersistenceManagerFactory(registry).get("own_neo4j")
    finally:
        registry.close()


class TestTheCopy:
    def test_a_save_keeps_it_from_the_property_and_a_load_doesnt_see_it(self, gom, pm, label):
        account = fragment(label)
        saved = gom.save(account(uuid="a1", email="Ada@X.COM", display_name="Ada Lovelace", name="Ada"))
        found = stored(pm, label)
        assert found["a1"][COPY] == "ada@x.com"
        assert found["a1"][NAME_COPY] == "ada lovelace"
        assert "__drivine.lower.name" not in found["a1"]
        loaded = gom.load(account, "a1")
        assert loaded == saved
        gom.save(saved.model_copy(update={"email": "Grace@X.com"}))
        assert (stored(pm, label))["a1"][COPY] == "grace@x.com"
        cleared = (gom.load(account, "a1")).model_copy(update={"email": None})
        gom.save(cleared, null_policy=NullPolicy.CLEAR)
        assert COPY not in (stored(pm, label))["a1"]

    def test_is_no_change_to_the_stamp(self, gom, pm, label):
        account = fragment(label)
        first = gom.save(account(uuid="a1", email="Ada@X.com"))
        # Out of step, as Cypher or SQL of your own leaves it: a save of the same values only puts it back.
        set_copy(pm, label, "a1", "stale")
        again = gom.save(account(uuid="a1", email="Ada@X.com"))
        assert (stored(pm, label))["a1"][COPY] == "ada@x.com"
        assert first.stamp.split(":")[0] == again.stamp.split(":")[0]

    def test_create_update_and_save_from_keep_it(self, gom, pm, label):
        account = fragment(label)
        gom.create(account(uuid="a1", email="Ada@X.com"))
        assert (stored(pm, label))["a1"][COPY] == "ada@x.com"
        gom.update(account, "a1", lambda it: it.model_copy(update={"email": "Bo@X.com"}))
        assert (stored(pm, label))["a1"][COPY] == "bo@x.com"
        loaded = gom.load(account, "a1")
        gom.save(loaded.model_copy(update={"email": "Cy@X.com"}), from_=loaded)
        assert (stored(pm, label))["a1"][COPY] == "cy@x.com"

    def test_save_all_patch_and_patch_all_keep_it(self, gom, pm, label):
        account = fragment(label)
        gom.save_all([account(uuid="a1", email="Ada@X.com"), account(uuid="a2", email="Bo@X.com")])
        assert {k: v[COPY] for k, v in (stored(pm, label)).items()} == {"a1": "ada@x.com", "a2": "bo@x.com"}
        gom.patch(account, "a1", set={"email": "Cy@X.com"})
        assert (stored(pm, label))["a1"][COPY] == "cy@x.com"
        gom.patch(account, "a1", clear=["email"])
        assert COPY not in (stored(pm, label))["a1"]
        changed = [account(uuid="a1", email="Di@X.com"), account(uuid="a2", email="Ed@X.com")]
        gom.patch_all(changed, only=["email"])
        assert {k: v[COPY] for k, v in (stored(pm, label)).items()} == {"a1": "di@x.com", "a2": "ed@x.com"}

    def test_a_patch_writes_it_only_on_a_node_it_changed(self, gom, pm, label):
        account = fragment(label)
        saved = gom.save(account(uuid="a1", email="Ann@X.com"))
        # Out of step, as Cypher or SQL of your own leaves it: a patch that changes nothing leaves it so.
        set_copy(pm, label, "a1", "stale")
        assert not (gom.patch(account, "a1", set={"email": "Ann@X.com"})).changed
        assert (stored(pm, label))["a1"][COPY] == "stale"
        assert (gom.load(account, "a1")).stamp == saved.stamp
        assert (gom.patch(account, "a1", set={"email": "Anne@X.com"})).changed
        assert (stored(pm, label))["a1"][COPY] == "anne@x.com"

    def test_a_views_save_keeps_it_on_the_root_and_related_nodes(self, gom, pm, label):
        account = fragment(label)
        friends = view_of(account)
        root, friend = account(uuid="a1", email="Ada@X.com"), account(uuid="a2", email="Bo@X")
        gom.save(friends(account=root, friends=[friend]))
        assert {k: v[COPY] for k, v in (stored(pm, label)).items()} == {"a1": "ada@x.com", "a2": "bo@x"}


class TestLookups:
    def test_read_the_copy_once_it_is_filled_in_and_the_property_before(self, gom, pm, label):
        account = fragment(label)
        # Written outside Drivine, so no node has a copy until the index fills it in.
        create(pm, label, {"uuid": "a1", "email": "Ada@X.com"}, {"uuid": "a2", "email": "Bo@X.com"})
        q = refs(account)
        assert [it.uuid for it in gom.load_all(account, q.email.eq_ignore_case("ADA@x.com"))] == ["a1"]
        assert (gom.indexes.ensure(range_index(label, "email", ignore_case=True))).status == "CREATED"
        assert {k: v.get(COPY) for k, v in (stored(pm, label)).items()} == {"a1": "ada@x.com", "a2": "bo@x.com"}
        assert [it.uuid for it in gom.load_all(account, q.email.eq_ignore_case("ADA@x.com"))] == ["a1"]
        assert [it.uuid for it in gom.load_all(account, q.email.contains_ignore_case("BO@"))] == ["a2"]
        # A decoy copy is found by the copy: the lookup reads it, not the property.
        set_copy(pm, label, "a2", "decoy")
        assert [it.uuid for it in gom.load_all(account, q.email.eq_ignore_case("DECOY"))] == ["a2"]
        assert gom.count(account, q.email.eq_ignore_case("decoy")) == 1
        groups = gom.groups.load(
            account, keys={"email": key(q.email)}, aggregates={"n": count()}, where=q.email.eq_ignore_case("Decoy")
        )
        assert groups == [{"email": "Bo@X.com", "n": 1}]
        patched = gom.patch(account, "a2", set={"name": "Bo"}, where=q.email.eq_ignore_case("DECOY"))
        assert patched.applied
        # An empty value is compared with the property: no copy is kept of one.
        assert gom.load_all(account, q.email.eq_ignore_case("")) == []
        # The patch changed the node, so it wrote the copy again from the property.
        assert (stored(pm, label))["a2"][COPY] == "bo@x.com"
        set_copy(pm, label, "a2", "decoy")
        assert gom.delete_all(account, q.email.eq_ignore_case("Decoy")) == 1
        assert (gom.indexes.ensure(range_index(label, "email", ignore_case=True))).status == "ALREADY_MATCHING"

    def test_another_manager_reads_the_record(self, gom, pm, label):
        account = fragment(label)
        create(pm, label, {"uuid": "a1", "email": "Ada@X.com"})
        gom.indexes.ensure(range_index(label, "email", ignore_case=True))
        set_copy(pm, label, "a1", "decoy")
        other = StatelessGraphObjectManager(pm)
        assert [it.uuid for it in other.load_all(account, refs(account).email.eq_ignore_case("Decoy"))] == ["a1"]

    def test_a_recreated_index_fills_the_copies_in_again(self, gom, pm, label):
        account = fragment(label)
        spec = range_index(label, "email", ignore_case=True)
        gom.save(account(uuid="a1", email="Ada@X.com"))
        gom.indexes.ensure(spec)
        set_copy(pm, label, "a1", "stale")
        assert (gom.indexes.recreate(spec)).status == "RECREATED"
        assert (stored(pm, label))["a1"][COPY] == "ada@x.com"

    def test_cypher_of_your_own_keeps_no_copy_unless_it_sets_one_or_a_recreate_follows(self, gom, pm, label):
        account = fragment(label)
        spec = range_index(label, "email", ignore_case=True)
        gom.save(account(uuid="c1", email="Ann@X.com"))
        gom.indexes.ensure(spec)
        q = refs(account)
        set_copy_ = lower_copies.set_clause("n", "email")

        def cypher(text: str) -> None:
            pm.execute(QuerySpecification[Any](cypher_statement(text)))

        def found(email: str) -> set[str]:
            return {it.uuid for it in gom.load_all(account, q.email.eq_ignore_case(email))}

        # Run as written, or compiled to SQL, a write adds nothing: the copy is stale, or missing.
        cypher(f"MATCH (n:{label} {{uuid: 'c1'}}) SET n.email = 'Anne@X.com'")
        cypher(f"CREATE (:{label} {{uuid: 'c2', email: 'Bob@X.com'}})")
        assert found("ANNE@x.com") == set()
        assert found("ann@x.com") == {"c1"}
        assert found("bob@x.com") == set()

        # The documented way: the copy set in the same statement, in a SET of its own.
        cypher(f"MATCH (n:{label} {{uuid: 'c1'}}) SET n.email = 'Cat@X.com' SET {set_copy_}")
        cypher(f"CREATE (n:{label} {{uuid: 'c3', email: 'Dan@X.com'}}) SET {set_copy_}")
        assert found("CAT@x.com") == {"c1"}
        assert found("dan@X.COM") == {"c3"}

        # A REMOVE of the property leaves the copy, so the node is found by its old value; the same SET after it
        # removes the copy.
        cypher(f"MATCH (n:{label} {{uuid: 'c1'}}) REMOVE n.email")
        assert (stored(pm, label))["c1"][COPY] == "cat@x.com"
        assert found("cat@x.com") == {"c1"}
        cypher(f"MATCH (n:{label} {{uuid: 'c3'}}) REMOVE n.email SET {set_copy_}")
        assert COPY not in (stored(pm, label))["c3"]
        assert found("dan@x.com") == set()

        # Or a recreate after the writes, which fills every copy of the label in again.
        gom.indexes.recreate(spec)
        assert found("BOB@x.com") == {"c2"}
        assert COPY not in (stored(pm, label))["c1"]
        assert found("cat@x.com") == set()

    def test_an_item_made_anew_fills_the_copies_in_again_though_they_are_recorded(self, gom, pm, label):
        account = fragment(label)
        index = range_index(label, "email", ignore_case=True)
        gom.save(account(uuid="a1", email="Ada@X.com"))
        gom.indexes.ensure(index)
        gom.indexes.drop(index)
        # Out of step while the index was gone, as a write made while the field was not CaseInsensitive leaves it.
        set_copy(pm, label, "a1", "stale")
        assert (gom.indexes.ensure(index)).status == "CREATED"
        assert (stored(pm, label))["a1"][COPY] == "ada@x.com"
        if pm.type is DatabaseType.FALKORDB:
            return
        # The index aside, which Neo4j would take for one in the constraint's way.
        gom.indexes.drop(index)
        set_copy(pm, label, "a1", "stale")
        constraint = uniqueness_constraint(label, "email", ignore_case=True)
        assert (gom.constraints.ensure(constraint)).status == "CREATED"
        assert (stored(pm, label))["a1"][COPY] == "ada@x.com"


class TestLongTexts:
    """A text past what Oracle reads a text as, and a VARCHAR2 holds (drivine4j#532)."""

    def test_one_of_40001_characters_is_saved_with_its_copy_and_found_once_the_index_is_there(self, gom, pm, label):
        if pm.type is DatabaseType.FALKORDB:
            pytest.skip("FalkorDB finds no node by eq_ignore_case on a text this long")
        account = fragment(label)
        spec = range_index(label, "email", ignore_case=True)
        long = "Ab" * 20_000 + "C"
        gom.save_all(
            [
                account(uuid="g1", email=long),
                account(uuid="g2", email="Ann@X.com"),
                account(uuid="g3", email=long + "d"),
                account(uuid="g4", email="Ab"),
            ]
        )
        assert (stored(pm, label))["g1"][COPY] == long.lower()
        # Saved again as an empty text, the copy goes on Oracle, which keeps an empty text as null; elsewhere it is
        # empty.
        gom.save(account(uuid="g4", email=""))
        if is_oracle(pm):
            assert COPY not in (stored(pm, label))["g4"]
        else:
            assert not (stored(pm, label))["g4"].get(COPY)

        try:
            if pm.type is DatabaseType.NEO4J:
                # Neo4j cannot hold a text this long in a range index: the index's build fails, which ensure raises,
                # naming the index, and the lookup reads the label's nodes.
                with pytest.raises(IndexNotBuilt) as failed:
                    gom.indexes.ensure(spec)
                assert f"{label}_email_ci_range" in str(failed.value)
                assert "too large for the index" in str(failed.value)
            else:
                gom.indexes.ensure(spec)
            q = refs(account)

            def found(email: str) -> set[str]:
                return {it.uuid for it in gom.load_all(account, q.email.eq_ignore_case(email))}

            assert found(long.upper()) == {"g1"}
            assert found("ann@x.COM") == {"g2"}
            assert found(long[:-1]) == set()
            assert (gom.load(account, "g1")).email == long
        finally:
            # Dropped, so that a failed index on Neo4j is not waited for by a test after this one.
            gom.indexes.drop(spec)

    def test_ensuring_the_index_fills_in_the_copy_of_a_text_of_5000_bytes_and_a_lookup_finds_it(self, gom, pm, label):
        if pm.type is DatabaseType.FALKORDB:
            pytest.skip("FalkorDB finds no node by eq_ignore_case on a text this long")
        account = fragment(label)
        long = "Ab" * 2_500
        # 2,800 characters of 4,200 bytes: past what Oracle reads a text as, though fewer than 4,000 characters. Only
        # its ASCII is lowercased, as Memgraph's toLower lowercases nothing else.
        wide = "\u00e9A" * 1_400
        # Written outside Drivine, so no node has a copy until the index fills it in.
        create(
            pm,
            label,
            {"uuid": "l1", "email": long},
            {"uuid": "l2", "email": "Bob@X.com"},
            {"uuid": "l3", "email": ""},
            {"uuid": "l4", "email": "", COPY: "stale"},
            {"uuid": "l5", "email": wide},
        )
        assert COPY not in (stored(pm, label))["l1"]

        gom.indexes.ensure(range_index(label, "email", ignore_case=True))
        copies = {k: v.get(COPY) for k, v in (stored(pm, label)).items()}
        assert copies["l1"] == long.lower()
        assert copies["l2"] == "bob@x.com"
        # The copy of an empty text is empty, or on Oracle, which keeps an empty text as null, there is none.
        assert not copies["l3"]
        assert not copies["l4"]
        if is_oracle(pm):
            assert COPY not in (stored(pm, label))["l4"]
        assert copies["l5"] == wide.lower()
        q = refs(account)

        def found(email: str) -> set[str]:
            return {it.uuid for it in gom.load_all(account, q.email.eq_ignore_case(email))}

        assert found(long.upper()) == {"l1"}
        assert found("BOB@x.com") == {"l2"}
        assert found(wide) == {"l5"}
        # Filled in again, the copies stay as the properties are.
        catalog = gom._lower_copies()
        assert catalog is not None
        catalog.fill_in(label, ["email"], True)
        again = {k: v.get(COPY) for k, v in (stored(pm, label)).items()}
        assert again["l1"] == long.lower()
        assert not again["l4"]
        assert again["l5"] == wide.lower()

    def test_under_the_index_over_the_copy_neo4j_refuses_to_save_one_of_40001_characters(self, pm, label, request):
        """
        The reference for a text too long for a range index: once there is the index over the copy, Neo4j refuses.
        On a Neo4j of its own (see `own_neo4j`). The save follows `ensure` at once, which returns only once the index
        is online (see `IndexManager._await_built`), so the save is checked against it and refused before it is logged.
        """
        if pm.type is not DatabaseType.NEO4J:
            pytest.skip("Neo4j's own refusal")
        with own_manager(request.getfixturevalue("own_neo4j")) as own:
            gom = StatelessGraphObjectManager(own)
            account = fragment(label)
            gom.indexes.ensure(range_index(label, "email", ignore_case=True))
            with pytest.raises(Exception, match="is too large for the index"):
                gom.save(account(uuid="g1", email="Ab" * 20_000 + "C"))


class TestUniqueness:
    def test_two_values_differing_only_in_case_are_a_violation_and_then_refused(self, gom, pm, label):
        account = fragment(label)
        spec = uniqueness_constraint(label, "email", ignore_case=True)
        create(pm, label, {"uuid": "a1", "email": "a@x.com"}, {"uuid": "a2", "email": "A@X.COM"})
        if pm.type is DatabaseType.FALKORDB:
            with pytest.raises(DrivineError, match="551"):
                gom.constraints.ensure(spec)
            assert COPY not in (stored(pm, label))["a1"]
            return
        assert (gom.constraints.ensure(spec)).status == "VIOLATION"
        gom.delete(account, "a2")
        assert (gom.constraints.ensure(spec)).status == "CREATED"
        with pytest.raises(Exception):  # noqa: B017 - each engine refuses in its own words
            gom.save(account(uuid="a3", email="A@X.COM"))
        gom.constraints.drop(spec)
