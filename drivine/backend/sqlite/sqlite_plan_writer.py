# A plan is untyped JSON, read here field by field.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from drivine.backend.sql.write_plan import WriteReturns
from drivine.backend.sqlite import exact_json
from drivine.backend.sqlite.sqlite_graph import SqliteGraph, has_labels, text_of
from drivine.model import lower_copies, stamps

R = TypeVar("R")

CHANGED = "1"
UNCHANGED = "0"
UNMET = "-"
"""What a `patch` step hands back in place of the stamp found: as Postgres's write function does."""


class Stopped(Exception):  # noqa: N818 - drivine4j's name: the plan stopped, which is no error.
    """The plan stopped: a root is stale, or a node a step needs is not there. Nothing it wrote is kept."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count


@dataclass(frozen=True)
class _Shape:
    """How a step finds a node: its labels, and the property its id is stored under."""

    labels: list[str]
    key: str


@dataclass(frozen=True)
class _Row:
    """A node as it is stored."""

    nid: int
    labels: list[str]
    props: dict[str, Any]
    stamp: str | None


@dataclass(frozen=True)
class _Handed:
    """A node whose stamp is handed back: under `index`, with what was found of it."""

    index: str
    found: str | None
    nid: int


class SqlitePlanWriter:
    """
    Carries out one plan of steps (see WritePlan) on a graph kept by SQLite, for the write function (see
    sqlite_functions): drivine4j's SqlitePlanWriter, a port of `<graph>_write` on Postgres step for step, so a save
    stamps, checks and writes a node here as it does there. What it works out, it works out over JSON in memory, and
    writes a row only where what the step leaves differs from what it found.

    - A `node` step finds the node of its shape by its id, or makes it. A root that is gone, carries another stamp, or
      is there when the save may only make it, is stale: the plan stops, and nothing is written. A node that is made
      is written whole, with a stamp of its own; one that is found gets a new first token only where the step changes
      it, or where it has no stamp of two tokens yet.
    - A `ref` step finds a node that must be there: one that is not stops the plan, which counts nothing.
    - `link`, `unlink` and `cut` write a node's relationships; each node whose relationships changed gets a new second
      token once, at the end.
    - A `patch` step writes to a node that is there, and only where it meets the query the plan was given, if any.
    - A `node` step, and a `patch` step that changes its node, write the lowercased copy of each property they name
      under `lower`, from what the node then holds (see lower_copies). The copies are no part of whether a step
      changed the node.

    Numbers are read and written as the decimals they are written as (see exact_json).
    """

    def __init__(
        self,
        graph: SqliteGraph,
        connection: sqlite3.Connection,
        plan: str,
        shapes: str,
        mark: str,
        patched_if: str | None,
    ) -> None:
        self._nodes_table = graph.nodes
        self._edges_table = graph.edges
        self._connection = connection
        self._steps: list[dict[str, Any]] = exact_json.loads(plan)
        self._shapes = [
            _Shape([str(it) for it in shape["labels"]], str(shape["key"])) for shape in exact_json.loads(shapes)
        ]
        self._mark = mark
        self._patched_if = patched_if
        self._nodes: dict[int, int] = {}
        """The node each `node` and `ref` step saved or found, by the number the plan gives it."""
        self._handed: list[_Handed] = []
        """The nodes whose stamps are handed back, in the order their steps came."""
        self._relinks: list[int] = []
        """The nodes whose relationships changed, each to get a new second token."""
        self._root: int | None = None
        self._root_made = False
        self._was: str | None = None
        self._carried: str | None = None
        self._count = 0

    def write(self, returns: WriteReturns) -> list[Any]:
        """Carries out the steps and gives back what `returns` asks; raises Stopped where the plan stops."""
        steps: dict[str, Callable[[dict[str, Any]], None]] = {
            "node": self._node,
            "ref": self._ref,
            "link": self._link,
            "unlink": self._unlink,
            "cut": self._cut,
            "patch": self._patch,
        }
        for step in self._steps:
            run = steps.get(str(step.get("op")))
            if run is None:
                raise ValueError(f"A plan has a step drivine_write does not know: {step.get('op')}")
            run(step)
        # Each node whose relationships changed gets a new second token, once. A stamp that is not two tokens, which
        # no save leaves, is replaced whole.
        if self._relinks:
            self._update(
                f"UPDATE {self._nodes_table} SET stamp = COALESCE(substr({_two_tokens_of('stamp')}, 1, 17), "
                "substr(?, 1, 17)) || substr(?, 18) WHERE nid IN (SELECT value FROM json_each(?))",
                self._mark,
                self._mark,
                exact_json.dumps(list(dict.fromkeys(self._relinks))),
            )
        if returns == "STAMPS":
            stamp = self._stamps()
            return [stamp] if stamp is not None else []
        if returns == "EACH_STAMP":
            current = self._current_stamps()
            return [f"{it.index}/{it.found or ''}={current.get(it.nid) or ''}" for it in self._handed]
        return [self._count]

    def _stamps(self) -> str | None:
        """
        The stamps a save hands back: the root's, keeping the object's own token wherever the node had another when the
        save began, then `index/found=stamp` for each node whose stamp is handed back.
        """
        current = self._current_stamps()
        if self._root is None or current.get(self._root) is None:
            return None
        stamp = current[self._root]
        assert stamp is not None
        carried, was = self._carried, self._was
        if carried is None:
            # An object that carried no stamp is handed the whole stamp only of a node its save made.
            if not self._root_made:
                stamp = stamp[:17]
        elif was != carried:
            first = stamp[:16] if _equal(was[:16] if was is not None else None, carried[:16]) else carried[:16]
            carried_second = carried[17:] or None
            was_second = (was[17:] or None) if was is not None else None
            second = (
                stamp[17:]
                if _equal(was_second, carried_second) or (self._root_made and carried_second is None)
                else carried[17:]
            )
            stamp = f"{first}:{second}"
        return stamp + "".join(f",{it.index}/{it.found or ''}={current.get(it.nid) or ''}" for it in self._handed)

    def _current_stamps(self) -> dict[int, str | None]:
        """The stamp of each node a step saved or found, as it is now; a node deleted since has none."""
        wanted = list(
            dict.fromkeys([*(it.nid for it in self._handed), *([self._root] if self._root is not None else [])])
        )
        if not wanted:
            return {}
        return dict(
            self._rows(
                f"SELECT nid, stamp FROM {self._nodes_table} WHERE nid IN (SELECT value FROM json_each(?))",
                lambda row: (int(row[0]), row[1]),
                exact_json.dumps(wanted),
            )
        )

    # ----- Steps -----

    def _node(self, step: dict[str, Any]) -> None:
        """Saves a node: found, or made. What the step writes is worked out over the node as it was found."""
        shape = self._shapes[int(step["s"])]
        expect = _text(step, "expect")
        fresh = str(step["fresh"])
        tries = 0
        nid = 0
        made = False
        found: str | None = None
        while True:
            row = self._find(shape, str(step["k"]))
            made = row is None
            if row is None:
                # A checked save does not make the node again: it is gone, and the object is stale. Nor does a step
                # that makes no node.
                if expect is not None or not _flag(step, "create", True):
                    raise Stopped(self._count)
                labels = [str(it) for it in step["labels"]]
                o: dict[str, Any] = {str(step["key"]): step["id"]}
                stamp: str | None = None
            elif _flag(step, "absent", False):
                # A step that only makes the node writes nothing to one that is there.
                raise Stopped(self._count)
            else:
                labels = list(row.labels)
                o = dict(row.props)
                stamp = row.stamp
            found = stamp
            if expect is not None and (
                stamp is None or (stamp if _flag(step, "whole", False) else stamp[:16]) != expect
            ):
                raise Stopped(self._count)

            # The properties the step sets, and without those it unsets.
            _set_and_unset(o, step)
            # The keys of each property bag the object no longer holds.
            for bag in step.get("bags") or []:
                for key in list(o):
                    if not _contains(bag.get("keep"), key) and _owns(bag, key):
                        del o[key]

            # The labels an open label field holds are recorded on the node, so the node knows which are its own. A
            # step that clears replaces the record, and drops the labels recorded that the field no longer holds, but
            # for those of the class; one that does not adds to the record.
            add = _strings(step.get("add"))
            drop = _strings(step.get("drop"))
            owned = step.get("owned")
            if owned is not None:
                property = str(owned["prop"])
                held = o.get(property)
                recorded = [str(it) for it in held] if isinstance(held, list) else []
                if _flag(owned, "clear", False):
                    base = _strings(owned.get("base"))
                    drop += [it for it in recorded if it not in add and it not in base]
                    if not add:
                        o.pop(property, None)
                    else:
                        o[property] = list(add)
                elif add:
                    o[property] = sorted(it for it in recorded if it not in add) + add

            # The node's labels with those the step adds and without those it drops, in order; labels that are as
            # they were are not written again.
            relabel = False
            if add or drop:
                # Python orders text by its code points, the byte order Postgres's `COLLATE "C"` sorts labels in.
                now = sorted(it for it in dict.fromkeys([*labels, *add]) if it not in drop)
                relabel = set(now) != set(labels)
                if relabel or made:
                    labels = now

            # The copies are written from what the step leaves, and are no part of whether it changed the node.
            copied = _lowered(o, step.get("lower"))
            if made:
                # A node that is made is written once, whole, with a stamp of its own. A uniqueness constraint may
                # refuse it: once, the node is looked for again, as on Postgres; a second time, the constraint is on
                # something other than this node's key, and refuses this save.
                inserted = self._rows(
                    f"INSERT INTO {self._nodes_table} (labels, props, stamp) VALUES (?, ?, ?) "
                    "ON CONFLICT DO NOTHING RETURNING nid",
                    lambda it: int(it[0]),
                    exact_json.dumps(labels),
                    exact_json.dumps(copied),
                    fresh,
                )
                if inserted:
                    nid = inserted[0]
                    break
                tries += 1
                if tries > 1:
                    self._update(
                        f"INSERT INTO {self._nodes_table} (labels, props, stamp) VALUES (?, ?, ?)",
                        exact_json.dumps(labels),
                        exact_json.dumps(copied),
                        fresh,
                    )
            else:
                assert row is not None
                nid = row.nid
                same = exact_json.same(o, row.props)
                # Whether the properties, copies and all, are as they were.
                kept = exact_json.same(copied, row.props)
                # A new first token only if the step changes the node, or the node has no stamp of two tokens yet.
                two = _two_tokens(stamp)
                if two is None or not same or relabel:
                    stamp = fresh[:16] + (two[16:] if two is not None else fresh[16:])
                    if relabel:
                        self._update(
                            f"UPDATE {self._nodes_table} SET labels = ? WHERE nid = ?", exact_json.dumps(labels), nid
                        )
                    if not kept:
                        self._update(
                            f"UPDATE {self._nodes_table} SET props = ?, stamp = ? WHERE nid = ?",
                            exact_json.dumps(copied),
                            stamp,
                            nid,
                        )
                    else:
                        self._update(f"UPDATE {self._nodes_table} SET stamp = ? WHERE nid = ?", stamp, nid)
                elif not kept:
                    # Only a copy was out of step with its property: the node keeps its stamp.
                    self._update(
                        f"UPDATE {self._nodes_table} SET props = ? WHERE nid = ?", exact_json.dumps(copied), nid
                    )
                break

        self._nodes[int(step["r"])] = nid
        index = _text(step, "i")
        if index is not None:
            # Nothing for a node the step made, and a mark for one that was there without a stamp.
            self._handed.append(_Handed(index, None if made else found or stamps.NEVER_STAMPED, nid))
        if _flag(step, "root", False):
            self._root = nid
            self._root_made = made
            self._was = found
            self._carried = _text(step, "carried")

    def _ref(self, step: dict[str, Any]) -> None:
        """Finds a node that must be there."""
        row = self._find(self._shapes[int(step["s"])], str(step["k"]))
        if row is None:
            raise Stopped(0)
        self._nodes[int(step["r"])] = row.nid

    def _link(self, step: dict[str, Any]) -> None:
        """Joins two nodes. The relationship is as the step says only if every one of its type between them is."""
        from_ = self._nodes[int(step["from"])]
        to = self._nodes[int(step["to"])]
        type_ = str(step["type"])
        direction = str(step["dir"])
        props: dict[str, Any] = step.get("props") or {}
        clear = _strings(step.get("clear"))
        seen = 0
        unchanged = 0
        if not _flag(step, "always", False):
            # A node's relationship to itself is one relationship, whichever way it is read.
            existing = self._rows(
                f"SELECT eid, props FROM {self._edges_table} WHERE type = ? AND ((? IN ('OUT', 'BOTH') AND src = ? "
                "AND dst = ?) OR ((? = 'IN' OR (? = 'BOTH' AND ? <> ?)) AND src = ? AND dst = ?)) ORDER BY eid",
                lambda it: (int(it[0]), _object(it[1])),
                type_,
                direction,
                from_,
                to,
                direction,
                direction,
                from_,
                to,
                to,
                from_,
            )
            for eid, stored in existing:
                seen += 1
                written = {**stored, **props}
                for key in clear:
                    written.pop(key, None)
                if exact_json.same(written, stored):
                    unchanged += 1
                else:
                    self._update(
                        f"UPDATE {self._edges_table} SET props = ? WHERE eid = ?", exact_json.dumps(written), eid
                    )
        if seen == 0:
            # An undirected field is satisfied by a relationship either way, and makes one from the first node.
            incoming = direction == "IN"
            self._update(
                f"INSERT INTO {self._edges_table} (src, dst, type, props) VALUES (?, ?, ?, ?)",
                to if incoming else from_,
                from_ if incoming else to,
                type_,
                exact_json.dumps(props),
            )
        if seen == 0 or unchanged < seen:
            self._relinks += [from_, to]
        self._count += max(seen, 1)

    def _unlink(self, step: dict[str, Any]) -> None:
        """Removes a field's relationships to the targets whose ids the step names, or to every target but those."""
        from_ = self._nodes[int(step["from"])]
        shape = self._shapes[int(step["s"])]
        type_ = str(step["type"])
        direction = str(step["dir"])
        ids = set(_strings(step.get("ids")))
        held = set(_strings(step.get("held")))
        keep = _flag(step, "keep", False)
        delete = _flag(step, "delete", False)
        key = text_of("t", shape.key)
        labels = f" AND {has_labels('t', shape.labels)}" if shape.labels else ""
        # A relationship from the node to itself is read as leaving it, and so once when both directions are read.
        reached = self._rows(
            f"SELECT e.eid, t.nid, {key} FROM {self._edges_table} e JOIN {self._nodes_table} t ON t.nid = e.dst "
            f"WHERE ? IN ('OUT', 'BOTH') AND e.src = ? AND e.type = ?{labels} "
            "UNION ALL "
            f"SELECT e.eid, t.nid, {key} FROM {self._edges_table} e JOIN {self._nodes_table} t ON t.nid = e.src "
            f"WHERE ? IN ('IN', 'BOTH') AND e.dst = ? AND e.type = ?{labels} AND (? = 'IN' OR e.src <> ?)",
            lambda it: (int(it[0]), int(it[1]), None if it[2] is None else str(it[2])),
            direction,
            from_,
            type_,
            direction,
            from_,
            type_,
            direction,
            from_,
        )
        gone = [it for it in reached if it[2] is not None and (it[2] in ids) != keep]
        if not gone:
            return
        targets = [it[1] for it in gone]
        for target in sorted(set(targets)):
            # A target gone since the relationships were read has nothing left to remove or count.
            if not self._rows(f"SELECT 1 FROM {self._nodes_table} WHERE nid = ?", lambda it: True, target):
                continue
            # What refers to a target is a relationship on the side this field reaches it from: counted before any is
            # removed.
            refs = 0
            if delete:
                if direction in ("OUT", "BOTH"):
                    refs += self._count_of(f"SELECT count(*) FROM {self._edges_table} WHERE dst = ?", (target,))
                if direction in ("IN", "BOTH"):
                    # A relationship from the node to itself is counted once.
                    refs += self._count_of(
                        f"SELECT count(*) FROM {self._edges_table} WHERE src = ? AND (? = 'IN' OR dst <> ?)",
                        (target, direction, target),
                    )
            removed = targets.count(target)
            id_ = next(it[2] for it in gone if it[1] == target)
            # The root is never deleted, and nor is a node the object still holds in another field.
            if delete and target != from_ and refs == removed and id_ not in held:
                self._relinks = [it for it in self._relinks if it != target]
                self._update(f"DELETE FROM {self._nodes_table} WHERE nid = ?", target)
            else:
                self._relinks.append(target)
        self._update(
            f"DELETE FROM {self._edges_table} WHERE eid IN (SELECT value FROM json_each(?))",
            exact_json.dumps([it[0] for it in gone]),
        )
        self._relinks.append(from_)
        self._count += len(gone)

    def _cut(self, step: dict[str, Any]) -> None:
        """
        Removes the relationships of a type from one node to another, or with no other node named to any in a
        direction.
        """
        from_ = self._nodes[int(step["from"])]
        to = self._nodes[int(step["to"])] if step.get("to") is not None else None
        type_ = str(step["type"])
        direction = str(step["dir"])
        gone = self._rows(
            f"SELECT eid, dst FROM {self._edges_table} WHERE ? IN ('OUT', 'BOTH') AND src = ? AND type = ? "
            "AND (? IS NULL OR dst = ?) "
            "UNION ALL "
            f"SELECT eid, src FROM {self._edges_table} WHERE ? IN ('IN', 'BOTH') AND dst = ? AND type = ? "
            "AND (? IS NULL OR src = ?) AND (? = 'IN' OR src <> ?)",
            lambda it: (int(it[0]), int(it[1])),
            direction,
            from_,
            type_,
            to,
            to,
            direction,
            from_,
            type_,
            to,
            to,
            direction,
            from_,
        )
        if not gone:
            return
        for target in sorted({it[1] for it in gone}):
            # An end gone since the relationships were read took them with it, and has no token to change.
            if self._rows(f"SELECT 1 FROM {self._nodes_table} WHERE nid = ?", lambda it: True, target):
                self._relinks.append(target)
        self._update(
            f"DELETE FROM {self._edges_table} WHERE eid IN (SELECT value FROM json_each(?))",
            exact_json.dumps([it[0] for it in gone]),
        )
        self._relinks.append(from_)
        self._count += len(gone)

    def _patch(self, step: dict[str, Any]) -> None:
        """
        Writes properties to a node that is there, and makes none. One that is not there is left out of what the call
        hands back; one that does not meet the query is left as it is, and said to.
        """
        row = self._find(self._shapes[int(step["s"])], str(step["k"]))
        if row is None:
            return
        fresh = str(step["fresh"])
        met = 1
        if self._patched_if is not None:
            # The query names the node's id `$1`, as Postgres's write function binds it.
            met = self._count_of(self._patched_if, {"1": row.nid})
        o = dict(row.props)
        if met > 0:
            _set_and_unset(o, step)
        same = exact_json.same(o, row.props)
        # A node the step changes has its copies written from what it now holds.
        if not same:
            o = _lowered(o, step.get("lower"))
        two = _two_tokens(row.stamp)
        if two is None or not same:
            stamp = fresh[:16] + (two[16:] if two is not None else fresh[16:])
            if not same:
                self._update(
                    f"UPDATE {self._nodes_table} SET props = ?, stamp = ? WHERE nid = ?",
                    exact_json.dumps(o),
                    stamp,
                    row.nid,
                )
            else:
                self._update(f"UPDATE {self._nodes_table} SET stamp = ? WHERE nid = ?", stamp, row.nid)
        outcome = UNMET if met == 0 else CHANGED if not same else UNCHANGED
        self._handed.append(_Handed(str(step["i"]), outcome, row.nid))

    # ----- Reading -----

    def _find(self, shape: _Shape, id_: str) -> _Row | None:
        """The node of the shape whose id is stored as `id_`, the first made where there are several."""
        labels = f" AND {has_labels('n', shape.labels)}" if shape.labels else ""
        found = self._rows(
            f"SELECT n.nid, n.labels, n.props, n.stamp FROM {self._nodes_table} n "
            f"WHERE {text_of('n', shape.key)} = ?{labels} ORDER BY n.nid LIMIT 1",
            lambda it: _Row(int(it[0]), [str(label) for label in exact_json.loads(it[1])], _object(it[2]), it[3]),
            id_,
        )
        return found[0] if found else None

    def _rows(self, sql: str, read: Callable[[Sequence[Any]], R], *values: Any) -> list[R]:
        cursor = self._connection.execute(sql, values)
        try:
            return [read(it) for it in cursor.fetchall()]
        finally:
            cursor.close()

    def _update(self, sql: str, *values: Any) -> None:
        self._connection.execute(sql, values).close()

    def _count_of(self, sql: str, values: Sequence[Any] | dict[str, Any]) -> int:
        cursor = self._connection.execute(sql, values)
        try:
            row = cursor.fetchone()
            return int(row[0]) if row is not None and row[0] is not None else 0
        finally:
            cursor.close()


