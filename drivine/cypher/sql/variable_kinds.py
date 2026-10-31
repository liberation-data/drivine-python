from __future__ import annotations

from collections.abc import Sequence
from typing import Literal as Typed
from typing import cast

from drivine.cypher.ast.clause import (
    Call,
    Clause,
    Create,
    Delete,
    Foreach,
    Match,
    Merge,
    Return,
    SetClause,
    Unwind,
    With,
)
from drivine.cypher.ast.expression import (
    Binary,
    Case,
    Entry,
    Exists,
    Expression,
    Function,
    HasLabels,
    Index,
    ListComprehension,
    ListLiteral,
    Literal,
    MapLiteral,
    MapProjection,
    PatternComprehension,
    PatternPredicate,
    Property,
    Quantifier,
    Reduce,
    Slice,
    Subquery,
    Unary,
    Variable,
)
from drivine.cypher.ast.pattern import Pattern
from drivine.cypher.ast.projection import Projection
from drivine.cypher.ast.query import Query
from drivine.cypher.ast.relationship_pattern import Group
from drivine.cypher.ast.set_item import SetItem, SetLabels
from drivine.cypher.sql.value import Known, known_of, noun_of
from drivine.drivine_error import DrivineError

Kind = Typed["NODE", "RELATIONSHIP", "RELATIONSHIPS", "PATH", "ANY"] | Known
"""A kind of variable: see `check_variable_kinds`. A value's kinds are Known's; `ANY` is any kind at all."""

_NOUNS: dict[str, str] = {
    "NODE": "a node",
    "RELATIONSHIP": "a relationship",
    "RELATIONSHIPS": "a list of relationships",
    "PATH": "a path",
    "ANY": "a value",
}
"""A variable of each kind that is no value's, as a message names it: a value's is `noun_of`'s."""

Scope = dict[str, Kind]
"""The variables in scope, by name, each of its kind."""


def check_variable_kinds(query: Query) -> None:
    """
    Refuses the statement where a variable of it is used as two kinds, `MATCH (a)-[a]->()` (Neo4j's
    VariableTypeConflict), or is bound already where a pattern makes it, `MATCH (a) CREATE (a)` (VariableAlreadyBound),
    as Neo4j refuses it when it plans the statement. Read from the statement alone, before it is compiled, so a
    statement that compiles is compiled as it was, and one refused here is refused for what is wrong with it, not for
    what the compiler does not take yet. As drivine4j's VariableKinds and Drivine for TypeScript's.

    Only a kind the statement fixes is known: a pattern's node, relationship, variable-length relationship or path, a
    value written, and a variable a `WITH` passes on as it is. Any other value (a property, a parameter, an unwound
    item, a function's result) may be of any kind, and is never refused here as being of another: what it is used as is
    left to the compiler, as before.
    """
    _query(query, {})


def _noun(kind: Kind) -> str:
    """A variable of the kind, as a message names it."""
    noun = _NOUNS.get(kind)
    return noun if noun is not None else noun_of(cast(Known, kind))


def _query(query: Query, outer: Scope) -> list[Scope]:
    """The query's variables, from those of `outer` it reads: what each part of it returns."""
    return [_clauses(it, dict(outer)) for it in (query.clauses, *(union.clauses for union in query.unions))]


def _clauses(clauses: Sequence[Clause], scope: Scope, foreach: bool = False, kept: Scope | None = None) -> Scope:
    """
    The clauses, in turn, over `scope`: what their `RETURN` returns, of the kinds it returns. Within a `FOREACH`,
    `foreach`, a `CREATE` or a `MERGE` may name a variable bound already, as Neo4j takes it there. `kept` stay in scope
    after each `WITH` that does not name them, as what a `CALL`'s brackets import does (drivine4j#464).
    """
    current = scope
    returned: Scope = {}
    for clause in clauses:
        if isinstance(clause, With):
            following = _projected(clause.projection, current, clause.where)
            for name, kind in (kept or {}).items():
                following.setdefault(name, kind)
            current = following
        elif isinstance(clause, Return):
            returned = _projected(clause.projection, current, None)
        else:
            _other(clause, current, foreach)
    return returned


def _other(clause: Clause, scope: Scope, foreach: bool) -> None:
    """A clause other than a `WITH` or a `RETURN`, over `scope`, which it binds what it names in: see `_clauses`."""
    if isinstance(clause, Match):
        for pattern in clause.patterns:
            _matched(pattern, scope)
        _each([clause.where], scope)
    elif isinstance(clause, Unwind):
        _expression(clause.list, scope)
        scope[clause.variable] = "ANY"
    elif isinstance(clause, Create):
        made: set[str] = set()
        for pattern in clause.patterns:
            _made(pattern, scope, made, False, foreach)
    elif isinstance(clause, Merge):
        _made(clause.pattern, scope, set(), True, foreach)
        for item in (*clause.on_create, *clause.on_match):
            _set(item, scope)
    elif isinstance(clause, SetClause):
        for item in clause.items:
            _set(item, scope)
    elif isinstance(clause, Delete):
        _each(list(clause.expressions), scope)
    elif isinstance(clause, Foreach):
        _expression(clause.list, scope)
        _clauses(clause.clauses, {**scope, clause.variable: "ANY"}, True)
    elif isinstance(clause, Call):
        scope.update(_called(clause, scope))
    # A REMOVE names nothing.


