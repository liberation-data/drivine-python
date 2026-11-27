"""
Warns, once per fragment and database, of a fragment whose NodeId no index serves under any of its labels: each save
and each load by id then reads every node of the label. drivine4j's NodeIdIndexCheck (drivine4j#828). With the range
index a fragment read into a SchemaCatalog is given by default (`SchemaCatalog.from_fragments`), this is the case where
that default is off or never applied.

Nothing here reads the store while a fragment is used, nor raises: a check that fails is passed over, at DEBUG, and the
save or load goes on. What serves an id is what SchemaManager's `enforce` (or `recreate_all`) listed of each database
(`listed`), after it applied the catalogs, outside any caller's transaction, and kept current by each ensure after it
(`ensured`). A fragment whose model was built before an enforce is checked when it runs, and one built after it against
what it listed, each only on the databases where it belongs: those it is `used` on (drivine4j#858), and those whose
catalogs name its labels (`catalogued`, drivine4j#848). Where no SchemaManager with
a catalog has been built, nothing applies one; as one may yet be, as an application starts, a fragment is warned of only
if none has been UNMANAGED_DELAY seconds after its model was first built, and no index or constraint ensured by hand by
then serves its id (`ensured`, drivine4j#886).

As vector_annotation_check, classes are held weakly, so a class made and dropped (in a test, say) isn't kept alive.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.schema.ensure_result import EnsureResult
from drivine.schema.range_index_spec import RangeIndexSpec
from drivine.schema.schema_item import SchemaItem
from drivine.schema.uniqueness_constraint_spec import UniquenessConstraintSpec

logger = logging.getLogger("drivine.schema")

UNMANAGED_DELAY = 30.0
"""How many seconds after a fragment's model is first built, with no SchemaManager built, it is warned of."""


@dataclass(frozen=True)
class _Shape:
    """What the check needs of a fragment's model, holding nothing of its class."""

    name: str
    labels: tuple[str, ...]
    property: str
    """The property its NodeId is stored under, which a registry's naming may choose."""


_lock = threading.Lock()

_admitted: weakref.WeakKeyDictionary[type, set[_Shape]] = weakref.WeakKeyDictionary()
"""The fragments with a NodeId admitted so far, each by the shapes of it built, for `listed` to check."""

_served: dict[str, set[tuple[str, str]]] = {}
"""
What serves an id on each database an enforce listed, by its registered name: its range indexes and uniqueness
constraints over one property, as (label, property).
"""

_ensured_unlisted: dict[str, set[tuple[str, str]]] = {}
"""
What serves an id that was ensured on a database no enforce has listed, as (label, property): with no SchemaManager,
the index the warning names, ensured by hand, serves the fragment, which `unmanaged` then doesn't warn of (#886).
"""

_warned: dict[str | None, weakref.WeakKeyDictionary[type, None]] = {}
"""The fragments warned of, by database (None for none: no SchemaManager), each once."""

_catalogued: dict[str, set[str]] = {}
"""
The labels the catalogs a SchemaManager applies declare items of, or were read from, by database, given before its
enforce lists any: a fragment whose labels some catalog names is checked only on the databases where one does, as it
belongs there, and isn't warned of on another it is never used on (drivine4j#848). A fragment no catalog names is
checked on every database listed.
"""

_used_on: dict[str, weakref.WeakKeyDictionary[type, set[_Shape]]] = {}
"""
The fragments `used` on each database, by its registered name, each by the shapes of it used there: they belong there
whatever the catalogs name (drivine4j#858).
"""

_uses: dict[int, tuple[weakref.ref[Any], int, set[str]]] = {}
"""
The databases each model was `used` on under a generation, by the model's identity (a model isn't hashable), held
weakly: the check each use makes, read without the lock, so a use after the first on a database costs a lookup.
"""

_managed = False
"""Whether a SchemaManager with a catalog has been built, which applies catalogs and lists what serves an id."""

_generation = 0
"""What `reset` last set: a warning due from before it is not given."""

_pending: list[tuple[float, weakref.ref[type], int]] = []
"""The fragments to warn of where no SchemaManager has been built by then: when, which, under what generation."""

_timer: threading.Timer | None = None
"""The one timer the pending warnings are given on, a daemon, set while any is pending."""


def managed() -> None:
    """A SchemaManager with a catalog was built: fragments are checked against what its enforce lists."""
    global _managed
    with _lock:
        _managed = True
        _cancel_pending()


def admit(model: FragmentModel) -> None:
    """Checks a newly built model: against each database an enforce listed, or, with no SchemaManager, later."""

    def check() -> None:
        shape = _shape_of(model)
        if shape is None:
            return
        with _lock:
            shapes = _admitted.get(model.type)
            if shapes is not None and shape in shapes:
                return
            _admitted.setdefault(model.type, set()).add(shape)
            for database, serving in _served.items():
                _check(database, model.type, shape, serving)
            if not _managed and shapes is None:
                _unmanaged_later(model.type)

    _guarded(check)


