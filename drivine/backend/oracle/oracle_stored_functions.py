from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import ClassVar

logger = logging.getLogger("drivine.oracle")

clock: Callable[[], float] = time.monotonic
"""
Seconds, as `time.monotonic` counts them, by which OracleStoredFunctions remembers a session that calls no function:
replaced by a test that lets ABSENT_FOR pass.
"""


def nulls_equal_name(graph: str) -> str:
    """The function the store keeps beside the graph `graph`'s tables that compares two values that may hold a null."""
    return f"{graph}_nulls_equal"


def nulls_equal_function(name: str) -> str:
    """
    The function `name`, which compares two JSON values as Cypher compares values that may hold a null: 1 where they
    are equal, 0 where they differ, and null where a pair of their items holds a null and no pair differs. It walks the
    two together with `JSON_ELEMENT_T`, list item by list item and map key by map key, at any depth. A query declares it
    with `WITH FUNCTION` where the store has none (see `OracleCypherCompiler.nulls_equal`); the store keeps it, made with
    the graph, as `CREATE OR REPLACE` of the same text (see `OracleGraph.functions`).

    The store's copy is part of the store layout the three Drivine libraries share (see AGENTS.md): it is
    `CREATE OR REPLACE ` and this text with `name` as `nulls_equal_name` gives it, the graph's name as written and then
    `_nulls_equal`, its lines joined by `\\n` with no newline after the last (the `#` comment below is not in it).
    drivine4j's `OracleCypherCompiler.nullsEqualFunction` is the reference, line for line: changing a byte of it changes
    the layout for all three, so it changes in all three together.
    """
    return "\n".join(
        (
            f"FUNCTION {name}(cy_x JSON, cy_y JSON) RETURN NUMBER IS",
            "    FUNCTION eq(a JSON_ELEMENT_T, b JSON_ELEMENT_T) RETURN NUMBER IS",
            "        r NUMBER := 1;",
            "        e NUMBER;",
            "        la JSON_ARRAY_T;",
            "        lb JSON_ARRAY_T;",
            "        oa JSON_OBJECT_T;",
            "        ob JSON_OBJECT_T;",
            "        ks JSON_KEY_LIST;",
            "        too_long EXCEPTION;",
            "        PRAGMA EXCEPTION_INIT(too_long, -40478);",
            "    BEGIN",
            "        IF a.is_Null OR b.is_Null THEN RETURN NULL; END IF;",
            "        IF a.is_Array AND b.is_Array THEN",
            "            la := TREAT(a AS JSON_ARRAY_T);",
            "            lb := TREAT(b AS JSON_ARRAY_T);",
            "            IF la.get_Size <> lb.get_Size THEN RETURN 0; END IF;",
            "            FOR i IN 0 .. la.get_Size - 1 LOOP",
            "                e := eq(la.get(i), lb.get(i));",
            "                IF e = 0 THEN RETURN 0; END IF;",
            "                IF e IS NULL THEN r := NULL; END IF;",
            "            END LOOP;",
            "            RETURN r;",
            "        END IF;",
            "        IF a.is_Object AND b.is_Object THEN",
            "            oa := TREAT(a AS JSON_OBJECT_T);",
            "            ob := TREAT(b AS JSON_OBJECT_T);",
            "            IF oa.get_Size <> ob.get_Size THEN RETURN 0; END IF;",
            "            ks := oa.get_Keys;",
            "            FOR i IN 1 .. ks.COUNT LOOP",
            "                IF NOT ob.has(ks(i)) THEN RETURN 0; END IF;",
            "                e := eq(oa.get(ks(i)), ob.get(ks(i)));",
            "                IF e = 0 THEN RETURN 0; END IF;",
            "                IF e IS NULL THEN r := NULL; END IF;",
            "            END LOOP;",
            "            RETURN r;",
            "        END IF;",
            "        IF a.is_Number AND b.is_Number THEN RETURN CASE WHEN a.to_Number = b.to_Number THEN 1 ELSE 0 END; END IF;",
            # As VARCHAR2s, about 3.5 times as fast as CLOBs; as CLOBs where one is past 32 KB, which to_String
            # raises on (ORA-40478).
            "        IF a.is_String AND b.is_String THEN",
            "            BEGIN",
            "                RETURN CASE WHEN a.to_String = b.to_String THEN 1 ELSE 0 END;",
            "            EXCEPTION WHEN too_long THEN",
            "                RETURN CASE WHEN DBMS_LOB.COMPARE(a.to_Clob, b.to_Clob) = 0 THEN 1 ELSE 0 END;",
            "            END;",
            "        END IF;",
            "        IF a.is_Boolean AND b.is_Boolean THEN RETURN CASE WHEN a.to_Boolean = b.to_Boolean THEN 1 ELSE 0 END; END IF;",
            "        RETURN 0;",
            "    END;",
            "BEGIN",
            "    RETURN eq(JSON_ELEMENT_T.parse(JSON_SERIALIZE(cy_x RETURNING CLOB)), JSON_ELEMENT_T.parse(JSON_SERIALIZE(cy_y RETURNING CLOB)));",
            "END;",
        )
    )


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
    store made by an earlier version has none until the graph is ensured again, and a user who may not make a function
    (`CREATE PROCEDURE`) has none at all: a Cypher statement is then compiled to call none, and says so once for each
    graph. drivine4j's `OracleStoredFunctions`.

    Asked only by a statement that would call one (see `OracleConnection`), which calls the function to find out. A
    session that may call it is remembered for the database, its user and the graph, for as long as the process runs: a
    function dropped after that is not noticed, and a statement that calls it fails until the process restarts. One
    that may not is remembered for ABSENT_FOR seconds, or until the graph is ensured in this process (see `ensured`), so
    a store without the function is not asked on every statement, and a graph ensured later still gains it.

    What it remembers is the process's, shared by the async API and the sync one.
    """

    ABSENT_FOR: ClassVar[float] = 60.0
    """How long, in seconds, a session found to call no function is taken to call none before it is asked again."""

    _callable: ClassVar[set[str]] = set()
    """The database, user and graph of each session found to call the function."""

    _absent: ClassVar[dict[str, float]] = {}
    """The database, user and graph of each session found to call none, and when (by `clock`) to ask again."""

    _warned: ClassVar[set[str]] = set()
    """The database, user and graph of each session that was said to have no function."""

    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def check(cls, graph: str) -> str:
        """The query that tells whether a session may call the graph `graph`'s function: it calls it."""
        return f"SELECT {nulls_equal_name(graph)}(JSON('[1]'), JSON('[1]')) FROM DUAL"

    @classmethod
    def known(cls, key: str) -> bool | None:
        """
        What is remembered of the session `key` (its database, its user and the graph, joined by `|`): true where it
        may call the function, false where it was found to call none within ABSENT_FOR, and None where it must be
        asked.
        """
        with cls._lock:
            if key in cls._callable:
                return True
            until = cls._absent.get(key)
            return False if until is not None and clock() < until else None

    @classmethod
    def found(cls, key: str, graph: str, callable_: bool) -> bool:
        """
        Remembers what the session `key` was found to call, and warns once for it where it may call no function.
        `callable_` is given back.
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
                "function (CREATE PROCEDURE, which the RESOURCE role grants). Until the graph is ensured by such a "
                "user, a Cypher statement that writes, or the step of a reduce(), compares two lists or maps that hold "
                "a null and differ as JSON as false, where Cypher may give null.",
                graph,
                nulls_equal_name(graph),
            )
        return False

    @classmethod
    def ensured(cls, graph: str) -> None:
        """Asks again, of every session, whether it may call the functions of `graph`: it has just been ensured."""
        with cls._lock:
            for key in [it for it in cls._absent if it.endswith(f"|{graph}")]:
                del cls._absent[key]
