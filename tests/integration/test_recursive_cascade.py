"""
A cascade through a view that contains itself deletes what a load of it reads: the nodes at every level and what each
level holds, and nothing the view does not declare. A tree deeper than the relationship's max_depth is refused before
anything is written, where the cascade would otherwise leave the levels below it behind. drivine4j's
RecursiveCascadeDeleteCrossEngineTest, on every store; DELETE_ORPHAN on all but Memgraph, which has none.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

import pytest

from drivine import (
    CascadeType,
    DatabaseType,
    Direction,
    DrivineError,
    GraphPath,
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    QuerySpecification,
    ReadOnly,
    Relationship,
    Root,
    StatelessGraphObjectManager,
    hop,
)
from tests.integration.stores import create, equals, is_oracle, is_postgres, node_ids, nodes, relate


class Session(NodeFragment, labels=["Session"]):
    id: Annotated[str, NodeId()]


class Message(NodeFragment, labels=["Message"]):
    id: Annotated[str, NodeId()]


class SessionTree(GraphView):
    session: Annotated[Session, Root()]
    messages: Annotated[list[Message], Relationship("HAS_MESSAGE")] = []
    parts: Annotated[list["SessionTree"], Relationship("PART_OF", Direction.INCOMING, max_depth=3)] = []


def tree(id: str, message: str, *parts: SessionTree) -> SessionTree:
    return SessionTree(session=Session(id=id), messages=[Message(id=message)], parts=list(parts))


def ids(view: SessionTree) -> set[str]:
    return {view.session.id, *(it.id for it in view.messages)}.union(*(ids(it) for it in view.parts))


async def test_deletes_what_each_level_holds_as_load_reads_it(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    await gom.save(tree("rc-root", "rc-m0", tree("rc-child", "rc-m1", tree("rc-grand", "rc-m2"))))
    loaded = await gom.load(SessionTree, "rc-root")
    assert loaded is not None
    assert ids(loaded) == {"rc-root", "rc-m0", "rc-child", "rc-m1", "rc-grand", "rc-m2"}

    assert await gom.delete(SessionTree, "rc-root", cascade=CascadeType.DELETE_ALL) == 1

    assert await nodes(pm, "Session") == []
    assert await nodes(pm, "Message") == []


# ----- drivine4j's RecursiveCascadeDeleteCrossEngineTest -----


class TreeSession(NodeFragment, labels=["Session"]):
    id: Annotated[str, NodeId()]
    title: str | None = None


class Attachment(NodeFragment, labels=["Attachment"]):
    id: Annotated[str, NodeId()]


class MessageWithAttachments(GraphView):
    message: Annotated[Message, Root()]
    attachments: Annotated[list[Attachment], Relationship("HAS_ATTACHMENT")] = []


class DeletableSessionTree(GraphView):
    """
    A Session, its messages, and the sessions PART_OF it, to three levels. Each level holds its own messages, as the
    root does, so a cascade deletes those at every level, and refuses a tree deeper than three levels rather than leave
    the rest of it behind.
    """

    session: Annotated[TreeSession, Root()]
    messages: Annotated[list[Message], Relationship("HAS_MESSAGE")] = []
    parts: Annotated[list["DeletableSessionTree"], Relationship("PART_OF", Direction.INCOMING, max_depth=3)] = []


class DeletableSessionTreeDeep(GraphView):
    """DeletableSessionTree whose messages are a nested view, so each level reaches its messages' attachments too."""

    session: Annotated[TreeSession, Root()]
    messages: Annotated[list[MessageWithAttachments], Relationship("HAS_MESSAGE")] = []
    parts: Annotated[list["DeletableSessionTreeDeep"], Relationship("PART_OF", Direction.INCOMING, max_depth=3)] = []


class DeletableSessionShelf(GraphView):
    """A session that shelves session trees: the recursive relationship is one view in."""

    session: Annotated[TreeSession, Root()]
    trees: Annotated[list[DeletableSessionTree], Relationship("SHELVES")] = []


Graph = tuple[list[tuple[str, str]], list[tuple[str, str, str]]]
"""A graph to seed: each node by label and id, and each relationship from one id to another."""


