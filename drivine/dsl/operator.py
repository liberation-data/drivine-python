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
    "EQ_LOWERED",
    "CONTAINS_LOWERED",
    "IS_NULL",
    "IS_NOT_NULL",
    "HAS_ITEM",
]
"""
How a property is tested. `predicate` takes one by name, for a test known only at run time.

`EQ_LOWERED` equals the value as the store lowercases it: what `eq_ignore_case` on a `CaseInsensitive` property becomes
once every node has its lowercased copy, compared with the copy (see lower_copies), which an index over the copy serves.
`CONTAINS_LOWERED` is `contains_ignore_case` on a copy alike. Drivine writes them.
"""
