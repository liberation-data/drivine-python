from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from drivine.annotation.direction import Direction
from drivine.backend.sql.node_writes import NodeWrites
from drivine.backend.sql.owned_labels import OwnedLabels
from drivine.backend.sql.root_step import RootStep
from drivine.model import stamps

WriteReturns = Literal["STAMPS", "EACH_STAMP", "COUNT"]
"""
What a statement that carries out a plan gives back: STAMPS, one row, the root's stamp as handed back, then
`index/found=stamp` for each node whose stamp is; EACH_STAMP, a row for each node whose stamp is handed back,
`index/found=stamp`; COUNT, one row, the number of relationships the steps counted.
"""


class WritePlan:
    """
    The steps drivine4j's write function carries out, gathered as they are decided: its plan, a JSON array of steps,
    each saying its kind under `op`. A node a step saves or finds is known to later steps by the number it is given
    here. The names a step's fields are read by are drivine4j's `WritePlan`'s, which the function reads.
    """

    PLAN = "_plan"
    """The parameter the plan is bound as."""
    MARK = "_mark"
    """The parameter that carries the stamp the steps take a new relationship token from."""

    def __init__(self) -> None:
        self.mark = stamps.fresh()
        """The stamp the plan's steps take the relationship token from."""
        self._steps: list[dict[str, Any]] = []
        """The steps in the order they were added, which is the order they are carried out in."""
        self._shapes: list[dict[str, Any]] = []
        """The shapes the steps name, by number, in the order they were first named: each `{labels, key}`."""
        self._nodes = 0
        """How many nodes the steps have saved or found, which is the number the next is given."""

    @property
    def plan(self) -> list[dict[str, Any]]:
        """The steps, as they are bound."""
        return self._steps

    @property
    def shapes_bound(self) -> list[dict[str, Any]]:
        """The shapes the steps name, as they are bound: each `{labels, key}`, in the order the plan numbers them."""
        return self._shapes

    def shape(self, labels: Sequence[str], key: str) -> int:
        """The number of the shape a node of the labels, found by the property `key`, has: one number for one shape."""
        for i, it in enumerate(self._shapes):
            if it["key"] == key and it["labels"] == list(labels):
                return i
        self._shapes.append({"labels": list(labels), "key": key})
        return len(self._shapes) - 1

    def node(
        self,
        shape: int,
        key: str,
        id: Any,
        stored_id: str,
        labels: Sequence[str],
        writes: NodeWrites,
        index: str | None = None,
        root: RootStep | None = None,
    ) -> int:
        """
        The step that saves the node of `shape` whose id is `id`, stored under `key` as the text `stored_id`: found and
        locked, or made with `labels`. Gives the number later steps know the node by. `index` is the index the node's
        stamp is returned under, when it is; `root` what the step says about the save's root, None for any other node.
        """
        node = self._nodes
        self._nodes += 1
        owned = writes.owned
        self._add(
            "node",
            {
                "r": node,
                "s": shape,
                "k": stored_id,
                "key": key,
                "id": id,
                "labels": sorted(labels),
                "fresh": stamps.fresh(),
                "set": _non_empty(writes.set),
                "unset": _non_empty(writes.unset),
                "add": _non_empty(writes.add),
                "drop": _non_empty(writes.drop),
                "owned": _owned(owned) if owned else None,
                "lower": _non_empty(writes.lower),
                "i": index,
                "root": True if root else None,
                "carried": root.carried if root else None,
                "expect": root.expect if root else None,
                "whole": True if root and root.whole else None,
                "create": False if root and root.mode == "FOUND" else None,
                "absent": True if root and root.mode == "ABSENT" else None,
            },
        )
        return node

    def ref(self, shape: int, stored_id: str) -> int:
        """The step that finds the node of `shape` whose stored id is `stored_id`, which must be there."""
        node = self._nodes
        self._nodes += 1
        self._add("ref", {"r": node, "s": shape, "k": stored_id})
        return node

    def link(
        self,
        from_: int,
        to: int,
        type: str,
        direction: Direction,
        properties: Mapping[str, Any],
        clear: Sequence[str] = (),
        always: bool = False,
    ) -> None:
        """
        The step that joins `from_` to `to` with a relationship of `type` in `direction`, as it says and once, unless
        `always` makes one more. `clear` names the properties the relationship must not have.
        """
        self._add(
            "link",
            {
                "from": from_,
                "to": to,
                "type": type,
                "dir": code(direction),
                "props": _non_empty(dict(properties)),
                "clear": _non_empty(list(clear)),
                "always": True if always else None,
            },
        )

    def unlink(
        self,
        from_: int,
        type: str,
        direction: Direction,
        shape: int,
        ids: Sequence[str],
        keep: bool,
        remove: bool,
        held: Sequence[str],
    ) -> None:
        """
        The step that removes the relationships of `type` in `direction` from `from_` to the nodes of `shape` whose
        stored ids are `ids`, or with `keep` to every one but those. With `remove`, a node left with no relationship on
        the field's side goes too, unless its id is among `held`.
        """
        self._add(
            "unlink",
            {
                "from": from_,
                "type": type,
                "dir": code(direction),
                "s": shape,
                "ids": list(ids),
                "keep": True if keep else None,
                "delete": True if remove else None,
                "held": _non_empty(list(held)),
            },
        )

    def cut(self, from_: int, to: int | None, type: str, direction: Direction) -> None:
        """
        The step that removes the relationships of `type` in `direction` from `from_` to `to`, or with none to every
        node.
        """
        self._add("cut", {"from": from_, "to": to, "type": type, "dir": code(direction)})

    def patch(
        self,
        shape: int,
        stored_id: str,
        index: str,
        set: Mapping[str, Any],
        unset: Sequence[str],
        lower: Sequence[str] = (),
    ) -> None:
        """
        The step that writes `set` to the node of `shape` whose stored id is `stored_id`, and removes `unset` from it,
        if the node is there. It makes no node. The node is returned under `index`, with whether the step changed it.
        It writes the lowercased copy of each of `lower` on a node it changed, as a `node` step does.
        """
        self._add(
            "patch",
            {
                "s": shape,
                "k": stored_id,
                "i": index,
                "fresh": stamps.fresh(),
                "set": _non_empty(dict(set)),
                "unset": _non_empty(list(unset)),
                "lower": _non_empty(list(lower)),
            },
        )

    def _add(self, op: str, fields: Mapping[str, Any]) -> None:
        """Adds a step of the kind with its fields, leaving out a field that is None: the function reads it as null."""
        self._steps.append({"op": op, **{key: value for key, value in fields.items() if value is not None}})


def _owned(owned: OwnedLabels) -> dict[str, Any]:
    """The labels an open NodeLabels field owns, as a step names them."""
    return {"prop": owned.property, "clear": True, "base": owned.base} if owned.clear else {"prop": owned.property}


def code(direction: Direction) -> str:
    """The direction as a step writes it."""
    if direction is Direction.INCOMING:
        return "IN"
    return "BOTH" if direction is Direction.UNDIRECTED else "OUT"


def _non_empty(value: Any) -> Any:
    """The value, or None where it is empty: a step leaves out what it has nothing of."""
    return value if value else None
