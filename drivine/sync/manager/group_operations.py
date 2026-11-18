# Written by scripts/unasync.py from drivine/manager/group_operations.py:
# change that, and run the script again.
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import TypeAdapter

from drivine.backend.graph_backend import GraphBackend
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.group_statement import path_key_id
from drivine.drivine_error import DrivineError
from drivine.dsl.model_scope import scope_of
from drivine.dsl.query_renderer import root_property
from drivine.dsl.ref import Ref
from drivine.manager.group_aggregate import GroupAggregate
from drivine.manager.group_query import Comparison, GroupQuery
from drivine.manager.groups import count, key
from drivine.manager.path_key import PathKey
from drivine.manager.property_key import PropertyKey
from drivine.mapper.graph_object_mapper import GraphObjectMapper
from drivine.model.fragment_model import FragmentModel
from drivine.model.model_registry import ModelRegistry


class GroupOperations:
    """
    Grouped reads: the nodes of a fragment grouped by keys, with numbers about each group, in one statement, so nothing
    is loaded to be tallied in the application. A group exists because a node is in it; ties in an order are broken by
    the keys.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        query: Callable[[GraphStatement], list[Any]],
        mapper: GraphObjectMapper,
        backend: GraphBackend,
        lookups: Callable[[FragmentModel, Any], Any] | None = None,
    ) -> None:
        """
        `lookups` gives a condition with its case-insensitive comparisons of the source's `CaseInsensitive` properties
        made comparisons with their lowercased copies, where every node is known to have them: see lower_copy_lookups.
        """
        self._registry = registry
        self._query = query
        self._mapper = mapper
        self._backend = backend
        self._lookups = lookups

    def load(
        self,
        source: type,
        *,
        keys: Mapping[str, PropertyKey | PathKey],
        aggregates: Mapping[str, GroupAggregate] | None = None,
        where: str | Any | None = None,
        bindings: Mapping[str, Any] | None = None,
        having: Sequence[tuple[str, Comparison, float]] = (),
        order_by: Sequence[tuple[str, Literal["asc", "desc"]]] = (),
        limit: int | None = None,
        skip: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        The groups of the source's nodes, each a dict of its keys and aggregates by the names given:

            q = refs(Review)
            gom.groups.load(
                Review,
                keys={"page": key(q.page)},
                aggregates={"votes": count(q.helpful.eq(True)), "average": avg(q.rating)},
                having=[("votes", "gte", 3)],
                order_by=[("votes", "desc")],
                limit=10,
            )

        A path key's value is the node it reaches, mapped to its class. A least or greatest value (`min_`, `max_`) is
        read as the type its property's field declares, as drivine4j's GroupRow reads one: a datetime as a datetime.
        """
        model = self._registry.fragment(source)
        if where is not None and not isinstance(where, str) and self._lookups is not None:
            where = self._lookups(model, where)
        query = GroupQuery(
            keys,
            {name: self._written_filter(it, bindings) for name, it in (aggregates or {}).items()},
            self._written(where, bindings),
            bindings or {},
            having,
            order_by,
            limit,
            skip,
        )
        targets: dict[str, FragmentModel] = {}
        # Checked before anything is written, so every store refuses the same keys: a path key's groups are ordered by
        # its node's id, so its fragment must have a NodeId (path_key_id; drivine4j's GroupQuery.check).
        for name, group_key in keys.items():
            if isinstance(group_key, PathKey):
                if not group_key.hops:
                    raise DrivineError(f"The path key {name} needs at least one hop.")
                target = self._registry.fragment(group_key.target)
                path_key_id(target)
                targets[name] = target
        rows = self._query(self._backend.groups(model, query, targets, self._labels))
        key_names = list(keys)
        aggregate_names = list(aggregates or {})
        readers = [_declared(model, it) for it in (aggregates or {}).values()]
        results: list[dict[str, Any]] = []
        for row in rows:
            result: dict[str, Any] = {}
            for i, name in enumerate(key_names):
                target = targets.get(name)
                result[name] = self._mapper.fragment(target, row[f"k{i}"]) if target else row.get(f"k{i}")
            for i, name in enumerate(aggregate_names):
                result[name] = readers[i](row.get(f"a{i}"))
            results.append(result)
        return results

    def _labels(self, type_: type) -> list[str]:
        return self._registry.fragment(type_).labels

    def count_by(
        self,
        source: type,
        property: str | Any,
        where: str | Any | None = None,
        bindings: Mapping[str, Any] | None = None,
    ) -> dict[Any, int]:
        """For each value of a property, how many nodes hold it. Nodes without it are counted under None."""
        rows = self.load(
            source, keys={"value": key(property)}, aggregates={"count": count()}, where=where, bindings=bindings
        )
        return {it["value"]: it["count"] or 0 for it in rows}

    def _written(self, where: str | Any | None, bindings: Mapping[str, Any] | None) -> str | Any | None:
        """
        A predicate the caller wrote, as the store reads it given what the read binds (see the backend's `written`);
        conditions as they are.
        """
        return self._backend.written(where, bindings) if isinstance(where, str) and where else where

    def _written_filter(self, aggregate: GroupAggregate, bindings: Mapping[str, Any] | None) -> GroupAggregate:
        """The aggregate with a filter the caller wrote read as the store reads it."""
        return dataclasses.replace(aggregate, filter=self._written(aggregate.filter, bindings))


def _declared(model: FragmentModel, aggregate: GroupAggregate) -> Callable[[Any], Any]:
    """
    How a group's value of `aggregate` is read: a least or greatest value as the type the source's field of its property
    declares, which a store may give as the text it holds (SQLite gives a date's ISO text), where a field declares it
    and holds it as the value it is; any other value, and one no field declares, as the store gives it.
    """
    if aggregate.function not in ("MIN", "MAX") or aggregate.property is None:
        return _as_given
    property_ = aggregate.property
    if isinstance(property_, Ref):
        property_ = root_property(property_.path, scope_of(model), "An aggregate")
    field = model.fields_by_property.get(str(property_))
    info = getattr(model.type, "model_fields", {}).get(field) if field is not None else None
    if info is None or field in model.json_packed or field in model.bytes_fields:
        return _as_given
    adapter: TypeAdapter[Any] = TypeAdapter(info.annotation)
    return lambda value: None if value is None else adapter.validate_python(value)


def _as_given(value: Any) -> Any:
    return value
