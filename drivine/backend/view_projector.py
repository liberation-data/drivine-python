from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from drivine.annotation.aggregate_function import AggregateFunction
from drivine.annotation.direction import Direction
from drivine.backend.cypher import label_expression, quote_name
from drivine.backend.cypher_grammar import CypherGrammar
from drivine.backend.view_projection import ViewProjection
from drivine.model.aggregate_model import AggregateModel
from drivine.model.fragment_model import FragmentModel
from drivine.model.graph_view_model import GraphViewModel
from drivine.model.path_model import PathModel
from drivine.model.relationship_fragment_model import RelationshipFragmentModel
from drivine.model.relationship_model import RelationshipModel
from drivine.model.target_model import TargetModel


class ViewProjector:
    """
    Writes the projection of a view, as TypeScript's Drivine writes it.

    A view projects as `{root: <fragment>, rels: {<field>: ...}}`, a fragment as `{props, labels}`, and a relationship
    fragment as `{rel, target}`. A list relationship projects as a list, a single one as its first item or null. A
    field goes by its stored name, by the view's property naming.

    A relationship is followed as many times as its max_depth along any one path; past that, a list is `[]` and a
    single item null. That is what ends a view that refers to itself.
    """

    def __init__(self, grammar: CypherGrammar, depth: Mapping[str, int] | None = None) -> None:
        """`depth` overrides max_depth for this query, by relationship field name."""
        self.grammar = grammar
        self.depth = depth or {}
        self._alias_count = 0

    def project(self, model: GraphViewModel, alias: str) -> ViewProjection:
        return self._view(model, alias, [])

    def requirements(self, model: GraphViewModel, alias: str) -> list[str]:
        """
        The predicates a node bound to `alias` must meet to be the root of `model`: each required relationship and
        path exists.
        """
        relationships = [
            self.grammar.existence_check(pattern(it, alias, "", f":{labels_of(target(it))}", self._hops(it)))
            for it in model.relationships
            if it.required
        ]
        paths = [self._path_requirement(it, alias) for it in model.paths if it.required]
        return relationships + paths

    def _path_requirement(self, path: PathModel, alias: str) -> str:
        if not self.grammar.far_labels_after_match:
            return self.grammar.existence_check(path_pattern(path, alias, "", True))
        self._alias_count += 1
        far = f"p{self._alias_count}"
        return self.grammar.some_match(
            path_pattern(path, alias, far, False), far, "", lambda _: label_test(far, far_labels(path))
        )

    def _depth_of(self, rel: RelationshipModel) -> int:
        return self.depth.get(rel.field, rel.max_depth)

    def _multi_hop(self, rel: RelationshipModel) -> bool:
        """Whether a relationship is read over several hops: fragments with a depth above 1."""
        return isinstance(rel.related, FragmentModel) and self._depth_of(rel) > 1

    def _hops(self, rel: RelationshipModel) -> str:
        """The hops a relationship's pattern spans: '' for one, `*1..k` for fragments read over several."""
        return f"*1..{self._depth_of(rel)}" if self._multi_hop(rel) else ""

    def _view(self, model: GraphViewModel, alias: str, path: list[RelationshipModel]) -> ViewProjection:
        calls: list[str] = []
        fields: list[str] = []
        for rel in model.relationships:
            projection = self._relationship(rel, alias, path)
            calls.extend(projection.calls)
            fields.append(f"{quote_name(rel.key)}: {projection.expression}")
        for it in model.paths:
            projection = self._path(it, alias)
            calls.extend(projection.calls)
            fields.append(f"{quote_name(it.key)}: {projection.expression}")
        for aggregate in model.aggregates:
            projection = self._aggregate(aggregate, alias)
            calls.extend(projection.calls)
            fields.append(f"{quote_name(aggregate.key)}: {projection.expression}")
        return ViewProjection(f"{{root: {fragment(alias)}, rels: {{{', '.join(fields)}}}}}", calls)

    def _relationship(self, rel: RelationshipModel, origin: str, path: list[RelationshipModel]) -> ViewProjection:
        if self._multi_hop(rel):
            return self._multi_hop_fragments(rel, origin)
        if sum(1 for it in path if it is rel) >= self._depth_of(rel):
            return ViewProjection("[]" if rel.is_collection else "null")
        self._alias_count += 1
        n = self._alias_count
        rel_alias, target_alias = f"r{n}", f"t{n}"
        into = target(rel)
        inner = (
            self._view(into, target_alias, [*path, rel])
            if isinstance(into, GraphViewModel)
            else ViewProjection(fragment(target_alias))
        )
        item = (
            f"{{rel: properties({rel_alias}), target: {inner.expression}}}"
            if isinstance(rel.related, RelationshipFragmentModel)
            else inner.expression
        )
        conditions = self.requirements(into, target_alias) if isinstance(into, GraphViewModel) else []
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        match = f"{pattern(rel, origin, rel_alias, f'{target_alias}:{labels_of(into)}')}{where}"

        if self.grammar.view_projection == "CALL_SUBQUERY":
            collected = f"collect(CASE WHEN {target_alias} IS NOT NULL THEN {item} END)"
            value = f"v{n}"
            nested = "".join(f" {it}" for it in inner.calls)
            returned = collected if rel.is_collection else f"head({collected})"
            return ViewProjection(
                value, [f"CALL {{ WITH {origin} OPTIONAL MATCH {match}{nested} RETURN {returned} AS {value} }}"]
            )
        if inner.calls:
            raise AssertionError("An inline projection has no CALL blocks.")
        comprehension = f"[{match} | {item}]"
        return ViewProjection(comprehension if rel.is_collection else f"head({comprehension})")

    def _path(self, path: PathModel, origin: str) -> ViewProjection:
        """
        The nodes at the far end of a path, each once, in the order one of its hops names where one does. Where the
        grammar says so, the far node's labels are tested as each is collected, after the match.
        """
        self._alias_count += 1
        n = self._alias_count
        t = f"t{n}"
        order = _path_order(path, t, n)
        if self.grammar.view_projection == "CALL_SUBQUERY":
            after = self.grammar.far_labels_after_match
            match = path_pattern(path, origin, t, not after, order.node if order else None)
            kept = label_test(t, far_labels(path)) if after else f"{t} IS NOT NULL"
            # collect(DISTINCT …) keeps each node where it first occurs, so sorting the rows first orders the list.
            ordered = ""
            if order:
                bound = ", ".join(dict.fromkeys([origin, t, order.node]))
                way = "ASC" if order.ascending else "DESC"
                ordered = f" WITH {bound} ORDER BY {order.key} IS NULL, {order.key} {way}"
            collected = f"collect(DISTINCT CASE WHEN {kept} THEN {fragment(t)} END)"
            value = f"v{n}"
            returned = collected if path.is_collection else f"head({collected})"
            return ViewProjection(
                value,
                [f"CALL {{ WITH {origin} OPTIONAL MATCH {match}{ordered} RETURN {returned} AS {value} }}"],
            )
        if order:
            reached = _sorted_by_key(path_pattern(path, origin, t, True, order.node), t, order, n)
        else:
            reached = f"[{path_pattern(path, origin, t, True)} | {t}]"
        distinct = f"reduce(acc = [], y IN {reached} | CASE WHEN y IN acc THEN acc ELSE acc + y END)"
        items = f"[x{n} IN {distinct} | {fragment(f'x{n}')}]"
        return ViewProjection(items if path.is_collection else f"head({items})")

    def _aggregate(self, aggregate: AggregateModel, origin: str) -> ViewProjection:
        """An aggregate over the nodes one relationship type leads to from `origin`."""
        self._alias_count += 1
        n = self._alias_count
        x = f"t{n}"
        edge = f"[:{quote_name(aggregate.type)}]"
        if aggregate.direction is Direction.INCOMING:
            step = f"({origin})<-{edge}-({x})"
        elif aggregate.direction is Direction.UNDIRECTED:
            step = f"({origin})-{edge}-({x})"
        else:
            step = f"({origin})-{edge}->({x})"
        prop = f"{x}.{quote_name(aggregate.property)}" if aggregate.property else None
        function = aggregate.function
        if self.grammar.view_projection == "CALL_SUBQUERY":
            value = f"v{n}"
            call = f"count({prop or x})" if function is AggregateFunction.COUNT else f"{function.value.lower()}({prop})"
            return ViewProjection(value, [f"CALL {{ WITH {origin} OPTIONAL MATCH {step} RETURN {call} AS {value} }}"])
        values = f"[{step} WHERE {prop} IS NOT NULL | {prop}]" if prop else f"[{step} | 1]"
        if function is AggregateFunction.COUNT:
            return ViewProjection(f"size({values})")
        if function is AggregateFunction.SUM:
            return ViewProjection(f"reduce(s = 0, v IN {values} | s + v)")
        if function is AggregateFunction.AVG:
            return ViewProjection(
                f"CASE WHEN size({values}) = 0 THEN null "
                f"ELSE reduce(s = 0.0, v IN {values} | s + v) / size({values}) END"
            )
        better = "<" if function is AggregateFunction.MIN else ">"
        return ViewProjection(
            f"reduce(m = null, v IN {values} | CASE WHEN m IS NULL OR v {better} m THEN v ELSE m END)"
        )

    def _multi_hop_fragments(self, rel: RelationshipModel, origin: str) -> ViewProjection:
        """A list of fragments read over 1 to depth hops, each node once."""
        self._alias_count += 1
        n = self._alias_count
        t = f"t{n}"
        match = pattern(rel, origin, "", f"{t}:{labels_of(target(rel))}", self._hops(rel))
        if self.grammar.view_projection == "CALL_SUBQUERY":
            collected = f"collect(DISTINCT CASE WHEN {t} IS NOT NULL THEN {fragment(t)} END)"
            value = f"v{n}"
            returned = collected if rel.is_collection else f"head({collected})"
            return ViewProjection(
                value, [f"CALL {{ WITH {origin} OPTIONAL MATCH {match} RETURN {returned} AS {value} }}"]
            )
        distinct = f"reduce(acc = [], y IN [{match} | {t}] | CASE WHEN y IN acc THEN acc ELSE acc + y END)"
        items = f"[x{n} IN {distinct} | {fragment(f'x{n}')}]"
        return ViewProjection(items if rel.is_collection else f"head({items})")


