from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from drivine.annotation.direction import Direction
from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.graph_statement import GraphStatement
from drivine.backend.matched_node import MatchedNode
from drivine.manager.node_ref import NodeRef
from drivine.manager.relate_mode import RelateMode
from drivine.mapper.graph_object_mapper import GraphObjectMapper
from drivine.model.model_registry import ModelRegistry

T = TypeVar("T")


class EdgeOperations:
    """
    Relationships on their own, between stored nodes matched by id: neither node is loaded or created, and neither's
    own properties are touched. Both ends get a new links token in their stamp when a relationship is made, removed, or
    has its properties changed, so a save that replaces a relationship list of either, from an object loaded before, is
    refused. Each statement is run again when the engine turns it away for contention.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        run: Callable[[GraphStatement], Awaitable[Any]],
        query: Callable[[GraphStatement], Awaitable[list[Any]]],
        mapper: GraphObjectMapper,
        backend: CypherBackend,
    ) -> None:
        self._registry = registry
        self._run = run
        self._query = query
        self._mapper = mapper
        self._backend = backend

    async def load_related(
        self, origin: NodeRef, type: str, target: type[T], direction: Direction = Direction.OUTGOING
    ) -> list[T]:
        """
        The nodes joined to `origin` by a relationship of the type in the direction, each once, as the target fragment
        class, or the subclass of it whose labels each carries.
        """
        model = self._registry.fragment(target)
        rows = await self._query(self._backend.related(self._node(origin), type, direction, model))
        return [self._mapper.fragment(model, row) for row in rows]

    async def relate(
        self,
        origin: NodeRef,
        to: NodeRef,
        type: str,
        properties: Mapping[str, Any] | None = None,
        mode: RelateMode = RelateMode.MERGE,
    ) -> bool:
        """
        Joins `origin` to `to` with a relationship of the type. Under MERGE, the default, one is made only when there is
        none, and the properties are set whether it was made or found; a call that finds it as it is marks neither end.
        Under CREATE each call makes another. A property that is None is left out. Returns whether both nodes were
        found and joined.
        """
        statement = self._backend.relate(self._node(origin), self._node(to), type, properties or {}, mode)
        return await self._run(statement) > 0

    async def unrelate(self, origin: NodeRef, to: NodeRef, type: str) -> int:
        """Removes every relationship of the type from `origin` to `to`, and returns how many there were."""
        return await self._run(self._backend.unrelate(self._node(origin), self._node(to), type))

    async def unrelate_all(self, origin: NodeRef, type: str, direction: Direction = Direction.OUTGOING) -> int:
        """Removes every relationship of the type `origin` has in the direction, and returns how many."""
        return await self._run(self._backend.unrelate_all(self._node(origin), type, direction))

    def _node(self, ref: NodeRef) -> MatchedNode:
        return MatchedNode(self._registry.fragment(ref.type), ref.id, tuple(ref.labels))
