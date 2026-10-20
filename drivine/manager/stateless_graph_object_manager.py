from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from drivine.backend.cypher_backend import CypherBackend
from drivine.backend.cypher_grammar import cypher_grammar_for
from drivine.drivine_error import DrivineError
from drivine.manager.persistence_manager import PersistenceManager
from drivine.mapper.graph_object_mapper import GraphObjectMapper
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.model_registry import ModelRegistry

T = TypeVar("T")


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


def _checked(depth: Mapping[str, int] | None) -> Mapping[str, int] | None:
    for field, it in (depth or {}).items():
        if isinstance(it, bool) or not isinstance(it, int) or it < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise DrivineError(f"The depth of {field} must be a whole number of at least 1.")
    return depth