def _object(text: str | None) -> dict[str, Any]:
    """The text as a JSON object: properties are stored as one."""
    if text is None:
        return {}
    value = exact_json.loads(text)
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _set_and_unset(o: dict[str, Any], step: dict[str, Any]) -> None:
    """`o` with the properties the step sets, and without those it unsets."""
    written = step.get("set")
    if isinstance(written, dict):
        o.update(written)
    for key in step.get("unset") or []:
        o.pop(str(key), None)


def _lowered(o: dict[str, Any], keys: Any) -> dict[str, Any]:
    """
    `o` with the lowercased copy of each property `keys` names, from what `o` holds, and without the copy of one that
    holds nothing: see lower_copies. It is lowercased as the lower function lowercases what a lookup compares the copy
    with. `o` itself where `keys` names none.
    """
    names = _strings(keys)
    if not names:
        return o
    copied = dict(o)
    for key in names:
        value = copied.get(key)
        if value is None:
            copied.pop(lower_copies.of(key), None)
        else:
            copied[lower_copies.of(key)] = _as_text(value).lower()
    return copied


def _as_text(value: Any) -> str:
    """A JSON value's text, as Jackson's `asText` gives it: a string as itself, anything else as its JSON."""
    return value if isinstance(value, str) else exact_json.dumps(value)


