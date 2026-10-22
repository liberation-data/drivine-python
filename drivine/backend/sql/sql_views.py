from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from drivine.annotation.aggregate_function import AggregateFunction
from drivine.annotation.direction import Direction
from drivine.annotation.hop import Hop
from drivine.backend.sql.nodes_of import NodesOf
from drivine.backend.sql.order_reads import OrderReads
from drivine.backend.sql.sql_graph import SqlGraph, sql_text
from drivine.backend.sql.sql_step import SqlStep
from drivine.backend.view_projector import target
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

    Oracle and Postgres read a view alike; what is each store's own is its dialect: a JSON object and array, a node's
    document, a label test, the nodes a relationship reaches to a depth, a property read as a number, and the hints
    Oracle's subqueries carry.
    """

    empty_object: str
    """An empty JSON object."""

    def __init__(self, graph: SqlGraph, depth: Mapping[str, int] | None = None) -> None:
        self.graph = graph
        self.depth = depth or {}
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

    def _reached_from_roots(self, model: GraphViewModel, roots: str) -> list[str]:
        """
        Queries of the ids of the nodes a load of the view from the roots `roots` selects of the node row `n` reaches:
        as far as each relationship's max_depth, and only into nested views whose requirements a node meets, as a load
        would.
        """
        return self._reached_from(model, "n", [], [f"FROM {self.graph.nodes} n"], [roots])

    def _reached_from(
        self,
        model: GraphViewModel,
        from_: str,
        path: list[RelationshipModel],
        froms: list[str],
        wheres: list[str],
    ) -> list[str]:
        queries: list[str] = []
        for rel in model.relationships:
            depth = self.depth.get(rel.field, rel.max_depth)
            into = target(rel)
            if isinstance(rel.related, FragmentModel) and depth > 1:
                x = self.next_alias("x")
                r = self.next_alias("r")
                reached = self.reached(rel.type, rel.direction, from_, depth)
                tests = [*wheres, self.has_labels(x, labels_of(into))]
                queries.append(
                    f"{self._select(by_id(x))}{x}.nid {' '.join(froms)} CROSS JOIN LATERAL ({reached}) {r} "
                    f"JOIN {self.graph.nodes} {x} ON {x}.nid = {r}.nid WHERE {' AND '.join(tests)}"
                )
                continue
            if sum(1 for it in path if it is rel) >= depth:
                continue
            step = self.step(rel.type, rel.direction, from_, labels_of(into))
            from_clause, where_clause = step.from_.split("\nWHERE ")
            next_froms = [*froms, from_clause.replace("FROM ", "CROSS JOIN ", 1)]
            required = self.requirements(into, step.node) if isinstance(into, GraphViewModel) else []
            next_wheres = [*wheres, where_clause, step.x, *required]
            queries.append(f"SELECT {step.node}.nid {' '.join(next_froms)} WHERE {' AND '.join(next_wheres)}")
            if isinstance(into, GraphViewModel):
                queries.extend(self._reached_from(into, step.node, [*path, rel], next_froms, next_wheres))
        return queries

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
        return f"(SELECT {item} {step.from_} AND {where} FETCH FIRST 1 ROW ONLY)"

    def _deep_fragments(self, rel: RelationshipModel, from_: str, depth: int) -> str:
        """A list of fragments read over 1 to `depth` hops, each node once."""
        x = self.next_alias("x")
        reached = self.reached(rel.type, rel.direction, from_, depth)
        nodes = self.nodes_of(reached, x)
        where = f"WHERE {self.has_labels(x, labels_of(target(rel)))}"
        document = self.node_document(x)
        if rel.is_collection:
            return f"({self._select(nodes.hint)}{self._array_of(document)} {nodes.from_} {where})"
        return f"({self._select(nodes.hint)}{document} {nodes.from_} {where} FETCH FIRST 1 ROW ONLY)"

    def _path(self, path: PathModel, from_: str) -> str:
        """The nodes at the far end of a path, each once: in the order one of its hops names, where one does."""
        x = self.next_alias("x")
        ordered = next((it for it in path.hops if it.order_by), None)
        if ordered is None:
            nodes = self.nodes_of(self._path_ids(path, from_), x)
            document = self.node_document(x)
            if path.is_collection:
                return f"({self._select(nodes.hint)}{self._array_of(document)} {nodes.from_})"
            return f"({self._select(nodes.hint)}{document} {nodes.from_} FETCH FIRST 1 ROW ONLY)"
        o = self.next_alias("o")
        direction = "ASC" if ordered.ascending else "DESC"
        nodes = (
            f"FROM {self.graph.nodes} {x} JOIN ({self._ordered_path_nodes(path, from_, ordered)}) {o} "
            f"ON {o}.nid = {x}.nid"
        )
        order = (
            f"{o}.order_kind {direction} NULLS LAST, {o}.order_number {direction} NULLS LAST, "
            f"{o}.order_text {direction} NULLS {'FIRST' if ordered.ascending else 'LAST'}, {x}.nid"
        )
        if path.is_collection:
            return f"(SELECT {self._array_of(self.node_document(x), order)} {nodes})"
        return f"(SELECT {self.node_document(x)} {nodes} ORDER BY {order} FETCH FIRST 1 ROW ONLY)"

    def _ordered_path_nodes(self, path: PathModel, from_: str, ordered: Hop) -> str:
        """
        The nodes at the far end of a path, each with the value the hop the path is ordered by keeps for it: that of
        the first node of the hop that leads to it, the lowest or, read highest first, the highest. The node the hop
        reaches is mapped to no class, so its property's type is not known: it is read as a number, which is null for
        a value that is not a JSON number (a text of digits included), and as text. `order_kind` is 0 for a text, 1 for
        a number and null for a node without the property, so a text comes before a number read lowest first, as the
        graph engines order them, and a node without the property last, whichever way the path is read. Only the kind
        kept is read further: a node reached through a text and a number keeps the text, read lowest first, and the
        number, read highest first. Where the store reads a stored `""` as null (Oracle), whether the node holds one is
        read on its own: it is a text, and the lowest. `order_text` is null for it, and orders first among the texts
        read lowest first.
        """
        assert ordered.order_by is not None
        body, nodes = self._path_rows(path, from_)
        node = nodes[list(path.hops).index(ordered)]
        last = nodes[-1]
        r = self.next_alias("r")
        keep = "MIN" if ordered.ascending else "MAX"
        reads = self._order_reads(node, ordered.order_by)
        kind = f"{keep}(CASE WHEN {r}.num IS NOT NULL THEN 1 WHEN {r}.txt IS NOT NULL OR {r}.emp = 1 THEN 0 END)"
        text = f"CASE WHEN {r}.num IS NULL THEN {r}.txt END"
        # Read lowest first, a node that a "" leads to keeps no text, which orders first among the texts.
        kept = f"CASE WHEN MAX({r}.emp) IS NULL THEN MIN({text}) END" if ordered.ascending else f"MAX({text})"
        return (
            f"SELECT {r}.nid, {kind} AS order_kind, "
            f"CASE WHEN {kind} = 1 THEN {keep}({r}.num) END AS order_number, "
            f"CASE WHEN {kind} = 0 THEN {kept} END AS order_text "
            f"FROM (SELECT {last}.nid AS nid, {reads.num} AS num, {reads.txt} AS txt, {reads.emp} AS emp {body}) "
            f"{r} GROUP BY {r}.nid"
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
    def _order_reads(self, node: str, property: str) -> OrderReads:
        """
        The property of the node row `node` that a path is ordered by, read as a number, as text, and as whether it is
        `""`.
        """


def by_id(x: str) -> str:
    """The hint that has Oracle read the node row `x` by its id, through the primary key."""
    return f"INDEX({x} (nid))"


def labels_of(into: TargetModel) -> list[str]:
    return (into.root if isinstance(into, GraphViewModel) else into).labels
