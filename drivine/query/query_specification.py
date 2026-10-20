from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Generic, Self, TypeVar

from drivine.mapper.filter_post_processor import FilterPostProcessor
from drivine.mapper.map_post_processor import MapPostProcessor
from drivine.mapper.result_post_processor import ResultPostProcessor
from drivine.mapper.transform_post_processor import TransformPostProcessor
from drivine.query.statement import QueryLanguage, Statement

T = TypeVar("T")

Parameters = Sequence[Any] | Mapping[str, Any]


class QuerySpecification(Generic[T]):
    """
    A statement, its parameters, and what to do with its results:

        spec = (QuerySpecification[Person]()
            .with_statement("MATCH (p:Person {city: $city}) RETURN properties(p)")
            .bind({"city": "Sydney"})
            .transform(Person)
            .limit(10))
    """

    def __init__(self, statement: str | Statement | None = None) -> None:
        self.statement: Statement | None = None
        self.parameters: Parameters = []
        self.post_processors: list[ResultPostProcessor] = []
        self._skip: int | None = None
        self._limit: int | None = None
        if statement is not None:
            self.with_statement(statement)

    def with_statement(self, statement: str | Statement) -> Self:
        if isinstance(statement, str):
            statement = Statement(statement)
        if not statement.text:
            raise ValueError("statement text is required")
        self.statement = statement
        return self

    def bind(self, parameters: Parameters | None) -> Self:
        """
        Binds parameters to the statement: a mapping for named parameters (`$city`), or a sequence for positional
        ones (`$1`, `$2`, …). None or empty does nothing.
        """
        if parameters:
            self.parameters = parameters
        return self

    def add_post_processors(self, *post_processors: ResultPostProcessor) -> Self:
        self.post_processors.extend(post_processors)
        return self

    def map(self, mapper: Callable[[Any], Any]) -> Self:
        self.post_processors.append(MapPostProcessor(mapper))
        return self

    def transform(self, type_: type[Any] | Any) -> Self:
        """
        Validates each result as the type, with Pydantic: a model, a dataclass, a TypedDict, or any type Pydantic
        validates.
        """
        self.post_processors.append(TransformPostProcessor(type_))
        return self

    def filter(self, predicate: Callable[[Any], bool]) -> Self:
        self.post_processors.append(FilterPostProcessor(predicate))
        return self

    def skip(self, results: int | None) -> Self:
        """Skips the first results. It is written into the statement, so it must be a whole number of at least 0."""
        self._skip = _whole_number(results, "skip")
        return self

    def limit(self, results: int | None) -> Self:
        """Returns at most this many results. It is written into the statement, so it must be a whole number of at
        least 0."""
        self._limit = _whole_number(results, "limit")
        return self

    def finalized_copy(self, language: QueryLanguage) -> Self:
        """
        A copy, with a PLATFORM_DEFAULT statement resolved to the given language.
        """
        if self.statement is None:
            raise ValueError("A statement is required: call with_statement first.")
        finalized = copy.copy(self)
        finalized.statement = self.statement.to_platform_default(language)
        finalized.post_processors = list(self.post_processors)
        return finalized

    def __repr__(self) -> str:
        text = self.statement.text if self.statement else None
        return f"{type(self).__name__}(statement={text!r}, parameters={self.parameters!r})"


def _whole_number(count: int | None, what: str) -> int | None:
    # A bool is an int, and a value from a request might be text: neither should be written into a statement.
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"{what} must be a whole number of at least 0, not {count!r}.")
    return count