def _owns(bag: dict[str, Any], key: str) -> bool:
    """
    Whether the property bag owns the stored key. A bag with a prefix owns the keys that begin with it; a flat bag,
    each key that is no one else's.
    """
    prefix = _text(bag, "prefix")
    if not _flag(bag, "flat", False):
        return len(key) > len(prefix or "") and (prefix is None or key.startswith(prefix))
    if _contains(bag.get("reservedKeys"), key):
        return False
    return not any(key.startswith(it) for it in _strings(bag.get("reservedPrefixes")))


def _text(node: dict[str, Any], name: str) -> str | None:
    """The text of the field, None where it is absent or null."""
    value = node.get(name)
    return None if value is None else _as_text(value)


def _flag(node: dict[str, Any], name: str, otherwise: bool) -> bool:
    """The boolean field, or `otherwise` where it is absent or null."""
    value = node.get(name)
    return otherwise if value is None else bool(value)


def _strings(node: Any) -> list[str]:
    """The strings of the array, none where it is absent."""
    return [_as_text(it) for it in node] if isinstance(node, list) else []


def _contains(node: Any, value: str) -> bool:
    """Whether the array holds the string."""
    return isinstance(node, list) and any(isinstance(it, str) and it == value for it in node)


def _equal(a: str | None, b: str | None) -> bool:
    """Whether both are there and the same: SQL's `=`, which a null never meets."""
    return a is not None and b is not None and a == b


def _two_tokens(stamp: str | None) -> str | None:
    """The stamp where it is two tokens, as every save leaves one, and None where it is not."""
    return stamp if stamp is not None and len(stamp) == 33 and stamp[16] == ":" else None


def _two_tokens_of(stamp: str) -> str:
    """The SQL that reads the column where it is two tokens, and null where it is not."""
    return f"CASE WHEN length({stamp}) = 33 AND substr({stamp}, 17, 1) = ':' THEN {stamp} END"
