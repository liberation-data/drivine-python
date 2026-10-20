from __future__ import annotations

import re
from collections.abc import Iterable

_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ESCAPED_BACKTICK = re.compile(r"\\u+0060")
"""
The Unicode escape of a backtick, with as many `u`s as an escape may have. Neo4j and FalkorDB decode `\\u0060` before
they read the quoting, even between backticks, so its escape would end a name's quotes as a backtick does.
"""


def quote_name(name: str) -> str:
    """
    A label, relationship type or property name as Cypher must write it: as it is when plain, otherwise between
    backticks, as one name whatever it holds. A name may come from data, so a backtick is doubled in either spelling,
    as TypeScript's Drivine and drivine4j quote names.
    """
    if _PLAIN_NAME.match(name):
        return name
    return "`" + _ESCAPED_BACKTICK.sub("`", name).replace("`", "``") + "`"


def quote_string(text: str) -> str:
    """A Cypher string literal: a backslash and a quote escaped, the backslash first."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def label_expression(labels: Iterable[str]) -> str:
    return "".join(f":{quote_name(it)}" for it in labels)
