from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import ClassVar, Literal

from drivine.backend.store_statements import store_statements
from drivine.drivine_error import DrivineError

logger = logging.getLogger("drivine.oracle")

clock: Callable[[], float] = time.monotonic
"""
Seconds, as `time.monotonic` counts them, by which OracleStoredFunctions remembers a session that calls no function:
replaced by a test that lets ABSENT_FOR pass.
"""


def nulls_equal_name(graph: str) -> str:
    """The function the store keeps beside the graph `graph`'s tables that compares two values that may hold a null."""
    return f"{graph}_nulls_equal"


def era_name(graph: str) -> str:
    """
    The function the store keeps beside the graph `graph`'s tables that renumbers the year of an ISO text as Oracle
    does: the shared module's, made with the graph (see OracleGraph.functions).
    """
    return f"{graph}_era"


def era_date_name(graph: str) -> str:
    """
    The function the store keeps beside the graph `graph`'s tables that renumbers the year Oracle writes as ISO 8601
    does: the shared module's, made with the graph (see OracleGraph.functions).
    """
    return f"{graph}_era_date"


def temporal_key_name(graph: str) -> str:
    """
    The function the store keeps beside the graph `graph`'s tables that gives a temporal value's key, by which it is
    compared and ordered: one of the era functions, the shared module's, made with the graph (see OracleGraph.functions).
    """
    return f"{graph}_temporal_key"


def temporal_arithmetic_name(graph: str) -> str:
    """
    The function the store keeps beside the graph `graph`'s tables that works out `+`, `-`, `*` and `/` of a temporal
    value read from the store: the shared module's, made with the graph (see OracleGraph.functions).
    """
    return f"{graph}_temporal_arith"


StoredFunction = Literal["nulls_equal", "era", "temporal_arithmetic"]
"""
Which of a graph's stored functions a session is asked of: `<graph>_nulls_equal`, the era functions together, or
`<graph>_temporal_arith`. Each is asked of apart, as a store made by an earlier version may have one and not another.
"""


NO_FUNCTION = frozenset({904, 942, 1031, 6575})
"""
The errors calling a function there is none of gives: ORA-00904 where there is none, or none the session may call
(Oracle says the same of one it may not execute, and of a synonym for one that is gone), ORA-06575 where it is not
valid, and ORA-00942 or ORA-01031 where something it reads is not granted. Any other error, a connection lost among
them, is the statement's to report.
"""


def is_missing(error: BaseException) -> bool:
    """Whether `error`, raised calling the function, says the session has no function it may call: see NO_FUNCTION."""
    detail = error.args[0] if error.args else None
    return getattr(detail, "code", None) in NO_FUNCTION


