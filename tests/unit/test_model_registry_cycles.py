"""
A view that fails takes with it every model built on its account: those may refer to it, as two views that refer to
each other do, and a model that refers to a refused view can't be loaded.
"""

from __future__ import annotations

import re
import threading
from typing import Annotated, Any

import pytest

from drivine import DrivineError, GraphView, NodeFragment, NodeId, Relationship, RelationshipFragment, Root, TargetNode
from drivine.model.model_registry import ModelRegistry


class Room(NodeFragment, labels=["Room"]):
    id: Annotated[str, NodeId()]


class Broken(NodeFragment, labels=["Broken"]):
    a: Annotated[str, NodeId()]
    b: Annotated[str, NodeId()]


class Left(GraphView):
    room: Annotated[Room, Root()]
    right: Annotated[Right | None, Relationship("NEXT")] = None


class Right(GraphView):
    room: Annotated[Room, Root()]
    left: Annotated[Left | None, Relationship("NEXT")] = None
    broken: Annotated[Broken | None, Relationship("BROKEN")] = None


class Visit(RelationshipFragment):
    to: Annotated[Right, TargetNode()]


class Tour(GraphView):
    room: Annotated[Room, Root()]
    visits: Annotated[list[Visit], Relationship("VISITS")] = []


REFUSED = re.escape("Broken has 2 NodeId fields (a, b). A node has one id.")


@pytest.mark.parametrize("first", [Left, Right])
def test_a_view_that_refers_to_a_refused_view_is_refused_whichever_is_asked_for_first(first: type):
    registry = ModelRegistry()
    with pytest.raises(DrivineError, match=REFUSED):
        registry.view(first)
    # The other was built, and cached, on the first's account: it is refused too, rather than loaded and failing later.
    for view in (Left, Right):
        with pytest.raises(DrivineError, match=REFUSED):
            registry.view(view)


def test_a_relationship_fragment_built_on_a_refused_view_s_account_is_refused_too():
    registry = ModelRegistry()
    with pytest.raises(DrivineError, match=REFUSED):
        registry.view(Tour)
    with pytest.raises(DrivineError, match=REFUSED):
        registry.relationship_fragment(Visit)


class Up(GraphView):
    room: Annotated[Room, Root()]
    down: Annotated[Down | None, Relationship("NEXT")] = None


class Down(GraphView):
    room: Annotated[Room, Root()]
    up: Annotated[Up | None, Relationship("NEXT")] = None


def test_two_views_that_refer_to_each_other_and_load_are_each_built_once():
    registry = ModelRegistry()
    down = registry.view(Down)
    up = registry.view(Up)
    assert registry.view(Down) is down
    assert down.relationships[0].related is up
    assert up.relationships[0].related is down


def test_a_view_refused_on_one_thread_is_refused_again_while_another_thread_builds_a_view(
    monkeypatch: pytest.MonkeyPatch,
):
    registry = ModelRegistry()
    build_view = registry._build_view  # pyright: ignore[reportPrivateUsage]
    building, done = threading.Event(), threading.Event()

    def slowly(type_: type) -> Any:
        if type_ is Down:
            building.set()
            done.wait(5)
        return build_view(type_)

    monkeypatch.setattr(registry, "_build_view", slowly)
    other = threading.Thread(target=registry.view, args=(Up,))
    other.start()
    assert building.wait(5)
    # Each thread's build is its own: this one's refusal isn't left to the other's to undo.
    with pytest.raises(DrivineError, match=REFUSED):
        registry.view(Right)
    done.set()
    other.join(5)
    with pytest.raises(DrivineError, match=REFUSED):
        registry.view(Right)
