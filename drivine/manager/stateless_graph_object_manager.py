from __future__ import annotations

import dataclasses
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import cypher_grammar_for
from drivine.backend.fragment_group import FragmentGroup
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.save_statement import SaveStatement
from drivine.backend.save_statement_builder import id_of, items, props_of, root_of, target
from drivine.backend.vector_writes import split_vectors
from drivine.drivine_error import DrivineError
from drivine.manager.cascade_type import CascadeType
from drivine.manager.contention import when_not_contended
from drivine.manager.edge_operations import EdgeOperations
from drivine.manager.null_policy import NullPolicy
from drivine.manager.patch_result import PatchResult
from drivine.manager.persistence_manager import PersistenceManager
from drivine.manager.removed_targets import RemovedTargets
from drivine.manager.save_options import SaveOptions
from drivine.manager.stale_object_error import StaleObjectError
from drivine.mapper.graph_object_mapper import GraphObjectMapper
from drivine.model import stamps
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import ModelRegistry
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.target_model import TargetModel
from drivine.transaction.transaction import current_transaction
from drivine.transaction.transactional import transaction

T = TypeVar("T")
R = TypeVar("R")

UPDATE_ATTEMPTS = 3
"""How many times `update` loads, changes and saves before a node that keeps changing raises StaleObjectError."""

PATCH_ROWS = 1000
"""How many nodes a patch_all writes in one statement."""

BATCH_ROWS = 1000
"""How many fragments of a save_all go to one statement."""


