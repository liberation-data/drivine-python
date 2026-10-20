from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import cypher_grammar_for
from drivine.backend.save_statement import SaveStatement
from drivine.backend.save_statement_builder import items, root_of, target
from drivine.drivine_error import DrivineError
from drivine.manager.cascade_type import CascadeType
from drivine.manager.contention import when_not_contended
from drivine.manager.null_policy import NullPolicy
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

T = TypeVar("T")

UPDATE_ATTEMPTS = 3
"""How many times `update` loads, changes and saves before a node that keeps changing raises StaleObjectError."""


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

    # ----- Writing -----

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
