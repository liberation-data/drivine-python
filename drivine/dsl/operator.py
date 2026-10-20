from typing import Literal

Operator = Literal[
    "EQ",
    "NEQ",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "IN",
    "NOT_IN",
    "CONTAINS",
    "STARTS_WITH",
    "ENDS_WITH",
    "MATCHES",
    "CONTAINS_IGNORE_CASE",
    "EQ_IGNORE_CASE",
    "IS_NULL",
    "IS_NOT_NULL",
    "HAS_ITEM",
]
"""How a property is tested. `predicate` takes one by name, for a test known only at run time."""