def fragment(alias: str) -> str:
    return f"{{props: properties({alias}), labels: labels({alias})}}"


def target(rel: RelationshipModel) -> TargetModel:
    """The node a relationship field leads to: for a relationship fragment, its target."""
    related = rel.related
    if isinstance(related, RelationshipFragmentModel):
        assert related.target is not None
        return related.target
    return related


def labels_of(into: TargetModel) -> str:
    model = into.root if isinstance(into, GraphViewModel) else into
    return label_expression(model.labels)[1:]


def pattern(rel: RelationshipModel, origin: str, rel_alias: str, to: str, hops: str = "") -> str:
    edge = f"[{rel_alias}:{quote_name(rel.type)}{hops}]"
    if rel.direction is Direction.INCOMING:
        return f"({origin})<-{edge}-({to})"
    if rel.direction is Direction.UNDIRECTED:
        return f"({origin})-{edge}-({to})"
    return f"({origin})-{edge}->({to})"


@dataclass(frozen=True)
class _PathOrder:
    """
    The order a path is read in, where one of its hops names one: the node that hop reaches (the far node when it is
    the last hop), the key read from it, and which way.
    """

    node: str
    key: str
    ascending: bool


def _path_order(path: PathModel, far: str, n: int) -> _PathOrder | None:
    at = next((index for index, it in enumerate(path.hops) if it.order_by), None)
    if at is None:
        return None
    hop = path.hops[at]
    assert hop.order_by is not None
    node = far if at == len(path.hops) - 1 else f"h{n}"
    return _PathOrder(node, f"{node}.{quote_name(hop.order_by)}", hop.ascending)