def plus(a: Graph, b: Graph) -> Graph:
    return [*a[0], *b[0]], [*a[1], *b[1]]


TREE: Graph = (
    [
        ("User", "rc-user"),
        ("Session", "rc-root"),
        ("Message", "rc-m0"),
        ("Session", "rc-child"),
        ("Message", "rc-m1"),
        ("Session", "rc-grand"),
        ("Message", "rc-m2"),
    ],
    [
        ("rc-root", "HAS_MESSAGE", "rc-m0"),
        ("rc-child", "PART_OF", "rc-root"),
        ("rc-child", "HAS_MESSAGE", "rc-m1"),
        ("rc-grand", "PART_OF", "rc-child"),
        ("rc-grand", "HAS_MESSAGE", "rc-m2"),
        ("rc-m1", "AUTHORED_BY", "rc-user"),
        ("rc-m2", "SENT_TO", "rc-user"),
    ],
)
"""
The tree (PART_OF points child → parent; the view's INCOMING relationship makes them parts): rc-root ← rc-child ←
rc-grand, each with a message, and a User the nested messages point at.
"""


def chain(levels: int) -> Graph:
    """Sessions rc-s0 ← rc-s1 ← … ← rc-s[levels], each with a message rc-sm[i]: `levels` below the root."""
    nodes_ = [node for i in range(levels + 1) for node in (("Session", f"rc-s{i}"), ("Message", f"rc-sm{i}"))]
    edges = [(f"rc-s{i}", "HAS_MESSAGE", f"rc-sm{i}") for i in range(levels + 1)]
    edges += [(f"rc-s{i}", "PART_OF", f"rc-s{i - 1}") for i in range(1, levels + 1)]
    return nodes_, edges


BOOKMARKED: Graph = (
    [
        ("User", "rc-user"),
        ("Session", "rc-other"),
        ("Message", "rc-quote"),
        ("Session", "rc-root"),
        ("Message", "rc-m0"),
        ("Message", "rc-m1"),
        ("Attachment", "rc-a1"),
        ("Session", "rc-child"),
        ("Message", "rc-m2"),
        ("Attachment", "rc-a2"),
        ("Message", "rc-m3"),
        ("Attachment", "rc-a3"),
    ],
    [
        ("rc-other", "HAS_MESSAGE", "rc-quote"),
        ("rc-root", "HAS_MESSAGE", "rc-m0"),
        ("rc-root", "HAS_MESSAGE", "rc-m1"),
        ("rc-m1", "HAS_ATTACHMENT", "rc-a1"),
        ("rc-m1", "BOOKMARKED_BY", "rc-user"),
        ("rc-child", "PART_OF", "rc-root"),
        ("rc-child", "HAS_MESSAGE", "rc-m2"),
        ("rc-m2", "HAS_ATTACHMENT", "rc-a2"),
        ("rc-child", "HAS_MESSAGE", "rc-m3"),
        ("rc-m3", "HAS_ATTACHMENT", "rc-a3"),
        ("rc-quote", "QUOTES", "rc-m3"),
    ],
)
"""
The sessions of a chat store: a user bookmarks a root message, and a message of another session quotes one of a
sub-session's. Each attachment hangs off its message alone.
"""


async def seed(pm: PersistenceManager, graph: Graph) -> None:
    nodes_, edges = graph
    if is_oracle(pm) or is_postgres(pm):
        for label, id in nodes_:
            await create(pm, label, {"id": id})
        for src, type, dst in edges:
            await relate(pm, src, type, dst)
        return
    variable = {id: f"v{i}" for i, (_, id) in enumerate(nodes_)}
    created = [f"({variable[id]}:{label} {{id: '{id}'}})" for label, id in nodes_]
    related = [f"({variable[src]})-[:{type}]->({variable[dst]})" for src, type, dst in edges]
    await pm.execute(QuerySpecification(f"CREATE {', '.join([*created, *related])}"))


def tree_ids(tree: DeletableSessionTree) -> set[str]:
    return {tree.session.id, *(it.id for it in tree.messages)}.union(*(tree_ids(it) for it in tree.parts))


