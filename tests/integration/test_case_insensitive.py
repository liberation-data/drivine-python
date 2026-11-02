"""
CaseInsensitive on every store: the lowercased copy each write keeps, the items over it, filling it in, and the lookups
that read it once it is. drivine4j's CaseInsensitiveCrossEngineTest.
"""

import asyncio
import uuid
from typing import Annotated, Any

import pytest
from pydantic import create_model

from drivine import (
    CaseInsensitive,
    DatabaseType,
    DrivineError,
    GraphProperty,
    GraphView,
    NodeFragment,
    NodeId,
    NodeStamp,
    NullPolicy,
    PersistenceManager,
    QuerySpecification,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    count,
    key,
    range_index,
    refs,
    uniqueness_constraint,
)
from tests.integration.stores import create, is_oracle, is_postgres, nodes

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


async def stored(pm: PersistenceManager, label: str) -> dict[str, dict[str, Any]]:
    return {it["uuid"]: it for it in await nodes(pm, label)}


async def set_copy(pm: PersistenceManager, label: str, uuid_: str, value: str) -> None:
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
    else:
        statement = f"MATCH (n:{label} {{uuid: $uuid}}) SET n.`{COPY}` = $value"
    await pm.execute(QuerySpecification[Any](statement).bind({"uuid": uuid_, "value": value}))


class TestTheCopy:
    async def test_a_save_keeps_it_from_the_property_and_a_load_doesnt_see_it(self, gom, pm, label):
        account = fragment(label)
        saved = await gom.save(account(uuid="a1", email="Ada@X.COM", display_name="Ada Lovelace", name="Ada"))
        found = await stored(pm, label)
        assert found["a1"][COPY] == "ada@x.com"
        assert found["a1"][NAME_COPY] == "ada lovelace"
        assert "__drivine.lower.name" not in found["a1"]
        loaded = await gom.load(account, "a1")
        assert loaded == saved
        await gom.save(saved.model_copy(update={"email": "Grace@X.com"}))
        assert (await stored(pm, label))["a1"][COPY] == "grace@x.com"
        cleared = (await gom.load(account, "a1")).model_copy(update={"email": None})
        await gom.save(cleared, null_policy=NullPolicy.CLEAR)
        assert COPY not in (await stored(pm, label))["a1"]

    async def test_is_no_change_to_the_stamp(self, gom, pm, label):
        account = fragment(label)
        first = await gom.save(account(uuid="a1", email="Ada@X.com"))
        # Out of step, as a write that bypassed Drivine leaves it: a save of the same values only puts it back.
        await set_copy(pm, label, "a1", "stale")
        again = await gom.save(account(uuid="a1", email="Ada@X.com"))
        assert (await stored(pm, label))["a1"][COPY] == "ada@x.com"
        assert first.stamp.split(":")[0] == again.stamp.split(":")[0]

    async def test_create_update_and_save_from_keep_it(self, gom, pm, label):
        account = fragment(label)
        await gom.create(account(uuid="a1", email="Ada@X.com"))
        assert (await stored(pm, label))["a1"][COPY] == "ada@x.com"
        await gom.update(account, "a1", lambda it: it.model_copy(update={"email": "Bo@X.com"}))
        assert (await stored(pm, label))["a1"][COPY] == "bo@x.com"
        loaded = await gom.load(account, "a1")
        await gom.save(loaded.model_copy(update={"email": "Cy@X.com"}), from_=loaded)
        assert (await stored(pm, label))["a1"][COPY] == "cy@x.com"

    async def test_save_all_patch_and_patch_all_keep_it(self, gom, pm, label):
        account = fragment(label)
        await gom.save_all([account(uuid="a1", email="Ada@X.com"), account(uuid="a2", email="Bo@X.com")])
        assert {k: v[COPY] for k, v in (await stored(pm, label)).items()} == {"a1": "ada@x.com", "a2": "bo@x.com"}
        await gom.patch(account, "a1", set={"email": "Cy@X.com"})
        assert (await stored(pm, label))["a1"][COPY] == "cy@x.com"
        await gom.patch(account, "a1", clear=["email"])
        assert COPY not in (await stored(pm, label))["a1"]
        changed = [account(uuid="a1", email="Di@X.com"), account(uuid="a2", email="Ed@X.com")]
        await gom.patch_all(changed, only=["email"])
        assert {k: v[COPY] for k, v in (await stored(pm, label)).items()} == {"a1": "di@x.com", "a2": "ed@x.com"}

    async def test_a_patch_writes_it_only_on_a_node_it_changed(self, gom, pm, label):
        account = fragment(label)
        saved = await gom.save(account(uuid="a1", email="Ann@X.com"))
        # Out of step, as a write that bypassed Drivine leaves it: a patch that changes nothing leaves it so.
        await set_copy(pm, label, "a1", "stale")
        assert not (await gom.patch(account, "a1", set={"email": "Ann@X.com"})).changed
        assert (await stored(pm, label))["a1"][COPY] == "stale"
        assert (await gom.load(account, "a1")).stamp == saved.stamp
        assert (await gom.patch(account, "a1", set={"email": "Anne@X.com"})).changed
        assert (await stored(pm, label))["a1"][COPY] == "anne@x.com"

    async def test_a_views_save_keeps_it_on_the_root_and_related_nodes(self, gom, pm, label):
        account = fragment(label)
        friends = view_of(account)
        root, friend = account(uuid="a1", email="Ada@X.com"), account(uuid="a2", email="Bo@X")
        await gom.save(friends(account=root, friends=[friend]))
        assert {k: v[COPY] for k, v in (await stored(pm, label)).items()} == {"a1": "ada@x.com", "a2": "bo@x"}