def used(database: str, model: FragmentModel | GraphViewModel) -> None:
    """
    `model`, a fragment's or a view's, is saved, loaded, counted, searched, deleted, patched, related or grouped through
    a manager on `database`, the name it is registered under: each fragment it reads or writes belongs on `database`
    (drivine4j#858), and is checked there now if an enforce has listed it, or when one does. After the first use on a
    database, a use costs a lookup in a set, without the lock.
    """
    seen = _uses.get(id(model))
    if seen is not None and seen[0]() is model and seen[1] == _generation and database in seen[2]:
        return

    def check() -> None:
        shapes = [(it.type, shape) for it in _fragments_of(model) if (shape := _shape_of(it)) is not None]
        with _lock:
            for type_, shape in shapes:
                _used_on.setdefault(database, weakref.WeakKeyDictionary()).setdefault(type_, set()).add(shape)
                serving = _served.get(database)
                if serving is not None:
                    _check(database, type_, shape, serving)

    def remember() -> None:
        key = id(model)
        with _lock:
            seen = _uses.get(key)
            if seen is None or seen[0]() is not model or seen[1] != _generation:
                # Dropped with the model, before its identity can be another's.
                seen = _uses[key] = (weakref.ref(model, lambda _: _forget(key)), _generation, set())
            seen[2].add(database)

    _guarded(check)
    # Once, even where the check failed: a use never pays for it again.
    _guarded(remember)


def _forget(key: int) -> None:
    """
    A model `used` is gone: its entry goes with it. Without the lock, which the collector may run this under: a pop is
    atomic, and an entry replaced since dropped the weak reference that would call this.
    """
    _uses.pop(key, None)


def listed(
    database: str,
    ranges: Sequence[SchemaItem],
    uniques: Sequence[SchemaItem],
    ranges_per_property: bool = False,
) -> None:
    """
    What an enforce found on `database`, by its registered name: its range indexes and uniqueness constraints. Each
    fragment admitted so far is checked against it. An item serves a property where it is over that property alone, or,
    for a range index on an engine that indexes a label's properties one by one (`ranges_per_property`, FalkorDB),
    where it is among them: the store's own reading of an item's identity, as drivine4j's `matchesIdentity` gives it.
    """

    def check() -> None:
        serving = {
            (item.label, property)
            for item in ranges
            if len(item.properties) == 1 or ranges_per_property
            for property in item.properties
        } | {(item.label, item.properties[0]) for item in uniques if len(item.properties) == 1}
        with _lock:
            _served[database] = serving
            for type_, shapes in list(_admitted.items()):
                for shape in list(shapes):
                    _check(database, type_, shape, serving)

    _guarded(check)


def catalogued(labels: Mapping[str, Iterable[str]]) -> None:
    """What the catalogs on each database name, as `_catalogued` keeps it: given by SchemaManager before it lists."""

    def check() -> None:
        global _catalogued
        with _lock:
            _catalogued = {database: set(named) for database, named in labels.items()}

    _guarded(check)


def catalogued_too(database: str, labels: Iterable[str]) -> None:
    """`labels` are named on `database` too, by a catalog SchemaManager's `ensure` gave after the enforce."""

    def check() -> None:
        with _lock:
            _catalogued.setdefault(database, set()).update(labels)

    _guarded(check)


def listed_on(database: str) -> bool:
    """Whether an enforce has listed `database`, whose listing a runtime ensure is to keep current."""
    with _lock:
        return database in _served


def ensured(database: str | None, spec: object, result: EnsureResult[Any]) -> None:
    """
    `spec` was ensured or recreated on `database`, by its registered name, with `result`, by an index or constraint
    manager: an id it serves is served from then on, so a fragment first used after it isn't warned of, on a database
    an enforce listed or, with no SchemaManager, by `unmanaged` (#886). Only a range index or a uniqueness constraint
    over one property, not ignoring case, made or there, serves one. Nothing is read of the store. A manager of no
    database (None) serves none.
    """

    def check() -> None:
        if result.status not in ("CREATED", "ALREADY_MATCHING", "RECREATED") or database is None:
            return
        if not isinstance(spec, (RangeIndexSpec, UniquenessConstraintSpec)) or spec.ignore_case:
            return
        if len(spec.properties) != 1:
            return
        item = (spec.label, spec.properties[0])
        with _lock:
            listing = _served.get(database)
            if listing is not None:
                listing.add(item)
            else:
                _ensured_unlisted.setdefault(database, set()).add(item)

    _guarded(check)


def unmanaged(type_: type, generation: int | None = None) -> None:
    """
    Warns of `type_`, admitted under `generation` (the current one by default), where no SchemaManager has been built
    since, unless an index or a constraint ensured by hand on some database serves its id: what the warning would
    advise. Given UNMANAGED_DELAY seconds after the class was admitted, on a daemon timer; called directly by tests.
    """

    def check() -> None:
        with _lock:
            if _managed or (generation is not None and generation != _generation):
                return
            for shape in list(_admitted.get(type_, ())):
                by_hand = any(
                    (label, shape.property) in items for items in _ensured_unlisted.values() for label in shape.labels
                )
                if not by_hand:
                    _warn(None, type_, shape)

    _guarded(check)