class StatelessGraphObjectManager:
    """
    Loads and saves node fragments and views, one statement each. It remembers nothing between calls: what it does
    depends only on the object and the arguments, so nothing another writer does can make it write stale data.
    """

    def __init__(self, persistence_manager: PersistenceManager, registry: ModelRegistry | None = None) -> None:
        self.persistence_manager = persistence_manager
        self.registry = registry or ModelRegistry()
        self.backend = CypherBackend(cypher_grammar_for(persistence_manager.type))
        self.mapper = GraphObjectMapper(self.registry)
        self.edges = EdgeOperations(self.registry, self._run_one, self._query, self.mapper, self.backend)
        """Relationships on their own, between stored nodes: see EdgeOperations."""

    @property
    def database(self) -> str:
        """Which database this manager works on."""
        return self.persistence_manager.database

    async def load_all(
        self,
        type_: type[T],
        where: str | None = None,
        bindings: Mapping[str, Any] | None = None,
        *,
        depth: Mapping[str, int] | None = None,
    ) -> list[T]:
        """
        Loads every object of the class, optionally filtered: `where` is a Cypher predicate over the root, `n`, with
        its values in `bindings`. `depth` overrides a recursive relationship's max_depth for this load, by field name.
        """
        target = self.registry.model_of(type_)
        depth = _checked(depth)
        if isinstance(target, GraphViewModel):
            statement = self.backend.load_all_views(target, where, depth)
            rows = await self.persistence_manager.query(statement.spec(bindings))
            return [self.mapper.view(target, row) for row in rows]
        statement = self.backend.load_all_fragments(target, where)
        rows = await self.persistence_manager.query(statement.spec(bindings))
        return [self.mapper.fragment(target, row) for row in rows]

    async def load(self, type_: type[T], id: Any, *, depth: Mapping[str, int] | None = None) -> T | None:
        """The object whose root has the id, or None."""
        target = self.registry.model_of(type_)
        depth = _checked(depth)
        if isinstance(target, GraphViewModel):
            statement = self.backend.load_view(target, depth)
        else:
            statement = self.backend.load_fragment(target)
        rows = await self.persistence_manager.query(statement.spec({"id": id}))
        if len(rows) > 1:
            raise DrivineError(f"{len(rows)} {type_.__name__} roots have the id {id!r}. An id is one node's.")
        if not rows:
            return None
        if isinstance(target, GraphViewModel):
            return self.mapper.view(target, rows[0])
        return self.mapper.fragment(target, rows[0])

    async def count(self, type_: type[Any], where: str | None = None, bindings: Mapping[str, Any] | None = None) -> int:
        """How many objects of the class a load would find."""
        target = self.registry.model_of(type_)
        if isinstance(target, GraphViewModel):
            statement = self.backend.count_views(target, where)
        else:
            statement = self.backend.count_fragments(target, where)
        return await self.persistence_manager.get_one(statement.spec(bindings))

    async def save(
        self,
        obj: T,
        *,
        replace: Sequence[str] | Literal["all"] | None = None,
        removed_targets: RemovedTargets = RemovedTargets.KEEP,
        null_policy: NullPolicy = NullPolicy.IGNORE,
        only: Sequence[str] | None = None,
        except_: Sequence[str] | None = None,
    ) -> T:
        """
        Writes the object in one statement: its fields that aren't None, and for a view, its relationships, each related
        node written whole. A save adds relationships and removes none, except from the fields named in `replace`,
        whose value is then the whole of the relationship. Returns the object, which carries the stamps the save left:
        use it from then on.

        An object whose root carries a stamp is checked: if the node changed since the object was loaded, nothing is
        written and StaleObjectError is raised. Outside a transaction, a save the engine turns away because another
        writer held what it writes is run again, after a pause. Inside one, it fails, and the transaction's owner
        decides.
        """
        options = SaveOptions(replace, removed_targets, null_policy, only, except_)
        saved = await self._write(obj, options, False)
        assert saved is not None
        return saved

    async def create(self, obj: T) -> T | None:
        """
        Makes the object's node only if no node has its id, in one statement, as `save` would make it. When one has,
        nothing is written and None is returned: it is then for `update` to merge into what is there. An object that
        carries a stamp was loaded, and is refused.
        """
        return await self._write(obj, SaveOptions(), True)

    async def update(
        self,
        type_: type[T],
        id: Any,
        change: Callable[[T], T | None | Awaitable[T | None]],
        attempts: int = UPDATE_ATTEMPTS,
    ) -> T | None:
        """
        Loads the object, applies `change`, and writes what the change altered: the root's fields that differ, a field
        set to None cleared, and for a view each relationship list that lost an item replaced (lists that only grew are
        added to). The save is checked against the stamp that was loaded; if the node changed meanwhile, it is loaded
        again and the change applied again, `attempts` times in all, then StaleObjectError is raised. Without a
        NodeStamp field a change by another writer isn't noticed.

        `change` may change the object it is given or return a copy (`model_copy`), but not give it another id or
        class, and may be async. Returns the saved object, or None when there is no such node or it is deleted
        meanwhile.
        """
        target_ = self.registry.model_of(type_)
        attempt = 0
        while True:
            attempt += 1
            loaded = await self.load(type_, id)
            if loaded is None:
                return None
            before = _copy(loaded)
            result = change(loaded)
            if inspect.isawaitable(result):
                result = await result
            changed: T = loaded if result is None else result  # pyright: ignore[reportAssignmentType]
            options = self._change_of(target_, before, changed)
            try:
                return await self._write(changed, options, False)
            except StaleObjectError as failure:
                if failure.id != id or attempt >= attempts:
                    raise
                if failure.deleted:
                    return None

    async def delete(
        self,
        type_: type[Any],
        id: Any,
        where: str | None = None,
        bindings: Mapping[str, Any] | None = None,
        *,
        cascade: CascadeType = CascadeType.NONE,
    ) -> int:
        """
        Deletes the object whose root has the id, with the root's relationships. A view's root is deleted only if it
        meets the view's requirements, and `where`, a predicate over `n`, when given. A cascade deletes nodes the view
        reaches too. Returns the number of roots deleted: 1, or 0 when there was none.
        """
        statement = self.backend.delete(self.registry.model_of(type_), where, cascade)
        return await self.persistence_manager.get_one(statement.spec({**(bindings or {}), "id": id}))

    async def delete_all(
        self, type_: type[Any], where: str | None = None, bindings: Mapping[str, Any] | None = None
    ) -> int:
        """
        Deletes every object a load with the same filter would find: its root, with the root's relationships. Without
        a filter, every one. Returns the number deleted.
        """
        statement = self.backend.delete_all(self.registry.model_of(type_), where)
        return await self.persistence_manager.get_one(statement.spec(bindings))

    async def save_all(
        self,
        objects: Sequence[T],
        *,
        replace: Sequence[str] | Literal["all"] | None = None,
        removed_targets: RemovedTargets = RemovedTargets.KEEP,
        null_policy: NullPolicy = NullPolicy.IGNORE,
        only: Sequence[str] | None = None,
        except_: Sequence[str] | None = None,
    ) -> list[T]:
        """
        Saves many objects, fragments and views of any classes, together. Fragments go in one statement for each 1,000,
        each merged on its id; a view is a statement of its own, as `save` writes it. Outside a transaction of the
        caller's, the statements run in one transaction, so a failure leaves nothing written; inside one, they join it.

        A fragment's stamp isn't checked: a batch only adds, and a fragment with a stale stamp is written all the same,
        keeping the stamp it had. A view that carries a stamp is checked as `save` checks it, and a stale one raises
        StaleObjectError, the batch then writing nothing. `replace` applies to views: a batch holding a fragment is
        refused with it. Returns the objects, in order.
        """
        options = SaveOptions(replace, removed_targets, null_policy, only, except_)
        fragments: dict[type, FragmentGroup] = {}
        views: list[T] = []
        for obj in objects:
            target_ = self.registry.model_of(type(obj))
            # A view, or a fragment with a property bag or node labels, whose entries no row of a batch can carry, is a
            # statement of its own.
            if isinstance(target_, GraphViewModel) or target_.property_bags or target_.node_labels:
                views.append(obj)
                continue
            if replace:
                raise DrivineError(f"replace applies to views. The batch holds a {target_.type.__name__}.")
            fragments.setdefault(target_.type, FragmentGroup(target_)).objects.append(obj)
        entries: list[tuple[GraphStatement, list[FragmentGroup] | None, Any]] = []
        for chunk in _chunked(list(fragments.values()), BATCH_ROWS):
            entries.append((self.backend.fragment_batch(chunk, options), chunk, None))
        for view in views:
            statement = self.backend.save(self.registry.model_of(type(view)), view, options, False, self.registry)
            entries.append((statement, None, view))
        results = await self._run_batch([it[0] for it in entries])
        for (statement, groups, view), rows in zip(entries, results, strict=True):
            if groups is not None:
                self._hand_back_fragments(groups, rows[0])
            else:
                assert isinstance(statement, SaveStatement)
                self._hand_back(view, statement, rows[0])
        return list(objects)

    async def patch(
        self,
        type_: type[Any],
        id: Any,
        *,
        set: Mapping[str, Any] | None = None,
        clear: Sequence[str] = (),
        where: str | None = None,
        bindings: Mapping[str, Any] | None = None,
    ) -> PatchResult:
        """
        Writes named fields of the node with the id, if it meets `where`, a predicate over `n`, in one statement,
        without loading it. `set` maps fields to values, and `clear` names fields to remove. It never makes a node,
        isn't checked against a stamp, and hands none back: a node it changed has a new stamp, so an object loaded
        before is refused when saved. On a view, the fields are its root's.
        """
        root = root_of(self.registry.model_of(type_))
        named = [*(set or {}), *clear]
        self._assert_patchable(root, named)
        values: dict[str, Any] = {}
        for field in named:
            value = (set or {}).get(field)
            packed = value is not None and field in root.json_packed
            values[root.graph_properties.get(field, field)] = json.dumps(to_jsonable_python(value)) if packed else value
        props, vectors = split_vectors(root, values)
        statement = self.backend.patch_one(root, where)
        spec = statement.spec({**(bindings or {}), "id": id, "set": props, "vectors": vectors})
        row = await self._when_uncontended(lambda: self.persistence_manager.get_one(spec))
        return PatchResult(row["found"], row["applied"], row["changed"])

    async def patch_all(
        self,
        objects: Sequence[Any],
        *,
        only: Sequence[str] | None = None,
        except_: Sequence[str] | None = None,
        null_policy: NullPolicy = NullPolicy.IGNORE,
    ) -> list[PatchResult]:
        """
        Writes named fields of many nodes, each where its node is there, making none. One result for each object, in
        order. `only` or `except_` is required: to write every field, save. Under NullPolicy.IGNORE, the default, a
        named field that is None is left as the store has it. For a view the fields are its root's; its relationships
        aren't written.
        """
        if not objects:
            return []
        if (only is None) == (except_ is None):
            raise DrivineError("patch_all needs only or except_, not both. To write every field of an object, save it.")
        kind: Any = objects[0].__class__
        if any(type(it) is not kind for it in objects):
            raise DrivineError(f"patch_all takes objects of one class; the first is a {kind.__name__}.")
        target_ = self.registry.model_of(kind)
        root = root_of(target_)
        self._assert_patchable(root, list(only or []))
        written = [
            field
            for field in root.graph_properties
            if (field in only if only is not None else field not in (except_ or ()))
            and field not in (root.node_id_field, root.stamp_field)
        ]
        excluded = [it for it in root.graph_properties if it not in written]
        rows: list[dict[str, Any]] = []
        for obj in objects:
            fragment = getattr(obj, target_.root_field) if isinstance(target_, GraphViewModel) else obj
            props = props_of(root, fragment, excluded, null_policy is NullPolicy.CLEAR, only or ())
            props, vectors = split_vectors(root, props)
            rows.append({"id": id_of(root, fragment), "props": props, "vectors": vectors})
        ids = [it["id"] for it in rows]
        if len(set_of(ids)) != len(ids):
            raise DrivineError("patch_all takes each id once.")
        # In order of id, so that two calls over the same nodes take their locks in the same order.
        ordered = sorted(rows, key=lambda it: str(it["id"]))
        results: dict[Any, PatchResult] = {}
        for start in range(0, len(ordered), PATCH_ROWS):
            spec = self.backend.patch_many(root).spec({"rows": ordered[start : start + PATCH_ROWS]})
            for it in await self._when_uncontended(lambda spec=spec: self.persistence_manager.query(spec)):
                results[it["id"]] = PatchResult(it["found"], it["found"], it["changed"])
        return [results[it] for it in ids]

    # ----- Writing -----

    async def _run_batch(self, statements: list[GraphStatement]) -> list[list[Any]]:
        """
        Runs statements as one batch: joining the caller's transaction if there is one, otherwise in a transaction of
        its own, rolled back on failure and run again when the engine turns it away for contention. A checked save that
        finds its root stale (it returns no row) fails the batch.
        """
        if not statements:
            return []

        async def each() -> list[list[Any]]:
            results: list[list[Any]] = []
            for statement in statements:
                rows = await self.persistence_manager.query(statement.spec())
                if isinstance(statement, SaveStatement) and statement.expected_stamp is not None and not rows:
                    raise _StaleInBatch(statement)
                results.append(rows)
            return results

        async def in_transaction() -> list[list[Any]]:
            async with transaction():
                return await each()

        try:
            if current_transaction() is not None:
                return await each()
            if len(statements) == 1:
                return await when_not_contended(each, self.backend.contended)
            return await when_not_contended(in_transaction, self.backend.contended)
        except _StaleInBatch as stale:
            raise await self._stale_object(stale.statement) from None

    def _hand_back_fragments(self, groups: list[FragmentGroup], result: Mapping[str, Any]) -> None:
        """Gives each fragment of a batch the stamp the batch left, as far as it was current."""
        for i, group in enumerate(groups):
            model = group.model
            if not model.stamp_field or not model.node_id_field:
                continue
            found = {it["id"]: it["stamp"] for it in result["groups"][i]["before"]}
            now = {it["id"]: it["stamp"] for it in result["groups"][i]["after"]}
            for obj in group.objects:
                node_id = getattr(obj, model.node_id_field)
                carried = getattr(obj, model.stamp_field)
                setattr(obj, model.stamp_field, stamps.handed_back(carried, found.get(node_id, ""), now[node_id]))

    @staticmethod
    def _assert_patchable(root: FragmentModel, fields: list[str]) -> None:
        refused = [
            it
            for it in fields
            if it in (root.node_id_field, root.stamp_field)
            or it in root.transient_fields
            or any(bag.field == it for bag in root.property_bags)
            or (root.node_labels is not None and root.node_labels.field == it)
        ]
        if refused:
            raise DrivineError(f"A patch can't write {', '.join(refused)} of {root.type.__name__}.")
        unknown = [it for it in fields if it not in root.graph_properties]
        if unknown:
            raise DrivineError(f"{root.type.__name__} has no field {', '.join(unknown)} to patch.")
        if len(set_of(fields)) != len(fields):
            raise DrivineError("A patch names each field once.")

    async def _when_uncontended(self, statement: Callable[[], Awaitable[R]]) -> R:
        """Runs a write again when the engine turns it away for contention, unless inside a caller's transaction."""
        if current_transaction() is not None:
            return await statement()
        return await when_not_contended(statement, self.backend.contended)

    async def _run_one(self, statement: GraphStatement) -> Any:
        return await self._when_uncontended(lambda: self.persistence_manager.get_one(statement.spec()))

    async def _query(self, statement: GraphStatement) -> list[Any]:
        return await self.persistence_manager.query(statement.spec())

    async def _write(self, obj: T, given: SaveOptions, create_only: bool) -> T | None:
        target_ = self.registry.model_of(type(obj))
        options = await self._with_held_keys(target_, obj, given)
        statement = self.backend.save(target_, obj, options, create_only, self.registry)

        async def run() -> list[Any]:
            return await self.persistence_manager.query(statement.spec())

        try:
            rows = (
                await run()
                if current_transaction() is not None
                else await when_not_contended(run, self.backend.contended)
            )
        except Exception as failure:
            # Turned away every time while another writer held the node: for a checked save, the answer is the same as
            # for a stale one. Nothing was written; load again and re-apply.
            if statement.expected_stamp is not None and self.backend.contended(failure):
                assert statement.root is not None
                raise StaleObjectError(
                    statement.root.type, statement.root_id, statement.expected_stamp, None, False
                ) from failure
            raise
        if not rows:
            if create_only:
                return None
            raise await self._stale_object(statement)
        self._hand_back(obj, statement, rows[0])
        return obj

    async def _with_held_keys(self, target_: TargetModel, obj: Any, options: SaveOptions) -> SaveOptions:
        """
        Under NullPolicy.CLEAR, a root with property bags or an open NodeLabels field is read first for the keys it
        holds, so that a save removes the entries a bag no longer has: one more statement, which writes nothing.
        """
        if options.null_policy is not NullPolicy.CLEAR:
            return options
        root = root_of(target_)
        root_object = getattr(obj, target_.root_field, None) if isinstance(target_, GraphViewModel) else obj
        reads = root.property_bags or (root.node_labels is not None and root.node_labels.members is None)
        root_id = getattr(root_object, root.node_id_field, None) if root.node_id_field else None
        if not reads or root_id is None:
            return options
        held = await self.persistence_manager.query(self.backend.held_properties(root).spec({"id": root_id}))
        return dataclasses.replace(options, held_properties=held[0] if held else {})

    def _change_of(self, target_: TargetModel, before: Any, changed: Any) -> SaveOptions:
        """
        What a change altered, as save options. The stamp the object was loaded with is put back, so the save is checked
        against it whatever the change did with the object's.
        """
        if type(changed) is not type(before):
            raise DrivineError(
                f"An update of a {type(before).__name__} must leave it one, not a {type(changed).__name__}."
            )
        root = root_of(target_)
        if isinstance(target_, GraphViewModel):
            root_before, root_after = getattr(before, target_.root_field), getattr(changed, target_.root_field)
        else:
            root_before, root_after = before, changed
        assert root.node_id_field is not None
        if getattr(root_after, root.node_id_field, None) != getattr(root_before, root.node_id_field):
            raise DrivineError(
                f"An update of {root.type.__name__} {getattr(root_before, root.node_id_field)!r} can't give it "
                f"another id."
            )
        if root.stamp_field:
            setattr(root_after, root.stamp_field, getattr(root_before, root.stamp_field))
        ignored = {root.node_id_field, root.stamp_field, *root.transient_fields}
        fields = [
            *root.graph_properties,
            *(it.field for it in root.property_bags),
            *([root.node_labels.field] if root.node_labels else []),
        ]
        only = [it for it in fields if it not in ignored and getattr(root_before, it) != getattr(root_after, it)]
        replace: list[str] = []
        if isinstance(target_, GraphViewModel):
            for rel in target_.relationships:
                after = set(_related_ids(rel, getattr(changed, rel.field, None)))
                if any(it not in after for it in _related_ids(rel, getattr(before, rel.field, None))):
                    replace.append(rel.field)
        return SaveOptions(replace=replace or None, null_policy=NullPolicy.CLEAR, only=only)

    def _hand_back(self, obj: Any, statement: SaveStatement, result: Mapping[str, Any]) -> None:
        """
        Gives the object (its root, and its stamped related nodes) the stamps a save left, as far as each was current.
        """
        for index, model in enumerate(statement.stamped_models):
            stamped = result["related"][index]
            found = {it["id"]: it["stamp"] for it in stamped["before"]}
            now = {it["id"]: it["stamp"] for it in stamped["after"]}
            assert model.node_id_field is not None and model.stamp_field is not None
            for node in _nodes_of(obj, model):
                node_id = getattr(node, model.node_id_field)
                if node_id in now:
                    carried = getattr(node, model.stamp_field)
                    setattr(node, model.stamp_field, stamps.handed_back(carried, found.get(node_id, ""), now[node_id]))
        root = statement.root
        if root is not None and root.stamp_field:
            handed = stamps.handed_back(statement.expected_stamp, result.get("found") or "", result["now"])
            setattr(statement.root_object, root.stamp_field, handed)

    async def _stale_object(self, statement: SaveStatement) -> StaleObjectError:
        assert statement.root is not None and statement.expected_stamp is not None
        found = await self.persistence_manager.query(
            self.backend.found_stamp(statement.root).spec({"id": statement.root_id})
        )
        return StaleObjectError(
            statement.root.type, statement.root_id, statement.expected_stamp, found[0] if found else None, not found
        )