def _sorted_by_key(pattern: str, far: str, order: _PathOrder, n: int) -> str:
    """
    The far nodes of `pattern`, in the order of the key: an insertion sort, since a pattern comprehension has no ORDER
    BY and an inline projection has nowhere to put a CALL. See TypeScript's ViewProjector for how it reads the key.
    """
    s, y, a, z = f"s{n}", f"y{n}", f"a{n}", f"z{n}"
    pairs = f"[{pattern} | {{k: {order.key}, s: toString({order.key}) = {order.key}, n: {far}}}]"
    if order.ascending:
        first, compares = f"{a}.s AND NOT {y}.s", f"{a}.k <= {y}.k"
    else:
        first, compares = f"{y}.s AND NOT {a}.s", f"{a}.k >= {y}.k"
    before = f"{y}.k IS NULL OR ({a}.k IS NOT NULL AND (({first}) OR ({a}.s = {y}.s AND coalesce({compares}, false))))"
    ordered = (
        f"reduce({s} = [], {y} IN {pairs} | [{a} IN {s} WHERE {before}] + [{y}] + [{a} IN {s} WHERE NOT ({before})])"
    )
    return f"[{z} IN {ordered} | {z}.n]"


def far_labels(path: PathModel) -> list[str]:
    """The labels a path's far node carries: its last hop's, and its target's."""
    last = path.hops[-1]
    return [*([last.label] if last.label else []), *path.target.labels]


def label_test(alias: str, labels: list[str]) -> str:
    """A test that the node bound to `alias` carries every one of the labels; that it is there, for none."""
    return " AND ".join(f"{alias}:{quote_name(it)}" for it in labels) if labels else f"{alias} IS NOT NULL"


def path_pattern(path: PathModel, origin: str, far: str, labelled: bool, ordered: str | None = None) -> str:
    """
    A path from `origin` through its hops, the far node bound to `far` (with its labels, its last hop's among them,
    when `labelled`), and the node of the hop the path is ordered by bound to `ordered`.
    """
    steps: list[str] = []
    for index, hop in enumerate(path.hops):
        last = index == len(path.hops) - 1
        edge = f"[:{quote_name(hop.type)}]"
        if hop.direction is Direction.INCOMING:
            arrow = f"<-{edge}-"
        elif hop.direction is Direction.UNDIRECTED:
            arrow = f"-{edge}-"
        else:
            arrow = f"-{edge}->"
        own = [hop.label] if hop.label else []
        if last:
            labels = label_expression(far_labels(path)) if labelled else ""
        else:
            labels = label_expression(own)
        named = far if last else (ordered if hop.order_by and ordered else "")
        steps.append(f"{arrow}({named}{labels})")
    return f"({origin}){''.join(steps)}"