class TestLookups:
    async def test_read_the_copy_once_it_is_filled_in_and_the_property_before(self, gom, pm, label):
        account = fragment(label)
        # Written outside Drivine, so no node has a copy until the index fills it in.
        await create(pm, label, {"uuid": "a1", "email": "Ada@X.com"}, {"uuid": "a2", "email": "Bo@X.com"})
        q = refs(account)
        assert [it.uuid for it in await gom.load_all(account, q.email.eq_ignore_case("ADA@x.com"))] == ["a1"]
        assert (await gom.indexes.ensure(range_index(label, "email", ignore_case=True))).status == "CREATED"
        assert {k: v.get(COPY) for k, v in (await stored(pm, label)).items()} == {"a1": "ada@x.com", "a2": "bo@x.com"}
        assert [it.uuid for it in await gom.load_all(account, q.email.eq_ignore_case("ADA@x.com"))] == ["a1"]
        assert [it.uuid for it in await gom.load_all(account, q.email.contains_ignore_case("BO@"))] == ["a2"]
        # A decoy copy is found by the copy: the lookup reads it, not the property.
        await set_copy(pm, label, "a2", "decoy")
        assert [it.uuid for it in await gom.load_all(account, q.email.eq_ignore_case("DECOY"))] == ["a2"]
        assert await gom.count(account, q.email.eq_ignore_case("decoy")) == 1
        groups = await gom.groups.load(
            account, keys={"email": key(q.email)}, aggregates={"n": count()}, where=q.email.eq_ignore_case("Decoy")
        )
        assert groups == [{"email": "Bo@X.com", "n": 1}]
        patched = await gom.patch(account, "a2", set={"name": "Bo"}, where=q.email.eq_ignore_case("DECOY"))
        assert patched.applied
        # An empty value is compared with the property: no copy is kept of one.
        assert await gom.load_all(account, q.email.eq_ignore_case("")) == []
        # The patch changed the node, so it wrote the copy again from the property.
        assert (await stored(pm, label))["a2"][COPY] == "bo@x.com"
        await set_copy(pm, label, "a2", "decoy")
        assert await gom.delete_all(account, q.email.eq_ignore_case("Decoy")) == 1
        assert (await gom.indexes.ensure(range_index(label, "email", ignore_case=True))).status == "ALREADY_MATCHING"

    async def test_another_manager_reads_the_record(self, gom, pm, label):
        account = fragment(label)
        await create(pm, label, {"uuid": "a1", "email": "Ada@X.com"})
        await gom.indexes.ensure(range_index(label, "email", ignore_case=True))
        await set_copy(pm, label, "a1", "decoy")
        other = StatelessGraphObjectManager(pm)
        assert [it.uuid for it in await other.load_all(account, refs(account).email.eq_ignore_case("Decoy"))] == ["a1"]

    @pytest.mark.async_only
    async def test_managers_recording_at_once_each_keep_theirs_and_record_each_once(self, pm, label):
        properties = ["p1", "p2", "p3", "p4"]
        # Each property twice: two record the same copy at once, and others different ones beside them.
        await asyncio.gather(
            *(StatelessGraphObjectManager(pm)._lower_copies().fill_in(label, [it], True) for it in properties * 2)
        )
        recorded = await StatelessGraphObjectManager(pm)._lower_copies()._stored()
        assert {it for it in recorded if it[0] == label} == {(label, it) for it in properties}

    async def test_a_recreated_index_fills_the_copies_in_again(self, gom, pm, label):
        account = fragment(label)
        spec = range_index(label, "email", ignore_case=True)
        await gom.save(account(uuid="a1", email="Ada@X.com"))
        await gom.indexes.ensure(spec)
        await set_copy(pm, label, "a1", "stale")
        assert (await gom.indexes.recreate(spec)).status == "RECREATED"
        assert (await stored(pm, label))["a1"][COPY] == "ada@x.com"

    async def test_an_item_made_anew_fills_the_copies_in_again_though_they_are_recorded(self, gom, pm, label):
        account = fragment(label)
        index = range_index(label, "email", ignore_case=True)
        await gom.save(account(uuid="a1", email="Ada@X.com"))
        await gom.indexes.ensure(index)
        await gom.indexes.drop(index)
        # Out of step while the index was gone, as a write made while the field was not CaseInsensitive leaves it.
        await set_copy(pm, label, "a1", "stale")
        assert (await gom.indexes.ensure(index)).status == "CREATED"
        assert (await stored(pm, label))["a1"][COPY] == "ada@x.com"
        if pm.type is DatabaseType.FALKORDB:
            return
        # The index aside, which Neo4j would take for one in the constraint's way.
        await gom.indexes.drop(index)
        await set_copy(pm, label, "a1", "stale")
        constraint = uniqueness_constraint(label, "email", ignore_case=True)
        assert (await gom.constraints.ensure(constraint)).status == "CREATED"
        assert (await stored(pm, label))["a1"][COPY] == "ada@x.com"


class TestUniqueness:
    async def test_two_values_differing_only_in_case_are_a_violation_and_then_refused(self, gom, pm, label):
        account = fragment(label)
        spec = uniqueness_constraint(label, "email", ignore_case=True)
        await create(pm, label, {"uuid": "a1", "email": "a@x.com"}, {"uuid": "a2", "email": "A@X.COM"})
        if pm.type is DatabaseType.FALKORDB:
            with pytest.raises(DrivineError, match="551"):
                await gom.constraints.ensure(spec)
            assert COPY not in (await stored(pm, label))["a1"]
            return
        assert (await gom.constraints.ensure(spec)).status == "VIOLATION"
        await gom.delete(account, "a2")
        assert (await gom.constraints.ensure(spec)).status == "CREATED"
        with pytest.raises(Exception):  # noqa: B017 - each engine refuses in its own words
            await gom.save(account(uuid="a3", email="A@X.COM"))
        await gom.constraints.drop(spec)