def _checked(depth: Mapping[str, int] | None) -> Mapping[str, int] | None:
    for field, it in (depth or {}).items():
        if isinstance(it, bool) or not isinstance(it, int) or it < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise DrivineError(f"The depth of {field} must be a whole number of at least 1.")
    return depth


def _copy(obj: Any) -> Any:
    return obj.model_copy(deep=True) if isinstance(obj, BaseModel) else obj


def _related_ids(rel: RelationshipModel, value: Any) -> list[Any]:
    """The ids of the nodes a relationship field's value leads to."""
    related = rel.related
    into = target(rel)
    root = root_of(into)
    ids: list[Any] = []
    for item in items(rel, value):
        node = getattr(item, related.target_field, None) if isinstance(related, RelationshipFragmentModel) else item
        fragment = getattr(node, into.root_field, None) if isinstance(into, GraphViewModel) else node
        ids.append(getattr(fragment, root.node_id_field, None) if root.node_id_field else None)
    return ids


def _nodes_of(obj: Any, model: FragmentModel, seen: set[int] | None = None) -> list[Any]:
    """The instances of a fragment class the object holds, at any depth, the root included."""
    seen = set() if seen is None else seen
    if not isinstance(obj, BaseModel) or id(obj) in seen:
        return []
    seen.add(id(obj))
    if isinstance(obj, model.type):
        return [obj]
    found: list[Any] = []
    for name in type(obj).model_fields:
        value = getattr(obj, name, None)
        if isinstance(value, (list, tuple, set, frozenset)):
            for it in value:  # pyright: ignore[reportUnknownVariableType]
                found.extend(_nodes_of(it, model, seen))
        else:
            found.extend(_nodes_of(value, model, seen))
    return found


set_of = set
"""The builtin, under a name `patch`'s `set` argument doesn't hide."""


class _StaleInBatch(Exception):
    def __init__(self, statement: SaveStatement) -> None:
        super().__init__()
        self.statement = statement


def _chunked(groups: list[FragmentGroup], size: int) -> list[list[FragmentGroup]]:
    """The groups in chunks of at most `size` rows, a group split across chunks where it is longer."""
    chunks: list[list[FragmentGroup]] = []
    current: list[FragmentGroup] = []
    rows = 0
    for group in groups:
        for start in range(0, len(group.objects), size):
            objects = group.objects[start : start + size]
            if rows + len(objects) > size and current:
                chunks.append(current)
                current, rows = [], 0
            current.append(FragmentGroup(group.model, objects))
            rows += len(objects)
    if current:
        chunks.append(current)
    return chunks