def _called(call: Call, scope: Scope) -> Scope:
    """
    What the call returns, of the kinds each of its queries returns, read from the row `scope` holds: only a variable
    its brackets import, `CALL (a) { … }` or `CALL (*) { … }`, or where it has none its leading `WITH` imports as it is,
    or with `*`, of the kind it is; any other of the row is a new variable there, as in Neo4j.
    """
    # The compiler's reading of what a call imports, read here as it reads it; imported here, as it imports this module.
    from drivine.cypher.sql.sql_cypher_compiler import bracketed, imports

    parts: list[Scope] = []
    for clauses in (call.query.clauses, *(it.clauses for it in call.query.unions)):
        imported = imports(call, clauses, scope)
        inner: Scope = {name: kind for name, kind in scope.items() if name in imported}
        # What its brackets import stays in scope after a WITH that does not name it.
        parts.append(_clauses(clauses, dict(inner), False, inner if bracketed(call) else None))
    called: Scope = {}
    for name, kind in parts[0].items():
        called[name] = kind if all(it.get(name) == kind for it in parts) else "ANY"
    return called


def _projected(projection: Projection, scope: Scope, where: Expression | None) -> Scope:
    """
    The scope after a `WITH` or a `RETURN` of the projection over `scope`: each item of the kind its expression is,
    and with `*` each variable in scope. Its `WHERE`, `where`, and its order read the items, and a variable before it
    as of any kind.
    """
    for item in projection.items:
        _expression(item.expression, scope)
    following: Scope = dict(scope) if projection.star else {}
    for item in projection.items:
        following[item.name] = _kind_of(item.expression, scope)
    reading: Scope = {**dict.fromkeys(scope, "ANY"), **following}
    _each([where, *(it.expression for it in projection.order_by)], reading)
    _each([projection.skip, projection.limit], scope)
    return following


def _kind_of(value: Expression, scope: Scope) -> Kind:
    """The kind of the value, where the statement fixes it: see `check_variable_kinds`."""
    if isinstance(value, Variable):
        return scope.get(value.name, "ANY")
    if isinstance(value, Literal):
        return known_of(value.value) or "ANY"
    if isinstance(value, ListLiteral):
        return "LIST"
    if isinstance(value, MapLiteral):
        return "MAP"
    return "ANY"


def _matched(pattern: Pattern, scope: Scope, within: bool = False) -> None:
    """
    A pattern a `MATCH` finds, over `scope`: what it names, bound as it names it, or used as it is bound. Its path
    variable is new, but in a pattern within an expression, `within`, where it is the pattern's own.
    """
    # In the order written, so the first of two kinds is the one the variable is bound as.
    for i, node in enumerate(pattern.nodes):
        _each([node.properties], scope)
        if node.variable is not None:
            _use(node.variable, "NODE", scope)
        if i == len(pattern.relationships):
            break
        relationship = pattern.relationships[i]
        _each([relationship.properties], scope)
        if relationship.group is not None:
            _grouped(relationship.group, scope)
        if relationship.variable is not None:
            _use(relationship.variable, "RELATIONSHIPS" if relationship.range else "RELATIONSHIP", scope)
    variable = pattern.variable
    if variable is not None:
        if not within and variable in scope:
            raise DrivineError(f"{variable} is bound already, so a pattern binds no path to it, as in Neo4j.")
        scope[variable] = "PATH"


def _grouped(group: Group, scope: Scope) -> None:
    """A quantified path pattern's group: what it names is a list outside it, of whatever it matched."""
    for it in (*group.pattern.nodes, *group.pattern.relationships):
        if it.variable is not None and it.variable not in scope:
            scope[it.variable] = "ANY"


def _use(variable: str, kind: Kind, scope: Scope) -> None:
    """The variable where a pattern binds it as the kind: bound so, or used so where it is bound already as that
    kind."""
    bound = scope.get(variable)
    if bound is None:
        scope[variable] = kind
    elif not _fits(bound, kind):
        raise _conflict(variable, bound, kind)


def _fits(bound: Kind, kind: Kind) -> bool:
    """Whether a variable bound as `bound` may be used where a pattern takes `kind`."""
    return bound == kind or bound == "ANY" or (kind == "RELATIONSHIPS" and bound == "LIST")


def _conflict(variable: str, bound: Kind, used: Kind) -> DrivineError:
    """The refusal of the variable, bound as `bound`, where a pattern uses it as `used`."""
    return DrivineError(
        f"{variable} is {_noun(bound)}, and is used as {_noun(used)}: a variable is of one kind, as in Neo4j."
    )