def reset() -> None:
    """Forgets everything, for tests: each class is admitted again when a registry next builds its model."""
    global _managed, _generation, _catalogued
    with _lock:
        _generation += 1
        _admitted.clear()
        _served.clear()
        _ensured_unlisted.clear()
        _used_on.clear()
        _uses.clear()
        _warned.clear()
        _catalogued = {}
        _managed = False
        _cancel_pending()


def _shape_of(model: FragmentModel) -> _Shape | None:
    """What the check needs of `model`: None for a fragment with no NodeId, or no label, which is never warned of."""
    if model.node_id_property is None or not model.labels:
        return None
    return _Shape(model.type.__name__, tuple(model.labels), model.node_id_property)


def _fragments_of(model: FragmentModel | GraphViewModel) -> list[FragmentModel]:
    """
    The fragments `model` reads and writes: itself if a fragment's; a view's root and what its relationships lead to,
    the targets of relationship fragments and the views nested in it too.
    """
    found: dict[int, FragmentModel] = {}
    views: set[int] = set()

    def visit(it: object) -> None:
        if isinstance(it, FragmentModel):
            found.setdefault(id(it), it)
        elif isinstance(it, RelationshipFragmentModel):
            visit(it.target)
        elif isinstance(it, GraphViewModel) and id(it) not in views:
            views.add(id(it))
            visit(it.root)
            for relationship in it.relationships:
                visit(relationship.related)

    visit(model)
    return list(found.values())


def _belongs(database: str, type_: type, shape: _Shape) -> bool:
    """
    Whether `shape` is checked on `database`: it was `used` there, or no catalog names its labels, or one on `database`
    does. The union keeps each warning the catalogs give, and adds the databases a fragment is used on, where its saves
    scan the label all the same.
    """
    used = _used_on.get(database)
    if used is not None and shape in used.get(type_, ()):
        return True
    if not any(label in named for label in shape.labels for named in _catalogued.values()):
        return True
    return any(label in _catalogued.get(database, ()) for label in shape.labels)


def _check(database: str, type_: type, shape: _Shape, serving: set[tuple[str, str]]) -> None:
    """
    Warns of the shape where no index `serving` is over its NodeId under any of its labels: a save and a load by id
    match every label of the fragment, so an index under any of them answers it. Under the lock.
    """
    if not _belongs(database, type_, shape):
        return
    if not any((label, shape.property) in serving for label in shape.labels):
        _warn(database, type_, shape)


def _warn(database: str | None, type_: type, shape: _Shape) -> None:
    """The warning, once per class and database, in drivine4j's words with Python's spellings. Under the lock."""
    warned = _warned.setdefault(database, weakref.WeakKeyDictionary())
    if type_ in warned:
        return
    warned[type_] = None
    label, property, fragment = shape.labels[0], shape.property, shape.name
    # Where there is more than one database, the one it is said of (drivine4j#848).
    on = f" On database '{database}'." if database is not None and (len(_catalogued) > 1 or len(_served) > 1) else ""
    logger.warning(
        "@NodeId '%s' of %s has no index under :%s, so each save scans every :%s node. Its default index is off "
        "(class %s(NodeFragment, node_id_index=False) / SchemaOptions(index_node_ids=False), or %s isn't in a "
        'SchemaCatalog): declare RangeIndex() or Unique() on it, or indexes.ensure(range_index("%s", "%s")).%s',
        property,
        fragment,
        label,
        label,
        fragment,
        fragment,
        label,
        property,
        on,
    )


def _unmanaged_later(type_: type) -> None:
    """Warns of `type_` after UNMANAGED_DELAY seconds if no SchemaManager has been built by then. Under the lock."""
    global _timer
    _pending.append((time.monotonic() + UNMANAGED_DELAY, weakref.ref(type_), _generation))
    if _timer is None:
        _schedule(UNMANAGED_DELAY)


def _schedule(delay: float) -> None:
    global _timer
    _timer = threading.Timer(max(0.0, delay), _due)
    # Never what keeps a process alive.
    _timer.daemon = True
    _timer.start()


def _due() -> None:
    """Gives the warnings now due, outside the lock, and sets the timer for the next."""
    global _timer
    due: list[tuple[float, weakref.ref[type], int]] = []
    with _lock:
        if _timer is None or threading.current_thread() is not _timer:
            return
        now = time.monotonic()
        due = [it for it in _pending if it[0] <= now]
        _pending[:] = [it for it in _pending if it[0] > now]
        _timer = None
        if _pending:
            _schedule(_pending[0][0] - now)
    for _, held, generation in due:
        type_ = held()
        if type_ is not None:
            unmanaged(type_, generation)


def _cancel_pending() -> None:
    """Drops the warnings pending, and their timer. Under the lock."""
    global _timer
    _pending.clear()
    if _timer is not None:
        _timer.cancel()
        _timer = None


def _guarded(check: Callable[[], None]) -> None:
    """Runs `check`, passing over anything it raises: a warning must never fail the save or load it is made in."""
    try:
        check()
    except Exception as error:
        logger.debug("The check for an unindexed @NodeId could not be made: %s", error)
