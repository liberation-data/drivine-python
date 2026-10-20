from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, NoReturn, TypeVar

from drivine.drivine_error import DrivineError
from drivine.dsl.any_label_test import AnyLabelTest
from drivine.dsl.compare_test import CompareTest
from drivine.dsl.condition import Condition, Where, all_of_one
from drivine.dsl.instance_of_test import InstanceOfTest
from drivine.dsl.operator import Operator
from drivine.dsl.order_term import OrderTerm
from drivine.dsl.path_condition import PathCondition
from drivine.dsl.quantify_test import QuantifyTest
from drivine.dsl.seek_value import SeekValue

T = TypeVar("T")

STORED = "\x00"
"""What a path holds for a property named at run time, by its stored name, rather than a field."""


class Ref:
    """
    A reference to a field of the class a query loads, or to the node or property a path of fields reaches:
    `refs(PersonCareer).person.bio`. Any attribute is the field of that name, checked against the model when the query
    is written for the store, so a misspelt field fails there, naming it and the class.

    A condition on a field reached across a relationship holds when at least one related node meets it. A field whose
    name is one of this class's methods is reached with `field(name)`.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Iterable[str] = ()) -> None:
        object.__setattr__(self, "_path", tuple(path))

    @property
    def path(self) -> tuple[str, ...]:
        return self._path  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownVariableType]

    def __getattr__(self, name: str) -> Ref:
        if name.startswith("_"):
            raise AttributeError(name)
        return Ref((*self.path, name))

    def field(self, name: str) -> Ref:
        """The field of this name, for a field named as one of this class's methods."""
        return Ref((*self.path, name))

    def property(self, name: str) -> Ref:
        """
        A property by the name it is stored under, known only at run time: a bag entry's whole name
        (`metadata.source`), say. Of a relationship fragment, the relationship's property.
        """
        return Ref((*self.path, f"{STORED}{name}"))

    def predicate(self, name: str, operator: Operator, value: Any = None) -> Condition:
        """A test of a property by its stored name, from an operator known only at run time."""
        return self.property(name)._compare(operator, value)

    # ----- Properties -----

    def eq(self, value: Any) -> Condition:
        return self._compare("EQ", value)

    def neq(self, value: Any) -> Condition:
        return self._compare("NEQ", value)

    def gt(self, value: Any) -> Condition:
        return self._compare("GT", value)

    def gte(self, value: Any) -> Condition:
        return self._compare("GTE", value)

    def lt(self, value: Any) -> Condition:
        return self._compare("LT", value)

    def lte(self, value: Any) -> Condition:
        return self._compare("LTE", value)

    def in_(self, values: Iterable[Any]) -> Condition:
        return self._compare("IN", list(values))

    def not_in(self, values: Iterable[Any]) -> Condition:
        return self._compare("NOT_IN", list(values))

    def contains(self, text: str) -> Condition:
        return self._compare("CONTAINS", text)

    def starts_with(self, text: str) -> Condition:
        return self._compare("STARTS_WITH", text)

    def ends_with(self, text: str) -> Condition:
        return self._compare("ENDS_WITH", text)

    def matches(self, regex: str) -> Condition:
        """The whole value matches the regular expression, in the engine's own dialect (Java's on Neo4j)."""
        return self._compare("MATCHES", regex)

    def contains_ignore_case(self, text: str) -> Condition:
        return self._compare("CONTAINS_IGNORE_CASE", text)

    def eq_ignore_case(self, text: str) -> Condition:
        return self._compare("EQ_IGNORE_CASE", text)

    def has_item(self, value: Any) -> Condition:
        """A list property holds the value: the mirror of `in_`. Of a NodeLabels field, the node carries the label."""
        return self._compare("HAS_ITEM", value)

    def is_null(self) -> Condition:
        return PathCondition(self.path, CompareTest("IS_NULL"))

    def is_not_null(self) -> Condition:
        return PathCondition(self.path, CompareTest("IS_NOT_NULL"))

    def asc(self) -> OrderTerm:
        return OrderTerm(self.path, "ASC")

    def desc(self) -> OrderTerm:
        return OrderTerm(self.path, "DESC")

    def after(self, value: Any) -> SeekValue:
        """The property's value at the end of the last page: a keyset cursor's component, for `seek`."""
        if value is None:
            raise DrivineError(f"A cursor holds a value of {_described(self.path)}, not None.")
        return SeekValue(self.path, value)

    # ----- Nodes -----

    def instance_of(self, type_: type) -> Condition:
        """The node is of the class, or a subclass: it carries all of the class's labels."""
        return PathCondition(self.path, InstanceOfTest(type_))

    def has_any_label(self, *labels: str) -> Condition:
        """The node carries at least one of the labels."""
        return PathCondition(self.path, AnyLabelTest(labels))

    def any(self, conditions: Callable[[Ref], Where]) -> Condition:
        """
        One of the nodes the relationship leads to meets all the conditions together:
        `q.mentions.any(lambda m: [m.role.eq("SUBJECT"), m.resolved_id.eq(entity_id)])`.
        """
        return PathCondition(self.path, QuantifyTest(False, (all_of_one(conditions(Ref())),)))

    def none(self, conditions: Callable[[Ref], Where]) -> Condition:
        """None of the nodes the relationship leads to meets the conditions together, including when there are none."""
        return PathCondition(self.path, QuantifyTest(True, (all_of_one(conditions(Ref())),)))

    # ----- Mistakes -----

    def __eq__(self, other: object) -> NoReturn:  # type: ignore[override]
        raise TypeError(f"Compare {_described(self.path)} with .eq(...), not ==: a query is built from conditions.")

    def __ne__(self, other: object) -> NoReturn:  # type: ignore[override]
        raise TypeError(f"Compare {_described(self.path)} with .neq(...), not !=: a query is built from conditions.")

    def __bool__(self) -> NoReturn:
        raise TypeError(f"{_described(self.path)} is a reference, not a value: test it with a condition.")

    __hash__ = None  # type: ignore[assignment]

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError("A reference can't be changed.")

    def __repr__(self) -> str:
        return f"Ref({'.'.join(it.replace(STORED, 'property:') for it in self.path) or '<root>'})"

    def _compare(self, operator: Operator, value: Any) -> Condition:
        if value is None:
            raise DrivineError(
                f"{_described(self.path)} can't be compared to None: use is_null() or is_not_null() to test for no "
                f"value."
            )
        return PathCondition(self.path, CompareTest(operator, value))


def refs(type_: type[T]) -> Any:
    """
    The references of a class's fields, for a query over it:

        q = refs(PersonCareer)
        leads = await gom.load_all(PersonCareer, where=q.person.bio.contains("Lead"), order_by=q.person.name.asc())
    """
    return Ref()


def _described(path: tuple[str, ...]) -> str:
    return ".".join(it.replace(STORED, "") for it in path) if path else "the root"