class OracleStoredFunctions:
    """
    Whether a session may call the functions a graph's store keeps beside its tables (see `OracleGraph.functions`). A
    store made by an earlier version lacks those added since until the graph is ensured again, and a user who may not
    make a function (`CREATE PROCEDURE`) has none at all: a Cypher statement is then compiled to call none of those it
    lacks, and a statement of the query DSL that calls one is refused; a warning says so once for each graph and
    function. drivine4j's `OracleStoredFunctions`.

    Asked only by a statement that would call one (see `OracleConnection`), which calls the function to find out:
    `<graph>_nulls_equal` by `check`, `<graph>_era`, `<graph>_era_date` and `<graph>_temporal_key` together by
    `era_check`, and `<graph>_temporal_arith` by `temporal_arithmetic_check`. A session that may call it is remembered
    for the function, the database, its user and the graph, for as long as the process runs: a function dropped after
    that is not noticed, and a statement that calls it fails until the process restarts. One that may not is remembered
    for ABSENT_FOR seconds, or until the graph is ensured in this process (see `ensured`), so a store without the
    function is not asked on every statement, and a graph ensured later still gains it.

    What it remembers is the process's, shared by the async API and the sync one.
    """

    ABSENT_FOR: ClassVar[float] = 60.0
    """How long, in seconds, a session found to call no function is taken to call none before it is asked again."""

    _callable: ClassVar[set[str]] = set()
    """The function, and the database, user and graph, of each session found to call it."""

    _absent: ClassVar[dict[str, float]] = {}
    """The function, and the database, user and graph, of each session found to call none, and when (by `clock`) to ask
    again."""

    _warned: ClassVar[set[str]] = set()
    """The function, and the database, user and graph, of each session that was said to have none."""

    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def check(cls, graph: str) -> str:
        """The query that tells whether a session may call the graph `graph`'s `<graph>_nulls_equal`: it calls it."""
        return _probe(graph, "nullsEqualStored")

    @classmethod
    def era_check(cls, graph: str) -> str:
        """
        The query that tells whether a session may call the functions the graph `graph` renumbers a year that may be BC
        and keys a temporal value by, `<graph>_era`, `<graph>_era_date` and `<graph>_temporal_key`: the shared module's
        probe, which calls them. They are asked of together, as a statement that calls one is compiled to call none
        where the session may not call one: the graph is made with them at once, and a store that has none gives the
        same rows, planned more slowly. They are asked of apart from `check`'s, which a store made by a version before
        them has.
        """
        return _probe(graph, "eraStored")

    @classmethod
    def temporal_arithmetic_check(cls, graph: str) -> str:
        """
        The query that tells whether a session may call the graph `graph`'s `<graph>_temporal_arith`: the shared
        module's probe, which calls it. Asked of apart, as a store made by a version before it has the others.
        """
        return _probe(graph, "temporalArithmeticStored")

    @classmethod
    def checked(cls, graph: str, function: StoredFunction) -> str:
        """The query that tells whether a session may call the graph `graph`'s `function` (see StoredFunction)."""
        if function == "era":
            return cls.era_check(graph)
        if function == "temporal_arithmetic":
            return cls.temporal_arithmetic_check(graph)
        return cls.check(graph)

    @classmethod
    def key(cls, database: str, graph: str, function: StoredFunction = "nulls_equal") -> str:
        """
        What a session of the database and user `database` is remembered by for the graph `graph`'s `function`: the
        function, then the database, the user and the graph, joined by `|`.
        """
        return f"{named(graph, function)}|{database}|{graph}"

    @classmethod
    def known(cls, key: str) -> bool | None:
        """
        What is remembered of the session `key` (see `key`): true where it may call the function, false where it was
        found to call none within ABSENT_FOR, and None where it must be asked.
        """
        with cls._lock:
            if key in cls._callable:
                return True
            until = cls._absent.get(key)
            return False if until is not None and clock() < until else None

    @classmethod
    def found(cls, key: str, graph: str, callable_: bool, function: StoredFunction = "nulls_equal") -> bool:
        """
        Remembers what the session `key` was found to call, and warns once for it where it may call no function: the
        graph `graph`'s `function`. `callable_` is given back.
        """
        with cls._lock:
            if callable_:
                cls._callable.add(key)
                cls._absent.pop(key, None)
                return True
            cls._absent[key] = clock() + cls.ABSENT_FOR
            first = key not in cls._warned
            cls._warned.add(key)
        if first:
            logger.warning(
                "The Oracle graph '%s' has no function %s, which the graph is made with where its user may make a "
                "function (CREATE PROCEDURE, which the RESOURCE role grants). %s",
                graph,
                named(graph, function),
                _LOST[function],
            )
        return False

    @classmethod
    def ensured(cls, graph: str) -> None:
        """Asks again, of every session, whether it may call the functions of `graph`: it has just been ensured."""
        with cls._lock:
            for key in [it for it in cls._absent if it.endswith(f"|{graph}")]:
                del cls._absent[key]


def _probe(graph: str, flag: str) -> str:
    """The shared module's statement that finds whether a session may call the functions the compiler flag `flag` names."""
    probes: dict[str, str] = (store_statements("oracle", graph).stored_functions or {}).get("probes", {})
    probe = probes.get(flag)
    if probe is None:
        raise DrivineError(f"The shared module has no statement that finds whether a session may call {flag}.")
    return probe


def named(graph: str, function: StoredFunction) -> str:
    """The graph `graph`'s `function`, as a warning names it, and as `OracleStoredFunctions.key` keys it."""
    if function == "era":
        return f"{era_name(graph)}, {era_date_name(graph)} and {temporal_key_name(graph)}"
    if function == "temporal_arithmetic":
        return temporal_arithmetic_name(graph)
    return nulls_equal_name(graph)


_NULLS_EQUAL_LOST = (
    "Until the graph is ensured by such a user, a Cypher statement that writes, or the step of a reduce(), compares two "
    "lists or maps that hold a null and differ as JSON as false, where Cypher may give null."
)
"""What a statement loses where the session may not call `<graph>_nulls_equal`."""

_ERA_LOST = (
    "Until the graph is ensured by such a user, Oracle takes longer to plan a Cypher statement that reads or writes a "
    "date or a moment whose year may be before 1000; it gives the same rows."
)
"""What a statement loses where the session may not call the era functions: time, and no rows."""

_TEMPORAL_ARITHMETIC_LOST = (
    "Until the graph is ensured by such a user, an order of the query DSL by arithmetic of a property's value is "
    "refused."
)
"""What a statement loses where the session may not call `<graph>_temporal_arith`."""

_LOST: dict[StoredFunction, str] = {
    "nulls_equal": _NULLS_EQUAL_LOST,
    "era": _ERA_LOST,
    "temporal_arithmetic": _TEMPORAL_ARITHMETIC_LOST,
}
"""What a statement loses where the session may not call each function."""
