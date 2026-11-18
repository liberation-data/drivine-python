from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from drivine.annotation.aggregate_function import AggregateFunction
from drivine.annotation.direction import Direction
from drivine.annotation.hop import Hop
from drivine.backend.cascade_beyond import CascadeBeyond
from drivine.backend.cypher import quote_name
from drivine.backend.save_statement_builder import root_of
from drivine.backend.sql.fragments import NODE, Fragments
from drivine.backend.sql.nodes_of import NodesOf
from drivine.backend.sql.sql_cascade import SqlCascade
from drivine.backend.sql.sql_graph import SqlGraph, sql_text
from drivine.backend.sql.sql_step import SqlStep
from drivine.backend.view_projector import target
from drivine.cypher.shared_compiler import SharedStore
from drivine.model.aggregate_model import AggregateModel
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.path_model import PathModel
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.target_model import TargetModel


class SqlViews(ABC):
    """
    Writes a view as one JSON document per root, `{root, rels}`, as the graph engines return it: each relationship a
    correlated subquery over the relationship table, its nodes aggregated into a JSON array. A relationship is followed
    as many times as its max_depth along any one path; past that, a list is empty and a single absent.

    Oracle, Postgres and SQLite read a view alike; what is each store's own is its dialect: a JSON object and array, a
    node's document, a label test, the nodes a relationship reaches to a depth, a property read as a number, how a
    query reads its first row alone, and the hints Oracle's subqueries carry.
    """

    empty_object: str
    """An empty JSON object."""

    first_row = "FETCH FIRST 1 ROW ONLY"
    """What ends a query that reads its first row alone."""

    def __init__(self, graph: SqlGraph, depth: Mapping[str, int] | None, store: SharedStore) -> None:
        self.graph = graph
        self.depth = depth or {}
        self.fragments = Fragments(store, graph.name)
        """
        The shared module's fragments the views write (a path ordered by a hop's property): what they bind is to be
        bound beside the statement's own, and on Oracle what they ask of the statement written into it (see
        oracle_fragments).
        """
        self._alias_count = 0

    # ----- Views -----

    def next_alias(self, prefix: str) -> str:
        """An alias no other part of the statement uses."""
        self._alias_count += 1
        return f"{prefix}{self._alias_count}"

    def document(self, model: GraphViewModel, alias: str) -> str:
        """The document of the view whose root is the node row `alias`."""
        return self._view(model, alias, [])

    def requirements(self, model: GraphViewModel, alias: str) -> list[str]:
        """What the node row `alias` must meet to be the view's root: each required relationship and path is there."""
        relationships = [
            f"EXISTS ({self.related_subquery()} {step.from_} AND {step.x})"
            for step in (
                self.step(rel.type, rel.direction, alias, labels_of(target(rel)))
                for rel in model.relationships
                if rel.required
            )
        ]
        paths = [f"EXISTS ({self._path_ids(path, alias)})" for path in model.paths if path.required]
        return [*relationships, *paths]

    def step(self, type: str, direction: Direction, from_: str, labels: Sequence[str]) -> SqlStep:
        """One relationship of the type from the node row `from_`."""
        edge = self.next_alias("e")
        node = self.next_alias("x")
        typed = f"{edge}.type = {sql_text(type)}"
        if direction is Direction.INCOMING:
            on, where = f"{node}.nid = {edge}.src", f"{edge}.dst = {from_}.nid"
        elif direction is Direction.UNDIRECTED:
            on = (
                f"(({edge}.src = {from_}.nid AND {node}.nid = {edge}.dst) OR ({edge}.dst = {from_}.nid AND "
                f"{node}.nid = {edge}.src))"
            )
            where = "1 = 1"
        else:
            on, where = f"{node}.nid = {edge}.dst", f"{edge}.src = {from_}.nid"
        return SqlStep(
            f"FROM {self.graph.edges} {edge} JOIN {self.graph.nodes} {node} ON {on}\nWHERE {where} AND {typed}",
            edge,
            node,
            self.has_labels(node, labels) if labels else "1 = 1",
        )

    def cascade(self, model: GraphViewModel, roots: str) -> SqlCascade:
        """
        What a cascade from the roots the predicate `roots` selects of the node row `n` reaches: queries of the ids of
        the nodes a load of the view reaches through the relationships a save writes, as far as each relationship's
        max_depth, and only into nested views whose requirements a node meets, as a load would; each relationship
        followed; and for a relationship to views of the same class, the query of the nodes one level below its
        max_depth.
        """
        walk = _CascadeWalk(SqlCascade())
        self._reached_from(model, "n", [], [f"FROM {self.graph.nodes} n"], [roots], walk)
        for beyond, view in zip(walk.cascade.beyond, walk.beyond, strict=True):
            beyond.within.extend(walk.views.get(view, []))
        return walk.cascade

    def _reached_from(
        self,
        model: GraphViewModel,
        from_: str,
        path: list[RelationshipModel],
        froms: list[str],
        wheres: list[str],
        walk: _CascadeWalk,
    ) -> None:
        cascade = walk.cascade
        # A read-only field (marked so, or a list of fragments read over several hops) is not the view's to delete. A
        # view nested in itself is not one: each level is one relationship from the level above.
        for rel in (it for it in model.relationships if not it.read_only):
            depth = self.depth.get(rel.field, rel.max_depth)
            into = target(rel)
            deep = sum(1 for it in path if it is rel) >= depth
            if deep and not (isinstance(into, GraphViewModel) and into.type is model.type):
                continue
            step = self.step(rel.type, rel.direction, from_, labels_of(into))
            from_clause, where_clause = step.from_.split("\nWHERE ")
            next_froms = [*froms, from_clause.replace("FROM ", "CROSS JOIN ", 1)]
            required = self.requirements(into, step.node) if isinstance(into, GraphViewModel) else []
            next_wheres = [*wheres, where_clause, step.x, *required]
            query = f"SELECT {step.node}.nid {' '.join(next_froms)} WHERE {' AND '.join(next_wheres)}"
            if deep:
                cascade.beyond.append(CascadeBeyond(model.type.__name__, rel.field, depth, query))
                walk.beyond.append(model.type)
                continue
            cascade.followed.append(rel)
            cascade.reached.append(query)
            if isinstance(into, GraphViewModel):
                walk.views.setdefault(into.type, []).append(query)
                self._reached_from(into, step.node, [*path, rel], next_froms, next_wheres, walk)

    def _view(self, model: GraphViewModel, alias: str, path: list[RelationshipModel]) -> str:
        entries = [
            *((rel.key, self._relationship(rel, alias, path)) for rel in model.relationships),
            *((it.key, self._path(it, alias)) for it in model.paths),
            *((it.key, self._aggregate(it, alias)) for it in model.aggregates),
        ]
        rels = self.object(entries) if entries else self.empty_object
        return self.object([("root", self.node_document(alias)), ("rels", rels)])

    def _relationship(self, rel: RelationshipModel, from_: str, path: list[RelationshipModel]) -> str:
        depth = self.depth.get(rel.field, rel.max_depth)
        if isinstance(rel.related, FragmentModel) and depth > 1:
            return self._deep_fragments(rel, from_, depth)
        if sum(1 for it in path if it is rel) >= depth:
            return "NULL"
        into = target(rel)
        step = self.step(rel.type, rel.direction, from_, labels_of(into))
        node = (
            self._view(into, step.node, [*path, rel])
            if isinstance(into, GraphViewModel)
            else self.node_document(step.node)
        )
        item = (
            self.object([("rel", f"{step.edge}.props"), ("target", node)])
            if isinstance(rel.related, RelationshipFragmentModel)
            else node
        )
        required = self.requirements(into, step.node) if isinstance(into, GraphViewModel) else []
        where = " AND ".join([step.x, *required])
        if rel.is_collection:
            return f"(SELECT {self._array_of(item)} {step.from_} AND {where})"
        return f"(SELECT {item} {step.from_} AND {where} {self.first_row})"

    def _deep_fragments(self, rel: RelationshipModel, from_: str, depth: int) -> str:
        """A list of fragments read over 1 to `depth` hops, each node once."""
        x = self.next_alias("x")
        reached = self.reached(rel.type, rel.direction, from_, depth)
        nodes = self.nodes_of(reached, x)
        where = f"WHERE {self.has_labels(x, labels_of(target(rel)))}"
        document = self.node_document(x)
        if rel.is_collection:
            return f"({self._select(nodes.hint)}{self._array_of(document)} {nodes.from_} {where})"
        return f"({self._select(nodes.hint)}{document} {nodes.from_} {where} {self.first_row})"

    def _path(self, path: PathModel, from_: str) -> str:
        """The nodes at the far end of a path, each once: in the order one of its hops names, where one does."""
        x = self.next_alias("x")
        ordered = next((it for it in path.hops if it.order_by), None)
        if ordered is None:
            nodes = self.nodes_of(self._path_ids(path, from_), x)
            document = self.node_document(x)
            if path.is_collection:
                return f"({self._select(nodes.hint)}{self._array_of(document)} {nodes.from_})"
            return f"({self._select(nodes.hint)}{document} {nodes.from_} {self.first_row})"
        o = self.next_alias("o")
        kept = f"{self.next_alias('k')}_"
        terms = self._path_terms(ordered)
        nodes = (
            f"FROM {self.graph.nodes} {x} JOIN ({self._ordered_path_nodes(path, from_, ordered, terms)}) {o} "
            f"ON {o}.nid = {x}.nid JOIN {self.graph.nodes} {kept} ON {self._same_node(f'{kept}.nid', f'{o}.hop')}"
        )
        order = f"{terms(kept)}, {x}.nid"
        if path.is_collection:
            return f"(SELECT {self._array_of(self.node_document(x), order)} {nodes})"
        return f"(SELECT {self.node_document(x)} {nodes} ORDER BY {order} {self.first_row})"

    def _path_terms(self, ordered: Hop) -> Callable[[str], str]:
        """
        The terms that order the node row `row`, of a node of the hop a path is ordered by, as Cypher orders the hop's
        property: the shared module's (drivine-cypher-spec#235, its fragments; drivine4j's `pathOrder`), whatever its
        kind, a date included, read highest first where the hop reads so, and a node without the property last
        whichever way the path is read.
        """
        assert ordered.order_by is not None
        cypher = f"{NODE}.{quote_name(ordered.order_by)}"
        return lambda row: ", ".join(self.fragments.ordered(cypher, not ordered.ascending, "LAST", row=row))

    def _ordered_path_nodes(self, path: PathModel, from_: str, ordered: Hop, terms: Callable[[str], str]) -> str:
        """
        The nodes at the far end of a path, each once, as `nid`, with as `hop` the node of the hop the path is ordered
        by that it keeps: the first of those that lead to it in Cypher's order of the hop's property, `terms`, the
        lowest or, read highest first, the highest. A node of the hop without the property is kept only where none that
        leads to it has it.
        """
        body, nodes = self._path_rows(path, from_)
        node = nodes[list(path.hops).index(ordered)]
        last = nodes[-1]
        return self._first_of_ways(
            f"SELECT {last}.nid AS nid, {node}.nid AS hop, {node}.props AS props, {node}.stamp AS stamp {body}",
            terms,
        )

    def _path_ids(self, path: PathModel, from_: str) -> str:
        """The ids of the nodes at the far end of a path from the node row `from_`, its hops' labels met."""
        body, nodes = self._path_rows(path, from_)
        return f"SELECT {nodes[-1]}.nid {body}"

    def _path_rows(self, path: PathModel, from_: str) -> tuple[str, list[str]]:
        """
        The FROM and WHERE of a path from the node row `from_`, its hops' labels met, and the row each hop reaches.
        """
        froms: list[str] = []
        wheres: list[str] = []
        nodes: list[str] = []
        for i, hop in enumerate(path.hops):
            last = i == len(path.hops) - 1
            labels = [*([hop.label] if hop.label else []), *(path.target.labels if last else [])]
            step = self.step(hop.type, hop.direction, nodes[i - 1] if i else from_, labels)
            from_clause, where_clause = step.from_.split("\nWHERE ")
            froms.append(from_clause if i == 0 else from_clause.replace("FROM ", "CROSS JOIN ", 1))
            wheres.extend([where_clause, *([step.x] if labels else [])])
            nodes.append(step.node)
        return f"{' '.join(froms)} WHERE {' AND '.join(wheres)}", nodes

    def _aggregate(self, aggregate: AggregateModel, from_: str) -> str:
        """An aggregate over the nodes one relationship type leads to: COUNT of relationships, else of values."""
        step = self.step(aggregate.type, aggregate.direction, from_, [])
        number = self.number(step.node, aggregate.property) if aggregate.property else None
        if aggregate.function is AggregateFunction.COUNT:
            fn = f"COUNT({number or '*'})"
        elif aggregate.function is AggregateFunction.SUM:
            fn = self.zero_if_null(f"SUM({number})")
        else:
            fn = f"{aggregate.function.value}({number})"
        return f"(SELECT {fn} {step.from_})"

    def _same_node(self, a: str, b: str) -> str:
        """Whether the node keys `a` and `b` are equal, as the store joins a node back to the node table by its key."""
        return f"{a} = {b}"

    def _steps(self, type: str, direction: Direction) -> str:
        """
        The relationships of the type as a subquery of rows `eid`, `a` and `b`, each a step from `a` to `b` in the
        direction, an undirected relationship a row each way round: what `reached` walks.
        """
        typed = f"FROM {self.graph.edges} WHERE type = {sql_text(type)}"
        if direction is Direction.UNDIRECTED:
            return f"(SELECT eid, src AS a, dst AS b {typed} UNION ALL SELECT eid, dst, src {typed})"
        if direction is Direction.INCOMING:
            return f"(SELECT eid, dst AS a, src AS b {typed})"
        return f"(SELECT eid, src AS a, dst AS b {typed})"

    # ----- The dialect -----

    @abstractmethod
    def object(self, entries: Sequence[tuple[str, str]]) -> str:
        """A JSON object of the entries, each a key and the expression for its value."""

    @abstractmethod
    def node_document(self, alias: str) -> str:
        """The map a load returns for the node row `alias`."""

    @abstractmethod
    def has_labels(self, alias: str, labels: Sequence[str]) -> str:
        """A predicate that holds when the node row `alias` carries every one of the labels."""

    @abstractmethod
    def related_subquery(self, *hints: str) -> str:
        """The SELECT of a subquery over related nodes, with the hints given, where the store reads hints."""

    @abstractmethod
    def reached(self, type: str, direction: Direction, from_: str, depth: int) -> str:
        """The ids (a column `nid`) of the nodes 1 to `depth` relationships of the type from the node row `from_`."""

    @abstractmethod
    def nodes_of(self, ids: str, x: str) -> NodesOf:
        """
        The node rows `x` whose ids the query `ids` selects, as a column `nid`, each once: the FROM of a query that
        reads them, and the hint its SELECT carries.
        """

    @abstractmethod
    def number(self, node: str, property: str) -> str:
        """The property of the node row `node` as a number where it holds one, and null where it holds anything else."""

    @abstractmethod
    def zero_if_null(self, value: str) -> str:
        """`value`, or 0 where it is null."""

    @abstractmethod
    def _array_of(self, item: str, order: str | None = None) -> str:
        """The JSON array of `item` over the rows gathered, in `order` where one is given."""

    @abstractmethod
    def _select(self, hint: str) -> str:
        """The SELECT keyword with `hint`, where the store reads one."""

    @abstractmethod
    def _first_of_ways(self, ways: str, terms: Callable[[str], str]) -> str:
        """
        The ways `ways` reaches the nodes at the far end of a path, rows of `nid`, `hop`, `props` and `stamp`, the node
        reached and the node of the ordered hop the way passes through, with its properties and stamp (which a path
        ordered by the stamp reads): grouped by `nid`, each with as `hop` the first of its ways' by `terms`, which order
        a row of the node of the hop.
        """


def by_id(x: str) -> str:
    """The hint that has Oracle read the node row `x` by its id, through the primary key."""
    return f"INDEX({x} (nid))"


def labels_of(into: TargetModel) -> list[str]:
    """The labels of the node a relationship leads to: a view's root's, or the fragment's."""
    return root_of(into).labels


@dataclass
class _CascadeWalk:
    """
    A cascade as it is walked: what it reaches, the queries of the roots of each view it reaches by the view's class,
    and the class of the view each of its `beyond` is a relationship of.
    """

    cascade: SqlCascade
    views: dict[type, list[str]] = field(default_factory=lambda: {})
    beyond: list[type] = field(default_factory=lambda: [])