def deep_tree_ids(tree: DeletableSessionTreeDeep) -> set[str]:
    held = {id for it in tree.messages for id in (it.message.id, *(a.id for a in it.attachments))}
    return {tree.session.id, *held}.union(*(deep_tree_ids(it) for it in tree.parts))


async def removed_by(pm: PersistenceManager, delete: Callable[[], Awaitable[int]]) -> tuple[set[str], int]:
    """Deletes with `delete` and gives the ids it removed, with the count it returned."""
    before = set(await node_ids(pm))
    deleted = await delete()
    return before - set(await node_ids(pm)), deleted


async def load_tree(gom: StatelessGraphObjectManager, id: str) -> set[str]:
    loaded = await gom.load(DeletableSessionTree, id)
    assert loaded is not None
    return tree_ids(loaded)


def orphans_on(pm: PersistenceManager) -> None:
    if pm.type is DatabaseType.MEMGRAPH:
        pytest.skip("DELETE_ORPHAN is not available on Memgraph.")


async def test_delete_all_removes_what_load_reads_at_every_level(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    await seed(pm, TREE)
    loaded = await load_tree(gom, "rc-root")
    assert loaded == {"rc-root", "rc-m0", "rc-child", "rc-m1", "rc-grand", "rc-m2"}

    removed, deleted = await removed_by(
        pm, lambda: gom.delete(DeletableSessionTree, "rc-root", cascade=CascadeType.DELETE_ALL)
    )

    assert removed == loaded
    assert deleted == 1
    # The nested messages point at the user, which the view does not declare.
    assert await node_ids(pm) == ["rc-user"]


async def test_delete_all_reaches_nested_views_at_every_level(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    attachments: Graph = (
        [("Attachment", "rc-a1"), ("Attachment", "rc-a2")],
        [
            ("rc-m1", "HAS_ATTACHMENT", "rc-a1"),
            ("rc-m2", "HAS_ATTACHMENT", "rc-a2"),
            ("rc-a2", "AUTHORED_BY", "rc-user"),
        ],
    )
    await seed(pm, plus(TREE, attachments))
    tree = {"rc-root", "rc-m0", "rc-child", "rc-m1", "rc-a1", "rc-grand", "rc-m2", "rc-a2"}
    loaded = await gom.load(DeletableSessionTreeDeep, "rc-root")
    assert loaded is not None
    assert deep_tree_ids(loaded) == tree

    removed, _ = await removed_by(
        pm, lambda: gom.delete(DeletableSessionTreeDeep, "rc-root", cascade=CascadeType.DELETE_ALL)
    )

    assert removed == tree
    assert await node_ids(pm) == ["rc-user"]


async def test_delete_all_reaches_a_recursive_view_one_view_in(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    await seed(pm, plus(TREE, ([("Session", "rc-shelf")], [("rc-shelf", "SHELVES", "rc-root")])))
    loaded = {"rc-shelf", *await load_tree(gom, "rc-root")}

    removed, _ = await removed_by(
        pm, lambda: gom.delete(DeletableSessionShelf, "rc-shelf", cascade=CascadeType.DELETE_ALL)
    )

    assert removed == loaded


async def test_a_tree_of_exactly_max_depth_and_a_ring_are_deleted(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    # Exactly max_depth levels below the root: nothing is left below, so nothing is refused.
    await seed(pm, chain(3))
    loaded = await load_tree(gom, "rc-s0")
    assert len(loaded) == 8
    removed, _ = await removed_by(pm, lambda: gom.delete(DeletableSessionTree, "rc-s0", cascade=CascadeType.DELETE_ALL))
    assert removed == loaded

    # A ring of four: one level past max_depth comes back to the root, which the cascade starts from.
    await seed(pm, plus(chain(3), ([], [("rc-s0", "PART_OF", "rc-s3")])))
    assert await load_tree(gom, "rc-s0") == loaded
    removed, _ = await removed_by(pm, lambda: gom.delete(DeletableSessionTree, "rc-s0", cascade=CascadeType.DELETE_ALL))
    assert removed == loaded


async def test_a_ring_through_a_shelf_back_to_the_tree_it_shelves_is_deleted(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    # One level past max_depth is the shelved tree again: a node the cascade takes, so nothing is refused.
    shelved: Graph = ([("Session", "rc-shelf")], [("rc-s0", "PART_OF", "rc-s3"), ("rc-shelf", "SHELVES", "rc-s0")])
    await seed(pm, plus(chain(3), shelved))
    loaded = {"rc-shelf", *await load_tree(gom, "rc-s0")}

    removed, _ = await removed_by(
        pm, lambda: gom.delete(DeletableSessionShelf, "rc-shelf", cascade=CascadeType.DELETE_ALL)
    )

    assert removed == loaded


async def test_a_tree_deeper_than_max_depth_is_refused(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    # Four levels below the root, one past max_depth = 3.
    await seed(pm, plus(chain(4), ([("Session", "rc-shelf")], [("rc-shelf", "SHELVES", "rc-s0")])))
    seeded = await node_ids(pm)

    cascades = [CascadeType.DELETE_ALL]
    if pm.type is not DatabaseType.MEMGRAPH:
        cascades.append(CascadeType.DELETE_ORPHAN)
    for cascade in cascades:
        with pytest.raises(DrivineError) as refused:
            await gom.delete(DeletableSessionTree, "rc-s0", cascade=cascade)
        assert str(refused.value) == (
            "Cannot cascade-delete DeletableSessionTree 'rc-s0': DeletableSessionTree.parts reaches 3 levels deep "
            "(max_depth = 3), and 1 node below that would be left behind. Raise max_depth on parts to cover the "
            "deepest level. Nothing was deleted."
        )
        # The recursive relationship one view in is checked from where the shelf reaches it.
        through_shelf = "DeletableSessionShelf 'rc-shelf': DeletableSessionTree.parts reaches 3 levels deep"
        with pytest.raises(DrivineError, match=through_shelf):
            await gom.delete(DeletableSessionShelf, "rc-shelf", cascade=cascade)
        assert await node_ids(pm) == seeded, "nothing is deleted when a cascade is refused"

    # The check reads the roots the delete would: a predicate that matches nothing refuses nothing.
    where = equals(pm, "title", "title")
    archived = await gom.delete(
        DeletableSessionTree, "rc-s0", where, {"title": "archived"}, cascade=CascadeType.DELETE_ALL
    )
    assert archived == 0
    assert await node_ids(pm) == seeded
    # Without a cascade the delete takes the root alone, as it always has.
    assert await gom.delete(DeletableSessionTree, "rc-s0") == 1


# ----- drivine4j's StatelessSaveSoundnessContract: a cascade follows only the relationships a save writes -----


class User(NodeFragment, labels=["User"]):
    id: Annotated[str, NodeId()]


class ReviewedSessionTree(GraphView):
    """DeletableSessionTree with reviewers it only reads, at every level: a cascade deletes the levels, not those."""

    session: Annotated[TreeSession, Root()]
    messages: Annotated[list[Message], Relationship("HAS_MESSAGE")] = []
    reviewers: Annotated[list[User], Relationship("REVIEWED_BY"), ReadOnly()] = []
    parts: Annotated[list["ReviewedSessionTree"], Relationship("PART_OF", Direction.INCOMING, max_depth=3)] = []


class SessionDescendants(GraphView):
    """The sessions below a session, a flat list read over several hops: read-only, as no one relationship holds it."""

    session: Annotated[TreeSession, Root()]
    below: Annotated[list[TreeSession], Relationship("PART_OF", Direction.INCOMING, max_depth=3)] = []


class SessionAuthors(GraphView):
    """The users who wrote a session's messages, read over a path of two hops."""

    session: Annotated[TreeSession, Root()]
    authors: Annotated[
        list[User], GraphPath([hop("HAS_MESSAGE", Direction.OUTGOING, "Message"), hop("AUTHORED_BY")])
    ] = []


CASCADES = pytest.mark.parametrize("cascade", [CascadeType.DELETE_ALL, CascadeType.DELETE_ORPHAN])


def cascades_on(pm: PersistenceManager, cascade: CascadeType) -> None:
    if cascade is CascadeType.DELETE_ORPHAN:
        orphans_on(pm)


@CASCADES
async def test_a_cascade_leaves_the_nodes_of_a_read_only_field(pm: PersistenceManager, cascade: CascadeType):
    cascades_on(pm, cascade)
    gom = StatelessGraphObjectManager(pm)
    reviewed: Graph = (
        [("User", "rc-reviewer")],
        [("rc-root", "REVIEWED_BY", "rc-reviewer"), ("rc-grand", "REVIEWED_BY", "rc-reviewer")],
    )
    await seed(pm, plus(TREE, reviewed))
    loaded = await gom.load(ReviewedSessionTree, "rc-root")
    assert loaded is not None
    assert [it.id for it in loaded.reviewers] == ["rc-reviewer"]
    assert [it.id for it in loaded.parts[0].parts[0].reviewers] == ["rc-reviewer"]

    removed, _ = await removed_by(pm, lambda: gom.delete(ReviewedSessionTree, "rc-root", cascade=cascade))

    # A view nested in itself is not read-only: each of its levels is deleted.
    if cascade is CascadeType.DELETE_ALL:
        assert removed == {"rc-root", "rc-m0", "rc-child", "rc-m1", "rc-grand", "rc-m2"}
    else:
        # The reviewer is outside the cascade, so it keeps rc-grand, with its message; rc-user keeps rc-m1.
        assert removed == {"rc-root", "rc-m0", "rc-child"}
    assert "rc-reviewer" in await node_ids(pm)


@CASCADES
async def test_a_cascade_leaves_the_nodes_of_a_list_read_over_several_hops(
    pm: PersistenceManager, cascade: CascadeType
):
    cascades_on(pm, cascade)
    gom = StatelessGraphObjectManager(pm)
    await seed(pm, chain(2))
    loaded = await gom.load(SessionDescendants, "rc-s0")
    assert loaded is not None
    assert {it.id for it in loaded.below} == {"rc-s1", "rc-s2"}

    removed, _ = await removed_by(pm, lambda: gom.delete(SessionDescendants, "rc-s0", cascade=cascade))

    assert removed == {"rc-s0"}


@CASCADES
async def test_a_cascade_does_not_follow_a_path_field_as_one_relationship(pm: PersistenceManager, cascade: CascadeType):
    cascades_on(pm, cascade)
    gom = StatelessGraphObjectManager(pm)
    # The path's first hop leads straight to a user here: taken as one relationship, the user would go.
    authored: Graph = (
        [("Session", "rc-root"), ("Message", "rc-m0"), ("User", "rc-user"), ("User", "rc-author")],
        [
            ("rc-root", "HAS_MESSAGE", "rc-user"),
            ("rc-root", "HAS_MESSAGE", "rc-m0"),
            ("rc-m0", "AUTHORED_BY", "rc-author"),
        ],
    )
    await seed(pm, authored)
    loaded = await gom.load(SessionAuthors, "rc-root")
    assert loaded is not None
    assert [it.id for it in loaded.authors] == ["rc-author"]

    removed, _ = await removed_by(pm, lambda: gom.delete(SessionAuthors, "rc-root", cascade=cascade))

    assert removed == {"rc-root"}


async def test_delete_orphan_keeps_a_nested_node_referred_to_from_outside(pm: PersistenceManager):
    orphans_on(pm)
    gom = StatelessGraphObjectManager(pm)
    await seed(pm, TREE)
    loaded = await load_tree(gom, "rc-root")

    removed, deleted = await removed_by(
        pm, lambda: gom.delete(DeletableSessionTree, "rc-root", cascade=CascadeType.DELETE_ORPHAN)
    )

    # The nested messages are kept: the user outside the view still refers to each. The sessions are deleted, though
    # each relates to its own messages and parts: those are in the cascade.
    assert removed == loaded - {"rc-m1", "rc-m2"}
    assert deleted == 1
    assert await node_ids(pm) == ["rc-m1", "rc-m2", "rc-user"]


async def test_delete_orphan_deletes_a_ring_back_to_its_root(pm: PersistenceManager):
    orphans_on(pm)
    gom = StatelessGraphObjectManager(pm)
    # A ring of three: the parts come back to the root at the last level, and with it the root's message.
    await seed(pm, plus(chain(2), ([], [("rc-s0", "PART_OF", "rc-s2")])))
    loaded = await load_tree(gom, "rc-s0")
    assert len(loaded) == 6

    removed, deleted = await removed_by(
        pm, lambda: gom.delete(DeletableSessionTree, "rc-s0", cascade=CascadeType.DELETE_ORPHAN)
    )

    assert removed == loaded
    assert deleted == 1


async def test_delete_orphan_keeps_what_a_kept_node_holds(pm: PersistenceManager):
    orphans_on(pm)
    gom = StatelessGraphObjectManager(pm)
    await seed(pm, BOOKMARKED)

    removed, _ = await removed_by(
        pm, lambda: gom.delete(DeletableSessionTreeDeep, "rc-root", cascade=CascadeType.DELETE_ORPHAN)
    )

    # The bookmarked and the quoted message are kept whole, with their attachments; their sessions go.
    assert removed == {"rc-root", "rc-m0", "rc-child", "rc-m2", "rc-a2"}
    assert set(await node_ids(pm)) == {"rc-user", "rc-other", "rc-quote", "rc-m1", "rc-a1", "rc-m3", "rc-a3"}


async def test_delete_orphan_keeps_a_kept_sub_session_whole(pm: PersistenceManager):
    orphans_on(pm)
    gom = StatelessGraphObjectManager(pm)
    # A sub-session of the sub-session, and a pin on the sub-session from outside.
    pinned: Graph = (
        [("Session", "rc-grand"), ("Message", "rc-m4")],
        [
            ("rc-grand", "PART_OF", "rc-child"),
            ("rc-grand", "HAS_MESSAGE", "rc-m4"),
            ("rc-child", "PINNED_BY", "rc-user"),
        ],
    )
    await seed(pm, plus(BOOKMARKED, pinned))

    removed, _ = await removed_by(
        pm, lambda: gom.delete(DeletableSessionTreeDeep, "rc-root", cascade=CascadeType.DELETE_ORPHAN)
    )

    # The pinned sub-session keeps everything below it, at every level; only the root's own go.
    assert removed == {"rc-root", "rc-m0"}


async def test_delete_orphan_keeps_a_chain_longer_than_the_view_below_a_kept_node(pm: PersistenceManager):
    """
    A sub-session that is also part of the root directly: the parts below a pinned one form a chain six steps long,
    longer than the view's fields taken to their depths one after another, and the rule keeps the whole of it.
    """
    orphans_on(pm)
    gom = StatelessGraphObjectManager(pm)
    await seed(
        pm,
        (
            [
                ("User", "rc-user"),
                ("Session", "rc-root"),
                ("Message", "rc-m0"),
                *(("Session", f"rc-n{i}") for i in range(6)),
                ("Message", "rc-m5"),
            ],
            [
                ("rc-root", "HAS_MESSAGE", "rc-m0"),
                ("rc-n0", "PART_OF", "rc-root"),
                ("rc-n1", "PART_OF", "rc-n0"),
                ("rc-n2", "PART_OF", "rc-n1"),
                ("rc-n3", "PART_OF", "rc-n2"),
                ("rc-n3", "PART_OF", "rc-root"),
                ("rc-n4", "PART_OF", "rc-n3"),
                ("rc-n5", "PART_OF", "rc-n4"),
                ("rc-n5", "HAS_MESSAGE", "rc-m5"),
                ("rc-n0", "PINNED_BY", "rc-user"),
            ],
        ),
    )

    removed, _ = await removed_by(
        pm, lambda: gom.delete(DeletableSessionTree, "rc-root", cascade=CascadeType.DELETE_ORPHAN)
    )

    # rc-n0 is pinned, and each of rc-n1 … rc-n5 and rc-m5 is one step below the one before it.
    assert removed == {"rc-root", "rc-m0"}
