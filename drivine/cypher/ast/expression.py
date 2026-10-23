from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from drivine.cypher.ast.pattern import Pattern
    from drivine.cypher.ast.query import Query


@dataclass(frozen=True, eq=False)
class Literal:
    """A literal: a string, a number (an int or a float), a boolean, or None."""

    value: Any

    def __eq__(self, other: object) -> bool:
        # Of its kind too, as in Cypher: 1 is not 1.0, nor true.
        return isinstance(other, Literal) and type(other.value) is type(self.value) and other.value == self.value

    def __hash__(self) -> int:
        return hash((type(self.value).__name__, self.value))


@dataclass(frozen=True)
class Parameter:
    """A parameter, `$name`, its value bound when the statement runs."""

    name: str


@dataclass(frozen=True)
class Variable:
    """A variable a clause bound: a node, a relationship, a path or a value."""

    name: str


@dataclass(frozen=True)
class Property:
    """A property of a node, a relationship or a map: `subject.key`."""

    subject: Expression
    key: str


@dataclass(frozen=True)
class HasLabels:
    """Whether a node carries each of its labels: `subject:Label`."""

    subject: Expression
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ListLiteral:
    """A list, `[a, b]`."""

    items: tuple[Expression, ...]


@dataclass(frozen=True)
class MapLiteral:
    """A map, `{key: value}`, its entries in the order written."""

    entries: tuple[tuple[str, Expression], ...]


@dataclass(frozen=True)
class Selected:
    """`.key`, of a map projection: the property of that name."""

    key: str


@dataclass(frozen=True)
class Entry:
    """`key: value`, of a map projection."""

    key: str
    value: Expression


@dataclass(frozen=True)
class AllProperties:
    """`.*`, of a map projection: every property."""


@dataclass(frozen=True)
class VariableElement:
    """`other`, of a map projection: a variable, under its own name."""

    name: str


MapProjectionElement = Selected | Entry | AllProperties | VariableElement
"""An element of a map projection."""


@dataclass(frozen=True)
class MapProjection:
    """A map projection, `n { .name, key: value, .*, other }`, its elements in the order written."""

    variable: str
    elements: tuple[MapProjectionElement, ...]


class BinaryOperator(Enum):
    OR = "OR"
    XOR = "XOR"
    AND = "AND"
    EQUALS = "EQUALS"
    NOT_EQUALS = "NOT_EQUALS"
    LESS_THAN = "LESS_THAN"
    GREATER_THAN = "GREATER_THAN"
    LESS_THAN_OR_EQUAL = "LESS_THAN_OR_EQUAL"
    GREATER_THAN_OR_EQUAL = "GREATER_THAN_OR_EQUAL"
    ADD = "ADD"
    SUBTRACT = "SUBTRACT"
    MULTIPLY = "MULTIPLY"
    DIVIDE = "DIVIDE"
    MODULO = "MODULO"
    POWER = "POWER"
    IN = "IN"
    STARTS_WITH = "STARTS_WITH"
    ENDS_WITH = "ENDS_WITH"
    CONTAINS = "CONTAINS"
    MATCHES = "MATCHES"


@dataclass(frozen=True)
class Binary:
    """An operator of two operands."""

    operator: BinaryOperator
    left: Expression
    right: Expression


class UnaryOperator(Enum):
    NOT = "NOT"
    NEGATE = "NEGATE"
    IS_NULL = "IS_NULL"
    IS_NOT_NULL = "IS_NOT_NULL"


@dataclass(frozen=True)
class Unary:
    """An operator of one operand."""

    operator: UnaryOperator
    operand: Expression


@dataclass(frozen=True)
class Index:
    """An item of a list, `subject[index]`, or the value of a map's key, `subject["key"]`."""

    subject: Expression
    index: Expression


@dataclass(frozen=True)
class Slice:
    """A slice of a list, `subject[from..to]`; a bound left out is open."""

    subject: Expression
    from_: Expression | None
    to: Expression | None


@dataclass(frozen=True)
class Function:
    """
    A function, by its name as written with its namespace (`toLower`, `apoc.text.join`): Cypher takes a function's name
    in any case.
    """

    name: str
    distinct: bool
    arguments: tuple[Expression, ...]


@dataclass(frozen=True)
class CountStar:
    """`count(*)`."""


@dataclass(frozen=True)
class Case:
    """`CASE`: with a subject, each alternative's key is compared with it; without one, each is a condition."""

    subject: Expression | None
    alternatives: tuple[tuple[Expression, Expression], ...]
    otherwise: Expression | None


@dataclass(frozen=True)
class ListComprehension:
    """`[variable IN list WHERE filter | projection]`."""

    variable: str
    list: Expression
    where: Expression | None
    projection: Expression | None


@dataclass(frozen=True)
class Reduce:
    """`reduce(accumulator = initial, variable IN list | expression)`: the list folded, from the first item to the
    last."""

    accumulator: str
    initial: Expression
    variable: str
    list: Expression
    expression: Expression


@dataclass(frozen=True)
class PatternComprehension:
    """`[(a)-[:R]->(b) WHERE filter | projection]`: a value for each way the pattern is found from what is bound."""

    pattern: Pattern
    where: Expression | None
    projection: Expression


@dataclass(frozen=True)
class PatternPredicate:
    """A pattern as a condition, `(a)-[:R]->()`: whether it is found."""

    pattern: Pattern


@dataclass(frozen=True)
class Exists:
    """`EXISTS { pattern WHERE … }`."""

    patterns: tuple[Pattern, ...]
    where: Expression | None


class SubqueryKind(Enum):
    EXISTS = "EXISTS"
    COUNT = "COUNT"


@dataclass(frozen=True)
class Subquery:
    """
    `EXISTS { … }` or `COUNT { … }` of a query: whether it gives a row, or how many it gives, for the row it is in. Its
    query reads from the variables around it, and need not end in a `RETURN`.
    """

    kind: SubqueryKind
    query: Query


class QuantifierKind(Enum):
    ALL = "ALL"
    ANY = "ANY"
    NONE = "NONE"
    SINGLE = "SINGLE"


@dataclass(frozen=True)
class Quantifier:
    """`all`, `any`, `none` or `single` of a list: `any(x IN list WHERE condition)`."""

    kind: QuantifierKind
    variable: str
    list: Expression
    where: Expression | None


Expression = (
    Literal
    | Parameter
    | Variable
    | Property
    | HasLabels
    | ListLiteral
    | MapLiteral
    | MapProjection
    | Binary
    | Unary
    | Index
    | Slice
    | Function
    | CountStar
    | Case
    | ListComprehension
    | Reduce
    | PatternComprehension
    | PatternPredicate
    | Exists
    | Subquery
    | Quantifier
)
"""An expression of a Cypher statement."""