def _made(pattern: Pattern, scope: Scope, made: set[str], merging: bool, foreach: bool) -> None:
    """
    A pattern a `CREATE` or a `MERGE` makes, over `scope`: each node of it new, or one bound already that it joins up,
    and each relationship new: a node a pattern of the same `CREATE` made before it is bound already. `made` holds the
    relationships the clause made before. A relationship bound before the clause, as one, is left to the compiler,
    which refuses it as not compiled yet, as the spec has it. Within a `FOREACH`, `foreach`, Neo4j takes a variable
    bound already in each of these places, and only one of another kind is refused.
    """
    clause = "MERGE" if merging else "CREATE"
    for node in pattern.nodes:
        _each([node.properties], scope)
        variable = node.variable
        if variable is None:
            continue
        bound = scope.get(variable)
        if bound is None:
            scope[variable] = "NODE"
            continue
        if not foreach and (node.labels or node.properties is not None):
            raise DrivineError(
                f"{variable} is bound already, so a CREATE or MERGE makes no node of it with labels or properties."
            )
        if not pattern.relationships:
            if not foreach:
                raise DrivineError(f"{variable} is bound already, so a {clause} makes no node of it, as in Neo4j.")
            continue
        if not _fits(bound, "NODE"):
            raise _conflict(variable, bound, "NODE")
    for relationship in pattern.relationships:
        _each([relationship.properties], scope)
        variable = relationship.variable
        if variable is None:
            continue
        if foreach:
            scope.setdefault(variable, "RELATIONSHIP")
            continue
        bound = scope.get(variable)
        if bound is not None and (bound != "RELATIONSHIP" or variable in made):
            raise DrivineError(f"{variable} is bound already, so a {clause} makes no relationship of it, as in Neo4j.")
        scope[variable] = "RELATIONSHIP"
        made.add(variable)
    if pattern.variable is not None:
        scope.setdefault(pattern.variable, "PATH")


def _set(item: SetItem, scope: Scope) -> None:
    """A `SET` item, over `scope`: the patterns within the value it sets, which a label it sets has none of."""
    if not isinstance(item, SetLabels):
        _expression(item.value, scope)


def _each(values: Sequence[Expression | None], scope: Scope) -> None:
    """Each of the values that is there, read over `scope`: see `_expression`."""
    for it in values:
        if it is not None:
            _expression(it, scope)


def _expression(value: Expression, scope: Scope) -> None:
    """
    The patterns within the value, each over `scope` and what it names only within it, and the functions of a variable
    whose kind they do not take.
    """
    if isinstance(value, (Property, HasLabels, Unary)):
        _expression(value.subject if not isinstance(value, Unary) else value.operand, scope)
    elif isinstance(value, ListLiteral):
        _each(value.items, scope)
    elif isinstance(value, MapLiteral):
        _each([it for _, it in value.entries], scope)
    elif isinstance(value, MapProjection):
        _each([it.value for it in value.elements if isinstance(it, Entry)], scope)
    elif isinstance(value, Binary):
        _each([value.left, value.right], scope)
    elif isinstance(value, Index):
        _each([value.subject, value.index], scope)
    elif isinstance(value, Slice):
        _each([value.subject, value.from_, value.to], scope)
    elif isinstance(value, Function):
        _each(value.arguments, scope)
        _function_of(value, scope)
    elif isinstance(value, Case):
        _each([value.subject, value.otherwise, *(it for pair in value.alternatives for it in pair)], scope)
    elif isinstance(value, ListComprehension):
        _expression(value.list, scope)
        _each([value.where, value.projection], {**scope, value.variable: "ANY"})
    elif isinstance(value, Quantifier):
        _expression(value.list, scope)
        _each([value.where], {**scope, value.variable: "ANY"})
    elif isinstance(value, Reduce):
        _each([value.initial, value.list], scope)
        _expression(value.expression, {**scope, value.accumulator: "ANY", value.variable: "ANY"})
    elif isinstance(value, PatternComprehension):
        inner = dict(scope)
        _matched(value.pattern, inner, True)
        _each([value.where, value.projection], inner)
    elif isinstance(value, PatternPredicate):
        _matched(value.pattern, dict(scope), True)
    elif isinstance(value, Exists):
        inner = dict(scope)
        for pattern in value.patterns:
            _matched(pattern, inner, True)
        _each([value.where], inner)
    elif isinstance(value, Subquery):
        _query(value.query, scope)
    # A literal, a parameter, a variable and count(*) hold no other expression.


def _function_of(function: Function, scope: Scope) -> None:
    """A function of a variable whose kind it does not take, as Neo4j refuses it: `size()` of a path."""
    argument = function.arguments[0] if len(function.arguments) == 1 else None
    kind = scope.get(argument.name) if isinstance(argument, Variable) else None
    if function.name.lower() == "size" and kind == "PATH":
        raise DrivineError("size() takes a list or a text, not a path, as in Neo4j: length() takes a path.")
